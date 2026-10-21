"""命令与菜单共用的全部业务动作——唯一业务入口（方案 5.1.1）。

职责边界：
- 这里决定“改什么、按什么顺序改、失败怎么回滚”；state.py 只管安全读写，render.py 只管生成配置，
  api.py 只管调接口，platform/ 只管系统命令。
- cli.py / menu.py 只收集参数、展示结果，不得绕过本模块直接改 state 或调接口。

并发约定（方案 5.1.2）：会改 state 的动作全程持 state.lock（等 130 秒）；调用
systemctl start/restart/stop/enable 之前必须先释放锁，否则 ExecStartPre 拿不到锁。
"""

from __future__ import annotations

import copy
import getpass
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import httpx

from . import core_dl, nodes as nodes_mod, render as render_mod
from .api import Api, ApiError, ApiUnavailable
from .i18n import t
from .platform import NotSupported, Platform, PlatformError, ServiceStatus
from .state import (
    CLI_LOCK_TIMEOUT_SEC,
    PROFILE_NAME_RE,
    SERVICE_LOCK_TIMEOUT_SEC,
    LockTimeout,
    Paths,
    StateError,
    StateLock,
    ensure_home,
    home_mode_ok,
    load_state,
    new_profile,
    save_state,
    write_atomic,
)

log = logging.getLogger("mihomo_cli")

# 退出码（方案 5.1.6）。
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_ROLLED_BACK = 4
EXIT_SUDO = 5
EXIT_PLATFORM = 6
EXIT_MANUAL = 7

BACKUP_KEEP = 10
# _service-reapply：等待就绪上限 60 秒，整体预算 90 秒，下发后等 2 秒回读，最多 3 次（方案 5.1.7）。
REAPPLY_READY_SEC = 60.0
REAPPLY_BUDGET_SEC = 90.0
REAPPLY_SETTLE_SEC = 2.0
REAPPLY_TRIES = 3
# _tun-dns-apply 等网卡与 53 端口就绪的上限（方案 5.1.7「启动耗时预算」）。
DNS_APPLY_WAIT_SEC = 10.0
EXIT_IP_URL = "https://ipinfo.io/json"
EXIT_IP_TIMEOUT_SEC = 5.0
TEST_ENV_DNS_REVERT = "MIHOMO_CLI_TEST_FAIL_DNS_REVERT"


