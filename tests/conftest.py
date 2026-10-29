"""L0 测试夹具：假内核（模拟管理接口与 mihomo -t / -v）+ 假系统层。

假内核按 render 生成的 config.yaml 计算 provider 与 PROXY 组成员，规则与真内核一致的部分：
手工节点加 "[M] " 前缀、exclude-filter 按正则排除、PROXY 末尾固定 REJECT。
真内核行为（解析各种链接、cache.db 恢复选择等）由 L1 在编译机用真 mihomo 验证。
"""

from __future__ import annotations

import base64
import os
import re
import stat
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

import pytest
import yaml

from mihomo_cli import actions
from mihomo_cli.api import ApiError
from mihomo_cli.platform.base import Platform, ServiceStatus
from mihomo_cli.state import Paths, ensure_home

FAKE_CORE = """#!/bin/sh
if [ "$1" = "-v" ]; then
  echo "Mihomo Meta v1.19.21 linux amd64 with go1.24 with_gvisor"
  exit 0
fi
if [ "$1" = "-t" ]; then
  for a in "$@"; do f="$a"; done
  if grep -q "no-such-type" "$f"; then echo "unsupported type: no-such-type"; exit 1; fi
  echo "configuration file test is successful"
  exit 0
fi
exit 0
"""


class FakeKernel:
    """内存里的“内核”：记录调用序列，支持注入失败。"""

    def __init__(self, paths: Paths):
        self.paths = paths
        self.sub_nodes: Dict[str, List[str]] = {}  # 订阅地址 → 节点名
        self.providers: Dict[str, List[str]] = {}
        self.proxy_all: List[str] = ["REJECT"]
        self.now: Dict[str, str] = {"PROXY": "REJECT", "CN": "DIRECT", "GLOBAL": "PROXY"}
        self.mode = "rule"
        self.port = 7897
        self.calls: List[tuple] = []
        self.fail_reload = 0  # >0 时接下来若干次 reload 抛 ApiError
        self.delays: Dict[str, int] = {}

    # ---- 按配置计算 ----
    def load(self, config_path: str) -> None:
        with open(config_path, encoding="utf-8") as fh:
            text = fh.read()
        cfg = yaml.safe_load(text)
        self.mode = cfg["mode"]
        self.port = cfg["mixed-port"]
        providers: Dict[str, List[str]] = {}
        for name, prov in (cfg.get("proxy-providers") or {}).items():
            path = os.path.join(self.paths.home, prov["path"])
            if prov["type"] == "http":
                providers[name] = list(self.sub_nodes.get(prov["url"], []))
            elif name == "manual":
                providers[name] = self._manual_names(path, prov["override"]["additional-prefix"])
            else:
                with open(path, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                providers[name] = [p["name"] for p in data.get("proxies") or []]
        self.providers = providers
        group = cfg["proxy-groups"][0]
        flt = group.get("exclude-filter")
        members: List[str] = []
        for name in group.get("use", []):
            for node in providers.get(name, []):
                if flt and re.search(flt, node):
                    continue
                members.append(node)
        self.proxy_all = members + ["REJECT"]
        if self.now["PROXY"] not in self.proxy_all:
            self.now["PROXY"] = self.proxy_all[0]

    @staticmethod
    def _manual_names(path: str, prefix: str) -> List[str]:
        with open(path, encoding="ascii") as fh:
            links = base64.b64decode(fh.read().strip()).decode("utf-8").splitlines()
        names = []
        for link in links:
            if link.startswith("bad://"):
                continue  # 模拟内核解析不了的链接
            frag = urlsplit(link).fragment
            names.append(prefix + (unquote(frag) or urlsplit(link).hostname or "?"))
        return names


class FakeApi:
    def __init__(self, kernel: FakeKernel):
        self.k = kernel

    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeApi":
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def version(self) -> Dict[str, Any]:
        return {"version": "v1.19.21"}

    def get_configs(self) -> Dict[str, Any]:
        return {"mode": self.k.mode, "mixed-port": self.k.port}

    def get_proxy(self, name: str) -> Dict[str, Any]:
        if name == "PROXY":
            return {"name": "PROXY", "now": self.k.now["PROXY"], "all": list(self.k.proxy_all)}
        if name == "CN":
            return {"name": "CN", "now": self.k.now["CN"], "all": ["DIRECT", "PROXY"]}
        if name in self.k.proxy_all:
            return {"name": name}
        raise ApiError("GET", "/proxies/" + name, 404, "not found")

    def get_providers(self) -> Dict[str, Any]:
        return {n: {"name": n, "proxies": [{"name": x} for x in v]} for n, v in self.k.providers.items()}

    def get_provider(self, name: str) -> Optional[Dict[str, Any]]:
        if name not in self.k.providers:
            return None
        return {"name": name, "proxies": [{"name": x} for x in self.k.providers[name]],
                "updatedAt": "2026-10-20T10:00:00Z"}

    def provider_node_names(self, name: str) -> List[str]:
        return list(self.k.providers.get(name, []))

    def select(self, group: str, name: str) -> None:
        self.k.calls.append(("select", group, name))
        if group == "PROXY" and name not in self.k.proxy_all:
            raise ApiError("PUT", "/proxies/PROXY", 400, "not found")
        self.k.now[group] = name

    def patch_configs(self, patch: Dict[str, Any]) -> None:
        self.k.calls.append(("patch", tuple(sorted(patch.items()))))
        if "mode" in patch:
            self.k.mode = patch["mode"]

    def reload(self, config_path: str) -> None:
        self.k.calls.append(("reload",))
        if self.k.fail_reload > 0:
            self.k.fail_reload -= 1
            raise ApiError("PUT", "/configs", 400, "injected")
        self.k.load(config_path)

    def update_provider(self, name: str) -> None:
        self.k.calls.append(("update_provider", name))

    def restart(self) -> None:
        self.k.calls.append(("restart",))

    def proxy_delay(self, name: str, url: str = "", timeout_ms: int = 5000) -> int:
        return self.k.delays.get(name, 0)

    def group_delay(self, group: str, url: str = "", timeout_ms: int = 5000) -> Dict[str, int]:
        # 真内核的组测速结果里会带上 REJECT（值为 0）。
        result = {n: self.k.delays.get(n, 0) for n in self.k.proxy_all}
        return result


class FakePlatform(Platform):
    name = "fake"

    def __init__(self) -> None:
        self.status = ServiceStatus(load_state="loaded", active_state="active", main_pid=4242,
                                    unit_file_state="enabled")
        self.conflicts: List[str] = []
        self.ports: Dict[int, List[str]] = {}
        self.hidden_ports: set = set()
        self.sudo_ok = True
        self.root_calls: List[str] = []
        self.root_rc: Dict[str, int] = {}
        self.actions: List[str] = []
        # dns-apply 相关：default_iface 先返回 None 的次数、回读结果、root 日志、dns_relink 的调用与异常
        self.iface_none_times = 0
        self.iface_calls = 0
        self.takeover_calls = 0
        self.dns_state = {"dropin": False, "marker_iface": None, "default_iface": "eth0", "iface_dns": ["127.0.0.1"]}
        self.root_journal = ""
        self.relink_calls: List[str] = []
        self.relink_exc: Optional[Exception] = None

    def core_asset_name(self, version: str) -> str:
        return "mihomo-linux-amd64-compatible-%s.gz" % version

    def unit_text(self, home: str, user: str, python_exe: str) -> str:
        return "[Unit]\n"

    def install_service(self, home: str, user: str, python_exe: str) -> None:
        self.actions.append("install")

    def uninstall_service(self) -> None:
        self.actions.append("uninstall")

    def service_action(self, action: str) -> None:
        self.actions.append(action)

    def service_status(self) -> ServiceStatus:
        return self.status

    def service_logs(self, follow: bool, lines: int = 200) -> int:
        return 0

    def sudo_validate(self) -> bool:
        # 记进 actions，供测试断言先预检 sudo、再动服务的顺序
        self.actions.append("sudo_validate")
        return self.sudo_ok

    def run_self_as_root(self, home: str, python_exe: str, subcommand: str, preserve_env: List[str]) -> int:
        self.root_calls.append(subcommand)
        return self.root_rc.get(subcommand, 0)

    def other_tun_instances(self) -> List[str]:
        return list(self.conflicts)

    def listening_conflicts(self, port: int, host: str = "127.0.0.1", own_listener: bool = False) -> List[str]:
        # hidden_ports：模拟“有人在听、属主不可见”，只有 own_listener 时才排除
        if own_listener and port in self.hidden_ports:
            return []
        return list(self.ports.get(port, []))

    def tun_device_exists(self, name: str) -> bool:
        return True

    def tun_supported(self) -> Optional[str]:
        return None

    def default_iface(self, timeout: Optional[float] = None) -> Optional[str]:
        self.iface_calls += 1
        if self.iface_calls <= self.iface_none_times:
            return None
        return "eth0"

    def service_root_journal(self) -> str:
        return self.root_journal

    def dns_relink(self, iface: str) -> None:
        self.relink_calls.append(iface)
        if self.relink_exc is not None:
            raise self.relink_exc

    def dns_takeover(self) -> str:
        self.takeover_calls += 1
        return "eth0"

    def dns_revert(self) -> bool:
        return True

    def dns_status(self) -> dict:
        return dict(self.dns_state)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """返回 (ctx, kernel, platform)：主目录已建好、假内核已放入 bin/mihomo、服务处于运行状态。"""
    for var in ("MIHOMO_CLI_TEST_BAD_CONFIG", "MIHOMO_CLI_TEST_FAIL_VERIFY", "MIHOMO_CLI_TEST_FAIL_ROLLBACK",
                "MIHOMO_CLI_TEST_FAIL_DNS_REVERT"):
        monkeypatch.delenv(var, raising=False)
    paths = Paths(str(tmp_path / "home"))
    ensure_home(paths)
    with open(paths.core, "w") as fh:
        fh.write(FAKE_CORE)
    os.chmod(paths.core, os.stat(paths.core).st_mode | stat.S_IXUSR)
    open(paths.sock, "w").close()
    platform = FakePlatform()
    kernel = FakeKernel(paths)
    ctx = actions.Ctx(paths, platform)
    monkeypatch.setattr(actions.Ctx, "api", lambda self: FakeApi(kernel))
    monkeypatch.setattr(actions, "_tcp_ok", lambda port, timeout=2.0: True)
    return ctx, kernel, platform
