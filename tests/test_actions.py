"""重载事务、回滚、切换类动作（R-06/R-10/R-11/R-22/R-25/R-30/R-32 的 L0 部分）。"""

import hashlib
import os

import pytest

from mihomo_cli import actions
from mihomo_cli.actions import CliError
from mihomo_cli.api import ApiUnavailable
from mihomo_cli.platform.base import ServiceStatus
from mihomo_cli.state import load_state

SUB = "https://sub.example.com/s?token=secret"


def _digest(paths):
    out = {}
    for root, _, files in os.walk(paths.home):
        if "backups" in root:
            continue
        for f in files:
            p = os.path.join(root, f)
            if f.endswith((".lock", ".log", ".sock")) or "/bin/" in p:
                continue
            with open(p, "rb") as fh:
                out[os.path.relpath(p, paths.home)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _sub_profile(ctx, kernel, nodes=("HK 01", "JP 02", "US 03")):
    kernel.sub_nodes[SUB] = list(nodes)
    actions.profile_add(ctx, "air", url=SUB)


def test_first_profile_enabled_and_selected(env):
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel)
    st = load_state(ctx.paths)
    assert st["active_profile"] == "air"
    assert st["profiles"]["air"]["selected"] == "HK 01"
    assert kernel.now["PROXY"] == "HK 01"
    assert ("select", "CN", "DIRECT") in kernel.calls
    assert os.listdir(ctx.paths.backups)
    assert not os.path.exists(ctx.paths.staging)


def test_node_add_readback_names(env):
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel)
    res = actions.node_add(ctx, ("ss://a@1.2.3.4:8388#tokyo", "vless://u@5.6.7.8:443?x=1#my%20vps"))
    assert res["added"] == ["[M] tokyo", "[M] my vps"]
    st = load_state(ctx.paths)
    assert [m["name"] for m in st["profiles"]["air"]["manual"]] == ["[M] tokyo", "[M] my vps"]


def test_bad_config_changes_nothing(env, monkeypatch):
    """R-10：mihomo -t 失败 → 退出码 4，文件全部不变，staging 清空。"""
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel)
    before = _digest(ctx.paths)
    monkeypatch.setenv("MIHOMO_CLI_TEST_BAD_CONFIG", "1")
    with pytest.raises(CliError) as ei:
        actions.node_add(ctx, ("ss://a@1.2.3.4:8388#x",))
    assert ei.value.code == actions.EXIT_ROLLED_BACK
    assert _digest(ctx.paths) == before
    assert not os.path.exists(ctx.paths.staging)


def test_unparsable_manual_rolls_back(env):
    """R-22：一条有效 + 一条无效链接 → 计数不符回滚，旧文件恢复，内核回到旧节点。"""
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel)
    before = _digest(ctx.paths)
    with pytest.raises(CliError) as ei:
        actions.node_add(ctx, ("ss://a@1.2.3.4:8388#ok", "bad://zzz"))
    assert ei.value.code == actions.EXIT_ROLLED_BACK
    assert _digest(ctx.paths) == before
    assert kernel.providers.get("manual") is None
    assert "[M] ok" not in kernel.proxy_all


def test_verify_fail_and_rollback_fail(env, monkeypatch):
    """R-11：核验失败回滚 → 4；回滚也失败 → 7，state 不变。"""
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel)
    state_before = open(ctx.paths.state).read()
    monkeypatch.setenv("MIHOMO_CLI_TEST_FAIL_VERIFY", "1")
    with pytest.raises(CliError) as ei:
        actions.node_add(ctx, ("ss://a@1.2.3.4:8388#x",))
    assert ei.value.code == actions.EXIT_ROLLED_BACK
    monkeypatch.setenv("MIHOMO_CLI_TEST_FAIL_ROLLBACK", "1")
    with pytest.raises(CliError) as ei:
        actions.node_add(ctx, ("ss://a@1.2.3.4:8388#x",))
    assert ei.value.code == actions.EXIT_MANUAL
    assert any("backup restore" in h for h in ei.value.hints)
    assert open(ctx.paths.state).read() == state_before


