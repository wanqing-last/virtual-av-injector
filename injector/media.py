# -*- coding: utf-8 -*-
"""媒体探测与音频轨道准备。

音频不走实时解码：加载时把任意容器（mp4/mkv/mp3/flac…）里的音轨
统一解码成 48k/16bit/立体声裸 PCM 落到缓存目录，播放时用 mmap 随机读，
既不吃内存也不会在实时线程里做解码。
"""

from __future__ import annotations

import hashlib
import json
import os
import time

import cv2

from .config import AUDIO_CHANNELS, AUDIO_SR, CACHE_DIR
from .logbus import log

try:
    import av
except Exception:  # pragma: no cover
    av = None


# ---------------------------------------------------------------- 视频

def probe_video(path: str) -> dict:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("无法打开该视频文件（编码不受支持或文件损坏）")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        if fps <= 1 or fps > 240:
            fps = 30.0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        duration = frames / fps if frames > 0 else 0.0
        return {
            "path": path,
            "name": os.path.basename(path),
            "width": w,
            "height": h,
            "fps": round(fps, 3),
            "frames": frames,
            "duration": round(duration, 3),
            "has_audio": has_audio_stream(path),
        }
    finally:
        cap.release()


def has_audio_stream(path: str) -> bool:
    if av is None:
        return False
    try:
        with av.open(path) as c:
            return len(c.streams.audio) > 0
    except Exception:
        return False


# ---------------------------------------------------------------- 音频

def _cache_key(path: str, sr: int) -> str:
    try:
        st = os.stat(path)
        raw = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{sr}"
    except OSError:
        raw = f"{path}|{sr}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _meta_path(key: str) -> str:
    return os.path.join(CACHE_DIR, key + ".json")


def _pcm_path(key: str) -> str:
    return os.path.join(CACHE_DIR, key + ".pcm")


def load_audio_meta(path: str, sr: int = AUDIO_SR) -> dict | None:
    """若缓存已存在则直接返回 meta。"""
    key = _cache_key(path, sr)
    mp, pp = _meta_path(key), _pcm_path(key)
    if os.path.exists(mp) and os.path.exists(pp):
        try:
            with open(mp, "r", encoding="utf-8") as f:
                meta = json.load(f)
            meta["pcm"] = pp
            return meta
        except Exception:
            return None
    return None


def extract_audio(path: str, sr: int = AUDIO_SR, on_progress=None) -> dict:
    """把任意媒体的音轨解码为 48k 立体声 16bit 裸 PCM，返回 meta。"""
    if av is None:
        raise RuntimeError("缺少 PyAV，无法解析音轨（pip install av）")

    cached = load_audio_meta(path, sr)
    if cached:
        log(f"音轨命中缓存：{os.path.basename(path)}")
        return cached

    key = _cache_key(path, sr)
    mp, pp = _meta_path(key), _pcm_path(key)
    tmp = pp + ".part"

    log(f"正在解析音轨：{os.path.basename(path)} …")
    t0 = time.time()
    total_frames = 0

    with av.open(path) as container:
        if not container.streams.audio:
            raise RuntimeError("该文件不含音频轨")
        stream = container.streams.audio[0]
        duration = float(stream.duration * stream.time_base) if stream.duration else 0.0
        resampler = av.AudioResampler(
            format="s16", layout="stereo", rate=sr,
        )
        with open(tmp, "wb") as out:
            for frame in container.decode(stream):
                for rf in resampler.resample(frame):
                    out.write(_frame_bytes(rf))
                    total_frames += rf.samples
                if on_progress and duration > 0:
                    done = min(1.0, (float(frame.pts * stream.time_base) if frame.pts is not None else 0) / duration)
                    on_progress(done)
            for rf in resampler.resample(None):
                out.write(_frame_bytes(rf))
                total_frames += rf.samples

    os.replace(tmp, pp)
    meta = {
        "src": os.path.abspath(path),
        "name": os.path.basename(path),
        "sr": sr,
        "ch": AUDIO_CHANNELS,
        "frames": total_frames,
        "duration": round(total_frames / sr, 3),
        "pcm": pp,
    }
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    log(f"音轨就绪：{meta['duration']:.1f}s / {total_frames} 帧，耗时 {time.time() - t0:.1f}s")
    return meta


def _frame_bytes(rf) -> bytes:
    """PyAV AudioFrame → 交错 16bit 字节流。"""
    arr = rf.to_ndarray()
    if arr.ndim == 2 and arr.shape[0] == AUDIO_CHANNELS:
        # 平面格式 (ch, n) → 交错
        arr = arr.T.reshape(1, -1)
    return arr.tobytes()


def clear_cache():
    n = 0
    for f in os.listdir(CACHE_DIR):
        if f.endswith((".pcm", ".json", ".part")):
            try:
                os.unlink(os.path.join(CACHE_DIR, f))
                n += 1
            except OSError:
                pass
    return n
