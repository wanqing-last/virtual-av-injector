# -*- coding: utf-8 -*-
"""注入引擎：把本地视频推到虚拟摄像头、把音频推到虚拟麦克风。

关键设计：
- 摄像头线程按「源时间轴」送帧：源帧率 > 输出帧率时自动抽帧，
  不会越推越快；暂停 / 播完时持续重发最后一帧，避免会议端画面黑掉或超时；
- 音频线程写虚拟声卡播放端点（CABLE Input），采样率跟设备对齐；
  暂停时写静音而不是停写，防止设备被应用判定为「无输入」；
- 所有状态用锁保护，Web 层只读快照。
"""

from __future__ import annotations

import os
import threading
import time

import cv2
import numpy as np

from .audio_track import AudioTrack
from .config import (
    AUDIO_BLOCK,
    AUDIO_SR,
    DEFAULT_FPS,
    DEFAULT_RESOLUTION,
)
from .devices import camera_status, find_virtual_mic, list_audio_outputs, mic_status
from .logbus import LOGS, log
from .media import extract_audio, load_audio_meta, probe_video

try:
    import pyvirtualcam
except Exception:  # pragma: no cover
    pyvirtualcam = None

try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None


class InjectorEngine:
    def __init__(self):
        self._lock = threading.RLock()

        # 媒体
        self.video: dict | None = None
        self.audio_meta: dict | None = None
        self.audio_source = "video"      # video | file | none
        self.audio_file: str | None = None

        # 播放参数
        self.loop = True
        self.volume = 0.9
        self.out_w, self.out_h = DEFAULT_RESOLUTION
        self.out_fps = DEFAULT_FPS
        self.audio_dev_index: int | None = None

        # 运行时
        self.state = "idle"              # idle | playing | paused | ended
        self.injecting = False
        self._cam = None
        self._stream = None
        self._track: AudioTrack | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._vt: threading.Thread | None = None
        self._at: threading.Thread | None = None
        self._seek_req: float | None = None
        self._restart_audio = False

        self._last_frame: np.ndarray | None = None
        self._preview_jpeg = b""
        self._pos = 0.0
        self._frames_sent = 0
        self._audio_frames = 0
        self._measured_fps = 0.0
        self._prep_state = "idle"        # idle | working | ready | error
        self._prep_msg = ""
        self._last_error = ""
        self.audio_error = ""

    # ------------------------------------------------------------------ 媒体加载

    def set_video(self, path: str) -> dict:
        if self.injecting:
            raise RuntimeError("请先停止注入，再更换素材")
        path = os.path.abspath(path)
        info = probe_video(path)
        with self._lock:
            self.video = info
            self._last_frame = None
            self._pos = 0.0
            self._preview_jpeg = b""
            self.state = "idle"
        log(f"已加载视频：{info['name']}  {info['width']}x{info['height']} "
            f"{info['fps']}fps  {info['duration']:.1f}s")
        if self.audio_source == "video":
            if info["has_audio"]:
                self._prepare_audio_async(path)
            else:
                with self._lock:
                    self.audio_meta = None
                    self._prep_state = "ready"
                    self._prep_msg = "该视频无音轨，将输出静音"
                log("该视频不含音轨，虚拟麦克风将输出静音", "warn")
        # 抓一张首帧给预览
        self._grab_first_frame()
        return info

    def set_audio_source(self, mode: str, path: str | None = None):
        if self.injecting:
            raise RuntimeError("请先停止注入，再更换音频源")
        if mode not in ("video", "file", "none"):
            raise ValueError("audio mode 只能是 video / file / none")
        with self._lock:
            self.audio_source = mode
            self.audio_file = os.path.abspath(path) if path else None
            self.audio_meta = None
            self._prep_state = "idle"

        if mode == "none":
            self._prep_state = "ready"
            self._prep_msg = "静音输出"
            log("音频源：静音")
            return
        if mode == "file":
            if not path:
                raise RuntimeError("未选择音频文件")
            self._prepare_audio_async(self.audio_file)
            log(f"音频源：外部文件 {os.path.basename(self.audio_file)}")
        else:
            if self.video and self.video.get("has_audio"):
                self._prepare_audio_async(self.video["path"])
                log("音频源：跟随视频音轨")
            else:
                self._prep_state = "ready"
                self._prep_msg = "当前视频无音轨，将输出静音"

    def _prepare_audio_async(self, src: str):
        with self._lock:
            self._prep_state = "working"
            self._prep_msg = "正在解析音轨…"

        def run():
            try:
                target_sr = self._target_sr()
                meta = load_audio_meta(src, target_sr) or extract_audio(src, target_sr)
                with self._lock:
                    self.audio_meta = meta
                    self._prep_state = "ready"
                    self._prep_msg = f"{meta['duration']:.1f}s 音轨就绪"
            except Exception as exc:
                with self._lock:
                    self._prep_state = "error"
                    self._prep_msg = f"音轨解析失败：{exc}"
                log(f"音轨解析失败：{exc}", "error")

        threading.Thread(target=run, daemon=True, name="prep-audio").start()

    def _target_sr(self) -> int:
        """音频以 48k 为基准；选中的设备不支持时用设备默认值。"""
        return AUDIO_SR

    def _grab_first_frame(self):
        if not self.video:
            return

        def run():
            cap = cv2.VideoCapture(self.video["path"])
            ok, frame = cap.read()
            cap.release()
            if ok and frame is not None:
                self._last_frame = self._fit(frame)
                self._update_preview(self._last_frame)

        threading.Thread(target=run, daemon=True, name="grab-frame").start()

    # ------------------------------------------------------------------ 设备

    def scan_devices(self) -> dict:
        cam = camera_status(force=True)
        mic = mic_status(force=True)
        devs = mic["devices"]
        with self._lock:
            if self.audio_dev_index is None and mic["device"]:
                self.audio_dev_index = mic["device"]["index"]
            if self.audio_dev_index is not None and not any(
                d["index"] == self.audio_dev_index for d in devs
            ):
                self.audio_dev_index = mic["device"]["index"] if mic["device"] else None
        return {"camera": cam, "mic": mic, "devices": devs,
                "selected_audio": self.audio_dev_index}

    def set_audio_device(self, index: int):
        if self.injecting:
            raise RuntimeError("请先停止注入，再切换音频设备")
        self.audio_dev_index = int(index)
        name = next((d["name"] for d in list_audio_outputs() if d["index"] == index), index)
        log(f"音频输出端点：{name}")

    def set_output(self, width: int | None = None, height: int | None = None, fps: int | None = None):
        if self.injecting:
            raise RuntimeError("请先停止注入，再修改输出参数")
        with self._lock:
            if width and height:
                self.out_w, self.out_h = int(width), int(height)
            if fps:
                self.out_fps = int(fps)
        log(f"输出参数：{self.out_w}x{self.out_h} @ {self.out_fps}fps")

    # ------------------------------------------------------------------ 启停

    def start(self):
        with self._lock:
            if self.injecting:
                return
            if not self.video:
                raise RuntimeError("请先选择视频文件")
            if pyvirtualcam is None:
                raise RuntimeError("缺少 pyvirtualcam，请先 pip install pyvirtualcam")
            if sd is None:
                raise RuntimeError("缺少 sounddevice，请先 pip install sounddevice")
            if self._prep_state == "working":
                raise RuntimeError("音轨还在解析，请稍候")

            cam_info = camera_status()
            if not cam_info["ok"]:
                raise RuntimeError(cam_info["message"])

            # 1) 打开虚拟摄像头
            try:
                self._cam = pyvirtualcam.Camera(
                    width=self.out_w, height=self.out_h, fps=self.out_fps,
                    backend=cam_info["backend"], print_fps=False,
                )
            except Exception as exc:
                raise RuntimeError(f"打开虚拟摄像头失败：{exc}")
            log(f"虚拟摄像头已开启：{self._cam.device} {self.out_w}x{self.out_h}@{self.out_fps}")

            # 2) 打开音频输出端点（失败不阻塞视频注入，只是没有声音）
            self.audio_error = ""
            try:
                self._open_audio_stream()
            except Exception as exc:
                self._stream = None
                self.audio_error = str(exc)
                log(f"虚拟麦克风未就绪：{exc}", "warn")

            # 3) 起线程
            self._stop.clear()
            self._pause.clear()
            self._seek_req = None
            self._frames_sent = 0
            self._audio_frames = 0
            self.state = "playing"
            self.injecting = True

            self._vt = threading.Thread(target=self._video_loop, daemon=True, name="video-loop")
            self._at = threading.Thread(target=self._audio_loop, daemon=True, name="audio-loop")
            self._vt.start()
            if self._stream is not None:
                self._at.start()

        log("注入已开始：第三方应用现在可以看到画面 / 听到声音", "ok")

    def _open_audio_stream(self):
        dev_index = self.audio_dev_index
        dev = next((d for d in list_audio_outputs() if d["index"] == dev_index), None)
        if dev is None:
            raise RuntimeError("未选择音频输出端点（需为虚拟声卡的 CABLE Input）")
        if not dev["is_virtual"]:
            log(f"注意：{dev['name']} 不是虚拟声卡端点，声音会从本机扬声器播放出去", "warn")

        # 建立音频轨道
        meta = None
        if self.audio_source != "none":
            src = self.video["path"] if self.audio_source == "video" else self.audio_file
            if src:
                if self._prep_state != "ready" or not self.audio_meta:
                    meta = load_audio_meta(src, AUDIO_SR) or extract_audio(src, AUDIO_SR)
                    with self._lock:
                        self.audio_meta = meta
                else:
                    meta = self.audio_meta
                if meta and meta.get("src") != os.path.abspath(src) and self.audio_source != "video":
                    meta = self.audio_meta

        with self._lock:
            if self._track:
                self._track.close()
            self._track = None
            if meta:
                self._track = AudioTrack(meta["pcm"], meta["sr"], meta["ch"], meta["frames"])
                log(f"虚拟麦克风音源：{meta['name']} {meta['duration']:.1f}s")

        # 优先 48k，失败退回设备默认采样率
        sr = AUDIO_SR
        try:
            self._stream = sd.OutputStream(
                device=dev_index, samplerate=sr, channels=2, dtype="float32",
                blocksize=AUDIO_BLOCK, latency="low",
            )
            self._stream.start()
        except Exception:
            sr = int(dev.get("samplerate") or 44100)
            if self._track and self._track.sr != sr:
                src = self.video["path"] if self.audio_source == "video" else self.audio_file
                if src:
                    meta = load_audio_meta(src, sr) or extract_audio(src, sr)
                    self._track.close()
                    self._track = AudioTrack(meta["pcm"], meta["sr"], meta["ch"], meta["frames"])
            self._stream = sd.OutputStream(
                device=dev_index, samplerate=sr, channels=2, dtype="float32",
                blocksize=int(sr * 0.02), latency="low",
            )
            self._stream.start()
        log(f"虚拟麦克风已开启：{dev['name']} @ {sr}Hz")

    def stop(self):
        with self._lock:
            if not self.injecting:
                return
            self.injecting = False
            self.state = "idle"
            self._stop.set()
            self._pause.clear()
        for t in (self._vt, self._at):
            if t and t.is_alive():
                t.join(timeout=1.5)
        self._vt = self._at = None
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        try:
            if self._cam is not None:
                self._cam.close()
        except Exception:
            pass
        self._cam = None
        if self._track:
            self._track.close()
            self._track = None
        log("注入已停止", "ok")

    # ------------------------------------------------------------------ 控制

    def toggle_pause(self):
        if not self.injecting:
            return
        if self._pause.is_set():
            self._pause.clear()
            self.state = "playing"
            log("继续播放")
        else:
            self._pause.set()
            self.state = "paused"
            log("已暂停（画面保持在最后一帧，麦克风静音）")

    def seek(self, seconds: float):
        if not self.video:
            return
        seconds = max(0.0, min(float(seconds), self.video["duration"]))
        self._seek_req = seconds
        self._pos = seconds
        if self._track:
            self._track.seek(seconds)

    def set_loop(self, on: bool):
        self.loop = bool(on)
        log(f"循环播放：{'开' if self.loop else '关'}")

    def set_volume(self, v: float):
        self.volume = max(0.0, min(1.0, float(v)))

    # ------------------------------------------------------------------ 线程

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        """等比缩放 + 黑边填充到输出尺寸。"""
        h, w = frame.shape[:2]
        tw, th = self.out_w, self.out_h
        if (w, h) == (tw, th):
            return np.ascontiguousarray(frame)
        scale = min(tw / w, th / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        resized = cv2.resize(frame, (nw, nh), interpolation=interp)
        canvas = np.zeros((th, tw, 3), np.uint8)
        x0, y0 = (tw - nw) // 2, (th - nh) // 2
        canvas[y0:y0 + nh, x0:x0 + nw] = resized
        return canvas

    def _video_loop(self):
        cap = cv2.VideoCapture(self.video["path"])
        src_fps = float(self.video["fps"] or 30)
        cam_period = 1.0 / max(1, self.out_fps)
        t0 = time.perf_counter()
        idx = 0
        last_sent_t = -1e9
        sent_window = []
        while not self._stop.is_set():
            # 处理 seek
            req = self._seek_req
            if req is not None:
                self._seek_req = None
                idx = int(req * src_fps)
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                t0 = time.perf_counter() - req
                last_sent_t = -1e9
                self._restart_audio = True

            if self._pause.is_set():
                if self._last_frame is not None:
                    self._send(self._last_frame, count=False)
                time.sleep(cam_period)
                continue

            ok, frame = cap.read()
            if not ok:
                if self.loop and idx > 0:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    idx = 0
                    t0 = time.perf_counter()
                    last_sent_t = -1e9
                    if self.audio_source == "video":
                        self._restart_audio = True
                    log("视频循环重播")
                    continue
                # 播完：压住最后一帧，保持设备有画面
                if self.state != "ended":
                    with self._lock:
                        self.state = "ended"
                    log("视频播放结束（保持最后一帧）")
                if self._last_frame is not None:
                    self._send(self._last_frame, count=False)
                time.sleep(cam_period)
                continue

            idx += 1
            t_src = idx / src_fps
            # 源帧率高于输出帧率时抽帧，保证按真实时长推进
            if t_src - last_sent_t < cam_period * 0.9:
                continue

            out = self._fit(frame)
            self._last_frame = out
            self._send(out)
            last_sent_t = t_src

            with self._lock:
                self._pos = t_src
                sent_window.append(time.perf_counter())

            # 节拍：按源时间轴睡眠
            target = t0 + t_src
            dt = target - time.perf_counter()
            if dt > 0:
                time.sleep(min(dt, cam_period * 3))
            elif dt < -cam_period * 5:
                t0 = time.perf_counter() - t_src

            self._update_preview(out)
            now = time.perf_counter()
            sent_window[:] = [x for x in sent_window if now - x < 1.0]
            self._measured_fps = float(len(sent_window))

        cap.release()

    def _send(self, frame: np.ndarray, count: bool = True):
        try:
            self._cam.send(frame)
            if count:
                self._frames_sent += 1
        except Exception as exc:
            if self._last_error != str(exc):
                self._last_error = str(exc)
                log(f"送帧失败：{exc}", "error")

    def _audio_loop(self):
        if self._stream is None:
            return
        period = 1.0 / AUDIO_SR
        while not self._stop.is_set():
            if self._restart_audio:
                self._restart_audio = False
                if self._track:
                    self._track.seek(0)
            if self._pause.is_set() or self._track is None:
                self._write_silence()
                continue
            block = self._track.read(AUDIO_BLOCK, loop=self.loop, volume=self.volume)
            try:
                self._stream.write(block)
                self._audio_frames += len(block)
            except Exception as exc:
                log(f"音频写入失败：{exc}", "error")
                time.sleep(0.05)

    def _write_silence(self):
        try:
            self._stream.write(np.zeros((AUDIO_BLOCK, 2), np.float32))
        except Exception:
            time.sleep(0.02)

    def _update_preview(self, frame: np.ndarray):
        try:
            h, w = frame.shape[:2]
            if w > 720:
                s = 720 / w
                small = cv2.resize(frame, (720, int(h * s)), interpolation=cv2.INTER_AREA)
            else:
                small = frame
            ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
            if ok:
                self._preview_jpeg = buf.tobytes()
        except Exception:
            pass

    # ------------------------------------------------------------------ 自检

    def selftest(self) -> dict:
        report = {"camera": None, "mic": None}
        cam_info = camera_status()
        if not cam_info["ok"]:
            report["camera"] = {"ok": False, "msg": cam_info["message"]}
        else:
            try:
                cam = pyvirtualcam.Camera(width=640, height=480, fps=30,
                                          backend=cam_info["backend"], print_fps=False)
                frame = np.zeros((480, 640, 3), np.uint8)
                frame[:, :, 1] = 210
                for _ in range(10):
                    cam.send(frame)
                    cam.sleep_until_next_frame()
                cam.close()
                report["camera"] = {"ok": True, "msg": f"{cam.device} 可写入（640x480 测试图案）"}
                log("自检：虚拟摄像头写入正常", "ok")
            except Exception as exc:
                report["camera"] = {"ok": False, "msg": f"写入失败：{exc}"}
                log(f"自检：虚拟摄像头写入失败 - {exc}", "error")

        mic = mic_status()
        if not mic["ok"]:
            report["mic"] = {"ok": False, "msg": mic["message"]}
        else:
            try:
                dev = mic["device"]
                sr = 48000
                stream = sd.OutputStream(device=dev["index"], samplerate=sr, channels=2,
                                         dtype="float32", blocksize=AUDIO_BLOCK)
                stream.start()
                t = np.arange(sr // 2) / sr
                tone = (0.25 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
                block = np.stack([tone, tone], axis=1)
                for i in range(0, len(block), AUDIO_BLOCK):
                    stream.write(block[i:i + AUDIO_BLOCK])
                stream.stop()
                stream.close()
                report["mic"] = {"ok": True, "msg": f"{dev['name']} 可写入（0.5s 440Hz 测试音）"}
                log("自检：虚拟麦克风写入正常（放了 0.5s 测试音）", "ok")
            except Exception as exc:
                report["mic"] = {"ok": False, "msg": f"写入失败：{exc}"}
                log(f"自检：虚拟麦克风写入失败 - {exc}", "error")
        return report

    # ------------------------------------------------------------------ 快照

    def snapshot(self) -> dict:
        cam = camera_status()
        mic = mic_status()
        with self._lock:
            # 首次有虚拟声卡就自动选中，省得用户手动挑
            if self.audio_dev_index is None:
                v = find_virtual_mic(mic["devices"])
                if v:
                    self.audio_dev_index = v["index"]
            return {
                "state": self.state,
                "injecting": self.injecting,
                "video": {
                    "name": self.video["name"] if self.video else None,
                    "path": self.video["path"] if self.video else None,
                    "width": self.video["width"] if self.video else 0,
                    "height": self.video["height"] if self.video else 0,
                    "fps": self.video["fps"] if self.video else 0,
                    "duration": self.video["duration"] if self.video else 0,
                    "has_audio": self.video["has_audio"] if self.video else False,
                },
                "audio": {
                    "source": self.audio_source,
                    "file": self.audio_file,
                    "prep_state": self._prep_state,
                    "prep_msg": self._prep_msg,
                    "duration": self.audio_meta["duration"] if self.audio_meta else 0,
                    "name": self.audio_meta["name"] if self.audio_meta else None,
                    "error": self.audio_error,
                },
                "loop": self.loop,
                "volume": self.volume,
                "out": {"w": self.out_w, "h": self.out_h, "fps": self.out_fps},
                "camera": cam,
                "mic": mic,
                "selected_audio": self.audio_dev_index,
                "pos": round(self._pos, 2),
                "frames_sent": self._frames_sent,
                "measured_fps": round(self._measured_fps, 1),
                "has_preview": bool(self._preview_jpeg),
            }

    def preview_jpeg(self) -> bytes:
        return self._preview_jpeg


ENGINE = InjectorEngine()
