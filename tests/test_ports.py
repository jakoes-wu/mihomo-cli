"""固定节点端口（feature-fixed-node-ports §8 P-01～P-09、P-18 的 L0 部分）。

真内核行为（端口与模式无关、空 provider 回落 REJECT、热加载保留旧节点）由 L1 在编译机用真 mihomo 验证。
"""

import base64
import os

import pytest
import yaml

from mihomo_cli import actions, cli, doctor, i18n, menu, render
from mihomo_cli.actions import CliError
from mihomo_cli.platform.base import ServiceStatus
from mihomo_cli.state import Paths, StateError, default_state, load_state, new_profile, parse_state, dump_state

L1 = "vless://u1@1.1.1.1:443?security=reality#n1"
L2 = "vless://u2@2.2.2.2:443?security=reality#n2"
L3 = "vless://u3@3.3.3.3:443?security=reality#n3"
SUB = "https://sub.example.com/s?token=secret"


@pytest.fixture
def penv(env, monkeypatch):
    """假内核报 v1.19.32，并有一个订阅类代理服务。"""
    ctx, kernel, platform = env
    monkeypatch.setenv("FAKE_CORE_VERSION", "v1.19.32")
    kernel.sub_nodes[SUB] = ["HK 01", "JP 02"]
    actions.profile_add(ctx, "air", url=SUB)
    return ctx, kernel, platform


def _files(paths):
    out = {}
    for d in (paths.nodes,):
        for f in sorted(os.listdir(d)):
            with open(os.path.join(d, f), "rb") as fh:
                out[f] = fh.read()
    with open(paths.state, "rb") as fh:
        out["state"] = fh.read()
    with open(paths.config, "rb") as fh:
        out["config"] = fh.read()
    return out


# ---------------- P-01 render ----------------

def _render_state(ports, with_profile=True):
    st = default_state()
    if with_profile:
        prof = new_profile("subscription", url=SUB)
        st["profiles"]["p"] = prof
        st["active_profile"] = "p"
    st["ports"] = [{"port": p, "link": "ss://a@1.1.1.1:1#x", "name": None} for p in ports]
    return st


def test_render_without_ports_has_no_new_keys(tmp_path):
    paths = Paths(str(tmp_path))
    cfg = render.render(_render_state([]), paths)
    assert "listeners" not in cfg
    assert not any(n.startswith("pin-") for n in cfg["proxy-providers"])
    assert [g["name"] for g in cfg["proxy-groups"]] == ["PROXY", "CN", "GLOBAL"]


@pytest.mark.parametrize("ports", [[7901], [7901, 7902, 7905]])
def test_render_ports(tmp_path, ports):
    paths = Paths(str(tmp_path))
    cfg = render.render(_render_state(ports), paths)
    for p in ports:
        assert cfg["proxy-providers"]["pin-%d" % p] == {"type": "file", "path": "./nodes/_port-%d.txt" % p}
    groups = {g["name"]: g for g in cfg["proxy-groups"]}
    for p in ports:
        assert groups["PORT-%d" % p] == {"name": "PORT-%d" % p, "type": "select", "use": ["pin-%d" % p],
                                         "empty-fallback": "REJECT"}
    assert groups["PROXY"]["use"] == ["main"]
    assert groups["GLOBAL"]["proxies"] == ["PROXY"]
    assert cfg["listeners"] == [{"name": "port-%d" % p, "type": "mixed", "listen": "127.0.0.1", "port": p,
                                 "udp": True, "proxy": "PORT-%d" % p} for p in ports]


def test_render_ports_without_profile(tmp_path):
    paths = Paths(str(tmp_path))
    cfg = render.render(_render_state([7901], with_profile=False), paths)
    assert list(cfg["proxy-providers"]) == ["pin-7901"]
    assert cfg["listeners"][0]["port"] == 7901


# ---------------- P-02 state ----------------

