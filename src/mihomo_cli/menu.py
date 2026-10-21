"""交互菜单（questionary）：无参数且在终端里运行时进入。

菜单只是另一种收集参数的方式，所有动作都调 actions，与命令行行为一致；
单个动作失败只打印原因并回到菜单，不退出。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

import click
import questionary

from . import actions
from .actions import CliError, Ctx
from .i18n import t
from .platform import NotSupported

BACK = "__back__"


def _choice(title: str, value: Any) -> questionary.Choice:
    return questionary.Choice(title=title, value=value)


def _do(ctx: Ctx, fn: Callable[[], Any], done: Optional[Callable[[Any], str]] = None) -> Any:
    try:
        result = fn()
    except CliError as exc:
        click.secho(exc.message, fg="red")
        if exc.detail:
            click.echo("  %s: %s" % (t("label.detail"), exc.detail))
        for hint in exc.hints:
            click.echo("  %s: %s" % (t("label.hint"), hint))
        return None
    except NotSupported as exc:
        click.secho("%s %s" % (t("err.not_supported"), exc), fg="red")
        return None
    if done is not None:
        click.secho(done(result), fg="green")
    return result


def _header(ctx: Ctx) -> None:
    info = _do(ctx, lambda: actions.status(ctx, with_ip=False))
    if not info:
        return
    tun = t("st.on" if info["tun"] else "st.off")
    if info.get("tun_suspended"):
        tun = t("warn.tun_suspended", reason=info["tun_suspended"])
    click.echo("")
    click.echo("%s: %s | %s: %s | %s: %s" % (
        t("st.profile"), info.get("profile") or "-", t("st.node"), info.get("node") or "-",
        t("st.mode"), t("mode." + info["mode"])))
    click.echo("%s: %s | %s: %s | %s: %s" % (
        t("st.cn"), t("cn." + info["cn_route"]), t("st.tun"), tun, t("st.service"),
        info["service"]["active_state"] if info["service"]["installed"] else t("st.not_installed")))


def run_menu(ctx: Ctx) -> None:
    while True:
        _header(ctx)
        pick = questionary.select(t("menu.title"), choices=[
            _choice(t("menu.node"), "node"),
            _choice(t("menu.mode"), "mode"),
            _choice(t("menu.cn"), "cn"),
            _choice(t("menu.profile"), "profile"),
            _choice(t("menu.tun"), "tun"),
            _choice(t("menu.service"), "service"),
            _choice(t("menu.status"), "status"),
            _choice(t("menu.doctor"), "doctor"),
            _choice(t("menu.quit"), BACK),
        ]).ask()
        if pick in (None, BACK):
            return
        {"node": _node_menu, "mode": _mode_menu, "cn": _cn_menu, "profile": _profile_menu,
         "tun": _tun_menu, "service": _service_menu, "status": _status, "doctor": _doctor}[pick](ctx)


def _status(ctx: Ctx) -> None:
    from .cli import _print_status

    info = _do(ctx, lambda: actions.status(ctx))
    if info:
        _print_status(info)


def _doctor(ctx: Ctx) -> None:
    from .cli import _print_check
    from .doctor import run

    fix = questionary.confirm(t("menu.doctor_fix"), default=False).ask()
    if fix is None:
        return
    _do(ctx, lambda: run(ctx, fix=fix, on_check=_print_check))


def _node_menu(ctx: Ctx) -> None:
    pick = questionary.select(t("menu.node"), choices=[
        _choice(t("menu.node_switch"), "switch"),
        _choice(t("menu.node_auto"), "auto"),
        _choice(t("menu.node_add"), "add"),
        _choice(t("menu.node_rm"), "rm"),
        _choice(t("menu.node_restore"), "restore"),
        _choice(t("menu.back"), BACK),
    ]).ask()
    if pick in (None, BACK):
        return
    if pick == "auto":
        _do(ctx, lambda: actions.node_auto(ctx, actions.render_mod.HEALTH_URL, 5000),
            lambda d: t("info.node_auto", name=d["node"], delay=d["delay"]))
        return
    if pick == "add":
        text = questionary.text(t("menu.node_add_prompt")).ask()
        if text:
            from .nodes import split_links

            _do(ctx, lambda: actions.node_add(ctx, tuple(split_links(text))),
                lambda d: t("info.node_added", count=len(d["added"])))
        return
    if pick == "restore":
        state = _do(ctx, lambda: actions.node_list(ctx))
        if state and state["hidden"]:
            name = questionary.select(t("menu.node_restore"), choices=state["hidden"] + [_choice(t("menu.back"), BACK)]).ask()
            if name not in (None, BACK):
                _do(ctx, lambda: actions.node_restore(ctx, name), lambda d: t("info.node_restored", name=name))
        else:
            click.echo(t("menu.nothing"))
        return
    data = _do(ctx, lambda: actions.node_list(ctx, with_delay=(pick == "switch")))
    if not data or not data["nodes"]:
        click.echo(t("menu.nothing"))
        return
    choices: List[Any] = []
    for n in data["nodes"]:
        label = n["name"]
        if pick == "switch":
            label += "  (%s)" % ("%d ms" % n["delay"] if n.get("delay") else t("st.timeout"))
        if n["name"] == data["now"]:
            label = "* " + label
        choices.append(_choice(label, n["name"]))
    choices.append(_choice(t("menu.back"), BACK))
    name = questionary.select(t("menu.node_switch" if pick == "switch" else "menu.node_rm"), choices=choices).ask()
    if name in (None, BACK):
        return
    if pick == "switch":
        _do(ctx, lambda: actions.node_use(ctx, name),
            lambda d: t("info.node_switched", old=d["old"] or "-", new=d["new"]))
    elif questionary.confirm(t("menu.confirm_rm", name=name), default=False).ask():
        _do(ctx, lambda: actions.node_rm(ctx, name),
            lambda d: t("info.node_hidden" if d["hidden"] else "info.node_removed", name=d["removed"]))


def _mode_menu(ctx: Ctx) -> None:
    value = questionary.select(t("menu.mode"), choices=[
        _choice(t("menu.mode_rule"), "rule"), _choice(t("menu.mode_global"), "global"),
        _choice(t("menu.mode_direct"), "direct"), _choice(t("menu.back"), BACK)]).ask()
    if value not in (None, BACK):
        _do(ctx, lambda: actions.mode_set(ctx, value),
            lambda d: t("info.mode_switched", old=t("mode." + d["old"]), new=t("mode." + d["new"])))


def _cn_menu(ctx: Ctx) -> None:
    value = questionary.select(t("menu.cn"), choices=[
        _choice(t("menu.cn_direct"), "direct"), _choice(t("menu.cn_proxy"), "proxy"),
        _choice(t("menu.back"), BACK)]).ask()
    if value not in (None, BACK):
        _do(ctx, lambda: actions.cn_set(ctx, value),
            lambda d: t("info.cn_switched", old=t("cn." + d["old"]), new=t("cn." + d["new"])))


def _profile_menu(ctx: Ctx) -> None:
    pick = questionary.select(t("menu.profile"), choices=[
        _choice(t("menu.profile_use"), "use"),
        _choice(t("menu.profile_add"), "add"),
        _choice(t("menu.profile_update"), "update"),
        _choice(t("menu.profile_rm"), "rm"),
        _choice(t("menu.back"), BACK),
    ]).ask()
    if pick in (None, BACK):
        return
    if pick == "update":
        _do(ctx, lambda: actions.profile_update(ctx, None),
            lambda d: t("info.profile_updated", count=d.get("nodes", 0)) if d.get("updated") else t("info.nothing_to_update"))
        return
    if pick == "add":
        _profile_add(ctx)
        return
    rows = _do(ctx, lambda: actions.profile_list(ctx)) or []
    if not rows:
        click.echo(t("info.no_profiles"))
        return
    choices = [_choice(("* " if r["active"] else "") + r["name"], r["name"]) for r in rows]
    name = questionary.select(t("menu.profile_" + pick), choices=choices + [_choice(t("menu.back"), BACK)]).ask()
    if name in (None, BACK):
        return
    if pick == "use":
        _do(ctx, lambda: actions.profile_use(ctx, name), lambda d: t("info.profile_switched", name=name))
    elif questionary.confirm(t("menu.confirm_rm", name=name), default=False).ask():
        _do(ctx, lambda: actions.profile_rm(ctx, name), lambda d: t("info.profile_removed", name=name))


def _profile_add(ctx: Ctx) -> None:
    kind = questionary.select(t("menu.profile_kind"), choices=[
        _choice(t("menu.kind_subscription"), "subscription"), _choice(t("menu.kind_file"), "file"),
        _choice(t("menu.kind_nodes"), "nodes"), _choice(t("menu.back"), BACK)]).ask()
    if kind in (None, BACK):
        return
    name = questionary.text(t("menu.profile_name")).ask()
    if not name:
        return
    url = file = None
    links: tuple = ()
    if kind == "subscription":
        url = questionary.text(t("menu.profile_url")).ask()
        if not url:
            return
    elif kind == "file":
        file = questionary.path(t("menu.profile_file")).ask()
        if not file:
            return
    else:
        from .nodes import split_links

        text = questionary.text(t("menu.node_add_prompt")).ask()
        if not text:
            return
        links = tuple(split_links(text))
    _do(ctx, lambda: actions.profile_add(ctx, name, url, file, links),
        lambda d: t("info.profile_added_active" if d["active"] else "info.profile_added", name=name))


def _tun_menu(ctx: Ctx) -> None:
    from .doctor import d13_ok

    value = questionary.select(t("menu.tun"), choices=[
        _choice(t("menu.tun_on"), "on"), _choice(t("menu.tun_off"), "off"), _choice(t("menu.back"), BACK)]).ask()
    if value == "on":
        _do(ctx, lambda: actions.tun_on(ctx, d13_ok), lambda d: t("info.tun_on") if d.get("changed") else t("info.tun_already_on"))
    elif value == "off":
        _do(ctx, lambda: actions.tun_off(ctx), lambda d: t("info.tun_off") if d.get("changed") else t("info.tun_already_off"))


def _service_menu(ctx: Ctx) -> None:
    value = questionary.select(t("menu.service"), choices=[
        _choice(t("menu.service_start"), "start"), _choice(t("menu.service_restart"), "restart"),
        _choice(t("menu.service_stop"), "stop"), _choice(t("menu.back"), BACK)]).ask()
    if value not in (None, BACK):
        _do(ctx, lambda: actions.service_control(ctx, value), lambda d: "%s: %s" % (t("st.service"), d["active_state"]))
