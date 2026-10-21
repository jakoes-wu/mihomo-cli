"""Linux 实现：systemd 服务、TUN 冲突探测、systemd-resolved 的 DNS 接管（方案 5.1.7、5.1.8）。"""

from __future__ import annotations

import json
import os
import platform as py_platform
import shlex
import socket
import subprocess
import tempfile
import time
from typing import Dict, List, Optional, Set, Tuple

from .base import NotSupported, Platform, PlatformError, ServiceStatus

UNIT_NAME = "mihomo-cli"
UNIT_PATH = "/etc/systemd/system/mihomo-cli.service"
TUN_DEVICE = "mihomocli0"
# drop-in 放在每次开机都清空的 /run 下：断电或卸载残留不会延续到下次开机（方案 5.1.8）。
RESOLVED_DROPIN_DIR = "/run/systemd/resolved.conf.d"
RESOLVED_DROPIN = RESOLVED_DROPIN_DIR + "/mihomo-cli.conf"
DNS_MARKER_DIR = "/run/mihomo-cli"
DNS_MARKER = DNS_MARKER_DIR + "/dns.state"
# 先写空的 DNS= 清空主配置与其它 drop-in 的全局 DNS 列表（列表是追加语义），
# 否则 resolved 可能优先用原有服务器而绕过 mihomo（方案 5.1.8）。
RESOLVED_DROPIN_TEXT = "[Resolve]\nDNS=\nDNS=127.0.0.1\nDomains=~.\n"
# 进程可执行文件名含这些字样即视为另一个代理内核（方案 5.1.7 other_tun_instances）。
KERNEL_NAME_MARKERS = ("mihomo", "clash", "sing-box")
# systemctl / resolvectl 单次调用的超时，秒；_tun-dns-apply 总预算见方案 5.1.7「启动耗时预算」。
CMD_TIMEOUT_SEC = 15


