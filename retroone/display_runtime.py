"""Server display adapter; profile preferences remain GPU-vendor-neutral.

Intel is the verified implementation. NVIDIA is intentionally an unavailable
adapter until device access, capture and NVENC are implemented and accepted.
"""
from __future__ import annotations

import os
import re
import subprocess
from urllib.request import urlopen

from .settings import DISPLAY_RESOLUTIONS, DisplaySettings


class IntelDisplayBackend:
    name = "intel"
    unsupported_reason = None

    def __init__(self, program):
        self._program = program

    def capabilities(self):
        """Read-only readiness: the server must own geometry before it is offered."""
        expected = {
            "SELKIES_MANUAL_WIDTH": "0", "SELKIES_MANUAL_HEIGHT": "0",
            "SELKIES_MANUAL_RESOLUTION": "false|locked", "SELKIES_ENABLE_RESIZE": "false|locked",
        }
        if any(os.getenv(key) != value for key, value in expected.items()):
            return {"available": False, "reason": "画面设置需要更新云端运行环境，当前游戏不受影响"}
        if not os.getenv("DISPLAY") or not self._program("selkies-resize") or not self._program("xrandr"):
            return {"available": False, "reason": "云端显示服务尚未就绪"}
        try:
            # The API starts before Xvfb/Selkies. Applying RandR during that
            # startup window can be overwritten by the desktop initialization.
            subfolder = os.getenv("SUBFOLDER", "/stream/").strip("/")
            with urlopen("http://127.0.0.1:8082/" + subfolder + "/api/status", timeout=1) as response:
                if response.status != 200:
                    raise OSError("Selkies is not ready")
        except (OSError, ValueError):
            return {"available": False, "reason": "云端串流服务正在启动，请稍后重试"}
        return {"available": True, "reason": None}

    def apply(self, snapshot):
        # This is a trusted API snapshot, but validate again before invoking the
        # official RandR helper. A saved preference alone is never an active size.
        settings = DisplaySettings.validate(snapshot)
        capability = self.capabilities()
        if not capability["available"]:
            raise RuntimeError(capability["reason"])
        dimensions = DISPLAY_RESOLUTIONS[settings["stream_resolution"]]
        width, height = dimensions["width"], dimensions["height"]
        try:
            resized = subprocess.run([self._program("selkies-resize"), f"{width}x{height}"],
                                     capture_output=True, text=True, timeout=20, check=True)
            # Upstream may return exit 0 with stdout 'None' when RandR failed.
            if not re.search(rf"^\({width},\s*{height}\)$", resized.stdout, re.MULTILINE):
                raise RuntimeError("云端显示服务未确认所选分辨率，游戏尚未启动")
            current = subprocess.run([self._program("xrandr"), "--current"],
                                     capture_output=True, text=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("云端分辨率设置失败，游戏尚未启动，请重试") from error
        actual = re.search(r"^Screen \d+:.*?current (\d+) x (\d+),", current.stdout, re.MULTILINE)
        if not actual or tuple(map(int, actual.groups())) != (width, height):
            raise RuntimeError("云端实际画面尺寸与所选分辨率不一致，游戏尚未启动")
        return {"settings": settings, "width": width, "height": height}


class UnavailableDisplayBackend:
    def __init__(self, name, reason):
        self.name = name
        self.unsupported_reason = reason

    def capabilities(self):
        return {"available": False, "reason": self.unsupported_reason}

    def apply(self, snapshot):
        raise RuntimeError(self.unsupported_reason)


def display_backend(program, name=None):
    selected = os.getenv("RETROONE_GPU_BACKEND", "intel") if name is None else name
    if selected == "intel":
        return IntelDisplayBackend(program)
    if selected == "nvidia":
        return UnavailableDisplayBackend("nvidia", "NVIDIA 运行适配尚未实现；需要配置显卡透传、驱动及 NVENC 并完成实机验证")
    return UnavailableDisplayBackend(selected, "未识别的 GPU 后端配置，请使用 intel；NVIDIA 适配尚未实现")