class CliError(Exception):
    """带退出码的业务失败。message 是给用户看的一句话，detail 是底层原因原文（已截断）。"""

    def __init__(self, code: int, message: str, detail: str = "", hints: Optional[List[str]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.hints = hints or []


class Ctx:
    """一次 CLI 调用的上下文。messages 收集提示语，由 cli 决定立即打印还是放进 --json 输出。"""

    def __init__(self, paths: Paths, platform: Platform, python_exe: Optional[str] = None,
                 emit: Optional[Callable[[str, str], None]] = None):
        self.paths = paths
        self.platform = platform
        self.python_exe = python_exe or sys.executable
        self.messages: List[Tuple[str, str]] = []
        self._emit = emit

    def say(self, level: str, text: str) -> None:
        self.messages.append((level, text))
        if self._emit:
            self._emit(level, text)

    def api(self) -> Api:
        return Api(self.paths.sock)


# ====================================================================
# 公共小工具
# ====================================================================

def check_home(ctx: Ctx) -> None:
    """主目录权限过宽时拒绝运行：套接字是 0666，只靠目录 0700 隔离其它用户（方案 §7 安全）。"""
    ok = home_mode_ok(ctx.paths)
    if ok is False:
        raise CliError(EXIT_ERROR, t("err.home_mode", home=ctx.paths.home),
                       hints=["chmod 700 %s" % ctx.paths.home])


def service_status(ctx: Ctx) -> ServiceStatus:
    try:
        return ctx.platform.service_status()
    except NotSupported:
        raise
    except PlatformError:
        return ServiceStatus()


def kernel_running(ctx: Ctx, st: Optional[ServiceStatus] = None) -> bool:
    """内核是否运行（方案 5.1.5「内核是否运行的判据」）。

    正在启动 / 停止时抛退出码 3：ExecStartPre 期间 MainPID 为 0，不能据此判为未运行，
    否则会出现“以为没运行只改文件、内核随后按旧配置起来”的错乱。
    active 且 MainPID 非 0 才算运行，此时套接字连不上同样是退出码 3。
    """
    st = st or service_status(ctx)
    if st.transitioning:
        raise CliError(EXIT_UNREACHABLE, t("err.kernel_transitioning", state=st.active_state))
    if not st.running:
        return False
    if not os.path.exists(ctx.paths.sock):
        raise CliError(EXIT_UNREACHABLE, t("err.kernel_unreachable"), detail=ctx.paths.sock)
    return True


@contextmanager
def locked(ctx: Ctx, timeout_sec: float = CLI_LOCK_TIMEOUT_SEC) -> Iterator[StateLock]:
    lock = StateLock(ctx.paths, timeout_sec)
    try:
        lock.acquire()
    except LockTimeout:
        raise CliError(EXIT_ERROR, t("err.locked")) from None
    try:
        yield lock
    finally:
        lock.release()


def read_state(ctx: Ctx) -> Dict[str, Any]:
    try:
        return load_state(ctx.paths)
    except StateError as exc:
        raise CliError(EXIT_ERROR, t("err.state_invalid"), detail=str(exc)) from exc


def require_profile(state: Dict[str, Any], name: str) -> Dict[str, Any]:
    prof = state["profiles"].get(name)
    if prof is None:
        raise CliError(EXIT_ERROR, t("err.no_such_profile", name=name))
    return prof


def require_active(state: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    name = state.get("active_profile")
    if not name:
        raise CliError(EXIT_ERROR, t("err.no_active_profile"))
    return name, state["profiles"][name]


def _api_call(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except ApiUnavailable as exc:
        raise CliError(EXIT_UNREACHABLE, t("err.kernel_unreachable"), detail=str(exc)) from exc


def manual_names(prof: Dict[str, Any]) -> List[Optional[str]]:
    return [m.get("name") for m in prof["manual"]]


def mirror_normalize(mirror: Optional[str]) -> Optional[str]:
    if mirror is None:
        return None
    mirror = mirror.strip().rstrip("/")
    if not mirror:
        return None
    if not (mirror.startswith("http://") or mirror.startswith("https://")):
        raise CliError(EXIT_USAGE, t("err.bad_mirror", mirror=mirror))
    return mirror


def normalize_version_or_usage(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    try:
        return core_dl.normalize_version(raw)
    except ValueError:
        raise CliError(EXIT_USAGE, t("err.bad_version", version=raw)) from None


def _tcp_ok(port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def kernel_listens_on(ctx: Ctx, port: int) -> bool:
    """本实例内核是否自称在听 port（管理接口 /configs 的 mixed-port）。

    端口检查要靠它排除自己：内核带 AmbientCapabilities 运行，同一用户也读不到它的 fd，
    平台层只能看到“有人在听、属主不可见”。接口不通（服务没跑）时返回 False，按占用处理。
    """
    try:
        api = ctx.api()
    except (ApiError, ApiUnavailable, OSError):
        return False
    try:
        return int(api.get_configs().get("mixed-port") or 0) == int(port)
    except (ApiError, ApiUnavailable, ValueError, TypeError):
        return False
    finally:
        api.close()


def port_conflicts(ctx: Ctx, port: int) -> List[str]:
    """混合端口占用检查（install 与 doctor D06 共用），本实例内核自己在听不算占用。"""
    conflicts = ctx.platform.listening_conflicts(port)
    if conflicts and kernel_listens_on(ctx, port):
        conflicts = ctx.platform.listening_conflicts(port, own_listener=True)
    return conflicts


def validate_config(ctx: Ctx, config_path: str) -> None:
    """`mihomo -t` 校验。-f 必须是绝对路径（相对路径按当前工作目录解析而非 -d，main.go:129-133）。

    注意 -t 只解析配置、不读 provider 内容，坏节点 / 坏订阅要靠重载后的计数核验拦截。
    """
    core = ctx.paths.core
    if not os.path.exists(core):
        raise CliError(EXIT_ERROR, t("err.no_core"), hints=["mihomo-cli install"])
    try:
        proc = subprocess.run([core, "-t", "-d", ctx.paths.home, "-f", os.path.abspath(config_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliError(EXIT_ROLLED_BACK, t("err.config_invalid"), detail=str(exc)) from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").strip().splitlines()[-5:])
        raise CliError(EXIT_ROLLED_BACK, t("err.config_invalid"), detail=tail[:600])


def write_config(ctx: Ctx, state: Dict[str, Any]) -> None:
    """不重载、只原子重写 config.yaml（切模式、改镜像、preflight 暂停 TUN 等用），保证重启后与 state 一致。"""
    write_atomic(ctx.paths.config, render_mod.render_text(state, ctx.paths))


# ====================================================================
# 备份与重载事务（方案 5.1.5）
# ====================================================================

def _backup_name(paths: Paths) -> str:
    base = time.strftime("%Y%m%d-%H%M%S")
    name, n = base, 1
    while os.path.exists(os.path.join(paths.backups, name)):
        n += 1
        name = "%s-%d" % (base, n)
    return name


def make_backup(ctx: Ctx, state: Dict[str, Any]) -> str:
    """备份 config.yaml、state.yaml、nodes/ 与本地文件类 providers/<名>.yaml；保留最近 10 份。"""
    paths = ctx.paths
    os.makedirs(paths.backups, mode=0o700, exist_ok=True)
    name = _backup_name(paths)
    dest = os.path.join(paths.backups, name)
    os.makedirs(os.path.join(dest, "nodes"))
    os.makedirs(os.path.join(dest, "providers"))
    for src in (paths.config, paths.state):
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, os.path.basename(src)))
    if os.path.isdir(paths.nodes):
        for fname in os.listdir(paths.nodes):
            if fname.endswith(".txt"):
                shutil.copy2(os.path.join(paths.nodes, fname), os.path.join(dest, "nodes", fname))
    for pname, prof in state["profiles"].items():
        src = paths.provider_file(pname)
        if prof["kind"] == "file" and os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, "providers", pname + ".yaml"))
    entries = sorted(d for d in os.listdir(paths.backups) if os.path.isdir(os.path.join(paths.backups, d)))
    for old in entries[:-BACKUP_KEEP]:
        shutil.rmtree(os.path.join(paths.backups, old), ignore_errors=True)
    return name


def _backup_path_for(ctx: Ctx, backup: str, final_path: str) -> Optional[str]:
    rel = os.path.relpath(final_path, ctx.paths.home)
    candidate = os.path.join(ctx.paths.backups, backup, rel)
    return candidate if os.path.exists(candidate) else None


def _clear_staging(ctx: Ctx) -> None:
    shutil.rmtree(ctx.paths.staging, ignore_errors=True)


def _stage(ctx: Ctx, new_state: Dict[str, Any], files: Dict[str, Optional[bytes]]) -> str:
    """第 1 步：所有新文件先写到 staging/（不在 provider 监视路径上）；返回 staging 里的 config 路径。"""
    _clear_staging(ctx)
    os.makedirs(ctx.paths.staging, mode=0o700)
    write_atomic(os.path.join(ctx.paths.staging, "state.yaml.new"), _dump(new_state))
    staged_config = os.path.join(ctx.paths.staging, "config.yaml.new")
    write_atomic(staged_config, render_mod.render_text(new_state, ctx.paths))
    for final_path, content in files.items():
        if content is None:
            continue
        rel = os.path.relpath(final_path, ctx.paths.home)
        staged = os.path.join(ctx.paths.staging, rel + ".new")
        os.makedirs(os.path.dirname(staged), exist_ok=True)
        with open(staged, "wb") as fh:
            fh.write(content)
    return staged_config


def _dump(state: Dict[str, Any]) -> str:
    from .state import dump_state

    return dump_state(state)


def _install_files(ctx: Ctx, files: Dict[str, Optional[bytes]]) -> None:
    for final_path, content in files.items():
        if content is None:
            try:
                os.unlink(final_path)
            except FileNotFoundError:
                pass
            continue
        rel = os.path.relpath(final_path, ctx.paths.home)
        staged = os.path.join(ctx.paths.staging, rel + ".new")
        os.makedirs(os.path.dirname(final_path), mode=0o700, exist_ok=True)
        os.replace(staged, final_path)


def manual_file_content(prof: Optional[Dict[str, Any]]) -> Optional[bytes]:
    """手工节点文件内容；没有手工节点返回 None（删除文件、render 也不写 manual provider）。"""
    if not prof or not prof["manual"]:
        return None
    return nodes_mod.encode_manual_file(m["link"] for m in prof["manual"]).encode("ascii")


def apply_offline(ctx: Ctx, new_state: Dict[str, Any], files: Dict[str, Optional[bytes]]) -> str:
    """内核未运行：校验后直接落盘，不重载（方案 5.1.5）。新手工条目的 name 保持 None，由下次重载回读。"""
    staged_config = _stage(ctx, new_state, files)
    try:
        validate_config(ctx, staged_config)
        backup = make_backup(ctx, read_state(ctx))
        _install_files(ctx, files)
        write_config(ctx, new_state)
        save_state(ctx.paths, new_state)
    finally:
        _clear_staging(ctx)
    log.info("[reload] offline backup=%s result=ok", backup)
    return backup


def ensure_selection(ctx: Ctx, api: Api, state: Dict[str, Any]) -> bool:
    """把 state 的 PROXY / CN 选择下发到内核；记录的节点已不在 PROXY 成员中时补选第一个非 REJECT 成员。

    返回 state 是否被修改（调用方负责在持锁时写回）。PROXY 只剩 REJECT 时保持阻断并提示。
    选择恢复失败不算回滚条件（方案 5.1.5 第 5 步）。
    """
    changed = False
    group = api.get_proxy("PROXY")
    members = [m for m in group.get("all", []) if m != "REJECT"]
    prof = render_mod.active_profile(state)
    wanted = prof.get("selected") if prof else None
    if wanted in members:
        try:
            api.select("PROXY", wanted)
        except ApiError as exc:
            ctx.say("warn", t("warn.select_failed", name=wanted, detail=exc.message))
    elif members:
        pick = members[0]
        api.select("PROXY", pick)
        if prof is not None:
            prof["selected"] = pick
            changed = True
        ctx.say("info", t("info.auto_selected", name=pick))
    else:
        if prof is not None and prof.get("selected") is not None:
            prof["selected"] = None
            changed = True
        ctx.say("warn", t("warn.no_nodes_blocked"))
    try:
        api.select("CN", "DIRECT" if state["cn_route"] == "direct" else "PROXY")
    except ApiError as exc:
        ctx.say("warn", t("warn.select_failed", name="CN", detail=exc.message))
    return changed


def _provider_counts(api: Api) -> Dict[str, int]:
    try:
        providers = api.get_providers()
    except ApiError:
        return {}
    return {name: len(p.get("proxies") or []) for name, p in providers.items() if name in ("main", "manual")}


def _force_fail(var: str) -> bool:
    return os.environ.get(var) == "1"


def reload(ctx: Ctx, old_state: Dict[str, Any], new_state: Dict[str, Any], action: str,
           files: Optional[Dict[str, Optional[bytes]]] = None,
           involved: Tuple[str, ...] = (),
           hidden_added: Tuple[str, ...] = (), restored: Tuple[str, ...] = (),
           cleanup_after: Tuple[str, ...] = ()) -> str:
    """重载事务：校验 → 备份 → 替换文件 → PUT /configs → 后置核验 → 提交 / 回滚（方案 5.1.5 第 1–6 步）。

    调用方必须已持 state.lock。state.yaml 只在核验通过后替换，任何失败时保持旧值。
    files：正式路径 → 新内容（None 表示删除）；involved：本次涉及、要精确核验的 provider（main / manual）。
    cleanup_after：提交成功后删除的文件（如改名后的旧名文件）。返回备份名。
    """
    files = dict(files or {})
    started = time.monotonic()
    new_state = copy.deepcopy(new_state)
    new_prof = render_mod.active_profile(new_state)
    involved_set = set(involved)
    if new_prof is not None and new_prof["manual"]:
        # 当前代理服务有手工条目时 manual 一律精确计数：内核未运行时写入的条目从未核验过，
        # 计数不等时按位置回读会把名字安错（方案 5.1.5 第 5 步）。
        involved_set.add("manual")
    # 选中的若是手工条目，记下它在新列表里的位置，回读改名后按位置迁移。
    sel_index: Optional[int] = None
    if new_prof is not None and new_prof.get("selected"):
        names = manual_names(new_prof)
        if new_prof["selected"] in names:
            sel_index = names.index(new_prof["selected"])

    # 第 1、2 步：staging + mihomo -t。失败时什么都没动过。
    staged_config = _stage(ctx, new_state, files)
    try:
        validate_config(ctx, staged_config)
    except CliError:
        _clear_staging(ctx)
        log.info("[reload] action=%s result=invalid", action)
        raise

    api = ctx.api()
    try:
        # 第 3 步：备份 + 记录重载前的 provider 节点数与 PROXY 成员。
        backup = make_backup(ctx, old_state)
        pre_counts = _api_call(lambda: _provider_counts(api))
        pre_members: List[str] = []
        try:
            pre_members = api.get_proxy("PROXY").get("all", [])
        except (ApiError, ApiUnavailable):
            pass
        replaced = list(files.keys()) + [ctx.paths.config]
        try:
            # 第 4 步：provider 文件在这一步之前不得进入正式路径（内核对 file provider 有文件监视，会立即热加载）。
            _install_files(ctx, files)
            os.replace(staged_config, ctx.paths.config)
            api.reload(ctx.paths.config)
            # 第 5 步：后置核验。
            readback = _verify(ctx, api, new_state, pre_counts, pre_members, involved_set,
                               hidden_added, restored)
        except (ApiError, ApiUnavailable, CliError, OSError) as exc:
            _rollback(ctx, api, old_state, backup, replaced, exc)
            raise AssertionError("unreachable") from exc  # _rollback 总会抛出

        # 提交：回读手工节点名 → 迁移选择 → 替换 state.yaml → 补选。
        if new_prof is not None and readback is not None:
            for entry, name in zip(new_prof["manual"], readback):
                entry["name"] = name
            if sel_index is not None and sel_index < len(readback):
                new_prof["selected"] = readback[sel_index]
        save_state(ctx.paths, new_state)
        try:
            if ensure_selection(ctx, api, new_state):
                save_state(ctx.paths, new_state)
        except (ApiError, ApiUnavailable) as exc:
            ctx.say("warn", t("warn.select_failed", name="PROXY", detail=str(exc)))
        for path in cleanup_after:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        old_state.clear()
        old_state.update(new_state)
        log.info("[reload] action=%s profile=%s backup=%s elapsed_ms=%d result=ok", action,
                 new_state.get("active_profile"), backup, (time.monotonic() - started) * 1000)
        return backup
    finally:
        _clear_staging(ctx)
        api.close()


def _verify(ctx: Ctx, api: Api, new_state: Dict[str, Any], pre_counts: Dict[str, int],
            pre_members: List[str], involved: set, hidden_added: Tuple[str, ...],
            restored: Tuple[str, ...]) -> Optional[List[str]]:
    """第 5 步后置核验；通过时返回 manual 的节点名（按顺序），没有手工节点返回 None。"""
    if _force_fail("MIHOMO_CLI_TEST_FAIL_VERIFY"):
        raise CliError(EXIT_ROLLED_BACK, t("err.verify_failed"), detail="MIHOMO_CLI_TEST_FAIL_VERIFY=1")
    cfg = api.get_configs()
    if str(cfg.get("mode", "")).lower() != new_state["mode"]:
        raise CliError(EXIT_ROLLED_BACK, t("err.verify_failed"), detail="mode=%s" % cfg.get("mode"))
    if int(cfg.get("mixed-port", 0)) != int(new_state["mixed_port"]):
        raise CliError(EXIT_ROLLED_BACK, t("err.verify_failed"), detail="mixed-port=%s" % cfg.get("mixed-port"))
    group = api.get_proxy("PROXY")
    if not _tcp_ok(int(new_state["mixed_port"])):
        raise CliError(EXIT_ROLLED_BACK, t("err.verify_failed"), detail="mixed port not reachable")

    prof = render_mod.active_profile(new_state)
    written = render_mod.provider_names(new_state)
    readback: Optional[List[str]] = None
    main_names: Optional[List[str]] = None
    for name in written:
        names = api.provider_node_names(name)
        count = len(names)
        if name == "manual":
            expected = len(prof["manual"])
            if count != expected:
                pending = [nodes_mod.link_summary(m["link"]) for m in prof["manual"] if m.get("name") is None]
                raise CliError(EXIT_ROLLED_BACK, t("err.manual_unparsable"),
                               detail="manual nodes %d != entries %d; unverified: %s"
                               % (count, expected, ", ".join(pending) or "-"))
            readback = names
        elif name == "main":
            main_names = names
            if "main" in involved:
                if count <= 0:
                    raise CliError(EXIT_ROLLED_BACK, t("err.main_empty"), detail="main nodes 0")
            elif pre_counts.get("main", 0) > 0 and count <= 0:
                raise CliError(EXIT_ROLLED_BACK, t("err.main_empty"), detail="main nodes dropped to 0")
    members = group.get("all", [])
    for name in hidden_added:
        if name in pre_members and name in members:
            raise CliError(EXIT_ROLLED_BACK, t("err.verify_failed"), detail="still visible: %s" % name)
    for name in restored:
        if main_names is not None and name in main_names and name not in members:
            raise CliError(EXIT_ROLLED_BACK, t("err.verify_failed"), detail="not restored: %s" % name)
    if render_mod.expects_no_nodes(new_state, main_names):
        ctx.say("warn", t("warn.no_nodes_blocked"))
    return readback


def _rollback(ctx: Ctx, api: Api, old_state: Dict[str, Any], backup: str, replaced: List[str],
              cause: BaseException) -> None:
    """第 6 步：恢复 config.yaml 与本次涉及的文件，再重载；仍失败则 POST /restart（仅 TUN 关闭时）。"""
    detail = getattr(cause, "detail", "") or str(cause)
    for final_path in replaced:
        saved = _backup_path_for(ctx, backup, final_path)
        try:
            if saved:
                # 先写临时文件再原子替换：provider 文件被内核监视，直接覆盖可能被读到半截内容。
                tmp = final_path + ".rollback.tmp"
                shutil.copy2(saved, tmp)
                os.replace(tmp, final_path)
            elif os.path.exists(final_path):
                os.unlink(final_path)
        except OSError as exc:
            log.error("[reload] rollback restore failed path=%s err=%s", final_path, exc)
    _clear_staging(ctx)
    recovered = False
    force_fail = _force_fail("MIHOMO_CLI_TEST_FAIL_ROLLBACK")
    if not force_fail:
        try:
            api.reload(ctx.paths.config)
            recovered = True
        except (ApiError, ApiUnavailable) as exc:
            log.error("[reload] rollback reload failed err=%s", exc)
    tun_on = render_mod.tun_effective_in_config(old_state)
    if not recovered and not force_fail and not tun_on:
        # 进程内重启不会重跑 systemd 的 Pre / Post；TUN 开启时重启期间整机域名解析中断，所以只在 TUN 关闭时用。
        try:
            api.restart()
            recovered = True
        except (ApiError, ApiUnavailable) as exc:
            log.error("[reload] rollback restart failed err=%s", exc)
    if recovered:
        try:
            ensure_selection(ctx, api, copy.deepcopy(old_state))
        except (ApiError, ApiUnavailable):
            pass
        log.warning("[reload] rolled_back backup=%s reason=%s", backup, detail[:300])
        message = getattr(cause, "message", None) or t("err.reload_failed")
        raise CliError(EXIT_ROLLED_BACK, message + " " + t("info.rolled_back", backup=backup), detail=detail)
    log.error("[reload] rollback incomplete backup=%s reason=%s", backup, detail[:300])
    raise CliError(EXIT_MANUAL, t("err.rollback_incomplete"), detail=detail,
                   hints=["mihomo-cli backup restore %s" % backup, "sudo systemctl restart mihomo-cli"])


def commit(ctx: Ctx, old_state: Dict[str, Any], new_state: Dict[str, Any], action: str,
           files: Optional[Dict[str, Optional[bytes]]] = None, **kwargs: Any) -> str:
    """内核运行时走重载事务，否则只落盘并提示启动服务。调用方持锁。"""
    if kernel_running(ctx):
        return reload(ctx, old_state, new_state, action, files=files, **kwargs)
    backup = apply_offline(ctx, new_state, files or {})
    for path in kwargs.get("cleanup_after", ()):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    ctx.say("info", t("info.saved_not_running"))
    return backup


# ====================================================================
# 状态概览
# ====================================================================

def exit_ip(port: int) -> Optional[Dict[str, Any]]:
    """经本实例混合端口访问外网，取出口 IP 与国家；失败返回 None。"""
    proxy_url = "http://127.0.0.1:%d" % port
    try:
        try:
            client = httpx.Client(proxy=proxy_url, timeout=EXIT_IP_TIMEOUT_SEC, trust_env=False)
        except TypeError:  # httpx < 0.26
            client = httpx.Client(proxies=proxy_url, timeout=EXIT_IP_TIMEOUT_SEC, trust_env=False)
        with client:
            resp = client.get(EXIT_IP_URL)
            if resp.status_code != 200:
                return None
            data = resp.json()
            return {"ip": data.get("ip"), "country": data.get("country")}
    except (httpx.HTTPError, ValueError):
        return None


def status(ctx: Ctx, with_ip: bool = True) -> Dict[str, Any]:
    state = read_state(ctx)
    st = service_status(ctx)
    prof = render_mod.active_profile(state)
    info: Dict[str, Any] = {
        "home": ctx.paths.home,
        "service": {"installed": st.installed, "active_state": st.active_state, "main_pid": st.main_pid,
                    "enabled": st.unit_file_state},
        "kernel_version": None,
        "profile": state.get("active_profile"),
        "profile_kind": prof["kind"] if prof else None,
        "profile_source": nodes_mod.redact_url(prof.get("url")) if prof and prof.get("url") else None,
        "node": prof.get("selected") if prof else None,
        "mode": state["mode"],
        "cn_route": state["cn_route"],
        "tun": state["tun"],
        "tun_suspended": state.get("tun_suspended"),
        "mixed_port": state["mixed_port"],
        "exit": None,
        "running": st.running,
    }
    if st.running and os.path.exists(ctx.paths.sock):
        try:
            with ctx.api() as api:
                info["kernel_version"] = api.version().get("version")
                info["node"] = api.get_proxy("PROXY").get("now")
                info["mode"] = str(api.get_configs().get("mode", state["mode"])).lower()
        except (ApiError, ApiUnavailable):
            pass
        if with_ip:
            info["exit"] = exit_ip(int(state["mixed_port"]))
    return info


# ====================================================================
# 安装 / 卸载 / 内核
# ====================================================================

def install(ctx: Ctx, core_version: Optional[str], mirror: Optional[str], port: Optional[int]) -> Dict[str, Any]:
    version = normalize_version_or_usage(core_version)
    mirror_n = mirror_normalize(mirror) if mirror is not None else None
    home = ctx.paths.home
    if any(ch.isspace() for ch in home):
        raise CliError(EXIT_USAGE, t("err.home_space", home=home))
    if port is not None and not (1 <= port <= 65535):
        raise CliError(EXIT_USAGE, t("err.bad_port", port=port))
    ensure_home(ctx.paths)
    before = service_status(ctx)
    result: Dict[str, Any] = {"home": home}
    with locked(ctx):
        state = read_state(ctx)
        if mirror is not None:
            state["mirror"] = mirror_n
        if port is not None:
            state["mixed_port"] = port
        conflicts = port_conflicts(ctx, int(state["mixed_port"]))
        if conflicts:
            raise CliError(EXIT_ERROR, t("err.port_in_use", port=state["mixed_port"]), detail="; ".join(conflicts),
                           hints=["mihomo-cli install --port <N>"])
        base = render_mod.site_base(state)
        if version is not None or not os.path.exists(ctx.paths.core):
            target = version or _latest(base)
            ctx.say("info", t("info.downloading_core", version=target))
            warning = _install_core(ctx, base, target)
            if warning:
                ctx.say("warn", t("warn.digest_unavailable"))
            result["core_version"] = target
        missing_geo = [n for n in core_dl.GEODATA_FILES if core_dl.geodata_path(home, n) is None]
        if missing_geo:
            ctx.say("info", t("info.downloading_geodata"))
            try:
                core_dl.install_geodata(home, base)
            except core_dl.DownloadError as exc:
                raise CliError(EXIT_ERROR, t("err.download_failed"), detail=str(exc)) from exc
        save_state(ctx.paths, state)
        write_config(ctx, state)
    # 调 systemctl enable --now 之前必须已释放锁（ExecStartPre 要拿它）。
    ctx.say("info", t("info.sudo_install"))
    # 先单独让 sudo 读密码（sudo -v 不设超时）。否则密码会在 install_service 的第一条 sudo 命令里读，
    # 输密码的时间算进那条命令 15 秒的超时，输慢了就失败。
    if not ctx.platform.sudo_validate():
        raise CliError(EXIT_SUDO, t("err.sudo_failed"))
    user = getpass.getuser()
    try:
        ctx.platform.install_service(home, user, ctx.python_exe)
        if before.running and port is not None:
            ctx.platform.service_action("restart")
    except PlatformError as exc:
        raise CliError(EXIT_SUDO, t("err.service_failed"), detail=str(exc)) from exc
    log.info("[install] home=%s user=%s", home, user)
    result["service"] = service_status(ctx).active_state
    return result


def _latest(base: str) -> str:
    try:
        return core_dl.latest_version(base)
    except core_dl.DownloadError as exc:
        raise CliError(EXIT_ERROR, t("err.download_failed"), detail=str(exc)) from exc


def _install_core(ctx: Ctx, base: str, version: str) -> Optional[str]:
    try:
        asset = ctx.platform.core_asset_name(version)
        return core_dl.install_core(ctx.paths.core, ctx.paths.staging, asset, base, version)
    except core_dl.DownloadError as exc:
        raise CliError(EXIT_ERROR, t("err.download_failed"), detail=str(exc)) from exc


def uninstall(ctx: Ctx, purge: bool) -> Dict[str, Any]:
    ctx.say("info", t("info.sudo_uninstall"))
    # 同 install：unit 文件不存在时第一条 sudo 是 15 秒超时的 daemon-reload，先单独读密码。
    if not ctx.platform.sudo_validate():
        raise CliError(EXIT_SUDO, t("err.sudo_failed"))
    try:
        # 停止时 ExecStopPost 会还原 DNS 接管。
        ctx.platform.uninstall_service()
    except PlatformError as exc:
        raise CliError(EXIT_SUDO, t("err.service_failed"), detail=str(exc)) from exc
    if purge and os.path.isdir(ctx.paths.home):
        shutil.rmtree(ctx.paths.home)
    log.info("[install] uninstall purge=%s", purge)
    return {"purged": purge}


def core_upgrade(ctx: Ctx, version: Optional[str], mirror: Optional[str]) -> Dict[str, Any]:
    version_n = normalize_version_or_usage(version)
    with locked(ctx):
        state = read_state(ctx)
        if mirror is not None:
            state["mirror"] = mirror_normalize(mirror)
            save_state(ctx.paths, state)
            # 镜像变化后与切模式一样原子重写 config.yaml（geox-url 跟着变），使 D04 一致。
            write_config(ctx, state)
        base = render_mod.site_base(state)
    target = version_n or _latest(base)
    old, _ = core_dl.core_version(ctx.paths.core)
    ctx.say("info", t("info.downloading_core", version=target))
    warning = _install_core(ctx, base, target)
    if warning:
        ctx.say("warn", t("warn.digest_unavailable"))
    log.info("[install] core upgrade from=%s to=%s", old, target)
    if service_status(ctx).running:
        ctx.say("info", t("info.restart_to_apply"))
    return {"from": old, "to": target}


# ====================================================================
# 服务
# ====================================================================

def service_control(ctx: Ctx, action: str) -> Dict[str, Any]:
    st = service_status(ctx)
    if not st.installed:
        raise CliError(EXIT_ERROR, t("err.not_installed"), hints=["mihomo-cli install"])
    try:
        # 本函数不持 state.lock：systemctl start 会同步等 ExecStartPre 拿锁。
        ctx.platform.service_action(action)
    except PlatformError as exc:
        raise CliError(EXIT_SUDO, t("err.service_failed"), detail=str(exc)) from exc
    log.info("[switch] service action=%s", action)
    return {"action": action, "active_state": service_status(ctx).active_state}


# ====================================================================
# 代理服务
# ====================================================================

def _check_profile_name(name: str) -> None:
    if not PROFILE_NAME_RE.match(name or ""):
        raise CliError(EXIT_USAGE, t("err.bad_profile_name", name=name))


def profile_list(ctx: Ctx) -> List[Dict[str, Any]]:
    state = read_state(ctx)
    out = []
    for name, prof in state["profiles"].items():
        out.append({
            "name": name, "kind": prof["kind"], "active": name == state.get("active_profile"),
            "source": nodes_mod.redact_url(prof.get("url")) if prof.get("url") else prof.get("file"),
            "manual": len(prof["manual"]), "hidden": len(prof["hidden"]), "selected": prof.get("selected"),
        })
    return out


def profile_show(ctx: Ctx, name: Optional[str], reveal: bool) -> Dict[str, Any]:
    state = read_state(ctx)
    if name is None:
        name, _ = require_active(state)
    prof = copy.deepcopy(require_profile(state, name))
    if prof.get("url") and not reveal:
        prof["url"] = nodes_mod.redact_url(prof["url"])
    if not reveal:
        for m in prof["manual"]:
            m["link"] = nodes_mod.link_summary(m["link"])
    prof["name"] = name
    prof["active"] = name == state.get("active_profile")
    return prof


def profile_add(ctx: Ctx, name: str, url: Optional[str] = None, file: Optional[str] = None,
                node_links: Tuple[str, ...] = (), interval: Optional[int] = None,
                user_agent: Optional[str] = None) -> Dict[str, Any]:
    _check_profile_name(name)
    if url and file:
        raise CliError(EXIT_USAGE, t("err.url_and_file"))
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise CliError(EXIT_USAGE, t("err.bad_url"))
    file_bytes: Optional[bytes] = None
    if file:
        try:
            with open(os.path.expanduser(file), "rb") as fh:
                file_bytes = fh.read()
        except OSError as exc:
            raise CliError(EXIT_ERROR, t("err.read_file", path=file), detail=str(exc)) from exc
    links = _validate_links(node_links)
    kind = "subscription" if url else ("file" if file else "nodes")
    ensure_home(ctx.paths)
    check_home(ctx)
    with locked(ctx):
        state = read_state(ctx)
        if name in state["profiles"]:
            raise CliError(EXIT_ERROR, t("err.profile_exists", name=name))
        prof = new_profile(kind, url=url, file=os.path.abspath(os.path.expanduser(file)) if file else None,
                           interval=interval or 86400, user_agent=user_agent)
        prof["manual"] = [{"link": link, "name": None} for link in links]
        with_profile = copy.deepcopy(state)
        with_profile["profiles"][name] = prof
        files: Dict[str, Optional[bytes]] = {}
        if file_bytes is not None:
            files[ctx.paths.provider_file(name)] = file_bytes
        if prof["manual"]:
            files[ctx.paths.nodes_file(name)] = manual_file_content(prof)
        if state.get("active_profile"):
            # 非当前代理服务：不进内核，直接落盘。
            _write_files_direct(ctx, files)
            save_state(ctx.paths, with_profile)
            log.info("[switch] profile add name=%s kind=%s", name, kind)
            return {"name": name, "kind": kind, "active": False}
        # 首个代理服务自动启用。
        new_state = copy.deepcopy(with_profile)
        new_state["active_profile"] = name
        try:
            commit(ctx, state, new_state, "profile-add", files=files, involved=("main", "manual"))
        except CliError as exc:
            if exc.code != EXIT_ROLLED_BACK:
                raise
            # 回滚后保留代理服务、不启用，提示修正后再 use（方案 5.1.5 动作表）。
            _write_files_direct(ctx, files)
            save_state(ctx.paths, with_profile)
            raise CliError(EXIT_ROLLED_BACK, t("err.added_not_enabled", name=name), detail=exc.detail,
                           hints=["mihomo-cli profile use %s" % name]) from None
        log.info("[switch] profile add name=%s kind=%s active=true", name, kind)
        return {"name": name, "kind": kind, "active": True}


def _write_files_direct(ctx: Ctx, files: Dict[str, Optional[bytes]]) -> None:
    for path, content in files.items():
        if content is None:
            continue
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path + ".tmp", "wb") as fh:
            fh.write(content)
        os.replace(path + ".tmp", path)


def profile_rm(ctx: Ctx, name: str) -> None:
    with locked(ctx):
        state = read_state(ctx)
        require_profile(state, name)
        if state.get("active_profile") == name:
            raise CliError(EXIT_ERROR, t("err.rm_active", name=name))
        del state["profiles"][name]
        save_state(ctx.paths, state)
        for path in (ctx.paths.provider_file(name), ctx.paths.nodes_file(name)):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    log.info("[switch] profile rm name=%s", name)


def profile_use(ctx: Ctx, name: str) -> Dict[str, Any]:
    check_home(ctx)
    with locked(ctx):
        state = read_state(ctx)
        prof = require_profile(state, name)
        old_name = state.get("active_profile")
        new_state = copy.deepcopy(state)
        new_state["active_profile"] = name
        files = {ctx.paths.nodes_file(name): manual_file_content(prof)}
        commit(ctx, state, new_state, "profile-use", files=files, involved=("main", "manual"))
    log.info("[switch] profile old=%s new=%s", old_name, name)
    return {"profile": name}


def profile_update(ctx: Ctx, name: Optional[str]) -> Dict[str, Any]:
    state = read_state(ctx)
    active, prof = require_active(state)
    name = name or active
    require_profile(state, name)
    if name != active:
        raise CliError(EXIT_ERROR, t("err.update_inactive", name=name))
    if prof["kind"] == "nodes":
        ctx.say("info", t("info.nothing_to_update"))
        return {"profile": name, "updated": False}
    if not kernel_running(ctx):
        raise CliError(EXIT_UNREACHABLE, t("err.kernel_not_running"), hints=["mihomo-cli service start"])
    with ctx.api() as api:
        try:
            _api_call(lambda: api.update_provider("main"))
        except ApiError as exc:
            # 更新失败时内核保留旧节点、不覆盖缓存（component/resource/fetcher.go:85-91）。
            raise CliError(EXIT_ERROR, t("err.update_failed"), detail=exc.message) from exc
        count = len(api.provider_node_names("main"))
    log.info("[switch] profile update name=%s nodes=%d", name, count)
    return {"profile": name, "updated": True, "nodes": count}


def profile_rename(ctx: Ctx, old: str, new: str) -> Dict[str, Any]:
    _check_profile_name(new)
    with locked(ctx):
        state = read_state(ctx)
        prof = require_profile(state, old)
        if new in state["profiles"]:
            raise CliError(EXIT_ERROR, t("err.profile_exists", name=new))
        new_state = copy.deepcopy(state)
        new_state["profiles"] = {(new if k == old else k): v for k, v in new_state["profiles"].items()}
        old_files = [ctx.paths.provider_file(old), ctx.paths.nodes_file(old)]
        if state.get("active_profile") != old:
            save_state(ctx.paths, new_state)
            for src in old_files:
                if os.path.exists(src):
                    dst = src.replace(os.sep + old + ".", os.sep + new + ".")
                    os.replace(src, dst)
            log.info("[switch] profile rename old=%s new=%s", old, new)
            return {"old": old, "new": new}
        # 当前代理服务改名：把旧名的订阅缓存 / 本地文件与节点文件复制成新名一起放进事务，
        # 避免新路径无缓存时内核去远端重拉（方案 5.1.5 动作表）。
        new_state["active_profile"] = new
        files: Dict[str, Optional[bytes]] = {}
        if os.path.exists(ctx.paths.provider_file(old)):
            with open(ctx.paths.provider_file(old), "rb") as fh:
                files[ctx.paths.provider_file(new)] = fh.read()
        if prof["manual"]:
            files[ctx.paths.nodes_file(new)] = manual_file_content(prof)
        commit(ctx, state, new_state, "profile-rename", files=files, cleanup_after=tuple(old_files))
    log.info("[switch] profile rename old=%s new=%s", old, new)
    return {"old": old, "new": new}


# ====================================================================
# 节点
# ====================================================================

def _validate_links(links: Tuple[str, ...]) -> List[str]:
    out = []
    for link in links:
        link = link.strip()
        if not link:
            continue
        if "://" not in link:
            raise CliError(EXIT_USAGE, t("err.bad_link", link=nodes_mod.link_summary(link)))
        out.append(link)
    return out


def node_list(ctx: Ctx, with_delay: bool = False) -> Dict[str, Any]:
    state = read_state(ctx)
    name, prof = require_active(state)
    result: Dict[str, Any] = {"profile": name, "now": prof.get("selected"), "nodes": [], "hidden": prof["hidden"],
                              "pending": [], "running": False}
    for idx, m in enumerate(prof["manual"], 1):
        if m.get("name") is None:
            result["pending"].append({"index": idx, "summary": nodes_mod.link_summary(m["link"])})
    if not kernel_running(ctx):
        result["nodes"] = [{"name": m["name"], "source": "manual", "index": i}
                           for i, m in enumerate(prof["manual"], 1) if m.get("name")]
        return result
    result["running"] = True
    with ctx.api() as api:
        group = _api_call(lambda: api.get_proxy("PROXY"))
        manual = set(api.provider_node_names("manual")) if prof["manual"] else set()
        delays: Dict[str, int] = {}
        if with_delay:
            try:
                delays = api.group_delay("PROXY")
            except ApiError:
                delays = {}
        result["now"] = group.get("now")
        index_of = {m.get("name"): i for i, m in enumerate(prof["manual"], 1)}
        for member in group.get("all", []):
            if member == "REJECT":
                continue
            item: Dict[str, Any] = {"name": member, "source": "manual" if member in manual else "subscription"}
            if member in manual:
                item["index"] = index_of.get(member)
            if with_delay:
                item["delay"] = delays.get(member, 0)
            result["nodes"].append(item)
    return result


def node_use(ctx: Ctx, name: str) -> Dict[str, Any]:
    with locked(ctx):
        state = read_state(ctx)
        _, prof = require_active(state)
        old = prof.get("selected")
        if name == "REJECT":
            raise CliError(EXIT_USAGE, t("err.no_such_node", name=name))
        if kernel_running(ctx):
            with ctx.api() as api:
                members = _api_call(lambda: api.get_proxy("PROXY")).get("all", [])
                if name not in members:
                    raise CliError(EXIT_ERROR, t("err.no_such_node", name=name))
                try:
                    api.select("PROXY", name)
                except ApiError as exc:
                    raise CliError(EXIT_ERROR, t("err.select_failed"), detail=exc.message) from exc
        else:
            ctx.say("info", t("info.saved_not_running"))
        prof["selected"] = name
        save_state(ctx.paths, state)
    log.info("[switch] node old=%s new=%s", old, name)
    return {"old": old, "new": name}


def node_add(ctx: Ctx, links: Tuple[str, ...] = (), file: Optional[str] = None) -> Dict[str, Any]:
    all_links = list(links)
    if file:
        try:
            with open(os.path.expanduser(file), encoding="utf-8") as fh:
                all_links += nodes_mod.split_links(fh.read())
        except OSError as exc:
            raise CliError(EXIT_ERROR, t("err.read_file", path=file), detail=str(exc)) from exc
    valid = _validate_links(tuple(all_links))
    if not valid:
        raise CliError(EXIT_USAGE, t("err.no_links"))
    check_home(ctx)
    with locked(ctx):
        state = read_state(ctx)
        name, prof = require_active(state)
        new_state = copy.deepcopy(state)
        new_prof = new_state["profiles"][name]
        new_prof["manual"] += [{"link": link, "name": None} for link in valid]
        files = {ctx.paths.nodes_file(name): manual_file_content(new_prof)}
        commit(ctx, state, new_state, "node-add", files=files, involved=("manual",))
        # 从落盘后的 state 取：在线时是回读后的名字，离线时为 None（待内核启动后回读）。
        # 不能用传进 commit 的旧 state——离线路径不会更新它。
        saved = read_state(ctx)["profiles"][name]["manual"]
        added = [m.get("name") for m in saved[-len(valid):]]
    log.info("[switch] node add profile=%s count=%d", name, len(valid))
    return {"profile": name, "added": added}


def _find_manual(prof: Dict[str, Any], target: str) -> Optional[int]:
    for i, m in enumerate(prof["manual"]):
        if m.get("name") == target:
            return i
    if target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(prof["manual"]):
            return idx
    return None


def node_rm(ctx: Ctx, target: str) -> Dict[str, Any]:
    """手工节点直接删除；订阅节点改为隐藏（订阅每次更新都会回来）。target 可为节点名或手工条目序号。"""
    with locked(ctx):
        state = read_state(ctx)
        name, prof = require_active(state)
        idx = _find_manual(prof, target)
        new_state = copy.deepcopy(state)
        new_prof = new_state["profiles"][name]
        if idx is not None:
            removed = new_prof["manual"].pop(idx)
            if removed.get("name") and new_prof.get("selected") == removed["name"]:
                # 删的正是选中条目：清空，交给 ensure_selection 补选。
                new_prof["selected"] = None
            files = {ctx.paths.nodes_file(name): manual_file_content(new_prof)}
            commit(ctx, state, new_state, "node-rm", files=files, involved=("manual",))
            log.info("[switch] node rm profile=%s name=%s", name, removed.get("name"))
            return {"removed": removed.get("name") or nodes_mod.link_summary(removed["link"]), "hidden": False}
        if prof["kind"] == "nodes":
            raise CliError(EXIT_ERROR, t("err.no_such_node", name=target))
        try:
            nodes_mod.hidden_filter([target])
        except nodes_mod.NodeNameError:
            raise CliError(EXIT_USAGE, t("err.cannot_hide", name=target)) from None
        if target in prof["hidden"]:
            raise CliError(EXIT_ERROR, t("err.already_hidden", name=target))
        if kernel_running(ctx):
            with ctx.api() as api:
                if target not in api.provider_node_names("main"):
                    raise CliError(EXIT_ERROR, t("err.no_such_node", name=target))
        new_prof["hidden"].append(target)
        if new_prof.get("selected") == target:
            new_prof["selected"] = None
        commit(ctx, state, new_state, "node-hide", hidden_added=(target,))
        ctx.say("info", t("info.hidden_instead"))
        log.info("[switch] node hide profile=%s name=%s", name, target)
        return {"removed": target, "hidden": True}


def node_restore(ctx: Ctx, target: str) -> Dict[str, Any]:
    with locked(ctx):
        state = read_state(ctx)
        name, prof = require_active(state)
        if target not in prof["hidden"]:
            raise CliError(EXIT_ERROR, t("err.not_hidden", name=target))
        new_state = copy.deepcopy(state)
        new_state["profiles"][name]["hidden"].remove(target)
        commit(ctx, state, new_state, "node-restore", restored=(target,))
    log.info("[switch] node restore profile=%s name=%s", name, target)
    return {"restored": target}


def node_test(ctx: Ctx, name: Optional[str], url: str, timeout_ms: int) -> Dict[str, int]:
    if not kernel_running(ctx):
        raise CliError(EXIT_UNREACHABLE, t("err.kernel_not_running"), hints=["mihomo-cli service start"])
    with ctx.api() as api:
        if name:
            try:
                return {name: _api_call(lambda: api.proxy_delay(name, url, timeout_ms))}
            except ApiError as exc:
                if exc.status == 404:
                    raise CliError(EXIT_ERROR, t("err.no_such_node", name=name)) from exc
                return {name: 0}
        try:
            result = _api_call(lambda: api.group_delay("PROXY", url, timeout_ms))
        except ApiError as exc:
            raise CliError(EXIT_ERROR, t("err.delay_failed"), detail=exc.message) from exc
    return {k: v for k, v in result.items() if k != "REJECT"}


def node_auto(ctx: Ctx, url: str, timeout_ms: int) -> Dict[str, Any]:
    delays = node_test(ctx, None, url, timeout_ms)
    alive = {k: v for k, v in delays.items() if v > 0}
    if not alive:
        raise CliError(EXIT_ERROR, t("err.no_alive_node"))
    best = min(alive, key=lambda k: alive[k])
    node_use(ctx, best)
    return {"node": best, "delay": alive[best]}


# ====================================================================
# 模式与国内开关
# ====================================================================

def mode_get(ctx: Ctx) -> Dict[str, Any]:
    return {"mode": read_state(ctx)["mode"]}


def mode_set(ctx: Ctx, mode: str) -> Dict[str, Any]:
    if mode not in ("global", "rule", "direct"):
        raise CliError(EXIT_USAGE, t("err.bad_mode", mode=mode))
    with locked(ctx):
        state = read_state(ctx)
        old = state["mode"]
        if kernel_running(ctx):
            with ctx.api() as api:
                try:
                    # PATCH 只改运行时，不重启、不断网（hub/route/configs.go:356-357）。
                    _api_call(lambda: api.patch_configs({"mode": mode}))
                except ApiError as exc:
                    raise CliError(EXIT_ERROR, t("err.mode_failed"), detail=exc.message) from exc
        else:
            ctx.say("info", t("info.saved_not_running"))
        state["mode"] = mode
        save_state(ctx.paths, state)
        # 同时重写 config.yaml（不重载），保证重启后仍是新模式。
        write_config(ctx, state)
    log.info("[switch] mode old=%s new=%s", old, mode)
    return {"old": old, "new": mode}


def cn_get(ctx: Ctx) -> Dict[str, Any]:
    return {"cn_route": read_state(ctx)["cn_route"]}


def cn_set(ctx: Ctx, route: str) -> Dict[str, Any]:
    if route not in ("direct", "proxy"):
        raise CliError(EXIT_USAGE, t("err.bad_cn", value=route))
    with locked(ctx):
        state = read_state(ctx)
        old = state["cn_route"]
        if kernel_running(ctx):
            with ctx.api() as api:
                try:
                    # 国内规则指向 CN 组，切换只改组选择，不改规则、不重启（方案 5.1.4）。
                    _api_call(lambda: api.select("CN", "DIRECT" if route == "direct" else "PROXY"))
                except ApiError as exc:
                    raise CliError(EXIT_ERROR, t("err.select_failed"), detail=exc.message) from exc
        else:
            ctx.say("info", t("info.saved_not_running"))
        state["cn_route"] = route
        save_state(ctx.paths, state)
    log.info("[switch] cn old=%s new=%s", old, route)
    return {"old": old, "new": route}


# ====================================================================
# TUN（方案 5.1.8）
# ====================================================================

def _preserve_env() -> List[str]:
    return [TEST_ENV_DNS_REVERT] if os.environ.get(TEST_ENV_DNS_REVERT) else []


def _dns_apply(ctx: Ctx) -> bool:
    return ctx.platform.run_self_as_root(ctx.paths.home, ctx.python_exe, "_tun-dns-apply", _preserve_env()) == 0


def _dns_revert(ctx: Ctx) -> bool:
    return ctx.platform.run_self_as_root(ctx.paths.home, ctx.python_exe, "_tun-dns-revert", _preserve_env()) == 0


def manual_dns_cleanup_hints() -> List[str]:
    return ["sudo resolvectl revert <iface>",
            "sudo rm -f /run/systemd/resolved.conf.d/mihomo-cli.conf /run/mihomo-cli/dns.state",
            "sudo systemctl restart systemd-resolved"]


def _tun_precheck(ctx: Ctx) -> None:
    reason = ctx.platform.tun_supported()
    if reason is None:
        _, out = core_dl.core_version(ctx.paths.core)
        # mixed 栈的 UDP 依赖 gvisor；官方发布版带 with_gvisor 标签。
        if out and "with_gvisor" not in out:
            reason = "kernel built without gvisor"
    if reason:
        raise CliError(EXIT_ERROR, t("err.tun_unsupported"), detail=reason)
    conflicts = ctx.platform.tun_conflicts()
    if conflicts:
        raise CliError(EXIT_ERROR, t("err.tun_conflict"), detail="; ".join(conflicts))


def tun_on(ctx: Ctx, d13: Optional[Callable[[Ctx], bool]] = None) -> Dict[str, Any]:
    st = service_status(ctx)
    running = kernel_running(ctx, st)
    state = read_state(ctx)
    if state["tun"] and not state.get("tun_suspended"):
        ctx.say("info", t("info.tun_already_on"))
        if running and d13 is not None and not d13(ctx):
            ctx.say("warn", t("warn.run_doctor_fix"))
        return {"tun": True, "changed": False}
    if running and not ctx.platform.sudo_validate():
        raise CliError(EXIT_SUDO, t("err.sudo_failed"))
    _tun_precheck(ctx)
    with locked(ctx):
        state = read_state(ctx)
        new_state = copy.deepcopy(state)
        new_state["tun"] = True
        new_state["tun_suspended"] = None
        if not kernel_running(ctx):
            commit(ctx, state, new_state, "tun-on")
            log.info("[tun] on offline")
            return {"tun": True, "changed": True, "dns": "deferred"}
        reload(ctx, state, new_state, "tun-on")
    # 事务提交后再接管 DNS：此时内核已在 127.0.0.1:53 监听。
    if _dns_apply(ctx):
        log.info("[tun] on dns=applied")
        return {"tun": True, "changed": True, "dns": "applied"}
    # 自动撤回：只还原 DNS 并关 TUN，不重新接管（方案 5.1.8 第 4 步）。
    log.error("[tun] dns apply failed, reverting")
    reverted = _dns_revert(ctx)
    try:
        with locked(ctx):
            state = read_state(ctx)
            off_state = copy.deepcopy(state)
            off_state["tun"] = False
            off_state["tun_suspended"] = None
            reload(ctx, state, off_state, "tun-on-revert")
    except CliError as exc:
        raise CliError(EXIT_MANUAL, t("err.tun_revert_failed"), detail=exc.detail,
                       hints=manual_dns_cleanup_hints()) from None
    if not reverted:
        raise CliError(EXIT_MANUAL, t("err.tun_revert_failed"), hints=manual_dns_cleanup_hints())
    raise CliError(EXIT_ROLLED_BACK, t("err.dns_apply_failed"))


def tun_off(ctx: Ctx) -> Dict[str, Any]:
    st = service_status(ctx)
    running = kernel_running(ctx, st)
    state = read_state(ctx)
    if not state["tun"]:
        ctx.say("info", t("info.tun_already_off"))
        return {"tun": False, "changed": False}
    if not running:
        with locked(ctx):
            state = read_state(ctx)
            new_state = copy.deepcopy(state)
            new_state["tun"] = False
            new_state["tun_suspended"] = None
            commit(ctx, state, new_state, "tun-off")
        log.info("[tun] off offline")
        return {"tun": False, "changed": True}
    if not ctx.platform.sudo_validate():
        raise CliError(EXIT_SUDO, t("err.sudo_failed"))
    # 先还原 DNS 再关 TUN：重载去掉 dns 段后 53 端口立即停听；先还原时 TUN 仍开，
    # 转去原上游的查询仍被 dns-hijack 接住，解析不中断（方案 5.1.8）。
    if not _dns_revert(ctx):
        raise CliError(EXIT_ERROR, t("err.dns_revert_failed"), hints=manual_dns_cleanup_hints())
    try:
        with locked(ctx):
            state = read_state(ctx)
            new_state = copy.deepcopy(state)
            new_state["tun"] = False
            new_state["tun_suspended"] = None
            reload(ctx, state, new_state, "tun-off")
    except CliError as exc:
        if exc.code == EXIT_MANUAL:
            # 回滚也没成功：内核状态未知，53 端口可能已不在监听，重新接管会把 DNS 指向不通的地址。
            log.error("[tun] tun off rollback incomplete, dns left reverted")
            raise
        if not _dns_apply(ctx):
            log.warning("[tun] re-apply dns after failed tun off also failed")
            ctx.say("warn", t("warn.dns_reapply_failed"))
        raise
    log.info("[tun] off")
    return {"tun": False, "changed": True}


def tun_status(ctx: Ctx) -> Dict[str, Any]:
    state = read_state(ctx)
    st = service_status(ctx)
    effective = render_mod.tun_effective_in_config(state) and st.running
    info = {"tun": state["tun"], "suspended": state.get("tun_suspended"), "effective": effective,
            "device": ctx.platform.tun_device_exists(render_mod.TUN_DEVICE)}
    try:
        info["dns"] = ctx.platform.dns_status()
    except (PlatformError, NotSupported):
        info["dns"] = None
    return info


# ====================================================================
# 终端代理环境变量
# ====================================================================

PROXY_ENV_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
NO_PROXY = "localhost,127.0.0.1,::1"


def proxy_env(ctx: Ctx, on: bool) -> str:
    """子进程改不了父 shell 的环境，只输出语句，由用户 eval（方案 5.1.6）。"""
    if not on:
        return "unset %s no_proxy NO_PROXY\n" % " ".join(PROXY_ENV_VARS)
    port = read_state(ctx)["mixed_port"]
    lines = []
    for var in PROXY_ENV_VARS:
        scheme = "socks5h" if var.lower() == "all_proxy" else "http"
        lines.append("export %s=%s://127.0.0.1:%d" % (var, scheme, port))
    lines.append("export no_proxy=%s" % NO_PROXY)
    lines.append("export NO_PROXY=%s" % NO_PROXY)
    return "\n".join(lines) + "\n"


# ====================================================================
# 连接与日志
# ====================================================================

def connections(ctx: Ctx) -> List[Dict[str, Any]]:
    if not kernel_running(ctx):
        raise CliError(EXIT_UNREACHABLE, t("err.kernel_not_running"), hints=["mihomo-cli service start"])
    with ctx.api() as api:
        data = _api_call(api.connections)
    out = []
    for c in (data or {}).get("connections") or []:
        meta = c.get("metadata") or {}
        host = meta.get("host") or meta.get("destinationIP") or ""
        out.append({
            "host": "%s:%s" % (host, meta.get("destinationPort", "")),
            "network": meta.get("network", ""),
            "rule": ("%s(%s)" % (c.get("rule", ""), c.get("rulePayload"))) if c.get("rulePayload") else c.get("rule", ""),
            "chain": " > ".join(reversed(c.get("chains") or [])),
            "upload": c.get("upload", 0),
            "download": c.get("download", 0),
        })
    return out


def stream_logs(ctx: Ctx, level: str) -> Iterator[Dict[str, Any]]:
    if not kernel_running(ctx):
        raise CliError(EXIT_UNREACHABLE, t("err.kernel_not_running"), hints=["mihomo-cli service start"])
    api = ctx.api()
    try:
        yield from api.stream_logs(level)
    except ApiUnavailable as exc:
        raise CliError(EXIT_UNREACHABLE, t("err.kernel_unreachable"), detail=str(exc)) from exc
    finally:
        api.close()


# ====================================================================
# 备份
# ====================================================================

def backup_list(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    if not os.path.isdir(ctx.paths.backups):
        return out
    for name in sorted(os.listdir(ctx.paths.backups), reverse=True):
        path = os.path.join(ctx.paths.backups, name)
        if not os.path.isdir(path):
            continue
        item: Dict[str, Any] = {"name": name, "profile": None, "profiles": []}
        try:
            with open(os.path.join(path, "state.yaml"), encoding="utf-8") as fh:
                from .state import parse_state

                bstate = parse_state(fh.read())
            item["profile"] = bstate.get("active_profile")
            item["profiles"] = list(bstate["profiles"].keys())
        except (OSError, StateError):
            pass
        out.append(item)
    return out


def backup_restore(ctx: Ctx, name: str) -> Dict[str, Any]:
    from .state import parse_state

    path = os.path.join(ctx.paths.backups, name)
    if os.sep in name or name.startswith(".") or not os.path.isdir(path):
        raise CliError(EXIT_ERROR, t("err.no_such_backup", name=name))
    try:
        with open(os.path.join(path, "state.yaml"), encoding="utf-8") as fh:
            bstate = parse_state(fh.read())
    except (OSError, StateError) as exc:
        raise CliError(EXIT_ERROR, t("err.backup_invalid", name=name), detail=str(exc)) from exc
    with locked(ctx):
        state = read_state(ctx)
        new_state = copy.deepcopy(state)
        for key in ("profiles", "active_profile", "mode", "cn_route"):
            new_state[key] = copy.deepcopy(bstate[key])
        # tun / tun_suspended / mixed_port / mirror 保留当前值：它们各有冲突检查与 sudo 步骤（方案 5.1.5）。
        files: Dict[str, Optional[bytes]] = {}
        backup_nodes = os.path.join(path, "nodes")
        wanted = set()
        if os.path.isdir(backup_nodes):
            for fname in os.listdir(backup_nodes):
                with open(os.path.join(backup_nodes, fname), "rb") as fh:
                    files[os.path.join(ctx.paths.nodes, fname)] = fh.read()
                wanted.add(fname)
        if os.path.isdir(ctx.paths.nodes):
            for fname in os.listdir(ctx.paths.nodes):
                if fname.endswith(".txt") and fname not in wanted:
                    files[os.path.join(ctx.paths.nodes, fname)] = None
        backup_providers = os.path.join(path, "providers")
        if os.path.isdir(backup_providers):
            for fname in os.listdir(backup_providers):
                with open(os.path.join(backup_providers, fname), "rb") as fh:
                    files[os.path.join(ctx.paths.providers, fname)] = fh.read()
        commit(ctx, state, new_state, "backup-restore", files=files, involved=("main", "manual"))
    log.info("[reload] backup restore name=%s", name)
    return {"restored": name}


# ====================================================================
# systemd 调用的隐藏子命令（方案 5.1.7）
# ====================================================================

def service_preflight(ctx: Ctx) -> int:
    """ExecStartPre：开机 / 重启时检查 TUN 冲突，冲突则按 TUN 关闭重写配置并记下原因。

    除 state.yaml 不可读外一律返回 0：不阻止内核以端口代理方式启动。
    """
    try:
        state = load_state(ctx.paths)
    except StateError as exc:
        print("[tun] preflight state unreadable: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    conflicts: List[str] = []
    if state["tun"]:
        try:
            conflicts = ctx.platform.tun_conflicts()
        except (PlatformError, NotSupported) as exc:
            conflicts = ["probe failed: %s" % exc]
    lock = StateLock(ctx.paths, SERVICE_LOCK_TIMEOUT_SEC)
    try:
        lock.acquire()
    except LockTimeout:
        if conflicts:
            # doctor D13 从 journal 里读这一条并报失败。
            print("[tun] preflight lock timeout, conflict=%s" % "; ".join(conflicts), file=sys.stderr)
        return EXIT_OK
    try:
        state = load_state(ctx.paths)
        changed = False
        if state["tun"] and conflicts:
            reason = "; ".join(conflicts)
            if state.get("tun_suspended") != reason:
                state["tun_suspended"] = reason
                changed = True
            print("[tun] suspended reason=%s" % reason, file=sys.stderr)
        elif state.get("tun_suspended"):
            state["tun_suspended"] = None
            changed = True
            print("[tun] resumed", file=sys.stderr)
        if changed:
            save_state(ctx.paths, state)
        if changed or not os.path.exists(ctx.paths.config):
            write_config(ctx, state)
    except (StateError, OSError) as exc:
        print("[tun] preflight error: %s" % exc, file=sys.stderr)
    finally:
        lock.release()
    return EXIT_OK


def _main_pid_gone(ctx: Ctx) -> bool:
    """ExecStartPost 期间内核主进程是否已退出。

    内核因配置错误秒退时，不判断这一点 reapply 会白等满 REAPPLY_READY_SEC，每次失败启动都多卡
    一分钟，StartLimitBurst 迟迟到不了，期间 CLI 一直报“服务正在启动”。优先用 systemd 传入的
    $MAINPID；没有时（主进程在 ExecStartPost 启动前就退出等）退而查服务的 MainPID，为 0 即已退出。
    查询失败一律按仍在运行处理，最坏只是退回原来的等满。
    """
    try:
        pid = int(os.environ.get("MAINPID", ""))
    except ValueError:
        pid = 0
    if pid <= 0:
        try:
            return ctx.platform.service_status().main_pid == 0
        except Exception:  # noqa: BLE001 - 兜底判断不能让 reapply 本身失败
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def service_reapply(ctx: Ctx) -> int:
    """ExecStartPost：等 provider 加载完后把 state 的 PROXY / CN 选择下发到内核。

    内核加载 provider 后才用 cache.db 恢复选择（hub/executor/executor.go:116、:442-470），切节点接口
    不经过应用配置的锁，落在两步之间的下发会被覆盖，所以下发后等 2 秒回读，不一致重来（方案 5.1.7）。
    """
    started = time.monotonic()
    try:
        state = load_state(ctx.paths)
    except StateError as exc:
        print("[reapply] state unreadable: %s" % exc, file=sys.stderr)
        return EXIT_OK
    api = ctx.api()
    try:
        ready = False
        while time.monotonic() - started < REAPPLY_READY_SEC:
            try:
                members = api.get_proxy("PROXY").get("all", [])
                if any(m != "REJECT" for m in members):
                    ready = True
                    break
                main_names = api.provider_node_names("main") if "main" in render_mod.provider_names(state) else None
                if render_mod.expects_no_nodes(state, main_names):
                    ready = True
                    break
            except (ApiError, ApiUnavailable):
                pass
            if _main_pid_gone(ctx):
                print("[reapply] kernel exited, skip", file=sys.stderr)
                return EXIT_OK
            time.sleep(1)
        if not ready:
            print("[reapply] WARN kernel not ready after %ds" % REAPPLY_READY_SEC, file=sys.stderr)
            return EXIT_OK
        quiet = Ctx(ctx.paths, ctx.platform, ctx.python_exe)
        for attempt in range(1, REAPPLY_TRIES + 1):
            if time.monotonic() - started > REAPPLY_BUDGET_SEC:
                break
            try:
                changed = ensure_selection(quiet, api, state)
                if changed:
                    _reapply_write_back(ctx, state)
                time.sleep(REAPPLY_SETTLE_SEC)
                prof = render_mod.active_profile(state)
                want_proxy = prof.get("selected") if prof else None
                want_cn = "DIRECT" if state["cn_route"] == "direct" else "PROXY"
                now_proxy = api.get_proxy("PROXY").get("now")
                now_cn = api.get_proxy("CN").get("now")
                if (want_proxy is None or now_proxy == want_proxy) and now_cn == want_cn:
                    print("[reapply] done attempt=%d proxy=%s cn=%s" % (attempt, now_proxy, now_cn))
                    return EXIT_OK
            except (ApiError, ApiUnavailable) as exc:
                print("[reapply] attempt=%d error=%s" % (attempt, exc), file=sys.stderr)
        print("[reapply] WARN selection not consistent after retries", file=sys.stderr)
        return EXIT_OK
    finally:
        api.close()


def _reapply_write_back(ctx: Ctx, state: Dict[str, Any]) -> None:
    """补选结果写回 state.selected：只短暂拿锁（最多 5 秒），拿不到放弃写回并记 WARN。"""
    lock = StateLock(ctx.paths, SERVICE_LOCK_TIMEOUT_SEC)
    try:
        lock.acquire()
    except LockTimeout:
        print("[reapply] WARN lock busy, selection not written back", file=sys.stderr)
        return
    try:
        current = load_state(ctx.paths)
        name = state.get("active_profile")
        if name and current.get("active_profile") == name:
            current["profiles"][name]["selected"] = state["profiles"][name]["selected"]
            save_state(ctx.paths, current)
    except (StateError, OSError) as exc:
        print("[reapply] WARN write back failed: %s" % exc, file=sys.stderr)
    finally:
        lock.release()


def tun_dns_apply(ctx: Ctx) -> int:
    """root 身份：TUN 生效时接管系统 DNS，否则还原。只读 state、不加锁、不写主目录（方案 5.1.7）。"""
    from .platform.linux import wait_port

    try:
        state = load_state(ctx.paths)
    except StateError as exc:
        print("[tun] dns-apply state unreadable: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    try:
        main_pid = ctx.platform.service_status().main_pid
        effective = render_mod.tun_effective_in_config(state) and main_pid != 0
        if not effective:
            # 不会留下指向一个没人监听的 127.0.0.1:53 的全局配置。
            ctx.platform.dns_revert()
            print("[tun] dns-apply tun not effective, reverted")
            return EXIT_OK
        deadline = time.monotonic() + DNS_APPLY_WAIT_SEC
        while time.monotonic() < deadline and not ctx.platform.tun_device_exists(render_mod.TUN_DEVICE):
            time.sleep(0.5)
        remaining = max(0.5, deadline - time.monotonic())
        if not ctx.platform.tun_device_exists(render_mod.TUN_DEVICE) or not wait_port("127.0.0.1", 53, remaining):
            print("[tun] dns-apply ERROR device or 127.0.0.1:53 not ready", file=sys.stderr)
            return EXIT_ERROR
        iface = ctx.platform.dns_takeover()
        print("[tun] dns-apply takeover iface=%s" % iface)
        return EXIT_OK
    except (PlatformError, OSError) as exc:
        print("[tun] dns-apply ERROR %s" % exc, file=sys.stderr)
        return EXIT_ERROR


def tun_dns_revert(ctx: Ctx) -> int:
    """root 身份：一律还原 DNS 接管，不看 state.tun。"""
    if os.environ.get(TEST_ENV_DNS_REVERT) == "1":
        print("[tun] dns-revert forced failure (test)", file=sys.stderr)
        return EXIT_ERROR
    try:
        did = ctx.platform.dns_revert()
        print("[tun] dns-revert %s" % ("done" if did else "nothing to do"))
        return EXIT_OK
    except (PlatformError, OSError) as exc:
        print("[tun] dns-revert ERROR %s" % exc, file=sys.stderr)
        return EXIT_ERROR
