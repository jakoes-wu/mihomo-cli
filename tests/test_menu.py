"""R-17：菜单与命令行调同一套动作，结果一致。questionary 用脚本化的假对象替换。

另有一条不打补丁的真实选择框测试，用来发现 questionary 与 prompt_toolkit 版本不兼容。
"""

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


def test_real_select_renders():
    """真实画一次选择框（fix-menu-py38）。

    上面的测试都把 questionary 换成了假对象，抓不到“选择框一打开就崩”这类依赖版本问题。
    回车后再送 Ctrl-C：万一回车没被选择框接收，ask() 会因 Ctrl-C 返回 None 让断言失败，
    而不是一直等输入把 CI 卡到超时。
    """
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text("\r\x03")
        picked = questionary.select("t", choices=["a", "b"], input=pipe, output=DummyOutput()).ask()
    assert picked == "a"
