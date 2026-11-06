# -*- coding: utf-8 -*-
"""生成演示素材 assets/demo.mp4（1280x720@30，12 秒，带 AAC 音轨）。

用法：python tools/make_demo.py
"""
import math
import os
import sys

import av
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "demo.mp4")

W, H, FPS, DUR = 1280, 720, 30, 12
SR = 48000


def make_video_frame(t: float) -> np.ndarray:
    """渐变背景 + 旋转的圆 + 跳动的频谱条。"""
    y, x = np.mgrid[0:H, 0:W].astype(np.float32)
    nx, ny = x / W, y / H
    base = np.zeros((H, W, 3), np.float32)
    base[..., 0] = 30 + 40 * nx + 25 * math.sin(t * 0.8)
    base[..., 1] = 45 + 60 * ny + 20 * math.sin(t * 1.1 + 1.2)
    base[..., 2] = 70 + 55 * (1 - ny) + 20 * math.sin(t * 0.6 + 2.4)

    # 旋转圆
    ang = t * 1.6
    cx, cy = W / 2 + math.cos(ang) * 260, H / 2 + math.sin(ang * 1.3) * 170
    r = 120 + 30 * math.sin(t * 2.2)
    d = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
    m = np.clip(1.0 - (d - r) / 3.0, 0, 1)[..., None]
    base = base * (1 - m) + np.array([90, 220, 220], np.float32) * m

    # 底部频谱条
    bars = 40
    bw = W // bars
    for i in range(bars):
        h = int(abs(math.sin(t * 3 + i * 0.5)) * 120 + 12)
        base[H - h:H, i * bw + 2:(i + 1) * bw - 2] = (200, 180, 90)

    img = np.clip(base, 0, 255).astype(np.uint8)
    return img


def make_audio() -> np.ndarray:
    """简单旋律 + 节拍，方便在会议/直播端一眼（耳）确认。"""
    n = int(SR * DUR)
    t = np.arange(n) / SR
    sig = np.zeros(n, np.float64)
    notes = [523.25, 587.33, 659.25, 783.99, 659.25, 587.33]
    step = DUR / len(notes)
    for i, f in enumerate(notes):
        s, e = int(i * step * SR), int((i + 1) * step * SR)
        tt = np.arange(e - s) / SR
        env = np.minimum(1.0, np.minimum(tt / 0.02, (step - tt) / 0.05))
        env = np.clip(env, 0, 1) * np.exp(-tt * 0.6)
        sig[s:e] += 0.35 * np.sin(2 * np.pi * f * tt) * env
    # 每秒一个低音鼓点
    for k in range(DUR):
        s = int(k * SR)
        ln = min(int(0.12 * SR), n - s)
        tt = np.arange(ln) / SR
        sig[s:s + ln] += 0.28 * np.sin(2 * np.pi * 90 * tt) * np.exp(-tt * 26)
    stereo = np.stack([sig, sig * 0.95], axis=1)
    return (stereo * 32767).astype(np.int16)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if os.path.exists(OUT):
        os.unlink(OUT)
    container = av.open(OUT, mode="w")
    vs = container.add_stream("libx264", rate=FPS)
    vs.width, vs.height = W, H
    vs.pix_fmt = "yuv420p"
    vs.options = {"preset": "veryfast", "crf": "23"}

    as_ = container.add_stream("aac", rate=SR)
    as_.layout = "stereo"

    total = FPS * DUR
    pcm = make_audio()
    af_step = SR // FPS

    for i in range(total):
        img = make_video_frame(i / FPS)
        vf = av.VideoFrame.from_ndarray(img, format="bgr24")
        vf.pts = i
        for pkt in vs.encode(vf):
            container.mux(pkt)

    for i in range(total):
        chunk = pcm[i * af_step:(i + 1) * af_step]
        if len(chunk) == 0:
            break
        af = av.AudioFrame.from_ndarray(
            chunk.reshape(1, -1).astype(np.int16), format="s16", layout="stereo"
        )
        af.sample_rate = SR
        af.pts = i * af_step
        for pkt in as_.encode(af):
            container.mux(pkt)

    for pkt in vs.encode():
        container.mux(pkt)
    for pkt in as_.encode():
        container.mux(pkt)
    container.close()
    print("生成完成:", OUT, os.path.getsize(OUT) // 1024, "KB")


if __name__ == "__main__":
    # CI 里 runner 的 stdout 可能是 cp1252，中文会炸；统一改成 UTF-8 并容错
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
