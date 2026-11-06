# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本：产出单文件 exe。

用法：
    python -m PyInstaller build.spec --noconfirm --clean

要点（这三处坑都已处理）：
- sounddevice 是单文件模块，它的 PortAudio DLL 在 site-packages/_sounddevice_data/，
  collect_data_files 拿不到，必须手动按路径加；
- pyvirtualcam 的 .pyd 后端要显式 collect（否则运行时报 "No camera registered"）；
- av 的 ffmpeg 动态库在 av.libs/，也要带。
"""

import os
import sys

import sounddevice
from PyInstaller.utils.hooks import collect_dynamic_libs

# PyInstaller 执行 spec 时没有 __file__，用内置的 SPECPATH
ROOT = os.path.abspath(SPECPATH)

# ---------------------------------------------------------------- 数据文件
datas = [
    (os.path.join(ROOT, "injector", "web"), os.path.join("injector", "web")),
]
demo = os.path.join(ROOT, "assets", "demo.mp4")
if os.path.exists(demo):
    datas.append((demo, "assets"))

# PortAudio 二进制：sounddevice 与 _sounddevice_data 同级
_sd_dir = os.path.dirname(os.path.abspath(sounddevice.__file__))
_pa_dir = os.path.join(_sd_dir, "_sounddevice_data")
if os.path.isdir(_pa_dir):
    datas.append((_pa_dir, "_sounddevice_data"))
else:
    print(f"[spec] 警告：没找到 PortAudio 目录 {_pa_dir}")

# ---------------------------------------------------------------- 二进制
binaries = collect_dynamic_libs("pyvirtualcam")
binaries += collect_dynamic_libs("av")

# ---------------------------------------------------------------- 隐式导入
hiddenimports = [
    "pyvirtualcam",
    "pyvirtualcam._native_windows_obs",
    "pyvirtualcam._native_windows_unity_capture",
    "sounddevice",
    "_cffi_backend",
    "pygrabber.dshow_graph",
    "pygrabber.dshow_core",
    "pygrabber.dshow_ids",
    "pygrabber.moniker",
    "pygrabber.win_api_extra",
    "pygrabber.win_common_types",
    "pygrabber.windows_media",
    "av",
    "av.audio",
    "av.video",
    "cv2",
    "numpy",
]

excludes = [
    "tkinter", "matplotlib", "scipy", "pandas",
    "PyQt5", "PySide2", "PySide6", "wx", "IPython", "jupyter",
    "pytest", "setuptools", "pip", "test", "unittest", "pydoc_data",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="虚拟音视频注入器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
