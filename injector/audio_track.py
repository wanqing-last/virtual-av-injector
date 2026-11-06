# -*- coding: utf-8 -*-
"""mmap 音频轨道：随机定位、循环、音量，全部零拷贝读。"""

from __future__ import annotations

import mmap
import threading

import numpy as np


class AudioTrack:
    def __init__(self, pcm_path: str, sr: int, channels: int, frames: int):
        self.path = pcm_path
        self.sr = sr
        self.channels = channels
        self.frames = frames
        self._f = open(pcm_path, "rb")
        if frames * channels * 2 != _size(self._f):
            # 以实际文件长度为准，避免缓存被截断
            self.frames = _size(self._f) // (channels * 2)
        self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        self._data = np.frombuffer(self._mm, dtype=np.int16).reshape(-1, channels)
        self.pos = 0
        self._lock = threading.Lock()

    # ---- 基本信息
    @property
    def duration(self) -> float:
        return self.frames / self.sr if self.sr else 0.0

    @property
    def position(self) -> float:
        return self.pos / self.sr if self.sr else 0.0

    # ---- 控制
    def seek(self, seconds: float):
        with self._lock:
            self.pos = max(0, min(self.frames, int(seconds * self.sr)))

    def seek_ratio(self, r: float):
        self.seek(max(0.0, min(1.0, r)) * self.duration)

    # ---- 读取
    def read(self, n: int, loop: bool = True, volume: float = 1.0) -> np.ndarray:
        """读 n 个采样帧，返回 (n, ch) float32；不足则补零。"""
        with self._lock:
            out = np.zeros((n, self.channels), dtype=np.float32)
            if self.frames == 0:
                return out
            filled = 0
            guard = 0
            while filled < n and guard < 8:
                guard += 1
                take = min(n - filled, self.frames - self.pos)
                if take > 0:
                    chunk = self._data[self.pos:self.pos + take]
                    out[filled:filled + take] = chunk.astype(np.float32) / 32768.0
                    self.pos += take
                    filled += take
                if filled >= n:
                    break
                if self.pos >= self.frames:
                    if loop:
                        self.pos = 0
                    else:
                        break
            if volume != 1.0:
                out *= float(volume)
            np.clip(out, -1.0, 1.0, out=out)
            return out

    def close(self):
        try:
            del self._data
            self._mm.close()
            self._f.close()
        except Exception:
            pass


def _size(f) -> int:
    cur = f.tell()
    f.seek(0, 2)
    n = f.tell()
    f.seek(cur)
    return n
