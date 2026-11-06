# -*- coding: utf-8 -*-
"""端到端自测：直接打本地 API，再从「第三方视角」采集虚拟摄像头画面校验。

用法：python tools/e2e_test.py [端口]
"""
import json
import sys
import time
import urllib.request

import cv2
import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
BASE = f"http://127.0.0.1:{PORT}"


def api(path, body=None, method="POST"):
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def state():
    return api("/api/state", method="GET")


def cam_index():
    import ctypes
    from pygrabber.dshow_graph import FilterGraph
    ctypes.windll.ole32.CoInitializeEx(None, 2)
    names = FilterGraph().get_input_devices()
    for i, n in enumerate(names):
        if "obs virtual camera" in n.lower():
            return i, names
    return None, names


def grab(idx, n=8):
    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
    frames = []
    if cap.isOpened():
        for _ in range(n):
            ok, f = cap.read()
            if ok:
                frames.append(f)
        cap.release()
    return frames


def main():
    print("== 1. 加载演示素材")
    r = api("/api/load_video", {"demo": True})
    assert r["ok"], r
    time.sleep(1.5)

    print("== 2. 选音频端点（有虚拟声卡就自动，没有则用系统默认）")
    s = state()
    idx = s["selected_audio"]
    if idx is None:
        out = [d for d in s["mic"]["devices"]]
        if out:
            idx = out[0]["index"]
            api("/api/audio_device", {"index": idx})
    api("/api/volume", {"v": 0.03})
    print("   音频端点 index =", idx, "| 虚拟麦克风就绪 =", s["mic"]["ok"])

    print("== 3. 开始注入")
    r = api("/api/start")
    assert r["ok"], r
    time.sleep(3)

    s = state()
    print(f"   状态={s['state']} 已送帧={s['frames_sent']} 实测={s['measured_fps']}fps "
          f"进度={s['pos']}s 音轨错误={s['audio'].get('error')!r}")
    assert s["injecting"], "注入未启动"
    assert s["frames_sent"] > 20, "送帧数异常"
    assert s["pos"] > 1.5, "播放进度没有推进"

    print("== 4. 第三方采集虚拟摄像头")
    ci, names = cam_index()
    print("   DShow 设备:", names)
    assert ci is not None, "系统里没有 OBS Virtual Camera"
    frames = grab(ci, 6)
    assert frames, "采集失败"
    f = frames[-1]
    std = float(f.reshape(-1, 3).std())
    print(f"   采集到 {len(frames)} 帧，尺寸 {f.shape[1]}x{f.shape[0]}，像素标准差 {std:.1f}")
    assert std > 8, "画面疑似纯色（未收到视频内容）"

    print("== 5. 暂停 → 应保持最后一帧且继续出帧")
    api("/api/pause")
    time.sleep(1.2)
    s2 = state()
    frames2 = grab(ci, 4)
    print(f"   状态={s2['state']}，暂停后仍采集到 {len(frames2)} 帧，标准差 "
          f"{float(frames2[-1].reshape(-1,3).std()):.1f}" if frames2 else "   暂停后无帧")
    assert s2["state"] == "paused" and frames2, "暂停时画面断了"

    print("== 6. 继续 → 停止")
    api("/api/pause")
    time.sleep(0.5)
    api("/api/stop")
    s3 = state()
    print(f"   状态={s3['state']} injecting={s3['injecting']}")
    assert not s3["injecting"]

    print("\n全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
