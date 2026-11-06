# -*- coding: utf-8 -*-
"""设备探测：DirectShow 摄像头 / WASAPI 音频输出 / 虚拟设备识别。

设计要点：
1. 摄像头用 DirectShow 枚举（OpenCV DSHOW 后端与 pygrabber 顺序一致），
   这样才能拿到「OBS Virtual Camera」这类纯 DirectShow 滤镜的真实索引；
2. 音频优先用 WASAPI 端点（名字完整、延迟低），退化到 MME；
3. 虚拟声卡靠关键词识别「CABLE Input」这类播放端点。
"""

from __future__ import annotations

import importlib.util
import threading

from .config import VIRTUAL_CAM_KEYWORDS, VIRTUAL_MIC_KEYWORDS

try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None


# ---------------------------------------------------------------- 摄像头

_dshow_cache = {"names": None, "ts": 0.0}
_dshow_lock = threading.Lock()
_com_tls = threading.local()


def _ensure_com():
    """每个线程首次用 DirectShow 前必须初始化 COM，否则枚举直接返回空。"""
    if getattr(_com_tls, "ready", False):
        return
    try:
        import ctypes
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # APARTMENTTHREADED
        if hr not in (0, 1):  # S_OK / S_FALSE
            ctypes.windll.ole32.CoInitialize(None)
    except Exception:
        pass
    _com_tls.ready = True


def list_dshow_cameras(force: bool = False):
    """返回 DirectShow 视频输入设备名列表（顺序 = OpenCV CAP_DSHOW 索引）。"""
    import time

    with _dshow_lock:
        if not force and _dshow_cache["names"] is not None and time.time() - _dshow_cache["ts"] < 5:
            return _dshow_cache["names"], None
        _ensure_com()
        try:
            from pygrabber.dshow_graph import FilterGraph
            names = list(FilterGraph().get_input_devices())
            _dshow_cache["names"] = names
            _dshow_cache["ts"] = time.time()
            return names, None
        except Exception as exc:  # pygrabber 缺失或 COM 异常
            return [], f"DirectShow 枚举失败：{exc}"


def _obs_backend_available() -> bool:
    return importlib.util.find_spec("pyvirtualcam._native_windows_obs") is not None


def _unity_backend_available() -> bool:
    return importlib.util.find_spec("pyvirtualcam._native_windows_unity_capture") is not None


def _obs_filter_registered() -> bool:
    """OBS 虚拟摄像头 DirectShow 滤镜是否已注册（装了 OBS 就有）。"""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT,
            r"CLSID\{A3FCE0F5-3493-419F-958A-ABA1250EC20B}\InprocServer32",
        ):
            return True
    except OSError:
        return False


def camera_status(force: bool = False) -> dict:
    """虚拟摄像头可用性 + 推荐后端。"""
    names, err = list_dshow_cameras(force=force)
    obs_dev = next((n for n in names if "obs virtual camera" in n.lower()), None)

    backend = None
    if _obs_backend_available() and (obs_dev or _obs_filter_registered()):
        backend = "obs"
        obs_dev = obs_dev or "OBS Virtual Camera"
    elif _unity_backend_available():
        backend = "unitycapture"

    if backend == "obs":
        return {
            "ok": True,
            "backend": "obs",
            "name": obs_dev,
            "index": names.index(obs_dev) if obs_dev in names else None,
            "cameras": names,
            "message": f"已就绪：{obs_dev}",
        }
    if backend == "unitycapture":
        return {
            "ok": True,
            "backend": "unitycapture",
            "name": "Unity Capture",
            "index": None,
            "cameras": names,
            "message": "已就绪：Unity Capture 虚拟摄像头",
        }
    return {
        "ok": False,
        "backend": None,
        "name": None,
        "index": None,
        "cameras": names,
        "message": err or "未检测到 OBS Virtual Camera，请安装 OBS Studio（安装时勾选虚拟摄像头）",
    }


# ---------------------------------------------------------------- 音频

def _hostapi_rank(hostapi_name: str) -> int:
    """WASAPI 最稳（低延迟 + 完整设备名）；WDM-KS 不支持阻塞写，排最后。"""
    n = (hostapi_name or "").lower()
    if "wasapi" in n:
        return 0
    if "directsound" in n:
        return 1
    if "mme" in n:
        return 2
    if "wdm-ks" in n:
        return 8
    return 9