@pytest.mark.parametrize("ports", [
    [{"port": 0, "link": L1}],
    [{"port": 70000, "link": L1}],
    [{"port": True, "link": L1}],
    [{"port": 7901, "link": L1}, {"port": 7901, "link": L2}],
    [{"port": 7897, "link": L1}],
    [{"port": 7901, "link": "nolink"}],
    "x",
])
def test_validate_ports_rejects(ports):
    st = default_state()
    st["ports"] = ports
    with pytest.raises(StateError):
        parse_state(yaml.safe_dump(st))


def test_ports_null_and_bare_link_entry():
    st = default_state()
    st["ports"] = None
    assert parse_state(yaml.safe_dump(st))["ports"] == []
    st["ports"] = [L1]
    with pytest.raises(StateError) as ei:
        parse_state(yaml.safe_dump(st))
    assert "u1" not in str(ei.value)


def test_old_state_without_ports_and_sorted():
    st = default_state()
    del st["ports"]
    assert parse_state(yaml.safe_dump(st))["ports"] == []
    st["ports"] = [{"port": 7902, "link": L2}, {"port": 7901, "link": L1, "name": "n1"}]
    got = parse_state(dump_state(parse_state(yaml.safe_dump(st))))["ports"]
    assert got == [{"port": 7901, "link": L1, "name": "n1"}, {"port": 7902, "link": L2, "name": None}]


# ---------------- P-03 / P-04 port add ----------------

def test_port_add_online(penv):
    ctx, kernel, _ = penv
    res = actions.port_add(ctx, 7901, L1)
    assert res["added"] == [{"port": 7901, "name": "n1"}]
    st = load_state(ctx.paths)
    assert st["ports"] == [{"port": 7901, "link": L1, "name": "n1"}]
    with open(ctx.paths.port_file(7901)) as fh:
        assert base64.b64decode(fh.read().strip()).decode() == L1
    assert kernel.providers["pin-7901"] == ["n1"]


def test_port_add_offline_name_none(penv):
    ctx, _, platform = penv
    platform.status = ServiceStatus(load_state="loaded", active_state="inactive", main_pid=0)
    res = actions.port_add(ctx, 7901, L1)
    assert res["added"] == [{"port": 7901, "name": None}]
    assert load_state(ctx.paths)["ports"][0]["name"] is None


def test_port_add_file_start(penv, tmp_path):
    ctx, _, _ = penv
    f = tmp_path / "links.txt"
    f.write_text("# 注释\n%s\n%s %s\n" % (L1, L2, L3))
    res = actions.port_add(ctx, file=str(f), start=7901)
    assert [a["port"] for a in res["added"]] == [7901, 7902, 7903]
    assert [p["name"] for p in load_state(ctx.paths)["ports"]] == ["n1", "n2", "n3"]


def test_port_add_batch_one_taken_changes_nothing(penv, tmp_path):
    ctx, _, platform = penv
    f = tmp_path / "links.txt"
    f.write_text("%s\n%s\n" % (L1, L2))
    platform.ports[7902] = ["nc pid=1"]
    before = _files(ctx.paths)
    with pytest.raises(CliError) as ei:
        actions.port_add(ctx, file=str(f), start=7901)
    assert ei.value.code == actions.EXIT_ERROR
    assert _files(ctx.paths) == before


@pytest.mark.parametrize("port", [7897])
def test_port_add_mixed_port_or_duplicate(penv, port):
    ctx, _, _ = penv
    with pytest.raises(CliError) as ei:
        actions.port_add(ctx, port, L1)
    assert ei.value.code == actions.EXIT_USAGE
    actions.port_add(ctx, 7901, L1)
    with pytest.raises(CliError) as ei:
        actions.port_add(ctx, 7901, L2)
    assert ei.value.code == actions.EXIT_USAGE


def test_port_add_single_with_two_links_rejected(penv):
    ctx, _, _ = penv
    with pytest.raises(CliError) as ei:
        actions.port_add(ctx, 7901, L1 + "\n" + L2)
    assert ei.value.code == actions.EXIT_USAGE
    assert load_state(ctx.paths)["ports"] == []


