"""doctor 自检 D01–D15（方案 5.1.10）。

每项给出“通过 / 警告 / 失败 / 跳过 + 现象 + 修复建议”；--fix 只执行标注可自动修复的项
（D01 / D02 / D04 / D07 / D11 / D13 / D15），修完重跑该项以修复后的结果为准。
平台相关的探测一律走 Platform 的基础方法，本模块不直接执行系统命令。
"""

from __future__ import annotations

import logging
import os
import socket
import stat
import time
from typing import Callable, Dict, List, Optional

from . import actions, core_dl, render as render_mod
from .actions import CliError, Ctx
from .api import ApiError, ApiUnavailable
from .i18n import t
from .platform import Check, NotSupported, Platform, PlatformError, ServiceStatus
from .state import StateError, load_state

log = logging.getLogger("mihomo_cli")

GEODATA_MAX_AGE_SEC = 30 * 86400
PREFLIGHT_TIMEOUT_MARK = "[tun] preflight lock timeout"
# 须与 actions.tun_dns_apply 各行输出的前缀一致；那边改了这里不改，D13 会静默认不出开机失败行。
DNS_APPLY_PREFIX = "[tun] dns-apply"


def _c(code: str, status: str, detail: str = "", hint: str = "", fixable: bool = False, **extra: object) -> Check:
    return Check(code=code, title=t("doc." + code), status=status, detail=detail, hint=hint,
                 fixable=fixable, extra=dict(extra))


