"""state.yaml 的读写、原子替换与文件锁。

state.yaml 是 mihomo-cli 的唯一权威状态（方案 5.1.2）：内核的 cache.db 只是缓存，
两者不一致时以这里为准。本模块只负责“怎么安全地读写”，不负责业务含义。
"""

from __future__ import annotations

import copy
import errno
import fcntl
import os
import re
import tempfile
import time
from typing import Any, Dict, Optional

import yaml

STATE_VERSION = 1
DEFAULT_PORT = 7897
DEFAULT_INTERVAL_SEC = 86400

# 代理服务名会出现在文件名（providers/<名>.yaml、nodes/<名>.txt）里，只允许安全字符。
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# CLI 事务持锁期间，其它 CLI 命令最多等 130 秒（覆盖一次 120 秒的 PUT /configs，方案 5.1.2）。
CLI_LOCK_TIMEOUT_SEC = 130.0
# systemd 的 preflight / reapply 拿锁最多等 5 秒，拿不到按各自规则放弃（方案 5.1.2）。
SERVICE_LOCK_TIMEOUT_SEC = 5.0


class StateError(Exception):
    """state.yaml 无法读取或内容不合法。"""


class LockTimeout(Exception):
    """在限定时间内没拿到 state.lock，说明另一个命令正在执行。"""


class Paths:
    """主目录下各文件的路径。主目录同时是 mihomo 的 -d 目录（方案 5.1.2）。"""

    def __init__(self, home: str):
        self.home = os.path.abspath(os.path.expanduser(home))
        self.state = os.path.join(self.home, "state.yaml")
        self.lock = os.path.join(self.home, "state.lock")
        self.config = os.path.join(self.home, "config.yaml")
        self.sock = os.path.join(self.home, "mihomo.sock")
        self.bin_dir = os.path.join(self.home, "bin")
        self.core = os.path.join(self.bin_dir, "mihomo")
        self.providers = os.path.join(self.home, "providers")
        self.nodes = os.path.join(self.home, "nodes")
        self.staging = os.path.join(self.home, "staging")
        self.backups = os.path.join(self.home, "backups")
        self.log = os.path.join(self.home, "mihomo-cli.log")

    def provider_file(self, profile: str) -> str:
        return os.path.join(self.providers, profile + ".yaml")

    def nodes_file(self, profile: str) -> str:
        return os.path.join(self.nodes, profile + ".txt")


def resolve_home(cli_home: Optional[str]) -> str:
    """主目录优先级：--home > MIHOMO_CLI_HOME > ~/.local/share/mihomo-cli。"""
    if cli_home:
        return os.path.abspath(os.path.expanduser(cli_home))
    env_home = os.environ.get("MIHOMO_CLI_HOME")
    if env_home:
        return os.path.abspath(os.path.expanduser(env_home))
    return os.path.abspath(os.path.expanduser("~/.local/share/mihomo-cli"))


def default_state() -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "mixed_port": DEFAULT_PORT,
        "active_profile": None,
        "mode": "rule",
        "cn_route": "direct",
        "tun": False,
        "mirror": None,
        "profiles": {},
        # 开机时因冲突暂停 TUN 的原因；None 表示未暂停（方案 5.1.7）。
        "tun_suspended": None,
    }


def new_profile(kind: str, url: Optional[str] = None, file: Optional[str] = None,
                interval: int = DEFAULT_INTERVAL_SEC, user_agent: Optional[str] = None) -> Dict[str, Any]:
    return {
        "kind": kind,  # subscription | file | nodes
        "url": url,
        "file": file,
        "interval": interval,
        "user_agent": user_agent,
        "hidden": [],
        # 每项 {link, name}；name 只在重载核验通过后从接口回读，未经重载时为 None（方案 5.1.5 第 5 步）。
        "manual": [],
        "selected": None,
    }