def test_port_add_unparsable_rolls_back(penv):
    ctx, _, _ = penv
    before = _files(ctx.paths)
    with pytest.raises(CliError) as ei:
        actions.port_add(ctx, 7901, "bad://x@1.1.1.1:1#bad")
    assert ei.value.code == actions.EXIT_ROLLED_BACK
    assert _files(ctx.paths) == before
    assert not os.path.exists(ctx.paths.port_file(7901))


def test_port_add_not_listening_rolls_back(penv, monkeypatch):
    ctx, _, _ = penv
    monkeypatch.setattr(actions, "_tcp_ok", lambda port, timeout=2.0: port != 7901)
    before = _files(ctx.paths)
    with pytest.raises(CliError) as ei:
        actions.port_add(ctx, 7901, L1)
    assert ei.value.code == actions.EXIT_ROLLED_BACK and "not listening" in ei.value.detail
    assert _files(ctx.paths) == before


def test_port_add_taken_after_reload_rolls_back(penv, monkeypatch):
    """K6：内核绑定失败不报错；端口被别人占着时 TCP 能连上（连到占用者），必须靠占用检查拦下。"""
    ctx, _, platform = penv
    calls = {"n": 0}
    real = platform.listening_conflicts

    def conflicts(port, host="127.0.0.1", own_listener=False):
        # 添加前检查时端口空闲，重载后被别的程序抢占（own_listener 也排除不掉）。
        if port == 7901:
            calls["n"] += 1
            return [] if calls["n"] == 1 else ["nc pid=9 uid=0"]
        return real(port, host, own_listener)
    monkeypatch.setattr(platform, "listening_conflicts", conflicts)
    before = _files(ctx.paths)
    with pytest.raises(CliError) as ei:
        actions.port_add(ctx, 7901, L1)
    assert ei.value.code == actions.EXIT_ROLLED_BACK
    assert _files(ctx.paths) == before


# ---------------- P-18 无关端口坏掉时其它动作照常 ----------------

def test_blocked_port_does_not_block_other_actions(penv, capsys):
    ctx, kernel, _ = penv
    actions.port_add(ctx, 7901, L1)
    # 模拟节点文件被改坏：内核下次加载时该 provider 为空。
    with open(ctx.paths.port_file(7901), "w") as fh:
        fh.write(base64.b64encode(b"bad://x@1.1.1.1:1#bad").decode())
    messages = []
    ctx.say = lambda level, msg: messages.append((level, msg))
    actions.node_add(ctx, ("ss://b@2.2.2.2:2#b",))
    assert kernel.providers["pin-7901"] == []
    assert ("warn", i18n.t("warn.port_blocked", port=7901)) in messages
    kernel.sub_nodes["https://other.example.com/s"] = ["SG 01"]
    actions.profile_add(ctx, "other", url="https://other.example.com/s")
    actions.profile_use(ctx, "other")
    # D04：手改 config.yaml 后用 doctor --fix 修
    with open(ctx.paths.config, "a") as fh:
        fh.write("# x\n")
    d = doctor.Doctor(ctx)
    assert d.d04().status == "fail"
    d.fix_d04()
    assert doctor.Doctor(ctx).d04().status == "ok"
    assert doctor.Doctor(ctx).d16().status == "fail"
    # 名字保留旧值（以 D16 为准）
    assert load_state(ctx.paths)["ports"][0]["name"] == "n1"


# ---------------- P-05 port rm ----------------

def test_port_rm(penv):
    ctx, _, _ = penv
    actions.port_add(ctx, 7901, L1)
    actions.port_add(ctx, 7902, L2)
    actions.port_rm(ctx, (7901,))
    assert [p["port"] for p in load_state(ctx.paths)["ports"]] == [7902]
    assert not os.path.exists(ctx.paths.port_file(7901))
    with pytest.raises(CliError) as ei:
        actions.port_rm(ctx, (7999,))
    assert ei.value.code == actions.EXIT_ERROR


# ---------------- P-06 内核版本与 install 端口 ----------------

def test_port_add_old_core_rejected(env, monkeypatch):
    ctx, kernel, _ = env
    with pytest.raises(CliError) as ei:
        actions.port_add(ctx, 7901, L1)
    assert ei.value.code == actions.EXIT_ERROR and "core upgrade" in " ".join(ei.value.hints)