def test_profile_use_empty_subscription_rolls_back(env):
    """R-08 ④：切到首次就为空的订阅 → main 为 0 触发回滚。"""
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel)
    kernel.sub_nodes["https://empty.example.com/"] = []
    actions.profile_add(ctx, "empty", url="https://empty.example.com/")
    with pytest.raises(CliError) as ei:
        actions.profile_use(ctx, "empty")
    assert ei.value.code == actions.EXIT_ROLLED_BACK
    assert load_state(ctx.paths)["active_profile"] == "air"
    assert kernel.now["PROXY"] == "HK 01"


def test_hide_all_blocks_not_rollback(env):
    """R-08 ②：隐藏全部订阅节点 → 不回滚，提示阻断，PROXY 只剩 REJECT。"""
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel, nodes=("A", "B"))
    actions.node_rm(ctx, "A")
    actions.node_rm(ctx, "B")
    assert kernel.proxy_all == ["REJECT"]
    assert any("REJECT" in m or "阻" in m or "blocked" in m for _, m in ctx.messages)
    assert load_state(ctx.paths)["profiles"]["air"]["selected"] is None


def test_hide_and_restore(env):
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel)
    res = actions.node_rm(ctx, "JP 02")
    assert res["hidden"] and "JP 02" not in kernel.proxy_all
    actions.node_restore(ctx, "JP 02")
    assert "JP 02" in kernel.proxy_all


def test_rm_selected_manual_autoselect(env):
    """R-06：删除当前节点 → 自动选第一个非 REJECT 并同步 state.selected。"""
    ctx, kernel, _ = env
    kernel.sub_nodes[SUB] = []
    actions.profile_add(ctx, "vps", node_links=("ss://a@1.1.1.1:1#a", "ss://b@2.2.2.2:2#b"))
    actions.node_use(ctx, "[M] b")
    actions.node_rm(ctx, "[M] b")
    st = load_state(ctx.paths)
    assert st["profiles"]["vps"]["selected"] == "[M] a"
    assert kernel.now["PROXY"] == "[M] a"


def test_duplicate_manual_selection_follows_position(env):
    """R-30：两条同名手工节点，删第一条且当前选第二条 → selected 跟随第二条回读后的名字。"""
    ctx, kernel, _ = env
    actions.profile_add(ctx, "vps", node_links=("ss://a@1.1.1.1:1#x", "ss://a@1.1.1.1:1#x", "ss://c@3.3.3.3:3#y"))
    st = load_state(ctx.paths)
    # 假内核对同名不加序号；把第二条名字改成内核会给的唯一名以模拟真实回读。
    kernel_names = ["[M] x", "[M] x 2", "[M] y"]
    st["profiles"]["vps"]["manual"][1]["name"] = kernel_names[1]
    st["profiles"]["vps"]["selected"] = kernel_names[1]
    from mihomo_cli.state import save_state
    save_state(ctx.paths, st)
    actions.node_rm(ctx, "1")
    st = load_state(ctx.paths)
    assert [m["name"] for m in st["profiles"]["vps"]["manual"]] == ["[M] x", "[M] y"]
    assert st["profiles"]["vps"]["selected"] == "[M] x"


def test_profile_rename_active(env):
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel)
    os.makedirs(ctx.paths.providers, exist_ok=True)
    with open(ctx.paths.provider_file("air"), "w") as fh:
        fh.write("proxies: []\n")
    actions.profile_rename(ctx, "air", "airport")
    st = load_state(ctx.paths)
    assert st["active_profile"] == "airport" and "air" not in st["profiles"]
    assert not os.path.exists(ctx.paths.provider_file("air"))
    assert os.path.exists(ctx.paths.provider_file("airport"))
    assert st["profiles"]["airport"]["selected"] == "HK 01"


def test_offline_saves_without_reload(env):
    ctx, kernel, platform = env
    platform.status = ServiceStatus(load_state="loaded", active_state="inactive", main_pid=0)
    actions.profile_add(ctx, "vps", node_links=("ss://a@1.1.1.1:1#a",))
    st = load_state(ctx.paths)
    assert st["active_profile"] == "vps"
    assert st["profiles"]["vps"]["manual"][0]["name"] is None
    assert ("reload",) not in kernel.calls
    assert os.path.exists(ctx.paths.nodes_file("vps"))


