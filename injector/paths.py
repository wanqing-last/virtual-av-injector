# -*- coding: utf-8 -*-
"""资源路径解析：兼容源码运行与 PyInstaller 打包后的 exe。"""

import os
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> str:
    """只读资源根目录（web/assets 等）。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_root() -> str:
    """exe / 项目所在目录，用于放可写的东西。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return resource_root()