def test_core_upgrade_and_install_old_version_rejected(penv, monkeypatch):
    ctx, _, _ = penv
    actions.port_add(ctx, 7901, L1)
    monkeypatch.setattr(actions, "_install_core", lambda *a: pytest.fail("must not download"))
    mirror_before = load_state(ctx.paths)["mirror"]
    with pytest.raises(CliError) as ei:
        actions.core_upgrade(ctx, "1.19.26", "https://mirror.example.com")
    assert ei.value.code == actions.EXIT_USAGE
    assert load_state(ctx.paths)["mirror"] == mirror_before
    with pytest.raises(CliError) as ei:
        actions.install(ctx, "1.19.26", None, None)
    assert ei.value.code == actions.EXIT_USAGE
    with pytest.raises(CliError) as ei:
        actions.install(ctx, None, None, 7901)
    assert ei.value.code == actions.EXIT_USAGE


# ---------------- P-07 备份恢复 ----------------

def _latest_backup(ctx):
    return sorted(os.listdir(ctx.paths.backups))[-1]


def test_backup_restore_ports(penv):
    ctx, kernel, _ = penv
    actions.port_add(ctx, 7901, L1)
    actions.port_add(ctx, 7902, L2)  # 备份里有 7901
    backup_with_7901 = _latest_backup(ctx)
    actions.port_rm(ctx, (7901, 7902))
    actions.backup_restore(ctx, backup_with_7901)
    st = load_state(ctx.paths)
    assert [p["port"] for p in st["ports"]] == [7901]
    assert os.path.exists(ctx.paths.port_file(7901)) and not os.path.exists(ctx.paths.port_file(7902))
    assert kernel.providers["pin-7901"] == ["n1"]


def test_backup_restore_old_format_clears_ports(penv, capsys):
    ctx, _, _ = penv
    actions.port_add(ctx, 7901, L1)
    backup = _latest_backup(ctx)  # 添加 7901 之前的状态
    actions.port_add(ctx, 7902, L2)
    path = os.path.join(ctx.paths.backups, backup, "state.yaml")
    data = yaml.safe_load(open(path))
    data.pop("ports", None)  # 模拟 0.1.3 及更早版本做的备份
    with open(path, "w") as fh:
        yaml.safe_dump(data, fh)
    messages = []
    ctx.say = lambda level, msg: messages.append((level, msg))
    actions.backup_restore(ctx, backup)
    assert load_state(ctx.paths)["ports"] == []
    assert ("warn", i18n.t("warn.backup_no_ports", count=2)) in messages


def test_backup_restore_port_taken_or_mixed_rejected(penv):
    ctx, _, platform = penv
    actions.port_add(ctx, 7901, L1)
    actions.port_add(ctx, 7902, L2)
    backup = _latest_backup(ctx)  # 只有 7901
    actions.port_rm(ctx, (7901,))
    platform.ports[7901] = ["nc pid=1"]
    before = _files(ctx.paths)
    with pytest.raises(CliError) as ei:
        actions.backup_restore(ctx, backup)
    assert ei.value.code == actions.EXIT_ERROR
    assert _files(ctx.paths) == before
    platform.ports.clear()
    # 备份端口等于当前混合端口
    path = os.path.join(ctx.paths.backups, backup, "state.yaml")
    data = yaml.safe_load(open(path))
    data["mixed_port"] = 7000
    data["ports"] = [{"port": 7897, "link": L1, "name": None}]
    with open(path, "w") as fh:
        yaml.safe_dump(data, fh)
    with pytest.raises(CliError) as ei:
        actions.backup_restore(ctx, backup)
    assert ei.value.code == actions.EXIT_USAGE
    assert _files(ctx.paths) == before


# ---------------- P-08 D16 与 status ----------------

def _d16(ctx):
    return doctor.Doctor(ctx).d16()