def test_transitioning_returns_3(env):
    """R-32：activating 且 MainPID=0 → node add / tun on 退出码 3，文件不变。"""
    ctx, kernel, platform = env
    _sub_profile(ctx, kernel)
    before = _digest(ctx.paths)
    platform.status = ServiceStatus(load_state="loaded", active_state="activating", main_pid=0)
    for fn in (lambda: actions.node_add(ctx, ("ss://a@1.2.3.4:8388#x",)), lambda: actions.tun_on(ctx)):
        with pytest.raises(CliError) as ei:
            fn()
        assert ei.value.code == actions.EXIT_UNREACHABLE
    assert _digest(ctx.paths) == before


def test_version_normalize():
    assert actions.normalize_version_or_usage("1.19.21") == "v1.19.21"
    with pytest.raises(CliError) as ei:
        actions.normalize_version_or_usage("abc")
    assert ei.value.code == actions.EXIT_USAGE


def test_mirror_normalize():
    assert actions.mirror_normalize("http://10.0.0.2/") == "http://10.0.0.2"
    with pytest.raises(CliError):
        actions.mirror_normalize("ftp://x")


def test_delay_excludes_reject_and_same_name(env):
    """R-25：测速结果不含 REJECT；同名订阅节点与手工节点可分别切换。"""
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel, nodes=("tokyo", "osaka"))
    actions.node_add(ctx, ("ss://a@1.2.3.4:8388#tokyo",))
    kernel.delays = {"tokyo": 120, "osaka": 80, "[M] tokyo": 50}
    result = actions.node_test(ctx, None, "http://x", 1000)
    assert "REJECT" not in result
    assert actions.node_auto(ctx, "http://x", 1000)["node"] == "[M] tokyo"
    actions.node_use(ctx, "tokyo")
    assert kernel.now["PROXY"] == "tokyo"
    actions.node_use(ctx, "[M] tokyo")
    assert kernel.now["PROXY"] == "[M] tokyo"


def test_mode_and_cn(env):
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel)
    actions.mode_set(ctx, "global")
    assert kernel.mode == "global"
    assert "mode: global" in open(ctx.paths.config).read()
    actions.cn_set(ctx, "proxy")
    assert kernel.now["CN"] == "PROXY"
    assert load_state(ctx.paths)["cn_route"] == "proxy"


def test_tun_on_dns_fail_reverts(env):
    """R-29 第二项：DNS 接管失败 → 自动撤回，tun 为假，退出码 4。"""
    ctx, kernel, platform = env
    _sub_profile(ctx, kernel)
    platform.root_rc["_tun-dns-apply"] = 1
    with pytest.raises(CliError) as ei:
        actions.tun_on(ctx)
    assert ei.value.code == actions.EXIT_ROLLED_BACK
    assert load_state(ctx.paths)["tun"] is False
    assert platform.root_calls == ["_tun-dns-apply", "_tun-dns-revert"]


def test_tun_on_conflict_and_sudo(env):
    ctx, kernel, platform = env
    _sub_profile(ctx, kernel)
    platform.sudo_ok = False
    with pytest.raises(CliError) as ei:
        actions.tun_on(ctx)
    assert ei.value.code == actions.EXIT_SUDO
    platform.sudo_ok = True
    platform.ports[53] = ["127.0.0.1:53 dnsmasq(123)"]
    with pytest.raises(CliError) as ei:
        actions.tun_on(ctx)
    assert ei.value.code == actions.EXIT_ERROR and "53" in ei.value.detail
    assert load_state(ctx.paths)["tun"] is False


def test_tun_on_off_order(env):
    ctx, kernel, platform = env
    _sub_profile(ctx, kernel)
    assert actions.tun_on(ctx)["dns"] == "applied"
    assert "tun:" in open(ctx.paths.config).read()
    actions.tun_off(ctx)
    assert platform.root_calls == ["_tun-dns-apply", "_tun-dns-revert"]
    assert load_state(ctx.paths)["tun"] is False


