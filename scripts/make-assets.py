#!/usr/bin/env python3
"""生成 README 演示 GIF 与 GitHub 社交预览图（docs/feature/feature-readme-demo.md §5.1）。

前置条件：
  - 在 macOS 上运行：字体固定用 /System/Library/Fonts/Menlo.ttc 与 Helvetica.ttc，找不到就报错退出，
    不回落到其它字体——换字体会让图片尺寸和排版悄悄变化。
  - 用仓库开发虚拟环境运行（.venv/bin/python），其中装好本项目依赖与 pytest、PyYAML，
    以及只供本脚本使用的 Pillow（.venv/bin/pip install Pillow）；不需要网络。
  - 从仓库任意目录运行均可：用本仓库 src/ 下的 mihomo-cli，输出默认写到仓库的 docs/assets/。

做法：在本进程里实际运行 mihomo-cli 的命令入口（cli.main），把系统层换成测试用的 FakePlatform、
管理接口换成 FakeApi + FakeKernel（tests/conftest.py），出口 IP 查询换成固定的文档保留地址，
抓取界面的真实输出，再按终端样式逐帧绘制。节点、订阅地址、IP 全部虚构。
数据目录是临时目录，结束时删除；不调用 systemctl / sudo，不碰真实的 ~/.local/share/mihomo-cli。
"""

import argparse
import contextlib
import io
import logging
import os
import shutil
import stat
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src"), os.path.join(ROOT, "tests")]

MONO_FONT = "/System/Library/Fonts/Menlo.ttc"
SANS_FONT = "/System/Library/Fonts/Helvetica.ttc"

# 终端配色：深色背景，提示符绿色、注释灰色、输出浅色（与 multi-claude 的演示图一致）。
BG = (30, 30, 46)
FG = (205, 214, 244)
PROMPT = (166, 227, 161)
COMMENT = (127, 132, 156)
TITLE_BAR = (49, 50, 68)
BUTTONS = ((243, 139, 168), (249, 226, 175), (166, 227, 161))  # 标题栏红黄绿三个圆点

# 演示数据全部虚构：example.com 订阅，203.0.113.0/24 是文档专用地址段（RFC 5737）。
DEMO_SUB = "https://sub.example.com/clash?token=demo"
DEMO_NODES = ["Tokyo 01", "Tokyo 02", "Singapore 01", "Hong Kong 01", "Los Angeles 01"]
DEMO_DELAYS = {"Tokyo 01": 86, "Tokyo 02": 112, "Singapore 01": 64, "Hong Kong 01": 0, "Los Angeles 01": 158}
DEMO_EXIT = {"ip": "203.0.113.24", "country": "SG"}
# 演示里显示给读者的数据目录（真实默认位置），替换临时目录的绝对路径。
SHOWN_HOME = "~/.local/share/mihomo-cli"
# 预览图取这一段输出画测速表；演示序列与预览图共用这个常量，改一处不会对不上。
TABLE_COMMAND = "mihomo-cli node list --delay"
# 测试用故障注入开关（render.py / actions.py 读取）：开发者 shell 里残留时会让演示失败或画面变样，
# 运行前一律去掉（同 tests/conftest.py env 夹具）。
FAULT_ENV = ("MIHOMO_CLI_TEST_BAD_CONFIG", "MIHOMO_CLI_TEST_FAIL_VERIFY", "MIHOMO_CLI_TEST_FAIL_ROLLBACK",
             "MIHOMO_CLI_TEST_FAIL_DNS_REVERT")

EXAMPLES = """examples:
  .venv/bin/python scripts/make-assets.py                    write docs/assets/demo.gif and social-preview.png
  .venv/bin/python scripts/make-assets.py --out /tmp/assets  write the two images to another directory
  .venv/bin/python scripts/make-assets.py --print            only print the captured terminal session
"""


def log(message):
    print("[make-assets] " + message, file=sys.stderr)


