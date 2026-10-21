"""macOS 占位：首版不实现，每个方法都抛 NotSupported，只为固定 Platform 接口（方案 5.1.7）。"""

from __future__ import annotations

from .base import NotSupported, Platform


def _unsupported(*_args, **_kwargs):
    raise NotSupported("macOS is not supported yet")


class DarwinPlatform(Platform):
    name = "darwin"

    core_asset_name = _unsupported
    unit_text = _unsupported
    install_service = _unsupported
    uninstall_service = _unsupported
    service_action = _unsupported
    service_status = _unsupported
    service_logs = _unsupported
    sudo_validate = _unsupported
    run_self_as_root = _unsupported
    other_tun_instances = _unsupported
    listening_conflicts = _unsupported
    tun_device_exists = _unsupported
    tun_supported = _unsupported
    dns_takeover = _unsupported
    dns_revert = _unsupported
    dns_status = _unsupported
