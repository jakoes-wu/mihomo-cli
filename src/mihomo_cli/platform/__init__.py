"""按 sys.platform 选择 Platform 实现；Windows 与其它系统抛 NotSupported（退出码 6）。"""

from __future__ import annotations

import sys

from .base import Check, NotSupported, Platform, PlatformError, ServiceStatus

__all__ = ["Check", "NotSupported", "Platform", "PlatformError", "ServiceStatus", "get_platform"]


def get_platform() -> Platform:
    if sys.platform.startswith("linux"):
        from .linux import LinuxPlatform

        return LinuxPlatform()
    if sys.platform == "darwin":
        from .darwin import DarwinPlatform

        return DarwinPlatform()
    raise NotSupported("platform %s is not supported" % sys.platform)