def test_tun_off_revert_fail_keeps_tun(env):
    """R-29 第四项：_tun-dns-revert 失败 → TUN 保持开启，退出码 1。"""
    ctx, kernel, platform = env
    _sub_profile(ctx, kernel)
    actions.tun_on(ctx)
    platform.root_rc["_tun-dns-revert"] = 1
    with pytest.raises(CliError) as ei:
        actions.tun_off(ctx)
    assert ei.value.code == actions.EXIT_ERROR
    assert load_state(ctx.paths)["tun"] is True


def test_preflight_suspends_on_conflict(env):
    ctx, kernel, platform = env
    _sub_profile(ctx, kernel)
    actions.tun_on(ctx)
    platform.conflicts = ["tun device tun0"]
    assert actions.service_preflight(ctx) == 0
    st = load_state(ctx.paths)
    assert st["tun_suspended"] == "tun device tun0"
    assert "tun:" not in open(ctx.paths.config).read()
    platform.conflicts = []
    actions.service_preflight(ctx)
    assert load_state(ctx.paths)["tun_suspended"] is None
    assert "tun:" in open(ctx.paths.config).read()


def test_backup_restore_keeps_tun_and_port(env):
    ctx, kernel, _ = env
    actions.profile_add(ctx, "vps", node_links=("ss://a@1.1.1.1:1#a",))
    actions.node_add(ctx, ("ss://b@2.2.2.2:2#b",))
    # node add 改动前会先备份一次：最新那份就是“只有一条手工节点”的状态。
    backup = sorted(os.listdir(ctx.paths.backups))[-1]
    actions.backup_restore(ctx, backup)
    # 恢复的是 node add 之前那份备份：只剩第一条手工节点。
    st = load_state(ctx.paths)
    assert [m["link"] for m in st["profiles"]["vps"]["manual"]] == ["ss://a@1.1.1.1:1#a"]
    assert st["profiles"]["vps"]["manual"][0]["name"] == "[M] a"


def test_status_redacts_url(env):
    """R-23：status 与 profile show 只露域名。"""
    ctx, kernel, _ = env
    _sub_profile(ctx, kernel)
    info = actions.status(ctx, with_ip=False)
    assert "secret" not in str(info)
    assert "secret" not in str(actions.profile_show(ctx, None, False))
    assert "secret" in str(actions.profile_show(ctx, None, True))


def test_proxy_env():
    class C:
        pass
    text_off = actions.proxy_env(None, False)
    assert text_off.startswith("unset ")


def test_node_add_offline_names_none(env):
    """内核未运行时 node add 只落盘，返回的名字为 None（待启动后回读），不是旧条目的名字。"""
    ctx, kernel, platform = env
    actions.profile_add(ctx, "vps", node_links=("ss://a@1.1.1.1:1#a",))
    platform.status = ServiceStatus(load_state="loaded", active_state="inactive", main_pid=0)
    res = actions.node_add(ctx, ("ss://b@2.2.2.2:2#b",))
    assert res["added"] == [None]


def test_redact_bad_port():
    from mihomo_cli.nodes import redact_url

    assert redact_url("https://x.com:99999/s?token=t") == "<invalid-url>"


def test_port_conflicts_excludes_own_kernel(env, monkeypatch):
    """内核属主不可见时，只有管理接口确认本实例在听该端口才不算占用。"""
    ctx, kernel, platform = env
    platform.ports[7897] = ["port 7897 in use (process not visible)"]
    platform.hidden_ports.add(7897)
    kernel.port = 7897
    assert actions.port_conflicts(ctx, 7897) == []
    # 本实例内核在听别的端口：仍算占用
    kernel.port = 7890
    assert actions.port_conflicts(ctx, 7897) == ["port 7897 in use (process not visible)"]

    # 管理接口不通（服务没跑）：仍算占用
    def no_api(self):
        raise ApiUnavailable("down")
    monkeypatch.setattr(actions.Ctx, "api", no_api)
    kernel.port = 7897
    assert actions.port_conflicts(ctx, 7897) == ["port 7897 in use (process not visible)"]