def list_audio_outputs() -> list[dict]:
    """所有可用播放端点（按后端优先级排序，同名去重）。"""
    if sd is None:
        return []
    try:
        hostapis = sd.query_hostapis()
        devs = sd.query_devices()
    except Exception:
        return []

    out = []
    for idx, d in enumerate(devs):
        if d.get("max_output_channels", 0) < 1:
            continue
        api_name = hostapis[d["hostapi"]]["name"] if d["hostapi"] < len(hostapis) else ""
        # WDM-KS 不支持 PortAudio 的阻塞写，且会把「端点未生效」的驱动也列出来，
        # 容易造成「看着有设备但写不进」的假象，直接排除。
        if "wdm-ks" in (api_name or "").lower():
            continue
        name = d["name"].strip()
        out.append({
            "index": idx,
            "name": name,
            "hostapi": api_name,
            "rank": _hostapi_rank(api_name),
            "channels": d["max_output_channels"],
            "samplerate": int(d.get("default_samplerate") or 48000),
            "is_virtual": is_virtual_mic_name(name),
        })

    out.sort(key=lambda x: (x["rank"], 0 if x["is_virtual"] else 1, x["name"]))

    # 同名去重（保留优先级最高的后端）
    seen, uniq = set(), []
    for d in out:
        key = d["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(d)
    return uniq


def is_virtual_mic_name(name: str) -> bool:
    low = (name or "").lower()
    return any(k in low for k in VIRTUAL_MIC_KEYWORDS)


def find_virtual_mic(devices: list[dict] | None = None):
    devices = devices if devices is not None else list_audio_outputs()
    for d in devices:
        if d["is_virtual"]:
            return d
    return None


def detect_inactive_virtual_endpoints() -> list[dict]:
    """驱动装了但端点没生效的诊断（注册表 DeviceState != 1）。

    典型场景：虚拟声卡驱动刚装完还没重启，或端点被系统标记为不可用——
    此时设备管理器里一切正常，但任何应用都看不到这台设备。
    """
    import re
    import winreg

    hits: list[dict] = []
    for kind in ("Render", "Capture"):
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\\" + kind
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        except OSError:
            continue
        try:
            n = winreg.QueryInfoKey(root)[0]
        except OSError:
            continue
        for i in range(n):
            try:
                sub = winreg.EnumKey(root, i)
                with winreg.OpenKey(root, sub) as sk:
                    state = int(winreg.QueryValueEx(sk, "DeviceState")[0])
                with winreg.OpenKey(root, sub + r"\Properties") as pk:
                    name = str(winreg.QueryValueEx(
                        pk, "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")[0])
            except OSError:
                continue
            if state == 1:
                continue
            if re.search(r"vb-audio|cable|virtual audio|voicemeeter|虚拟", name, re.I):
                hits.append({"name": name, "kind": kind, "state": hex(state)})
    return hits


_mic_cache = {"data": None, "ts": 0.0}


def mic_status(force: bool = False) -> dict:
    import time

    now = time.time()
    if not force and _mic_cache["data"] and now - _mic_cache["ts"] < 1.5:
        return _mic_cache["data"]

    devices = list_audio_outputs()
    virt = find_virtual_mic(devices)
    if virt:
        data = {
            "ok": True,
            "device": virt,
            "message": f"已就绪：{virt['name']}（应用侧选对应的 CABLE Output 录音设备）",
            "devices": devices,
            "inactive": [],
        }
        _mic_cache.update(data=data, ts=now)
        return data

    inactive = detect_inactive_virtual_endpoints()
    if inactive:
        names = "、".join(dict.fromkeys(h["name"] for h in inactive))
        msg = (f"驱动已安装但端点未生效（{names}）。系统当前没有向应用暴露虚拟声卡，"
               f"重启电脑后即可正常使用")
    else:
        msg = "未检测到虚拟声卡。需先安装 VB-CABLE（免费），装完重启再点『重新扫描』"

    data = {
        "ok": False,
        "device": None,
        "message": msg,
        "devices": devices,
        "inactive": inactive,
    }
    _mic_cache.update(data=data, ts=now)
    return data