def _run(args: List[str], timeout: float = CMD_TIMEOUT_SEC, check: bool = True,
         capture: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.PIPE if capture else None,
                              text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise PlatformError("%s: command not found" % args[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise PlatformError("%s: timed out after %ss" % (" ".join(args), timeout)) from exc
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300] if capture else ""
        raise PlatformError("%s: exit %d %s" % (" ".join(args), proc.returncode, err))
    return proc


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


class LinuxPlatform(Platform):
    name = "linux"

    # ---------- 内核资产 ----------
    def core_asset_name(self, version: str) -> str:
        machine = py_platform.machine().lower()
        # 资产名按 v1.19.0 / v1.19.11 / v1.19.21 发布页核对（方案 5.1.7）：amd64 取 compatible
        # （不要求 AVX 等新指令，老 CPU 也能跑）。不能用 amd64-v1-：它从 v1.19.12 才出现，
        # 装 1.19.0～1.19.11 会 404。
        if machine in ("x86_64", "amd64"):
            return "mihomo-linux-amd64-compatible-%s.gz" % version
        if machine in ("aarch64", "arm64"):
            return "mihomo-linux-arm64-%s.gz" % version
        raise NotSupported("unsupported CPU architecture: %s" % machine)

    # ---------- systemd ----------
    def unit_text(self, home: str, user: str, python_exe: str) -> str:
        # 单元里的“mihomo-cli 绝对路径”一律展开为 <python> -m mihomo_cli，并显式传 --home：
        # + 前缀行（root 身份）的 $HOME 手册未写明，不能依赖 ~ 解析（方案 5.1.7）。
        self_cmd = "%s -m mihomo_cli --home %s" % (python_exe, home)
        return (
            "[Unit]\n"
            "Description=mihomo-cli managed mihomo kernel\n"
            "After=network-online.target mihomo-vps.service mihomo.service\n"
            "Wants=network-online.target\n"
            "StartLimitIntervalSec=300\n"
            "StartLimitBurst=5\n"
            "\n"
            "[Service]\n"
            "User=%(user)s\n"
            "Environment=MIHOMO_CLI_HOME=%(home)s\n"
            "ExecStartPre=%(self)s _service-preflight\n"
            "ExecStart=%(home)s/bin/mihomo -d %(home)s\n"
            "ExecStartPost=-%(self)s _service-reapply\n"
            "ExecStartPost=-+%(self)s _tun-dns-apply\n"
            "ExecStopPost=-+%(self)s _tun-dns-revert\n"
            "AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW\n"
            "CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "TimeoutStartSec=180\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        ) % {"user": user, "home": home, "self": self_cmd}

    def installed_unit_text(self) -> Optional[str]:
        return _read(UNIT_PATH)

    def install_service(self, home: str, user: str, python_exe: str) -> None:
        fd, tmp = tempfile.mkstemp(prefix="mihomo-cli.", suffix=".service")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.unit_text(home, user, python_exe))
            _run(["sudo", "install", "-m", "0644", tmp, UNIT_PATH], check=True, capture=False)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        _run(["sudo", "systemctl", "daemon-reload"], capture=False)
        # enable --now 会同步等 ExecStartPost 跑完（最长 TimeoutStartSec=180）。
        _run(["sudo", "systemctl", "enable", "--now", UNIT_NAME], timeout=200, capture=False)

    def uninstall_service(self) -> None:
        if os.path.exists(UNIT_PATH):
            _run(["sudo", "systemctl", "disable", "--now", UNIT_NAME], timeout=120, check=False, capture=False)
            _run(["sudo", "rm", "-f", UNIT_PATH], capture=False)
        _run(["sudo", "systemctl", "daemon-reload"], capture=False)

    def service_action(self, action: str) -> None:
        if action not in ("start", "stop", "restart"):
            raise ValueError(action)
        _run(["sudo", "systemctl", action, UNIT_NAME], timeout=200, capture=False)

    def service_status(self) -> ServiceStatus:
        try:
            proc = _run(["systemctl", "show", "-p", "LoadState,ActiveState,SubState,MainPID,NRestarts,UnitFileState",
                         UNIT_NAME], check=False)
        except PlatformError:
            return ServiceStatus()
        kv: Dict[str, str] = {}
        for line in (proc.stdout or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        return parse_service_status(kv)

    def service_logs(self, follow: bool, lines: int = 200) -> int:
        args = ["journalctl", "-u", UNIT_NAME, "-n", str(lines), "--no-pager"]
        if follow:
            args.append("-f")
        try:
            return subprocess.call(args)
        except FileNotFoundError:
            raise PlatformError("journalctl: command not found") from None
        except KeyboardInterrupt:
            return 0

    def service_journal(self, lines: int = 200) -> str:
        try:
            proc = _run(["journalctl", "-u", UNIT_NAME, "-b", "-n", str(lines), "--no-pager", "-o", "cat"],
                        check=False)
        except PlatformError:
            return ""
        return proc.stdout or ""

    def main_pid(self) -> int:
        return self.service_status().main_pid

    # ---------- sudo ----------
    def sudo_validate(self) -> bool:
        try:
            return subprocess.call(["sudo", "-v"]) == 0
        except FileNotFoundError:
            return False

    def run_self_as_root(self, home: str, python_exe: str, subcommand: str, preserve_env: List[str]) -> int:
        args = ["sudo"]
        if preserve_env:
            args.append("--preserve-env=" + ",".join(preserve_env))
        args += [python_exe, "-m", "mihomo_cli", "--home", home, subcommand]
        try:
            return subprocess.call(args, timeout=60)
        except subprocess.TimeoutExpired:
            return 1
        except FileNotFoundError:
            return 1

    # ---------- 冲突探测 ----------
    def other_tun_instances(self) -> List[str]:
        found = []
        for dev in self._tun_devices():
            if dev != TUN_DEVICE:
                found.append("tun device %s" % dev)
        exclude = self._self_pids()
        main_pid = self.main_pid()
        if main_pid:
            # 只排除本服务 MainPID 这一个进程，不按可执行文件路径排除（同一路径的其它进程照样算冲突）。
            exclude.add(main_pid)
        for pid, exe_name in iter_processes():
            if pid in exclude:
                continue
            low = exe_name.lower()
            if any(marker in low for marker in KERNEL_NAME_MARKERS):
                found.append("process %s (pid %d)" % (exe_name, pid))
        return found

    def _tun_devices(self) -> List[str]:
        try:
            proc = _run(["ip", "-j", "link", "show", "type", "tun"], check=False)
            data = json.loads(proc.stdout or "[]")
            return [d.get("ifname", "") for d in data if d.get("ifname")]
        except (PlatformError, ValueError):
            return []

    def _self_pids(self) -> Set[int]:
        """当前 CLI 进程及其父进程链：以 mihomo-cli 为名的入口不能被算成冲突。"""
        pids = set()
        pid = os.getpid()
        while pid > 1 and pid not in pids:
            pids.add(pid)
            stat = _read("/proc/%d/stat" % pid)
            if not stat:
                break
            try:
                pid = int(stat.rsplit(")", 1)[1].split()[1])
            except (IndexError, ValueError):
                break
        return pids

    def listening_conflicts(self, port: int, host: str = "127.0.0.1", own_listener: bool = False) -> List[str]:
        # own_listener：调用方已经通过本实例管理接口确认内核自己在听这个端口。内核带
        # AmbientCapabilities 运行，同一用户读 /proc/<pid>/fd 的链接也会被拒（ptrace 权限检查），
        # 看不到属主的监听只能靠这个旁证排除，否则重跑 install / doctor 会把自己误报成占用。
        # 只排除 uid 等于当前用户的那条：内核以安装用户身份运行，root 或其它用户的监听照常算占用。
        sockets = listening_sockets(port, host)
        if not sockets:
            return []
        main_pid = self.main_pid()
        owner = inode_owners({inode for inode, _ in sockets})
        my_uid = os.getuid()
        out = []
        for inode, uid in sockets:
            pid = owner.get(inode)
            if pid is not None and pid == main_pid and main_pid:
                continue
            if pid is None:
                if own_listener and uid == my_uid:
                    continue
                out.append("port %d in use (process not visible)" % port)
            else:
                out.append("port %d in use by %s (pid %d)" % (port, process_name(pid), pid))
        return out

    def tun_device_exists(self, name: str) -> bool:
        return os.path.exists("/sys/class/net/%s" % name)

    def tun_supported(self) -> Optional[str]:
        if not os.path.exists("/dev/net/tun"):
            return "/dev/net/tun not found"
        return None

    # ---------- DNS 接管（root） ----------
    def default_iface(self) -> Optional[str]:
        try:
            proc = _run(["ip", "-j", "route", "show", "default"], check=False)
            routes = json.loads(proc.stdout or "[]")
        except (PlatformError, ValueError):
            return None
        for route in routes:
            if route.get("dev"):
                return route["dev"]
        return None

    def dns_takeover(self) -> str:
        iface = self.default_iface()
        if not iface:
            raise PlatformError("no default route interface")
        # 先写还原标记、再动任何 DNS 设置：后面任一步失败（或进程被杀），dns_revert 仍能凭标记
        # 对这块网卡执行 revert；若标记写在最后，部分成功时网卡 DNS 会一直指向 127.0.0.1，
        # 关掉 TUN 后本机解析全断。对没真正改过的网卡执行 revert 没有副作用。
        os.makedirs(DNS_MARKER_DIR, mode=0o755, exist_ok=True)
        tmp = DNS_MARKER + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("iface=%s\n" % iface)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, DNS_MARKER)
        os.makedirs(RESOLVED_DROPIN_DIR, exist_ok=True)
        with open(RESOLVED_DROPIN, "w", encoding="utf-8") as fh:
            fh.write(RESOLVED_DROPIN_TEXT)
        os.chmod(RESOLVED_DROPIN, 0o644)
        _run(["systemctl", "restart", "systemd-resolved"])
        # 网卡级设置放在重启 resolved 之后，避免重启后网卡级状态不确定（方案 5.1.8）。
        _run(["resolvectl", "dns", iface, "127.0.0.1"])
        _run(["resolvectl", "domain", iface, "~."])
        return iface

    def dns_revert(self) -> bool:
        marker_iface = read_marker_iface()
        did = False
        if marker_iface:
            # 还原标记里记录的那块网卡，即使此后默认路由已换了网卡。
            _run(["resolvectl", "revert", marker_iface], check=False)
            did = True
        if os.path.exists(RESOLVED_DROPIN):
            os.unlink(RESOLVED_DROPIN)
            _run(["systemctl", "restart", "systemd-resolved"])
            did = True
        if os.path.exists(DNS_MARKER):
            os.unlink(DNS_MARKER)
            did = True
        # 标记与 drop-in 都不存在时什么也不做：不对没接管过的网卡执行 revert，以免清掉网络管理器下发的设置。
        return did

    def dns_status(self) -> dict:
        iface = self.default_iface()
        iface_dns: List[str] = []
        if iface:
            try:
                proc = _run(["resolvectl", "dns", iface], check=False)
                # 输出形如 "Link 2 (eth0): 127.0.0.1 1.1.1.1"
                tail = (proc.stdout or "").split(":", 1)[1] if ":" in (proc.stdout or "") else ""
                iface_dns = tail.split()
            except PlatformError:
                pass
        return {
            "dropin": os.path.exists(RESOLVED_DROPIN),
            "marker_iface": read_marker_iface(),
            "default_iface": iface,
            "iface_dns": iface_dns,
        }


def wait_port(host: str, port: int, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def read_marker_iface() -> Optional[str]:
    text = _read(DNS_MARKER)
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("iface="):
            return line.split("=", 1)[1].strip() or None
    return None


def parse_service_status(kv: Dict[str, str]) -> ServiceStatus:
    def _int(v: str) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    return ServiceStatus(
        load_state=kv.get("LoadState", "not-found") or "not-found",
        active_state=kv.get("ActiveState", "inactive") or "inactive",
        sub_state=kv.get("SubState", ""),
        main_pid=_int(kv.get("MainPID", "0")),
        n_restarts=_int(kv.get("NRestarts", "0")),
        unit_file_state=kv.get("UnitFileState", ""),
    )


def process_name(pid: int, proc_root: str = "/proc") -> str:
    """可执行文件名：优先 /proc/<pid>/exe 的目标文件名。

    不用 comm：以 mihomo-cli 为名的 Python 入口的 comm 就是 mihomo-cli（方案 5.1.7）。
    其它用户（如 root 跑的 mihomo-vps）的 exe 普通用户读不到，此时退回 cmdline 第 0 项的文件名——
    Python 入口的第 0 项是解释器路径，同样不会被误判。
    """
    try:
        target = os.readlink("%s/%d/exe" % (proc_root, pid))
        name = os.path.basename(target)
        if name.endswith(" (deleted)"):
            name = name[: -len(" (deleted)")]
        return name
    except OSError:
        pass
    cmdline = _read("%s/%d/cmdline" % (proc_root, pid)) or ""
    argv0 = cmdline.split("\0", 1)[0]
    return os.path.basename(argv0) if argv0 else ""


def iter_processes(proc_root: str = "/proc"):
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        name = process_name(pid, proc_root)
        if name:
            yield pid, name


def _hex_addr_matches(hex_addr: str, host: str) -> bool:
    """/proc/net/tcp* 的本地地址是否与 host 冲突：通配地址与回环地址都算。"""
    wildcard4, loop4 = "00000000", "0100007F"
    wildcard6 = "0" * 32
    loop6 = "00000000000000000000000001000000"
    mapped_loop6 = "0000000000000000FFFF00000100007F"
    hex_addr = hex_addr.upper()
    if host in ("127.0.0.1", "localhost"):
        return hex_addr in (wildcard4, loop4, wildcard6, loop6, mapped_loop6)
    return True


def listening_inodes(port: int, host: str = "127.0.0.1", proc_root: str = "/proc") -> List[str]:
    """读 /proc/net/tcp 与 tcp6，返回在该端口 LISTEN（状态 0A）的 socket inode。"""
    return [inode for inode, _ in listening_sockets(port, host, proc_root)]


def listening_sockets(port: int, host: str = "127.0.0.1", proc_root: str = "/proc") -> List[Tuple[str, int]]:
    """同 listening_inodes，但连同监听者 uid（第 8 列）一起返回。

    uid 不受 fd 读权限限制，属主进程看不到时可用它区分“我自己的”和“别人的”监听。
    """
    out = []
    for fname in ("tcp", "tcp6"):
        text = _read("%s/net/%s" % (proc_root, fname))
        if not text:
            continue
        for line in text.splitlines()[1:]:
            cols = line.split()
            if len(cols) < 10 or cols[3] != "0A":
                continue
            addr, _, port_hex = cols[1].rpartition(":")
            try:
                if int(port_hex, 16) != port:
                    continue
            except ValueError:
                continue
            if _hex_addr_matches(addr, host):
                try:
                    uid = int(cols[7])
                except ValueError:
                    uid = -1
                out.append((cols[9], uid))
    return out


def inode_owners(inodes: Set[str], proc_root: str = "/proc") -> Dict[str, int]:
    """socket inode → pid。只能看到有权限读其 fd 目录的进程；看不到的不出现在结果里。"""
    owners: Dict[str, int] = {}
    if not inodes:
        return owners
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return owners
    targets = {"socket:[%s]" % i: i for i in inodes}
    for entry in entries:
        if not entry.isdigit():
            continue
        fd_dir = "%s/%s/fd" % (proc_root, entry)
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if link in targets:
                owners[targets[link]] = int(entry)
    return owners


def quote_cmd(args: List[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)