def capture_session():
    """在临时数据目录里运行演示命令，返回 [(命令或注释, 输出行列表)]。"""
    # 延迟导入：-h 不需要项目依赖也能显示。
    import conftest
    from mihomo_cli import actions, cli
    from mihomo_cli.state import Paths, ensure_home

    tmp = tempfile.mkdtemp(prefix="mihomo-cli-assets-")
    try:
        home = os.path.join(tmp, "home")
        paths = Paths(home)
        ensure_home(paths)
        # 假内核可执行文件与管理套接字占位：让“内核在运行”的判断成立（同 tests/conftest.py env 夹具）。
        with open(paths.core, "w") as handle:
            handle.write(conftest.FAKE_CORE)
        os.chmod(paths.core, os.stat(paths.core).st_mode | stat.S_IXUSR)
        open(paths.sock, "w").close()
        platform = conftest.FakePlatform()
        kernel = conftest.FakeKernel(paths)
        kernel.sub_nodes[DEMO_SUB] = list(DEMO_NODES)
        kernel.delays = dict(DEMO_DELAYS)

        def run(*args):
            out = io.StringIO()
            code = 0
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    cli.main(["--home", home, "--lang", "en"] + list(args))
                except SystemExit as exc:
                    code = exc.code or 0
                finally:
                    # cli.main 每次都给 mihomo_cli 日志器挂一个写临时目录的文件 handler，用完即关，
                    # 否则 5 条命令叠 5 个 handler，同一条日志重复写。
                    logger = logging.getLogger("mihomo_cli")
                    for handler in list(logger.handlers):
                        logger.removeHandler(handler)
                        handler.close()
            text = out.getvalue().rstrip("\n").replace(home, SHOWN_HOME)
            if code != 0:
                sys.exit("[make-assets] command failed (exit %s): mihomo-cli %s\n%s" % (code, " ".join(args), text))
            log("capture mihomo-cli " + " ".join(args))
            # 表格列按宽度补了空格，行尾空白画出来看不见，去掉免得影响宽度计算。
            return [line.rstrip() for line in text.splitlines()]

        patches = [
            mock.patch.object(cli, "get_platform", lambda: platform),
            mock.patch.object(actions.Ctx, "api", lambda self: conftest.FakeApi(kernel)),
            mock.patch.object(actions, "_tcp_ok", lambda port, timeout=2.0: True),
            mock.patch.object(actions, "exit_ip", lambda port: dict(DEMO_EXIT)),
            # 关掉颜色：输出被重定向时本就不带颜色，这里防御性再保证一次。
            mock.patch.dict(os.environ, {"NO_COLOR": "1"}),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            for name in FAULT_ENV:
                os.environ.pop(name, None)  # patch.dict 退出时恢复原值
            return [
                ("mihomo-cli profile add airport --url '<subscription>'",
                 run("profile", "add", "airport", "--url", DEMO_SUB)),
                (TABLE_COMMAND, run("node", "list", "--delay")),
                ("mihomo-cli node auto", run("node", "auto")),
                ("mihomo-cli mode global", run("mode", "global")),
                ("mihomo-cli status", run("status")),
            ]
    finally:
        shutil.rmtree(tmp)


def load_fonts():
    for path in (MONO_FONT, SANS_FONT):
        if not os.path.exists(path):
            sys.exit(f"font not found: {path} (this script runs on macOS only, see -h)")
    from PIL import ImageFont
    return (ImageFont.truetype(MONO_FONT, 15), ImageFont.truetype(SANS_FONT, 64), ImageFont.truetype(SANS_FONT, 30),
            ImageFont.truetype(MONO_FONT, 21))


def make_gif(session, out_path, mono):
    """终端样式逐帧动画：命令逐字打出，输出整段出现；画布高度按全部行数一次定好，不滚动。"""
    from PIL import Image, ImageDraw

    width, line_h, pad, top = 760, 21, 18, 34
    rows = sum(1 + len(output) for _, output in session)
    height = top + pad * 2 + rows * line_h
    lines = []  # 已经显示的 (颜色, 文本)
    frames, durations = [], []

    def render(cursor_text=None):
        image = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, width, top - 8], fill=TITLE_BAR)
        for i, color in enumerate(BUTTONS):
            draw.ellipse([14 + i * 20, 9, 26 + i * 20, 21], fill=color)
        y = top + pad
        shown = lines + ([cursor_text] if cursor_text else [])
        for color, text in shown:
            if color == PROMPT:
                draw.text((pad, y), "$", font=mono, fill=PROMPT)
                draw.text((pad + 18, y), text, font=mono, fill=FG)
            else:
                draw.text((pad, y), text, font=mono, fill=color)
            y += line_h
        return image

    def add(image, ms):
        frames.append(image)
        durations.append(ms)

    add(render(), 600)
    for command, output in session:
        is_comment = command.startswith("#")
        color = COMMENT if is_comment else PROMPT
        # 逐字出现，每 2 个字符一帧；注释整行出现。
        if not is_comment:
            for end in range(2, len(command) + 2, 2):
                add(render((color, command[:end] + "_")), 45)
        lines.append((color, command))
        add(render(), 500 if not is_comment else 1400)
        if output:
            lines.extend((FG, line) for line in output)
            add(render(), 1800)
    add(render(), 3500)
    # 全部帧共用一套调色板，取自内容最多的末帧：逐帧各自量化时，文字多的帧会把
    # 窗口按钮的红黄绿挤成灰色，帧间颜色也会闪。
    # 按钮只占几十个像素，直接量化会被当成噪点丢掉；取色样本下方垫上按钮纯色块保住它们。
    sample = Image.new("RGB", (width, height + 60), BG)
    sample.paste(frames[-1], (0, 0))
    for i, color in enumerate(BUTTONS):
        sample.paste(color, (i * 60, height, i * 60 + 60, height + 60))
    palette = sample.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    palette_frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    palette_frames[0].save(out_path, save_all=True, append_images=palette_frames[1:], duration=durations,
                           loop=0, optimize=True, disposal=1)


