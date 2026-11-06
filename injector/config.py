# -*- coding: utf-8 -*-
"""全局配置与常量。"""

import os
import tempfile

APP_NAME = "虚拟音视频注入器"
APP_ID = "virtual-av-injector"
VERSION = "1.0.0"

# 预览与输出预设
RESOLUTION_PRESETS = [
    ("640x480", 640, 480),
    ("1280x720", 1280, 720),
    ("1920x1080", 1920, 1080),
]
DEFAULT_RESOLUTION = (1280, 720)
FPS_PRESETS = [15, 24, 25, 30, 60]
DEFAULT_FPS = 30

# 虚拟声卡识别关键词（不区分大小写）
VIRTUAL_MIC_KEYWORDS = [
    "cable input",          # VB-Audio Virtual Cable 的播放端点
    "vb-audio",
    "voicemeeter",
    "virtual audio",
    "virtual cable",
    "虚拟声卡",
]

# 虚拟摄像头识别关键词
VIRTUAL_CAM_KEYWORDS = [
    "obs virtual camera",
    "obs-camera",
    "unity capture",
    "unitycapture",
    "virtual camera",
    "虚拟摄像头",
]

TEMP_ROOT = os.path.join(tempfile.gettempdir(), APP_ID)
CACHE_DIR = os.path.join(TEMP_ROOT, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 音频统一内部采样率
AUDIO_SR = 48000
AUDIO_CHANNELS = 2
AUDIO_BLOCK = 960          # 20ms @48k，虚拟声卡够稳

VIDEO_EXTS = ".mp4 .mkv .avi .mov .webm .flv .ts .m4v .wmv .mpg .mpeg .m2ts"
AUDIO_EXTS = ".mp3 .wav .flac .ogg .m4a .aac .wma .opus .aiff .ape"