def test_d16_branches(penv, monkeypatch):
    ctx, kernel, platform = penv
    assert _d16(ctx).status == "skip"
    actions.port_add(ctx, 7901, L1)
    actions.port_add(ctx, 7902, L2)
    c = _d16(ctx)
    assert c.status == "ok" and c.detail == "2 ports: 7901-7902"
    # 本实例内核在听、属主不可见：不算被占
    platform.ports[7901] = ["port 7901 in use (process not visible)"]
    platform.hidden_ports.add(7901)
    assert _d16(ctx).status == "ok"
    # 别人占着
    platform.hidden_ports.clear()
    assert _d16(ctx).status == "fail"
    platform.ports.clear()
    # 没在听
    monkeypatch.setattr(actions, "_tcp_ok", lambda port, timeout=2.0: port != 7902)
    assert _d16(ctx).status == "fail"
    monkeypatch.setattr(actions, "_tcp_ok", lambda port, timeout=2.0: True)
    # 节点为空
    kernel.providers["pin-7902"] = []
    assert _d16(ctx).status == "fail"
    kernel.providers["pin-7902"] = ["n2"]
    # 内核未运行
    platform.status = ServiceStatus(load_state="loaded", active_state="inactive", main_pid=0)
    assert _d16(ctx).status == "skip"
    # 版本低
    monkeypatch.setenv("FAKE_CORE_VERSION", "v1.19.26")
    assert _d16(ctx).status == "fail"


def test_status_ports_summary(penv, capsys):
    ctx, _, platform = penv
    info = actions.status(ctx, with_ip=False)
    assert info["ports"] == [] and info["ports_listening"] is None
    cli._print_status(info)
    assert "7901" not in capsys.readouterr().out
    actions.port_add(ctx, 7901, L1)
    actions.port_add(ctx, 7902, L2)
    actions.port_add(ctx, 7905, L3)
    info = actions.status(ctx, with_ip=False)
    assert info["ports"] == [7901, 7902, 7905] and info["ports_listening"] == 3
    i18n.set_lang("en")
    cli._print_status(info)
    assert "3: 7901,7902,7905 (3 listening)" in capsys.readouterr().out
    platform.status = ServiceStatus(load_state="loaded", active_state="inactive", main_pid=0)
    info = actions.status(ctx, with_ip=False)
    assert info["ports_listening"] is None
    cli._print_status(info)
    assert "(core not running)" in capsys.readouterr().out
    assert actions.format_ports([7903, 7901, 7902]) == "7901-7903"


def test_port_list_hides_credentials(penv):
    ctx, _, _ = penv
    actions.port_add(ctx, 7901, L1)
    d = actions.port_list(ctx)
    assert d["ports"][0]["name"] == "n1" and d["ports"][0]["listening"] is True
    assert "u1" not in str(d)


# ---------------- P-09 菜单 ----------------

class _Q:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


def _script(monkeypatch, answers):
    import questionary

    answers = list(answers)
    for name in ("select", "text", "confirm", "path"):
        monkeypatch.setattr(questionary, name, lambda *_a, **_k: _Q(answers.pop(0)))
    return answers


def test_menu_port_add_rm_equals_command(penv, monkeypatch):
    ctx, _, _ = penv
    left = _script(monkeypatch, ["port", "add", "7901", L1, "__back__"])
    menu.run_menu(ctx)
    assert left == []
    via_menu = load_state(ctx.paths)["ports"]
    actions.port_rm(ctx, (7901,))
    actions.port_add(ctx, 7901, L1)
    assert load_state(ctx.paths)["ports"] == via_menu
    left = _script(monkeypatch, ["port", "rm", 7901, True, "__back__"])
    menu.run_menu(ctx)
    assert left == [] and load_state(ctx.paths)["ports"] == []


@pytest.mark.parametrize("argv", [
    ["port", "add", "7901"],
    ["port", "add", "--file", "x.txt"],
    ["port", "add", "--file", "x.txt", "--start", "7901", "7902", L1],
    ["port", "add", "--start", "7901", "7902", L1],
    ["port", "rm"],
])
def test_port_cli_usage_errors(argv, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MIHOMO_CLI_HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as ei:
        cli.main(["--lang", "en"] + argv)
    assert ei.value.code == 2
