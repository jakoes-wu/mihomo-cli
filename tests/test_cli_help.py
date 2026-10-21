"""R-18：中英文下每个命令的 -h 都能打开并带示例；源码里用到的文案键都存在。"""

import os
import re

import click
import pytest

from mihomo_cli import cli, i18n


def _walk(group, prefix=()):
    for name, cmd in group.commands.items():
        if cmd.hidden:
            continue
        path = prefix + (name,)
        yield path, cmd
        if isinstance(cmd, click.Group):
            yield from _walk(cmd, path)


def _commands():
    i18n.set_lang("en")
    return [p for p, _ in _walk(cli.build_cli())]


def _run_main(argv, capsys):
    code = 0
    try:
        cli.main(argv)
    except SystemExit as exc:
        code = exc.code or 0
    return code, capsys.readouterr()


@pytest.mark.parametrize("lang,marker", [("zh", "示例"), ("en", "Examples")])
@pytest.mark.parametrize("path", _commands(), ids=lambda p: " ".join(p))
def test_help_has_examples(path, lang, marker, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MIHOMO_CLI_HOME", str(tmp_path / "home"))
    code, out = _run_main(["--lang", lang] + list(path) + ["-h"], capsys)
    assert code == 0, out.err
    assert marker in out.out, " ".join(path)


@pytest.mark.parametrize("lang,marker", [("zh", "退出码"), ("en", "Exit codes")])
def test_root_help(lang, marker, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MIHOMO_CLI_HOME", str(tmp_path / "home"))
    code, out = _run_main(["--lang", lang, "-h"], capsys)
    assert code == 0 and marker in out.out


def test_help_command_and_unknown(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MIHOMO_CLI_HOME", str(tmp_path / "home"))
    code, out = _run_main(["--lang", "en", "help", "node", "add"], capsys)
    assert code == 0 and "Examples" in out.out
    code, _ = _run_main(["--lang", "en", "help", "nosuch"], capsys)
    assert code == 2


def test_all_message_keys_exist():
    src = os.path.join(os.path.dirname(cli.__file__))
    missing = set()
    for root, _, files in os.walk(src):
        for f in files:
            if f.endswith(".py"):
                text = open(os.path.join(root, f), encoding="utf-8").read()
                for key in re.findall(r"""\bt\(\s*["']([a-z_]+\.[A-Za-z0-9_.]+)["']""", text):
                    # 以下划线结尾的是动态拼接前缀（如 t("ex.service_" + name)），不单独核。
                    if not key.endswith("_") and key not in i18n.MESSAGES:
                        missing.add(key)
    assert not missing, sorted(missing)


def test_messages_have_both_languages():
    bad = [k for k, v in i18n.MESSAGES.items() if not (isinstance(v, tuple) and len(v) == 2 and all(v))]
    assert not bad, bad


def test_home_too_open_rejected(capsys, tmp_path, monkeypatch):
    """R-21 的 L0 部分：主目录 0755 时普通命令拒绝运行，doctor 与帮助照常可用。"""
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o755)
    monkeypatch.setenv("MIHOMO_CLI_HOME", str(home))
    code, out = _run_main(["--lang", "en", "node", "use", "x"], capsys)
    assert code == 1 and "chmod 700" in out.err
    code, _ = _run_main(["--lang", "en", "mode"], capsys)
    assert code == 1
    code, _ = _run_main(["--lang", "en", "node", "use", "-h"], capsys)
    assert code == 0


def test_root_rejected_but_help_allowed(capsys, tmp_path, monkeypatch):
    """root 运行普通命令退出码 5；help 子命令与 -h 一样放行。"""
    monkeypatch.setenv("MIHOMO_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    code, _ = _run_main(["--lang", "en", "status"], capsys)
    assert code == 5
    code, out = _run_main(["--lang", "en", "help", "node", "add"], capsys)
    assert code == 0 and "Usage" in out.out
    code, out = _run_main(["--lang", "en", "node", "add", "-h"], capsys)
    assert code == 0 and "Usage" in out.out