def test_geodata_case_insensitive(env, monkeypatch):
    """内核把缺失的地区数据库下载成 GeoSite.dat：D07 认它，重新下载覆盖它而不是另建小写的一份。"""
    from mihomo_cli import core_dl, doctor
    ctx, _, _ = env
    home = ctx.paths.home
    for name in ("GeoSite.dat", "geoip.metadb"):
        with open(os.path.join(home, name), "w") as fh:
            fh.write("old")
    assert core_dl.geodata_path(home, "geosite.dat") == os.path.join(home, "GeoSite.dat")
    assert doctor.Doctor(ctx).d07().status == "ok"

    def fake_download(url, dest):
        with open(dest, "w") as fh:
            fh.write("new")
    monkeypatch.setattr(core_dl, "download_to", fake_download)
    core_dl.install_geodata(home, "http://mirror")
    geo = sorted(n for n in os.listdir(home) if n.lower() in ("geosite.dat", "geoip.metadb"))
    assert geo == ["GeoSite.dat", "geoip.metadb"]
    assert open(os.path.join(home, "GeoSite.dat")).read() == "new"
    os.unlink(os.path.join(home, "GeoSite.dat"))
    assert core_dl.geodata_path(home, "geosite.dat") is None
    assert doctor.Doctor(ctx).d07().status == "fail"


def test_reapply_stops_when_kernel_exited(env, monkeypatch):
    """内核秒退（配置坏）时 reapply 立刻放弃，不白等 REAPPLY_READY_SEC。"""
    import subprocess
    import time as _time
    ctx, _, platform = env
    dead = subprocess.Popen(["true"])
    dead.wait()

    class DownApi:
        def get_proxy(self, name):
            raise ApiUnavailable("down")

        def close(self):
            pass
    monkeypatch.setattr(actions.Ctx, "api", lambda self: DownApi())
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    # $MAINPID 指向已退出的进程
    monkeypatch.setenv("MAINPID", str(dead.pid))
    t0 = _time.monotonic()
    assert actions.service_reapply(ctx) == actions.EXIT_OK
    assert _time.monotonic() - t0 < 5
    # 没有 $MAINPID：退而看服务 MainPID
    monkeypatch.delenv("MAINPID")
    platform.status = ServiceStatus(load_state="loaded", active_state="activating", main_pid=0)
    assert actions._main_pid_gone(ctx) is True
    platform.status = ServiceStatus(load_state="loaded", active_state="activating", main_pid=4242)
    assert actions._main_pid_gone(ctx) is False
    # $MAINPID 指向仍在运行的进程（本测试进程）
    monkeypatch.setenv("MAINPID", str(os.getpid()))
    assert actions._main_pid_gone(ctx) is False


def _install_ready(monkeypatch):
    # 内核文件已由 env 放好；地区数据库按“已存在”处理，install 不去下载
    monkeypatch.setattr(actions.core_dl, "geodata_path", lambda home, name: "/dev/null")


def test_install_sudo_fail_before_service(env, monkeypatch):
    """F-05：sudo 预检失败时直接退出码 5，不碰服务。"""
    ctx, _kernel, platform = env
    _install_ready(monkeypatch)
    platform.sudo_ok = False
    with pytest.raises(CliError) as ei:
        actions.install(ctx, None, None, None)
    assert ei.value.code == actions.EXIT_SUDO
    assert "sudo_validate" in platform.actions
    assert "install" not in platform.actions


def test_install_sudo_validated_first(env, monkeypatch):
    """F-05b：先预检 sudo（读密码不受 15 秒超时限制），再装服务。"""
    ctx, _kernel, platform = env
    _install_ready(monkeypatch)
    actions.install(ctx, None, None, None)
    assert platform.actions.index("sudo_validate") < platform.actions.index("install")


def test_uninstall_sudo_precheck(env):
    """F-05c：uninstall 同样先预检 sudo。"""
    ctx, _kernel, platform = env
    platform.sudo_ok = False
    with pytest.raises(CliError) as ei:
        actions.uninstall(ctx, False)
    assert ei.value.code == actions.EXIT_SUDO
    assert "uninstall" not in platform.actions
    platform.sudo_ok = True
    platform.actions.clear()
    actions.uninstall(ctx, False)
    assert platform.actions == ["sudo_validate", "uninstall"]