def _validate(state: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(state, dict):
        raise StateError("state.yaml is not a mapping")
    version = state.get("version")
    if version != STATE_VERSION:
        # 结构升级时在这里做迁移；首版只有 version 1。
        raise StateError("unsupported state version: %r" % (version,))
    merged = default_state()
    merged.update(state)
    if merged["mode"] not in ("global", "rule", "direct"):
        raise StateError("invalid mode: %r" % (merged["mode"],))
    if merged["cn_route"] not in ("direct", "proxy"):
        raise StateError("invalid cn_route: %r" % (merged["cn_route"],))
    if not isinstance(merged["profiles"], dict):
        raise StateError("profiles is not a mapping")
    for name, prof in merged["profiles"].items():
        if not PROFILE_NAME_RE.match(str(name)):
            raise StateError("invalid profile name: %r" % (name,))
        base = new_profile(prof.get("kind", "nodes"))
        base.update(prof)
        if base["kind"] not in ("subscription", "file", "nodes"):
            raise StateError("invalid profile kind: %r" % (base["kind"],))
        base["manual"] = [{"link": m.get("link"), "name": m.get("name")} for m in (base["manual"] or [])]
        base["hidden"] = list(base["hidden"] or [])
        merged["profiles"][name] = base
    if merged["active_profile"] is not None and merged["active_profile"] not in merged["profiles"]:
        raise StateError("active_profile %r not in profiles" % (merged["active_profile"],))
    return merged


def parse_state(text: str) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StateError("state.yaml is not valid YAML: %s" % exc) from exc
    return _validate(data)


def load_state(paths: Paths) -> Dict[str, Any]:
    """读 state.yaml；不存在时返回默认状态（尚未写盘）。只读，不加锁——调用方按需自己持锁。"""
    try:
        with open(paths.state, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return default_state()
    except OSError as exc:
        raise StateError("cannot read %s: %s" % (paths.state, exc)) from exc
    return parse_state(text)


def dump_state(state: Dict[str, Any]) -> str:
    return yaml.safe_dump(state, allow_unicode=True, sort_keys=False, default_flow_style=False)


def write_atomic(path: str, text: str, mode: int = 0o600) -> None:
    """写临时文件 → fsync → os.replace。读者要么看到旧文件，要么看到完整的新文件。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state(paths: Paths, state: Dict[str, Any]) -> None:
    write_atomic(paths.state, dump_state(_validate(copy.deepcopy(state))))


class StateLock:
    """state.lock 上的排他文件锁。

    CLI 的重载事务从读 state 到提交 / 回滚结束全程持有它；调用 systemctl start/restart/stop
    之前必须先释放，否则 ExecStartPre（_service-preflight）拿不到锁（方案 5.1.2）。
    root 身份的子命令不得使用本锁：创建锁文件会在用户目录留下 root 属主文件（方案 5.1.7）。
    """

    def __init__(self, paths: Paths, timeout_sec: float = CLI_LOCK_TIMEOUT_SEC, poll_sec: float = 0.2):
        self.paths = paths
        self.timeout_sec = timeout_sec
        self.poll_sec = poll_sec
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        os.makedirs(self.paths.home, mode=0o700, exist_ok=True)
        fd = os.open(self.paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout_sec
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    os.close(fd)
                    raise
            if time.monotonic() >= deadline:
                os.close(fd)
                raise LockTimeout(self.paths.lock)
            time.sleep(self.poll_sec)

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "StateLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def ensure_home(paths: Paths) -> None:
    """创建主目录并设为 0700。mihomo 把套接字固定设为 0666（hub/route/server.go:277），隔离只能靠目录权限。"""
    os.makedirs(paths.home, mode=0o700, exist_ok=True)
    os.chmod(paths.home, 0o700)
    for sub in (paths.bin_dir, paths.providers, paths.nodes, paths.backups):
        os.makedirs(sub, mode=0o700, exist_ok=True)


def home_mode_ok(paths: Paths) -> Optional[bool]:
    """主目录权限是否为 0700；目录不存在返回 None。"""
    try:
        st = os.stat(paths.home)
    except FileNotFoundError:
        return None
    return (st.st_mode & 0o077) == 0