def make_social(session, out_path, mono, title_font, tagline_font):
    """社交预览图：链接被分享到 X、Reddit、Slack 时显示的缩略图。只放测速表，字号要大到缩略图里也看得清。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1280, 640), BG)
    draw = ImageDraw.Draw(image)
    draw.text((80, 60), "mihomo-cli", font=title_font, fill=FG)
    draw.text((82, 145), "A command-line Clash-style proxy client for headless Linux", font=tagline_font,
              fill=COMMENT)
    box = [80, 215, 1200, 525]
    draw.rounded_rectangle(box, radius=14, fill=(24, 24, 37), outline=TITLE_BAR, width=2)
    command = TABLE_COMMAND
    table = next(output for cmd, output in session if cmd == command)
    y = box[1] + 26
    draw.text((box[0] + 32, y), "$", font=mono, fill=PROMPT)
    draw.text((box[0] + 58, y), command, font=mono, fill=FG)
    y += 8
    for line in table:
        y += 40
        draw.text((box[0] + 32, y), line, font=mono, fill=FG)
    draw.text((82, 555), "github.com/jakoes-wu/mihomo-cli", font=tagline_font, fill=PROMPT)
    image.save(out_path, optimize=True)


def main():
    parser = argparse.ArgumentParser(
        description="Generate docs/assets/demo.gif and docs/assets/social-preview.png from a real "
                    "mihomo-cli session against a fake core (macOS, needs Pillow).",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=os.path.join(ROOT, "docs", "assets"),
                        help="output directory (default: docs/assets in this repository)")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="print the captured session and exit without drawing")
    args = parser.parse_args()

    session = capture_session()
    if args.print_only:
        for command, output in session:
            print(command if command.startswith("#") else "$ " + command)
            for line in output:
                print(line)
        return 0
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("Pillow is required: .venv/bin/pip install Pillow")
    mono, title_font, tagline_font, social_mono = load_fonts()
    os.makedirs(args.out, exist_ok=True)
    gif = os.path.join(args.out, "demo.gif")
    png = os.path.join(args.out, "social-preview.png")
    make_gif(session, gif, mono)
    make_social(session, png, social_mono, title_font, tagline_font)
    log(f"write {gif} ({os.path.getsize(gif) // 1024} KB)")
    log(f"write {png} ({os.path.getsize(png) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
