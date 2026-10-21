"""Linux 系统层的纯函数：systemd 状态解析、进程名判定、监听端口解析（伪造 /proc）。"""

import os

from mihomo_cli.platform import linux


def test_parse_service_status():
    st = linux.parse_service_status({"LoadState": "loaded", "ActiveState": "activating", "MainPID": "0",
                                     "NRestarts": "x", "SubState": "start-pre"})
    assert st.load_state == "loaded" and st.active_state == "activating"
    assert st.main_pid == 0 and st.n_restarts == 0
    empty = linux.parse_service_status({})
    assert empty.load_state == "not-found" and empty.active_state == "inactive"


def _proc(tmp_path, pid, exe=None, cmdline=None):
    d = tmp_path / str(pid)
    d.mkdir()
    if exe:
        os.symlink(exe, d / "exe")
    if cmdline is not None:
        (d / "cmdline").write_bytes(cmdline.encode())
    return d


def test_process_name(tmp_path):
    _proc(tmp_path, 10, exe="/usr/local/bin/mihomo")
    _proc(tmp_path, 11, exe="/usr/bin/python3.8", cmdline="/usr/bin/python3\0/home/u/.local/bin/mihomo-cli\0")
    _proc(tmp_path, 12, cmdline="/usr/local/bin/mihomo\0-d\0/etc/mihomo-vps\0")  # 读不到 exe（他人进程）
    _proc(tmp_path, 13, exe="/opt/mihomo (deleted)")
    root = str(tmp_path)
    assert linux.process_name(10, root) == "mihomo"
    assert linux.process_name(11, root) == "python3.8"  # Python 入口不算冲突
    assert linux.process_name(12, root) == "mihomo"
    assert linux.process_name(13, root) == "mihomo"
    assert linux.process_name(99, root) == ""
    names = dict(linux.iter_processes(root))
    assert names[10] == "mihomo" and names[11] == "python3.8"


TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def _row(addr, st, inode, uid=1000):
    return "   0: %s 00000000:0000 %s 00000000:00000000 00:00000000 00000000  %d        0 %s 1\n" % (addr, st, uid, inode)


def test_listening_inodes(tmp_path):
    net = tmp_path / "net"
    net.mkdir()
    (net / "tcp").write_text(TCP_HEADER
                             + _row("0100007F:0035", "0A", "111")    # 127.0.0.1:53 LISTEN
                             + _row("3500007F:0035", "0A", "222")    # 127.0.0.53:53 → 不与 127.0.0.1 冲突
                             + _row("00000000:1ED9", "0A", "333")    # 0.0.0.0:7897
                             + _row("0100007F:0035", "01", "444"))   # 非 LISTEN
    (net / "tcp6").write_text(TCP_HEADER + _row("00000000000000000000000001000000:0035", "0A", "555"))
    root = str(tmp_path)
    assert sorted(linux.listening_inodes(53, "127.0.0.1", root)) == ["111", "555"]
    assert linux.listening_inodes(7897, "127.0.0.1", root) == ["333"]
    assert linux.listening_inodes(7890, "127.0.0.1", root) == []
    assert linux.listening_sockets(7897, "127.0.0.1", root) == [("333", 1000)]


def test_inode_owners(tmp_path):
    fd = tmp_path / "42" / "fd"
    fd.mkdir(parents=True)
    os.symlink("socket:[111]", fd / "3")
    os.symlink("/dev/null", fd / "0")
    assert linux.inode_owners({"111", "999"}, str(tmp_path)) == {"111": 42}
    assert linux.inode_owners(set(), str(tmp_path)) == {}


def test_unit_text_shape():
    text = linux.LinuxPlatform().unit_text("/home/u/.local/share/mihomo-cli", "u", "/home/u/.venv/bin/python")
    assert "User=u" in text
    assert "_service-preflight" in text and "_service-reapply" in text
    assert "TimeoutStartSec=180" in text
    assert "AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE" in text


def test_dns_takeover_partial_failure_still_reverts(tmp_path, monkeypatch):
    """还原标记先于任何 DNS 改动写入：resolvectl domain 失败后，revert 仍要还原这块网卡。"""
    monkeypatch.setattr(linux, "RESOLVED_DROPIN_DIR", str(tmp_path / "conf.d"))
    monkeypatch.setattr(linux, "RESOLVED_DROPIN", str(tmp_path / "conf.d" / "mihomo-cli.conf"))
    monkeypatch.setattr(linux, "DNS_MARKER_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(linux, "DNS_MARKER", str(tmp_path / "run" / "dns.state"))
    calls = []

    def fake_run(args, timeout=None, check=True, **_kw):
        calls.append(args)
        if args[:2] == ["resolvectl", "domain"]:
            raise linux.PlatformError("boom")

    monkeypatch.setattr(linux, "_run", fake_run)
    plat = linux.LinuxPlatform()
    monkeypatch.setattr(plat, "default_iface", lambda: "eth0")
    try:
        plat.dns_takeover()
    except linux.PlatformError:
        pass
    assert linux.read_marker_iface() == "eth0"
    calls.clear()
    assert plat.dns_revert() is True
    assert ["resolvectl", "revert", "eth0"] in calls
    assert not os.path.exists(linux.DNS_MARKER)


def test_core_asset_name_covers_min_version(monkeypatch):
    # amd64-v1- 前缀从 v1.19.12 才有，最低支持版本 1.19.0 只有 compatible 资产（发布页核对）。
    import platform as py_platform
    monkeypatch.setattr(py_platform, "machine", lambda: "x86_64")
    assert linux.LinuxPlatform().core_asset_name("v1.19.0") == "mihomo-linux-amd64-compatible-v1.19.0.gz"
    monkeypatch.setattr(py_platform, "machine", lambda: "aarch64")
    assert linux.LinuxPlatform().core_asset_name("v1.19.21") == "mihomo-linux-arm64-v1.19.21.gz"


def test_listening_conflicts_own_listener(monkeypatch):
    """属主不可见的监听：默认算占用；own_listener 时只排除当前用户 uid 的那条，
    其它 uid（如 root）的不可见监听和可见的他人进程始终算占用。"""
    plat = linux.LinuxPlatform.__new__(linux.LinuxPlatform)
    me = os.getuid()
    monkeypatch.setattr(linux, "listening_sockets",
                        lambda port, host: [("111", me), ("222", me), ("333", me + 1)])
    monkeypatch.setattr(linux, "inode_owners", lambda inodes: {"222": 77})
    monkeypatch.setattr(linux, "process_name", lambda pid: "nginx")
    monkeypatch.setattr(plat, "main_pid", lambda: 0)
    hidden = "port 7897 in use (process not visible)"
    nginx = "port 7897 in use by nginx (pid 77)"
    assert plat.listening_conflicts(7897) == [hidden, nginx, hidden]
    assert plat.listening_conflicts(7897, own_listener=True) == [nginx, hidden]
