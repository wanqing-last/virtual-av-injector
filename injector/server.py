# -*- coding: utf-8 -*-
"""本地 HTTP 服务：静态页面 + JSON API + MJPEG 预览。

用标准库跑，避免额外框架依赖；只监听 127.0.0.1。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .config import AUDIO_EXTS, VIDEO_EXTS
from .engine import ENGINE
from .logbus import LOGS, log
from .media import clear_cache
from .paths import resource_root

WEB_DIR = os.path.join(resource_root(), "injector", "web")
ASSET_DIR = os.path.join(resource_root(), "assets")

_pick_lock = threading.Lock()
_placeholder_cache = b""
_HTTPD = None


def _shutdown():
    """先停注入（否则虚拟摄像头被占着，第三方应用会黑屏），再关服务、退进程。"""
    import time as _t
    try:
        ENGINE.stop()
    except Exception:
        pass
    _t.sleep(0.4)
    global _HTTPD
    try:
        if _HTTPD is not None:
            _HTTPD.shutdown()
    except Exception:
        pass
    _t.sleep(0.3)
    os._exit(0)


class Handler(BaseHTTPRequestHandler):
    server_version = "VAInjector/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return {}

    # ------------------------------------------------------------ GET

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/api/state":
            qs = parse_qs(urlparse(self.path).query)
            since = int((qs.get("since") or ["0"])[0])
            snap = ENGINE.snapshot()
            snap["logs"] = LOGS.since(since)
            snap["log_id"] = LOGS.tail(1)[0]["id"] if LOGS.tail(1) else 0
            return self._json(snap)
        if path == "/preview.jpg":
            jpg = ENGINE.preview_jpeg()
            if not jpg:
                return self._json({"ok": False}, 404)
            return self._send(200, jpg, "image/jpeg")
        if path == "/stream.mjpg":
            return self._mjpeg()
        if path == "/api/devices":
            return self._json(ENGINE.scan_devices())
        return self._json({"ok": False, "error": "not found"}, 404)

    def _serve_file(self, name: str, ctype: str):
        p = os.path.join(WEB_DIR, name)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return self._json({"ok": False, "error": "missing web asset"}, 404)
        self._send(200, data, ctype)

    def _mjpeg(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        last = None
        try:
            while True:
                jpg = ENGINE.preview_jpeg()
                if jpg and jpg is not last:
                    last = jpg
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                time.sleep(1 / 20)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ------------------------------------------------------------ POST

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._body()
        try:
            if path == "/api/load_video":
                if data.get("demo"):
                    demo = os.path.join(ASSET_DIR, "demo.mp4")
                    return self._json({"ok": True, "video": ENGINE.set_video(demo)})
                p = (data.get("path") or "").strip('"').strip()
                if not p or not os.path.exists(p):
                    return self._json({"ok": False, "error": "文件不存在"}, 400)
                return self._json({"ok": True, "video": ENGINE.set_video(p)})

            if path == "/api/pick":
                kind = data.get("kind", "video")
                p = pick_file(kind)
                return self._json({"ok": True, "path": p})

            if path == "/api/audio_source":
                ENGINE.set_audio_source(data.get("mode", "video"), data.get("path"))
                return self._json({"ok": True})

            if path == "/api/start":
                ENGINE.start()
                return self._json({"ok": True})

            if path == "/api/stop":
                ENGINE.stop()
                return self._json({"ok": True})

            if path == "/api/pause":
                ENGINE.toggle_pause()
                return self._json({"ok": True})

            if path == "/api/loop":
                ENGINE.set_loop(bool(data.get("on")))
                return self._json({"ok": True})

            if path == "/api/volume":
                ENGINE.set_volume(data.get("v", 0.9))
                return self._json({"ok": True})

            if path == "/api/seek":
                ENGINE.seek(float(data.get("t", 0)))
                return self._json({"ok": True})

            if path == "/api/output":
                ENGINE.set_output(data.get("w"), data.get("h"), data.get("fps"))
                return self._json({"ok": True})

            if path == "/api/audio_device":
                ENGINE.set_audio_device(int(data.get("index")))
                return self._json({"ok": True})

            if path == "/api/scan":
                return self._json({"ok": True, **ENGINE.scan_devices()})

            if path == "/api/selftest":
                return self._json({"ok": True, "report": ENGINE.selftest()})

            if path == "/api/clear_cache":
                n = clear_cache()
                log(f"已清理 {n} 个音频缓存文件")
                return self._json({"ok": True, "n": n})

            if path == "/api/quit":
                log("收到退出指令，正在关闭…", "warn")
                self._json({"ok": True})
                threading.Thread(target=_shutdown, daemon=True).start()
                return

            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:
            log(f"{exc}", "error")
            return self._json({"ok": False, "error": str(exc)}, 400)


# ---------------------------------------------------------------- 原生文件选择框

_PS_PICK = r"""
Add-Type -AssemblyName System.Windows.Forms | Out-Null
Add-Type -AssemblyName System.Drawing | Out-Null
$f = New-Object System.Windows.Forms.OpenFileDialog
$f.Title = '{title}'
$f.Filter = '{filter}'
$f.Multiselect = $false
$f.RestoreDirectory = $true
$r = $f.ShowDialog()
if ($r -eq [System.Windows.Forms.DialogResult]::OK) {{
  [System.IO.File]::WriteAllText('{out}', $f.FileName, [System.Text.Encoding]::UTF8)
}}
"""


def pick_file(kind: str = "video") -> str:
    """调用 Windows 原生打开对话框（拖到浏览器里拿不到本地绝对路径）。"""
    if kind == "video":
        title, filt = "选择视频文件", f"视频文件|*{VIDEO_EXTS.replace(' ', ';*')}|所有文件|*.*"
    else:
        title, filt = "选择音频文件", f"音频文件|*{AUDIO_EXTS.replace(' ', ';*')}|所有文件|*.*"

    out = os.path.join(tempfile.gettempdir(), f"vai_pick_{os.getpid()}.txt")
    try:
        if os.path.exists(out):
            os.unlink(out)
    except OSError:
        pass

    script = _PS_PICK.format(title=title, filter=filt, out=out.replace("'", "''"))
    with _pick_lock:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", script],
                startupinfo=si, timeout=300,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) & 0,
            )
        except Exception as exc:
            raise RuntimeError(f"打开文件对话框失败：{exc}")

        if os.path.exists(out):
            with open(out, "r", encoding="utf-8-sig") as f:
                p = f.read().strip()
            try:
                os.unlink(out)
            except OSError:
                pass
            if p and os.path.exists(p):
                return p
    return ""


def serve(host: str = "127.0.0.1", port: int = 0):
    global _HTTPD
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    _HTTPD = httpd
    return httpd