class Doctor:
    """一次 doctor 运行的上下文：缓存服务状态、state 与管理接口，避免每项重复查询。"""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.paths = ctx.paths
        self.platform = ctx.platform
        self.st: ServiceStatus = actions.service_status(ctx)
        try:
            self.state: Optional[Dict] = load_state(self.paths)
            self.state_error = ""
        except StateError as exc:
            self.state = None
            self.state_error = str(exc)

    # ---- 共用 ----
    def api_ok(self) -> bool:
        if not self.st.running or not os.path.exists(self.paths.sock):
            return False
        try:
            with self.ctx.api() as api:
                api.version()
            return True
        except (ApiError, ApiUnavailable):
            return False

    def refresh(self) -> None:
        self.st = actions.service_status(self.ctx)
        try:
            self.state = load_state(self.paths)
        except StateError:
            pass

    # ---- D01 内核 ----
    def d01(self) -> Check:
        core = self.paths.core
        if not os.path.exists(core):
            return _c("D01", "fail", t("doc.d01_missing", path=core), "mihomo-cli install", fixable=True)
        if not os.access(core, os.X_OK):
            return _c("D01", "fail", t("doc.d01_not_exec", path=core), "chmod +x %s" % core, fixable=True)
        version, _ = core_dl.core_version(core)
        if version is None:
            return _c("D01", "fail", t("doc.d01_bad"), "mihomo-cli core upgrade", fixable=True)
        if core_dl.version_tuple(version) < core_dl.MIN_CORE_VERSION:
            return _c("D01", "fail", t("doc.d01_old", version=version), "mihomo-cli core upgrade", fixable=True)
        return _c("D01", "ok", version)

    def fix_d01(self) -> None:
        version, _ = core_dl.core_version(self.paths.core)
        # 读得到且不低于最低版本时重下同一版本，否则取最新版（方案 5.1.5「版本」）。
        if version is None or core_dl.version_tuple(version) < core_dl.MIN_CORE_VERSION:
            version = None
        actions.core_upgrade(self.ctx, version, None)

    # ---- D02 服务 ----
    def d02(self) -> Check:
        st = self.st
        if not st.installed:
            return _c("D02", "fail", t("doc.d02_not_installed"), "mihomo-cli install")
        problems: List[str] = []
        status = "ok"
        fixable = False
        if st.unit_file_state != "enabled":
            problems.append(t("doc.d02_disabled"))
            status, fixable = "fail", True
        if not st.running:
            problems.append(t("doc.d02_not_running", state=st.active_state))
            status, fixable = "fail", True
        if st.n_restarts > 0:
            problems.append(t("doc.d02_restarts", n=st.n_restarts))
            status = "fail" if st.active_state == "failed" else ("warn" if status == "ok" else status)
        installed = self.platform.installed_unit_text()
        if installed is not None:
            try:
                expected = self.platform.unit_text(self.paths.home, _unit_user(installed), self.ctx.python_exe)
            except NotSupported:
                expected = installed
            if installed != expected:
                problems.append(t("doc.d02_unit_stale"))
                if status == "ok":
                    status = "warn"
            exe = _unit_python(installed)
            if exe and not os.access(exe, os.X_OK):
                problems.append(t("doc.d02_python_missing", path=exe))
                status = "fail"
                return _c("D02", status, "; ".join(problems),
                          " ; ".join(["mihomo-cli install"] + actions.manual_dns_cleanup_hints()), fixable=False)
        hint = "mihomo-cli install" if t("doc.d02_unit_stale") in problems else ""
        if fixable:
            hint = "mihomo-cli service start"
        return _c("D02", status, "; ".join(problems) or st.active_state, hint, fixable=fixable)

    def fix_d02(self) -> None:
        if self.st.unit_file_state != "enabled":
            self.platform.install_service(self.paths.home, _current_user(), self.ctx.python_exe)
        elif not self.st.running:
            self.platform.service_action("start")

    # ---- D03 管理接口 ----
    def d03(self) -> Check:
        if not self.st.running:
            return _c("D03", "skip", t("doc.need_running"))
        try:
            with self.ctx.api() as api:
                version = api.version().get("version", "")
            return _c("D03", "ok", str(version))
        except (ApiError, ApiUnavailable) as exc:
            return _c("D03", "fail", str(exc), "mihomo-cli service restart")

    # ---- D04 配置与 state 一致 ----
    def d04(self) -> Check:
        if self.state is None:
            return _c("D04", "fail", t("err.state_invalid") + " " + self.state_error)
        try:
            with open(self.paths.config, encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            return _c("D04", "fail", t("doc.d04_missing"), "mihomo-cli doctor --fix", fixable=True)
        expected = render_mod.render_text(self.state, self.paths)
        if current != expected:
            return _c("D04", "fail", t("doc.d04_diff"), "mihomo-cli doctor --fix", fixable=True)
        return _c("D04", "ok")

    def fix_d04(self) -> None:
        with actions.locked(self.ctx):
            state = actions.read_state(self.ctx)
            if actions.kernel_running(self.ctx):
                actions.reload(self.ctx, state, state, "doctor-fix-d04")
            else:
                actions.write_config(self.ctx, state)

    # ---- D05 mihomo -t ----
    def d05(self) -> Check:
        if not os.path.exists(self.paths.core) or not os.path.exists(self.paths.config):
            return _c("D05", "skip", t("doc.d05_skip"))
        try:
            actions.validate_config(self.ctx, self.paths.config)
        except CliError as exc:
            return _c("D05", "fail", exc.detail or exc.message)
        return _c("D05", "ok")

    # ---- D06 混合端口 ----
    def d06(self) -> Check:
        if self.state is None:
            return _c("D06", "skip", t("err.state_invalid"))
        port = int(self.state["mixed_port"])
        conflicts = actions.port_conflicts(self.ctx, port)
        if conflicts:
            return _c("D06", "fail", t("err.port_in_use", port=port) + " " + "; ".join(conflicts),
                      "mihomo-cli install --port <N>")
        if not self.st.running:
            return _c("D06", "skip", t("doc.need_running"))
        if not actions._tcp_ok(port):
            return _c("D06", "fail", t("doc.d06_not_listening", port=port), "mihomo-cli service restart")
        return _c("D06", "ok", "127.0.0.1:%d" % port)

    # ---- D07 地区数据库 ----
    def d07(self) -> Check:
        now = time.time()
        problems = []
        missing = False
        for name in core_dl.GEODATA_FILES:
            path = core_dl.geodata_path(self.paths.home, name)
            if path is None:
                missing = True
                problems.append(t("doc.d07_missing", name=name))
            elif now - os.path.getmtime(path) > GEODATA_MAX_AGE_SEC:
                problems.append(t("doc.d07_old", name=name, days=int((now - os.path.getmtime(path)) // 86400)))
        if problems:
            return _c("D07", "fail" if missing else "warn",
                      "; ".join(problems), "mihomo-cli doctor --fix", fixable=True)
        return _c("D07", "ok")

    def fix_d07(self) -> None:
        if self.api_ok():
            with self.ctx.api() as api:
                api.update_geo()
            return
        state = self.state or {}
        core_dl.install_geodata(self.paths.home, render_mod.site_base(state) if state else render_mod.GITHUB)

    # ---- D08 可用节点 ----
    def d08(self) -> Check:
        if self.state is None or not self.state.get("active_profile"):
            return _c("D08", "fail", t("err.no_active_profile"), "mihomo-cli profile add")
        if not self.api_ok():
            return _c("D08", "skip", t("doc.need_running"))
        with self.ctx.api() as api:
            group = api.get_proxy("PROXY")
        members = [m for m in group.get("all", []) if m != "REJECT"]
        if not members:
            return _c("D08", "fail", t("warn.no_nodes_blocked"), "mihomo-cli profile update")
        if group.get("now") == "REJECT":
            return _c("D08", "fail", t("doc.d08_reject"), "mihomo-cli node use <name>")
        return _c("D08", "ok", t("doc.d08_count", count=len(members), now=group.get("now")))

    # ---- D09 当前节点延迟 ----
    def d09(self) -> Check:
        if not self.api_ok():
            return _c("D09", "skip", t("doc.need_running"))
        with self.ctx.api() as api:
            now = api.get_proxy("PROXY").get("now")
            if not now or now == "REJECT":
                return _c("D09", "skip", t("doc.d08_reject"))
            try:
                delay = api.proxy_delay(now)
            except ApiError as exc:
                return _c("D09", "fail", "%s: %s" % (now, exc.message or exc.status), "mihomo-cli node auto")
        if delay <= 0:
            return _c("D09", "fail", "%s: %s" % (now, t("st.timeout")), "mihomo-cli node auto")
        return _c("D09", "ok", "%s %d ms" % (now, delay))

    # ---- D10 出口 IP ----
    def d10(self) -> Check:
        if not self.st.running or self.state is None:
            return _c("D10", "skip", t("doc.need_running"))
        info = actions.exit_ip(int(self.state["mixed_port"]))
        if not info:
            return _c("D10", "fail", t("doc.d10_fail"), "mihomo-cli node auto")
        return _c("D10", "ok", "%s %s" % (info.get("ip") or "", info.get("country") or ""))

    # ---- D11 订阅更新时间 ----
    def d11(self) -> Check:
        prof = render_mod.active_profile(self.state) if self.state else None
        if not prof or prof["kind"] != "subscription":
            return _c("D11", "skip", t("doc.d11_not_sub"))
        if not self.api_ok():
            return _c("D11", "skip", t("doc.need_running"))
        with self.ctx.api() as api:
            prov = api.get_provider("main")
        if not prov:
            return _c("D11", "fail", t("err.main_empty"), "mihomo-cli doctor --fix", fixable=True)
        updated = prov.get("updatedAt") or ""
        count = len(prov.get("proxies") or [])
        status = "ok" if count > 0 else "fail"
        return _c("D11", status, t("doc.d11_updated", at=updated[:19].replace("T", " "), count=count),
                  "mihomo-cli doctor --fix" if status != "ok" else "", fixable=True, updated_at=updated)

    def fix_d11(self) -> None:
        actions.profile_update(self.ctx, None)

    # ---- D12 其它实例 ----
    def d12(self) -> Check:
        try:
            others = self.platform.other_tun_instances()
        except (PlatformError, NotSupported) as exc:
            return _c("D12", "skip", str(exc))
        if others:
            return _c("D12", "warn", "; ".join(others))
        return _c("D12", "ok")

    # ---- D13 TUN 与 DNS 接管 ----
    def d13(self) -> Check:
        if self.state is None:
            return _c("D13", "skip", t("err.state_invalid"))
        journal = self.platform.service_journal(300)
        timeout_lines = [ln for ln in journal.splitlines() if PREFLIGHT_TIMEOUT_MARK in ln]
        if timeout_lines:
            return _c("D13", "fail", timeout_lines[-1].strip()[:300], "mihomo-cli service restart")
        suspended = self.state.get("tun_suspended")
        if self.state["tun"] and suspended:
            return _c("D13", "warn", t("warn.tun_suspended", reason=suspended), "mihomo-cli service restart")
        try:
            dns = self.platform.dns_status()
        except (PlatformError, NotSupported) as exc:
            return _c("D13", "skip", str(exc))
        effective = render_mod.tun_effective_in_config(self.state) and self.st.running
        if not effective:
            if dns.get("dropin"):
                # TUN 没生效但全局 DNS 仍指向 127.0.0.1：整机解析会断，必须清掉。
                return _c("D13", "fail", t("doc.d13_stale_dropin"), "mihomo-cli doctor --fix", fixable=True)
            return _c("D13", "ok", t("st.off"))
        problems = []
        if not self.platform.tun_device_exists(render_mod.TUN_DEVICE):
            problems.append(t("doc.d13_no_device", dev=render_mod.TUN_DEVICE))
        if not _tcp53():
            problems.append(t("doc.d13_no_53"))
        if not dns.get("dropin"):
            problems.append(t("doc.d13_no_route") if _dns_apply_no_route(self.platform)
                            else t("doc.d13_no_dropin"))
        if "127.0.0.1" not in (dns.get("iface_dns") or []):
            problems.append(t("doc.d13_iface_dns", iface=dns.get("default_iface") or "?",
                              dns=" ".join(dns.get("iface_dns") or []) or "-"))
        if not _resolve_ok():
            problems.append(t("doc.d13_resolve"))
        if problems:
            return _c("D13", "fail", "; ".join(problems), "mihomo-cli doctor --fix", fixable=True)
        return _c("D13", "ok", t("st.on"))

    def fix_d13(self) -> None:
        if not self.platform.sudo_validate():
            raise actions.sudo_error()
        # _tun-dns-apply 自己判断：TUN 生效则接管，否则还原。
        if not actions._dns_apply(self.ctx):
            # 失败原因由 _tun-dns-apply 以 [tun] 开头直接打到终端；err.dns_apply_failed 说的是“已回滚 TUN”，
            # doctor 并没有回滚，不能沿用。
            raise CliError(actions.EXIT_ERROR, t("err.doctor_dns_apply_failed"))

    # ---- D14 终端代理环境变量 ----
    def d14(self) -> Check:
        if self.state is None:
            return _c("D14", "skip", t("err.state_invalid"))
        port = int(self.state["mixed_port"])
        found = {v: os.environ[v] for v in actions.PROXY_ENV_VARS if os.environ.get(v)}
        if not found:
            return _c("D14", "ok", t("doc.d14_none"))
        wrong = [k for k, v in found.items() if not v.rstrip("/").endswith(":%d" % port)]
        if wrong:
            return _c("D14", "warn", t("doc.d14_wrong", vars=", ".join(wrong), port=port),
                      'eval "$(mihomo-cli proxy-env on)"')
        return _c("D14", "ok", t("doc.d14_ok", port=port))

    # ---- D15 主目录权限 ----
    def d15(self) -> Check:
        try:
            mode = stat.S_IMODE(os.stat(self.paths.home).st_mode)
        except OSError:
            return _c("D15", "fail", t("doc.d15_missing", home=self.paths.home), "mihomo-cli install")
        if mode != 0o700:
            return _c("D15", "fail", "%s %o" % (self.paths.home, mode), "chmod 700 %s" % self.paths.home, fixable=True)
        return _c("D15", "ok", "0700")

    def fix_d15(self) -> None:
        os.chmod(self.paths.home, 0o700)


CHECK_ORDER = ["D15", "D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10", "D11", "D12",
               "D13", "D14"]


def run(ctx: Ctx, fix: bool = False, on_check: Optional[Callable[[Check], None]] = None) -> List[Check]:
    doc = Doctor(ctx)
    results: List[Check] = []
    for code in CHECK_ORDER:
        check = _safe(doc, code)
        fixer = getattr(doc, "fix_" + code.lower(), None)
        if fix and check.fixable and check.status in ("fail", "warn") and fixer is not None:
            try:
                fixer()
                doc.refresh()
                again = _safe(doc, code)
                again.fixed = again.status == "ok"
                again.extra.setdefault("before", check.detail)
                check = again
            except (CliError, PlatformError, NotSupported, core_dl.DownloadError, OSError,
                    ApiError, ApiUnavailable) as exc:
                check.fixed = False
                check.extra["fix_error"] = getattr(exc, "detail", "") or getattr(exc, "message", "") or str(exc)
            log.info("[doctor] fix code=%s result=%s", code, "ok" if check.fixed else "fail")
        results.append(check)
        if on_check:
            on_check(check)
    log.info("[doctor] run fix=%s fail=%d warn=%d", fix, sum(c.status == "fail" for c in results),
             sum(c.status == "warn" for c in results))
    return results


def _safe(doc: Doctor, code: str) -> Check:
    try:
        return getattr(doc, code.lower())()
    except NotSupported as exc:
        return _c(code, "skip", str(exc) or t("err.not_supported"))
    except CliError as exc:
        return _c(code, "fail", exc.message + (" " + exc.detail if exc.detail else ""))
    except (ApiError, ApiUnavailable, PlatformError, OSError) as exc:
        return _c(code, "fail", str(exc)[:300])


def d13_ok(ctx: Ctx) -> bool:
    """tun on 遇到“已开启”时顺带做的 D13 检查。"""
    return Doctor(ctx).d13().status == "ok"


def _tcp53() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 53), timeout=2):
            return True
    except OSError:
        return False


def _resolve_ok() -> bool:
    try:
        socket.getaddrinfo("www.gstatic.com", 443)
        return True
    except OSError:
        return False


def _dns_apply_no_route(platform: Platform) -> bool:
    """本次开机里最后一次 dns-apply 是否因等不到默认路由而失败（新旧两版失败行都含 DNS_NO_ROUTE_MARK）。

    只看以 root 运行的那部分服务日志：内核日志同在这个单元里，按行数取尾部会把 dns-apply 那行挤出去。
    只在系统 DNS 未接管时调用，否则接管已恢复后仍会引用旧的失败行。读不到日志时返回 False，退回原提示。
    """
    lines = [ln for ln in platform.service_root_journal().splitlines() if DNS_APPLY_PREFIX in ln]
    return bool(lines) and actions.DNS_NO_ROUTE_MARK in lines[-1]


def _unit_user(unit: str) -> str:
    for line in unit.splitlines():
        if line.startswith("User="):
            return line.split("=", 1)[1].strip()
    return _current_user()


def _unit_python(unit: str) -> Optional[str]:
    for line in unit.splitlines():
        if line.startswith("ExecStartPre=") or line.startswith("ExecStart="):
            for token in line.split("=", 1)[1].lstrip("+-").split():
                if token.startswith("/") and "python" in os.path.basename(token):
                    return token
    return None


def _current_user() -> str:
    import getpass

    return getpass.getuser()
