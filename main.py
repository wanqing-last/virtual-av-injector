# -*- coding: utf-8 -*-
"""启动入口：拉起本地服务，用 Chrome/Edge 的 --app 模式开一个无边框窗口。

这样既有原生桌面窗口的观感，又不用依赖 tkinter / Qt。
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

BROWSERS = [
    (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome"),
    (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "chrome"),
    (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "edge"),
    (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "edge"),
    (os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"), "chrome"),
    (os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"), "edge"),
]


def free_port(preferred: int = 0) -> int:
    if preferred:
        return preferred
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def open_window(url: str, profile_dir: str) -> bool:
    for path, kind in BROWSERS:
        if not os.path.exists(path):
            continue
        args = [path, f"--app={url}", "--window-size=1400,900",
                "--window-position=120,60", f"--user-data-dir={profile_dir}",
                "--no-first-run", "--no-default-browser-check"]
        try:
            subprocess.Popen(args, close_fds=True)
            return True
        except Exception:
            continue
    return False


def say(msg: str):
    """打包成无控制台 exe 后 stdout 为 None。"""
    if sys.stdout is not None:
        try:
            print(msg, flush=True)
        except Exception:
            pass


def fatal(msg: str):
    """无控制台时用原生消息框报错，否则控制台一闪而过用户看不到原因。"""
    say(msg)
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "虚拟音视频注入器", 0x10)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="虚拟音视频注入器")
    ap.add_argument("--port", type=int, default=0, help="本地服务端口（默认随机）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开窗口")
    args = ap.parse_args()

    try:
        from injector.server import serve
        from injector.logbus import log
        from injector.config import APP_NAME, VERSION
    except Exception as exc:
        fatal(f"启动失败（模块加载异常）：\n{exc}")
        return 1

    missing = []
    for mod, pkg in [("numpy", "numpy"), ("cv2", "opencv-python"), ("pyvirtualcam", "pyvirtualcam"),
                     ("sounddevice", "sounddevice"), ("av", "av")]:
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    if missing:
        msg = "缺少依赖，请先执行：\n    pip install " + " ".join(missing)
        if getattr(sys, "frozen", False):
            fatal("程序组件缺失（可能是杀毒软件拦截了临时解压目录）：\n"
                  + "、".join(missing) + "\n\n请把整个文件夹加入杀软白名单后重试。")
        else:
            say(msg)
        return 1

    port = free_port(args.port)
    httpd = serve("127.0.0.1", port)
    import threading
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()

    url = f"http://127.0.0.1:{port}/"
    log(f"{APP_NAME} v{VERSION} 已启动：{url}", "ok")

    if not args.no_browser:
        import tempfile
        profile = os.path.join(tempfile.gettempdir(), "virtual-av-injector", "window-profile")
        os.makedirs(profile, exist_ok=True)
        if not open_window(url, profile):
            webbrowser.open(url)

    say(f"\n  界面地址： {url}")
    say("  关闭界面上的『退出程序』按钮，或结束本进程即退出\n")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            from injector.engine import ENGINE
            ENGINE.stop()
        except Exception:
            pass
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
