"""R-17：菜单与命令行调同一套动作，结果一致。questionary 用脚本化的假对象替换。"""

import questionary

from mihomo_cli import actions, menu
from mihomo_cli.state import load_state

SUB = "https://sub.example.com/s"


class _Q:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


def _script(monkeypatch, answers):
    answers = list(answers)

    def _next(*_a, **_k):
        return _Q(answers.pop(0))

    for name in ("select", "text", "confirm", "path"):
        monkeypatch.setattr(questionary, name, _next)
    return answers


def _setup(env):
    ctx, kernel, _ = env
    kernel.sub_nodes[SUB] = ["HK 01", "JP 02"]
    actions.profile_add(ctx, "air", url=SUB)
    return ctx, kernel


def test_menu_node_switch_equals_command(env, monkeypatch):
    ctx, kernel = _setup(env)
    left = _script(monkeypatch, ["node", "switch", "JP 02", "__back__"])
    menu.run_menu(ctx)
    assert left == []
    via_menu = load_state(ctx.paths)
    actions.node_use(ctx, "HK 01")
    actions.node_use(ctx, "JP 02")
    assert load_state(ctx.paths) == via_menu
    assert kernel.now["PROXY"] == "JP 02"


def test_menu_mode_and_cn(env, monkeypatch):
    ctx, kernel = _setup(env)
    _script(monkeypatch, ["mode", "global", "cn", "proxy", "__back__"])
    menu.run_menu(ctx)
    st = load_state(ctx.paths)
    assert kernel.mode == "global" and st["cn_route"] == "proxy"


def test_menu_error_returns_to_menu(env, monkeypatch, capsys):
    """动作失败只打印原因，回到主菜单继续。"""
    ctx, _ = _setup(env)
    left = _script(monkeypatch, ["profile", "use", "air", "__back__"])
    monkeypatch.setattr(actions, "profile_use", lambda c, n: (_ for _ in ()).throw(actions.CliError(1, "boom")))
    menu.run_menu(ctx)
    assert left == [] and "boom" in capsys.readouterr().out


def test_menu_add_nodes_profile(env, monkeypatch):
    ctx, kernel = _setup(env)
    _script(monkeypatch, ["profile", "add", "nodes", "vps", "ss://a@1.1.1.1:1#a ss://b@2.2.2.2:2#b", "__back__"])
    menu.run_menu(ctx)
    st = load_state(ctx.paths)
    assert [m["link"] for m in st["profiles"]["vps"]["manual"]] == ["ss://a@1.1.1.1:1#a", "ss://b@2.2.2.2:2#b"]
    assert st["active_profile"] == "air"
