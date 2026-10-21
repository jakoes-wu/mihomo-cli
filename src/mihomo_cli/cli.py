"""命令行入口：click 命令树、全局选项、输出格式与退出码映射（方案 5.1.6）。

只负责收集参数和展示结果，业务一律调 actions；帮助文本取自 i18n，因此 main() 先从 argv
预读 --lang 定语言，再构建命令树（click 的 help 在装饰时求值）。
提示语写 stderr，结果写 stdout——`eval "$(mihomo-cli proxy-env on)"` 依赖 stdout 只有语句。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
from typing import Any, Callable, List, Optional

import click

from . import __version__, actions
from .actions import CliError, Ctx
from .i18n import detect_lang, set_lang, t
from .platform import Check, NotSupported, get_platform
from .state import Paths, resolve_home

# 以 root 身份运行的隐藏子命令（由 sudo 调起，方案 5.1.7）；其余命令一律拒绝 root。
ROOT_COMMANDS = ("_tun-dns-apply", "_tun-dns-revert")
SERVICE_COMMANDS = ("_service-preflight", "_service-reapply") + ROOT_COMMANDS
# 命令行里带 -h / --help / --version 时只看帮助、不碰主目录。click 先执行 group 回调、
# 后解析子命令的 -h，回调里看不到它，所以由 main 预读 argv 后写在这里。
_HELP_REQUESTED = [False]
HOME_CHECK_EXEMPT = ("install", "uninstall", "doctor", "help", "completion") + SERVICE_COMMANDS
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3
CONTEXT = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


# ====================================================================
# 输出
# ====================================================================

class Out:
    """统一输出：--json 时把结果、提示、错误收成一个 JSON 对象打印到 stdout。"""

    def __init__(self, as_json: bool):
        self.as_json = as_json

    def emit(self, level: str, text: str) -> None:
        if self.as_json:
            return
        color = {"warn": "yellow", "error": "red"}.get(level)
        click.secho(text, fg=color, err=True)

    def result(self, ctx: Ctx, data: Any, human: Optional[Callable[[], None]] = None) -> None:
        if self.as_json:
            click.echo(json.dumps({"ok": True, "data": data, "messages": [m for _, m in ctx.messages]},
                                  ensure_ascii=False, indent=2, default=str))
        elif human is not None:
            human()


def _table(headers: List[str], rows: List[List[Any]]) -> None:
    from rich.console import Console
    from rich.table import Table

    table = Table(box=None, pad_edge=False, header_style="bold")
    for h in headers:
        table.add_column(h, overflow="fold")
    for row in rows:
        table.add_row(*["" if v is None else str(v) for v in row])
    Console().print(table)


def _kv(pairs: List[tuple]) -> None:
    width = max(len(k) for k, _ in pairs) if pairs else 0
    for k, v in pairs:
        click.echo("%s  %s" % (k.ljust(width), "-" if v in (None, "") else v))


def _human_bytes(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return "%.0f%s" % (size, unit) if unit == "B" else "%.1f%s" % (size, unit)
        size /= 1024
    return "%.1fT" % size


# ====================================================================
# 运行时上下文
# ====================================================================

class App:
    def __init__(self, home: Optional[str], as_json: bool):
        self.paths = Paths(resolve_home(home))
        self.out = Out(as_json)
        self._ctx: Optional[Ctx] = None

    @property
    def ctx(self) -> Ctx:
        if self._ctx is None:
            self._ctx = Ctx(self.paths, get_platform(), emit=self.out.emit)
        return self._ctx


pass_app = click.make_pass_decorator(App)


def _setup_logging(paths: Paths, as_root: bool) -> None:
    logger = logging.getLogger("mihomo_cli")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if as_root or not os.path.isdir(paths.home):
        # root 身份不往用户主目录写文件（会留下 root 属主的日志）；输出交给 systemd journal。
        logger.addHandler(logging.NullHandler())
        return
    try:
        handler = logging.handlers.RotatingFileHandler(paths.log, maxBytes=LOG_MAX_BYTES,
                                                       backupCount=LOG_BACKUPS, encoding="utf-8")
    except OSError:
        logger.addHandler(logging.NullHandler())
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def _run(app: App, fn: Callable[[Ctx], Any], human: Optional[Callable[[Any], None]] = None) -> Any:
    data = fn(app.ctx)
    app.out.result(app.ctx, data, (lambda: human(data)) if human else None)
    return data


# ====================================================================
# 命令树
# ====================================================================

def build_cli() -> click.Group:  # noqa: C901 —— 命令树集中定义，拆开反而难找
    @click.group(context_settings=CONTEXT, invoke_without_command=True, help=t("help.root"),
                 epilog=t("help.root_epilog"))
    @click.option("--json", "as_json", is_flag=True, help=t("opt.json"))
    @click.option("--lang", type=click.Choice(["zh", "en"]), help=t("opt.lang"))
    @click.option("--home", metavar="DIR", help=t("opt.home"))
    @click.version_option(__version__, "--version", prog_name="mihomo-cli")
    @click.pass_context
    def cli(cctx: click.Context, as_json: bool, lang: Optional[str], home: Optional[str]) -> None:
        cctx.obj = App(home, as_json)
        # 主目录权限过宽时，除下列命令外一律拒绝（方案 §7、R-21）：管理接口套接字是 0666，
        # 只靠目录 0700 挡住其它用户。豁免 doctor（D15 要能报告并修复它）、install / uninstall、
        # 帮助与补全；隐藏子命令由 systemd 调用，拒绝会让服务起不来，由 doctor 负责提示。
        if cctx.invoked_subcommand not in HOME_CHECK_EXEMPT and not _HELP_REQUESTED[0]:
            actions.check_home(cctx.obj.ctx)
        if cctx.invoked_subcommand is None:
            if sys.stdin.isatty() and sys.stdout.isatty() and not as_json:
                from .menu import run_menu

                run_menu(cctx.obj.ctx)
            else:
                click.echo(cctx.get_help())

    # ---------------- status ----------------
    @cli.command(help=t("help.status"), epilog=t("ex.status"))
    @click.option("--no-ip", is_flag=True, help=t("opt.no_ip"))
    @pass_app
    def status(app: App, no_ip: bool) -> None:
        _run(app, lambda c: actions.status(c, with_ip=not no_ip), _print_status)

    # ---------------- install / uninstall / core ----------------
    @cli.command(help=t("help.install"), epilog=t("ex.install"))
    @click.option("--core-version", metavar="V", help=t("opt.core_version"))
    @click.option("--mirror", metavar="URL", help=t("opt.mirror"))
    @click.option("--port", type=int, metavar="N", help=t("opt.port"))
    @pass_app
    def install(app: App, core_version: Optional[str], mirror: Optional[str], port: Optional[int]) -> None:
        _run(app, lambda c: actions.install(c, core_version, mirror, port),
             lambda d: click.echo(t("info.installed", state=d.get("service"))))

    @cli.command(help=t("help.uninstall"), epilog=t("ex.uninstall"))
    @click.option("--purge", is_flag=True, help=t("opt.purge"))
    @click.option("-y", "--yes", is_flag=True, help=t("opt.yes"))
    @pass_app
    def uninstall(app: App, purge: bool, yes: bool) -> None:
        if purge and not yes and not app.out.as_json:
            click.confirm(t("confirm.purge", home=app.paths.home), abort=True)

        def human(d: Any) -> None:
            click.echo(t("info.uninstalled"))
            if d.get("purged"):
                click.echo(t("info.purged"))
        _run(app, lambda c: actions.uninstall(c, purge), human)

    @cli.group(help=t("help.core"), context_settings=CONTEXT, epilog=t("ex.core"))
    def core() -> None:
        pass

    @core.command("upgrade", help=t("help.core_upgrade"), epilog=t("ex.core_upgrade"))
    @click.option("--version", "version", metavar="V", help=t("opt.core_version"))
    @click.option("--mirror", metavar="URL", help=t("opt.mirror"))
    @pass_app
    def core_upgrade(app: App, version: Optional[str], mirror: Optional[str]) -> None:
        _run(app, lambda c: actions.core_upgrade(c, version, mirror),
             lambda d: click.echo(t("info.core_upgraded", old=d.get("from") or "-", new=d["to"])))

    # ---------------- service ----------------
    @cli.group(help=t("help.service"), context_settings=CONTEXT, epilog=t("ex.service"))
    def service() -> None:
        pass

    for action_name in ("start", "stop", "restart"):
        def _make(action_name: str = action_name) -> None:
            @service.command(action_name, help=t("help.service_" + action_name),
                             epilog=t("ex.service_" + action_name))
            @pass_app
            def _cmd(app: App) -> None:
                _run(app, lambda c: actions.service_control(c, action_name),
                     lambda d: click.echo("%s: %s" % (t("st.service"), d["active_state"])))
        _make()

    @service.command("status", help=t("help.service_status"), epilog=t("ex.service_status"))
    @pass_app
    def service_status_cmd(app: App) -> None:
        def fn(c: Ctx) -> Any:
            st = actions.service_status(c)
            return {"installed": st.installed, "active_state": st.active_state, "sub_state": st.sub_state,
                    "main_pid": st.main_pid, "restarts": st.n_restarts, "enabled": st.unit_file_state}
        _run(app, fn, lambda d: _kv([(k, v) for k, v in d.items()]))

    @service.command("logs", help=t("help.service_logs"), epilog=t("ex.service_logs"))
    @click.option("-f", "--follow", is_flag=True, help=t("opt.follow"))
    @click.option("-n", "--lines", type=int, default=200, show_default=True, help=t("opt.lines"))
    @pass_app
    def service_logs(app: App, follow: bool, lines: int) -> None:
        sys.exit(app.ctx.platform.service_logs(follow, lines))

    # ---------------- profile ----------------
    @cli.group(help=t("help.profile"), context_settings=CONTEXT, epilog=t("ex.profile"))
    def profile() -> None:
        pass

    @profile.command("list", help=t("help.profile_list"), epilog=t("ex.profile_list"))
    @pass_app
    def profile_list(app: App) -> None:
        def human(rows: Any) -> None:
            if not rows:
                click.echo(t("info.no_profiles"))
                return
            _table(["", t("col.name"), t("col.kind"), t("col.source"), t("col.manual"), t("col.hidden"),
                    t("col.node")],
                   [["*" if r["active"] else "", r["name"], t("kind." + r["kind"]), r["source"], r["manual"],
                     r["hidden"], r["selected"]] for r in rows])
        _run(app, actions.profile_list, human)

    @profile.command("show", help=t("help.profile_show"), epilog=t("ex.profile_show"))
    @click.argument("name", required=False)
    @click.option("--reveal", is_flag=True, help=t("opt.reveal"))
    @pass_app
    def profile_show(app: App, name: Optional[str], reveal: bool) -> None:
        def human(p: Any) -> None:
            _kv([(t("col.name"), p["name"]), (t("col.kind"), t("kind." + p["kind"])),
                 (t("col.source"), p.get("url") or p.get("file")), (t("col.active"), t("st.yes" if p["active"] else "st.no")),
                 (t("col.node"), p.get("selected")), (t("col.hidden"), ", ".join(p["hidden"]))])
            for i, m in enumerate(p["manual"], 1):
                click.echo("  [%d] %s  %s" % (i, m.get("name") or "?", m["link"]))
        _run(app, lambda c: actions.profile_show(c, name, reveal), human)

    @profile.command("add", help=t("help.profile_add"), epilog=t("ex.profile_add"))
    @click.argument("name")
    @click.option("--url", help=t("opt.url"))
    @click.option("--file", "file", type=click.Path(dir_okay=False), help=t("opt.profile_file"))
    @click.option("--node", "node_links", multiple=True, metavar="LINK", help=t("opt.node_link"))
    @click.option("--interval", type=int, metavar="SEC", help=t("opt.interval"))
    @click.option("--user-agent", metavar="UA", help=t("opt.user_agent"))
    @pass_app
    def profile_add(app: App, name: str, url: Optional[str], file: Optional[str], node_links: tuple,
                    interval: Optional[int], user_agent: Optional[str]) -> None:
        if not (url or file or node_links):
            raise click.UsageError(t("err.profile_source_required"))
        _run(app, lambda c: actions.profile_add(c, name, url, file, node_links, interval, user_agent),
             lambda d: click.echo(t("info.profile_added_active" if d["active"] else "info.profile_added", name=name)))

    @profile.command("rm", help=t("help.profile_rm"), epilog=t("ex.profile_rm"))
    @click.argument("name")
    @pass_app
    def profile_rm(app: App, name: str) -> None:
        _run(app, lambda c: actions.profile_rm(c, name) or {"removed": name},
             lambda d: click.echo(t("info.profile_removed", name=name)))

    @profile.command("use", help=t("help.profile_use"), epilog=t("ex.profile_use"))
    @click.argument("name")
    @pass_app
    def profile_use(app: App, name: str) -> None:
        _run(app, lambda c: actions.profile_use(c, name), lambda d: click.echo(t("info.profile_switched", name=name)))

    @profile.command("update", help=t("help.profile_update"), epilog=t("ex.profile_update"))
    @click.argument("name", required=False)
    @pass_app
    def profile_update(app: App, name: Optional[str]) -> None:
        _run(app, lambda c: actions.profile_update(c, name),
             lambda d: click.echo(t("info.profile_updated", count=d["nodes"])) if d.get("updated") else None)

    @profile.command("rename", help=t("help.profile_rename"), epilog=t("ex.profile_rename"))
    @click.argument("old")
    @click.argument("new")
    @pass_app
    def profile_rename(app: App, old: str, new: str) -> None:
        _run(app, lambda c: actions.profile_rename(c, old, new),
             lambda d: click.echo(t("info.profile_renamed", old=old, new=new)))

    # ---------------- node ----------------
    @cli.group(help=t("help.node"), context_settings=CONTEXT, epilog=t("ex.node"))
    def node() -> None:
        pass

    @node.command("list", help=t("help.node_list"), epilog=t("ex.node_list"))
    @click.option("--delay", is_flag=True, help=t("opt.delay"))
    @pass_app
    def node_list(app: App, delay: bool) -> None:
        def human(d: Any) -> None:
            headers = ["", t("col.index"), t("col.node"), t("col.source")] + ([t("col.delay")] if delay else [])
            rows = []
            for n in d["nodes"]:
                row = ["*" if n["name"] == d["now"] else "", n.get("index") or "", n["name"], t("src." + n["source"])]
                if delay:
                    row.append("%d ms" % n["delay"] if n.get("delay") else t("st.timeout"))
                rows.append(row)
            _table(headers, rows)
            if d["hidden"]:
                click.echo("%s: %s" % (t("col.hidden"), ", ".join(d["hidden"])))
            if d["pending"]:
                click.echo(t("info.pending_nodes"))
                for p in d["pending"]:
                    click.echo("  [%d] %s" % (p["index"], p["summary"]))
            if not d["running"]:
                click.echo(t("info.saved_not_running"), err=True)
        _run(app, lambda c: actions.node_list(c, delay), human)

    @node.command("use", help=t("help.node_use"), epilog=t("ex.node_use"))
    @click.argument("name")
    @pass_app
    def node_use(app: App, name: str) -> None:
        _run(app, lambda c: actions.node_use(c, name),
             lambda d: click.echo(t("info.node_switched", old=d["old"] or "-", new=d["new"])))

    @node.command("add", help=t("help.node_add"), epilog=t("ex.node_add"))
    @click.argument("links", nargs=-1)
    @click.option("--file", "file", type=click.Path(dir_okay=False), help=t("opt.links_file"))
    @pass_app
    def node_add(app: App, links: tuple, file: Optional[str]) -> None:
        if not links and not file:
            raise click.UsageError(t("err.no_links"))
        _run(app, lambda c: actions.node_add(c, links, file),
             lambda d: click.echo(t("info.node_added", count=len(d["added"]))))

    @node.command("rm", help=t("help.node_rm"), epilog=t("ex.node_rm"))
    @click.argument("target")
    @pass_app
    def node_rm(app: App, target: str) -> None:
        _run(app, lambda c: actions.node_rm(c, target),
             lambda d: click.echo(t("info.node_hidden" if d["hidden"] else "info.node_removed", name=d["removed"])))

    @node.command("restore", help=t("help.node_restore"), epilog=t("ex.node_restore"))
    @click.argument("name")
    @pass_app
    def node_restore(app: App, name: str) -> None:
        _run(app, lambda c: actions.node_restore(c, name), lambda d: click.echo(t("info.node_restored", name=name)))

    @node.command("test", help=t("help.node_test"), epilog=t("ex.node_test"))
    @click.argument("name", required=False)
    @click.option("--url", default=actions.render_mod.HEALTH_URL, show_default=True, help=t("opt.test_url"))
    @click.option("--timeout", "timeout_ms", type=click.IntRange(100, 32767), default=5000, show_default=True,
                  help=t("opt.timeout_ms"))
    @pass_app
    def node_test(app: App, name: Optional[str], url: str, timeout_ms: int) -> None:
        def human(d: Any) -> None:
            rows = sorted(d.items(), key=lambda kv: (kv[1] <= 0, kv[1]))
            _table([t("col.node"), t("col.delay")], [[k, "%d ms" % v if v > 0 else t("st.timeout")] for k, v in rows])
        _run(app, lambda c: actions.node_test(c, name, url, timeout_ms), human)

    @node.command("auto", help=t("help.node_auto"), epilog=t("ex.node_auto"))
    @click.option("--url", default=actions.render_mod.HEALTH_URL, show_default=True, help=t("opt.test_url"))
    @click.option("--timeout", "timeout_ms", type=click.IntRange(100, 32767), default=5000, show_default=True,
                  help=t("opt.timeout_ms"))
    @pass_app
    def node_auto(app: App, url: str, timeout_ms: int) -> None:
        _run(app, lambda c: actions.node_auto(c, url, timeout_ms),
             lambda d: click.echo(t("info.node_auto", name=d["node"], delay=d["delay"])))

    # ---------------- mode / cn ----------------
    @cli.command(help=t("help.mode"), epilog=t("ex.mode"))
    @click.argument("value", required=False, type=click.Choice(["global", "rule", "direct"]))
    @pass_app
    def mode(app: App, value: Optional[str]) -> None:
        if value is None:
            _run(app, actions.mode_get, lambda d: click.echo(t("info.mode_now", mode=t("mode." + d["mode"]))))
        else:
            _run(app, lambda c: actions.mode_set(c, value),
                 lambda d: click.echo(t("info.mode_switched", old=t("mode." + d["old"]), new=t("mode." + d["new"]))))

    @cli.command(help=t("help.cn"), epilog=t("ex.cn"))
    @click.argument("value", required=False, type=click.Choice(["direct", "proxy"]))
    @pass_app
    def cn(app: App, value: Optional[str]) -> None:
        if value is None:
            _run(app, actions.cn_get, lambda d: click.echo(t("info.cn_now", value=t("cn." + d["cn_route"]))))
        else:
            _run(app, lambda c: actions.cn_set(c, value),
                 lambda d: click.echo(t("info.cn_switched", old=t("cn." + d["old"]), new=t("cn." + d["new"]))))

    # ---------------- tun ----------------
    @cli.group(help=t("help.tun"), context_settings=CONTEXT, epilog=t("ex.tun"))
    def tun() -> None:
        pass

    @tun.command("on", help=t("help.tun_on"), epilog=t("ex.tun_on"))
    @pass_app
    def tun_on(app: App) -> None:
        from .doctor import d13_ok

        def human(d: Any) -> None:
            if d.get("changed"):
                click.echo(t("info.tun_on_deferred" if d.get("dns") == "deferred" else "info.tun_on"))
        _run(app, lambda c: actions.tun_on(c, d13_ok), human)

    @tun.command("off", help=t("help.tun_off"), epilog=t("ex.tun_off"))
    @pass_app
    def tun_off(app: App) -> None:
        _run(app, actions.tun_off, lambda d: click.echo(t("info.tun_off")) if d.get("changed") else None)

    @tun.command("status", help=t("help.tun_status"), epilog=t("ex.tun_status"))
    @pass_app
    def tun_status(app: App) -> None:
        def human(d: Any) -> None:
            pairs = [(t("st.tun"), t("st.on" if d["tun"] else "st.off")),
                     ("effective", t("st.yes" if d["effective"] else "st.no")),
                     ("device", t("st.yes" if d["device"] else "st.no"))]
            if d.get("suspended"):
                pairs.append(("suspended", d["suspended"]))
            dns = d.get("dns") or {}
            if dns:
                pairs.append(("dns", "%s %s" % (dns.get("default_iface") or "-", " ".join(dns.get("iface_dns") or []))))
                pairs.append(("drop-in", t("st.yes" if dns.get("dropin") else "st.no")))
            _kv(pairs)
        _run(app, actions.tun_status, human)

    # ---------------- proxy-env ----------------
    @cli.command("proxy-env", help=t("help.proxy_env"), epilog=t("ex.proxy_env"))
    @click.argument("value", required=False, default="on", type=click.Choice(["on", "off"]))
    @pass_app
    def proxy_env(app: App, value: str) -> None:
        text = actions.proxy_env(app.ctx, value == "on")
        if app.out.as_json:
            app.out.result(app.ctx, {"script": text})
        else:
            click.echo(text, nl=False)

    # ---------------- conns / logs ----------------
    @cli.command(help=t("help.conns"), epilog=t("ex.conns"))
    @click.option("--watch", is_flag=True, help=t("opt.watch"))
    @pass_app
    def conns(app: App, watch: bool) -> None:
        def human(rows: Any) -> None:
            if not rows:
                click.echo(t("info.no_conns"))
                return
            _table([t("col.host"), t("col.network"), t("col.rule"), t("col.chain"), t("col.traffic")],
                   [[r["host"], r["network"], r["rule"], r["chain"],
                     "%s / %s" % (_human_bytes(r["upload"]), _human_bytes(r["download"]))] for r in rows])
        if not watch or app.out.as_json:
            _run(app, actions.connections, human)
            return
        try:
            while True:
                rows = actions.connections(app.ctx)
                click.clear()
                human(rows)
                time.sleep(2)
        except KeyboardInterrupt:
            pass

    @cli.command(help=t("help.logs"), epilog=t("ex.logs"))
    @click.option("-f", "--follow", is_flag=True, help=t("opt.logs_follow"))
    @click.option("--level", type=click.Choice(["debug", "info", "warning", "error"]), default="info",
                  show_default=True, help=t("opt.level"))
    @pass_app
    def logs(app: App, follow: bool, level: str) -> None:
        if not follow:
            sys.exit(app.ctx.platform.service_logs(False, 100))
        try:
            for item in actions.stream_logs(app.ctx, level):
                if app.out.as_json:
                    click.echo(json.dumps(item, ensure_ascii=False))
                else:
                    click.echo("%s %s" % (str(item.get("type", "")).upper()[:4].ljust(4), item.get("payload", "")))
        except KeyboardInterrupt:
            pass

    # ---------------- backup ----------------
    @cli.group(help=t("help.backup"), context_settings=CONTEXT, epilog=t("ex.backup"))
    def backup() -> None:
        pass

    @backup.command("list", help=t("help.backup_list"), epilog=t("ex.backup_list"))
    @pass_app
    def backup_list(app: App) -> None:
        def human(rows: Any) -> None:
            if not rows:
                click.echo(t("info.no_backups"))
                return
            _table([t("col.backup"), t("col.profile"), t("col.name")],
                   [[r["name"], r["profile"], ", ".join(r["profiles"])] for r in rows])
        _run(app, actions.backup_list, human)

    @backup.command("restore", help=t("help.backup_restore"), epilog=t("ex.backup_restore"))
    @click.argument("name")
    @pass_app
    def backup_restore(app: App, name: str) -> None:
        _run(app, lambda c: actions.backup_restore(c, name), lambda d: click.echo(t("info.backup_restored", name=name)))

    # ---------------- doctor ----------------
    @cli.command(help=t("help.doctor"), epilog=t("ex.doctor"))
    @click.option("--fix", is_flag=True, help=t("opt.fix"))
    @click.option("--json", "local_json", is_flag=True, help=t("opt.json"))
    @pass_app
    def doctor(app: App, fix: bool, local_json: bool) -> None:
        from .doctor import run as run_doctor

        as_json = app.out.as_json or local_json
        results = run_doctor(app.ctx, fix=fix, on_check=None if as_json else _print_check)
        counts = {s: sum(1 for c in results if c.status == s) for s in ("ok", "warn", "fail", "skip")}
        if as_json:
            click.echo(json.dumps({"ok": counts["fail"] == 0, "summary": counts,
                                   "checks": [_check_dict(c) for c in results]}, ensure_ascii=False, indent=2))
        else:
            click.echo(t("chk.summary", **counts))
        if counts["fail"]:
            sys.exit(actions.EXIT_ERROR)

    # ---------------- completion ----------------
    @cli.command(help=t("help.completion"), epilog=t("ex.completion"))
    @click.argument("shell", type=click.Choice(["bash", "zsh"]))
    @click.option("--install", "do_install", is_flag=True, help=t("opt.completion_install"))
    @click.pass_context
    def completion(cctx: click.Context, shell: str, do_install: bool) -> None:
        text = completion_script(cctx.find_root().command, shell)
        if not do_install:
            click.echo(text, nl=False)
            return
        path = install_completion(shell, text)
        click.echo(t("info.completion_installed", path=path))

    # ---------------- help ----------------
    @cli.command("help", help=t("help.help"), epilog=t("ex.help"))
    @click.argument("command", nargs=-1)
    @click.pass_context
    def help_cmd(cctx: click.Context, command: tuple) -> None:
        root = cctx.find_root()
        cmd: click.Command = root.command
        parent = root
        names = []
        for name in command:
            if not isinstance(cmd, click.Group):
                raise click.UsageError(t("err.no_such_command", name=" ".join(command)))
            sub = cmd.get_command(parent, name)
            if sub is None or getattr(sub, "hidden", False):
                raise click.UsageError(t("err.no_such_command", name=" ".join(command)))
            names.append(name)
            parent = click.Context(sub, info_name=name, parent=parent)
            cmd = sub
        click.echo(parent.get_help())

    # ---------------- 隐藏子命令（写进 systemd 单元，名字与参数跨版本不变） ----------------
    @cli.command("_service-preflight", hidden=True)
    @pass_app
    def _preflight(app: App) -> None:
        sys.exit(actions.service_preflight(app.ctx))

    @cli.command("_service-reapply", hidden=True)
    @pass_app
    def _reapply(app: App) -> None:
        sys.exit(actions.service_reapply(app.ctx))

    @cli.command("_tun-dns-apply", hidden=True)
    @pass_app
    def _dns_apply(app: App) -> None:
        sys.exit(actions.tun_dns_apply(app.ctx))

    @cli.command("_tun-dns-revert", hidden=True)
    @pass_app
    def _dns_revert(app: App) -> None:
        sys.exit(actions.tun_dns_revert(app.ctx))

    return cli


# ====================================================================
# 展示辅助
# ====================================================================

def _print_status(d: Any) -> None:
    svc = d["service"]
    svc_text = t("st.not_installed") if not svc["installed"] else "%s (%s)" % (svc["active_state"], svc["enabled"])
    tun = t("st.on" if d["tun"] else "st.off")
    if d.get("tun_suspended"):
        tun = t("warn.tun_suspended", reason=d["tun_suspended"])
    exit_info = d.get("exit")
    pairs = [
        (t("st.service"), svc_text),
        (t("st.kernel"), d.get("kernel_version")),
        (t("st.profile"), "%s (%s)" % (d["profile"], t("kind." + d["profile_kind"])) if d.get("profile") else None),
        (t("st.node"), d.get("node")),
        (t("st.mode"), t("mode." + d["mode"])),
        (t("st.cn"), t("cn." + d["cn_route"])),
        (t("st.tun"), tun),
        (t("st.port"), "127.0.0.1:%s" % d["mixed_port"]),
        (t("st.exit"), "%s %s" % (exit_info.get("ip") or "", exit_info.get("country") or "") if exit_info else None),
        (t("st.home"), d["home"]),
    ]
    _kv(pairs)


def _print_check(c: Check) -> None:
    color = {"ok": "green", "warn": "yellow", "fail": "red", "skip": None}[c.status]
    mark = t("chk." + c.status)
    line = "%s %s %s" % (click.style(mark.ljust(4), fg=color), c.code, c.title)
    if c.fixed is not None:
        line += "  [%s]" % (t("info.fix_applied", code=c.code) if c.fixed else "fix failed")
    click.echo(line)
    if c.detail:
        click.echo("       %s" % c.detail)
    if c.status in ("warn", "fail") and c.hint:
        click.echo("       %s: %s" % (t("chk.hint"), c.hint))
    if c.extra.get("fix_error"):
        click.echo("       %s" % c.extra["fix_error"])


def _check_dict(c: Check) -> dict:
    return {"code": c.code, "title": c.title, "status": c.status, "detail": c.detail, "hint": c.hint,
            "fixable": c.fixable, "fixed": c.fixed, "extra": c.extra}


MPROXY_FUNC = """
# mihomo-cli: mproxy on|off 设置 / 清除当前终端的代理环境变量
mproxy() { eval "$(mihomo-cli proxy-env "${1:-on}")"; }
"""


def completion_script(cli: click.Command, shell: str) -> str:
    from click.shell_completion import get_completion_class

    cls = get_completion_class(shell)
    if cls is None:
        raise click.UsageError(shell)
    return cls(cli, {}, "mihomo-cli", "_MIHOMO_CLI_COMPLETE").source() + MPROXY_FUNC


def install_completion(shell: str, text: str) -> str:
    """补全脚本写到 ~/.local/share/mihomo-cli/，rc 文件里追加一行 source（已有则不重复）。"""
    target_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "mihomo-cli")
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, "completion.%s" % shell)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    rc = os.path.expanduser("~/.bashrc" if shell == "bash" else "~/.zshrc")
    line = '[ -f "%s" ] && source "%s"  # mihomo-cli' % (path, path)
    existing = ""
    if os.path.exists(rc):
        with open(rc, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    if path not in existing:
        with open(rc, "a", encoding="utf-8") as fh:
            fh.write("\n" + line + "\n")
    return path


# ====================================================================
# 入口
# ====================================================================

def _prescan(argv: List[str], option: str) -> Optional[str]:
    for i, arg in enumerate(argv):
        if arg == option and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(option + "="):
            return arg.split("=", 1)[1]
    return None


def _first_command(argv: List[str]) -> Optional[str]:
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in ("--lang", "--home"):
            skip = True
            continue
        if not arg.startswith("-"):
            return arg
    return None


def _print_error(exc: CliError, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps({"ok": False, "code": exc.code, "error": exc.message, "detail": exc.detail,
                               "hints": exc.hints}, ensure_ascii=False, indent=2))
        return
    click.secho(exc.message, fg="red", err=True)
    if exc.detail:
        click.echo("  %s: %s" % (t("label.detail"), exc.detail), err=True)
    for hint in exc.hints:
        click.echo("  %s: %s" % (t("label.hint"), hint), err=True)


def main(argv: Optional[List[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    set_lang(detect_lang(_prescan(argv, "--lang")))
    as_json = "--json" in argv
    _HELP_REQUESTED[0] = bool({"-h", "--help", "--version"} & set(argv))
    command = _first_command(argv)
    as_root = hasattr(os, "geteuid") and os.geteuid() == 0
    # help 子命令与 -h 等价，只打印帮助、不碰主目录，root 下同样放行（老 pipx 以 root 装完常先看帮助）
    if as_root and command not in ROOT_COMMANDS + ("help",) and not ({"-h", "--help", "--version"} & set(argv)):
        # root 运行会在用户主目录留下 root 属主的文件，之后普通用户再也改不动（方案 5.1.7）。
        _print_error(CliError(actions.EXIT_SUDO, t("err.root")), as_json)
        sys.exit(actions.EXIT_SUDO)
    try:
        _setup_logging(Paths(resolve_home(_prescan(argv, "--home"))), as_root)
    except Exception:  # noqa: BLE001 —— 日志不可用不影响命令本身
        pass
    cli = build_cli()
    try:
        cli.main(args=argv, prog_name="mihomo-cli", standalone_mode=False)
    except CliError as exc:
        _print_error(exc, as_json)
        sys.exit(exc.code)
    except NotSupported as exc:
        _print_error(CliError(actions.EXIT_PLATFORM, t("err.not_supported"), detail=str(exc)), as_json)
        sys.exit(actions.EXIT_PLATFORM)
    except click.exceptions.Abort:
        click.echo(t("info.cancelled"), err=True)
        sys.exit(1)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.exceptions.Exit as exc:
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 —— 兜底：记日志、给一句人话，不吐整段回溯
        if os.environ.get("MIHOMO_CLI_DEBUG") == "1":
            raise
        logging.getLogger("mihomo_cli").exception("unexpected error")
        _print_error(CliError(actions.EXIT_ERROR, t("err.unexpected"), detail="%s: %s" % (type(exc).__name__, exc)),
                     as_json)
        sys.exit(actions.EXIT_ERROR)
