# README 首页：徽章、用法示例、终端演示动画与分享预览图

> 2026-10-21 注记：已落地（分支 `feature/readme-demo`）。脚本 `scripts/make-assets.py`（抓取 `capture_session`、绘制 `make_gif` / `make_social`），产物 `docs/assets/demo.gif`、`social-preview.png`，README 改动在 `README.md:5-22`、`README.zh-CN.md:5-22`。本地 T-01～T-06 通过；T-07 需合并到 main 后在 GitHub 网页核。

## 1. 背景

用户要求参照 multi-claude 的 README 首页调整 mihomo-cli 的 README（用户原话：“模仿 multi-claude 的README 进行优化调整，如增加tag标签有动画展示”）。用户已选定：

- 动画内容“用程序真实输出”：脚本运行真正的 mihomo-cli，接测试用假内核与虚构节点，抓取界面输出再绘制成 GIF；
- 同时做“三行用法示例”“分享预览图”“中英两份 README 同步”。

multi-claude 的现成做法（只读参考，`/Users/jakoes/VSCodeProjects/ai/multi-claude`）：

- `README.md:1-24`：标题 → 语言切换 → 5 个徽章（release / CI / PyPI / Python / License）→ 简介 → 三行 `sh` 示例 → `docs/assets/demo.gif`（raw.githubusercontent 绝对地址）→ `<sub>` 说明演示数据是示例；
- `scripts/make-assets.py`（208 行）：临时 HOME 里实际运行程序抓输出，用 Pillow + macOS Menlo/Helvetica 字体逐帧绘制终端样式 GIF（深色背景、标题栏三个圆点、提示符逐字打出），并生成 1280×640 的 `social-preview.png`；支持 `--out`、`--print`、`-h`。

mihomo-cli 现状：`README.md:1-16` 只有标题、语言切换、简介和特性列表，无徽章、无演示；`README.zh-CN.md` 结构相同。

## 2. 目标 / 非目标

目标：

1. 两份 README 首页加徽章、三行用法示例、演示 GIF 与“演示节点为示例”说明。
2. 新增 `scripts/make-assets.py`，离线、可重复地生成 `docs/assets/demo.gif` 与 `docs/assets/social-preview.png`，画面全部来自 mihomo-cli 真实输出。

非目标：

- 不改 `src/` 下任何程序代码、不改 README 中“Install”及之后的章节内容。
- 不把 Pillow 加进运行依赖或 CI；不在 CI 里生成图片。
- 不代替用户在 GitHub 仓库设置里上传分享预览图（只生成文件，用户手动上传）。

## 3. 假设与约束

- 只在 macOS 上运行脚本：字体固定 `/System/Library/Fonts/Menlo.ttc`、`Helvetica.ttc`，找不到即报错退出，不回落（与 multi-claude 一致，防止尺寸悄悄变化）。
- 用开发虚拟环境运行（`.venv/bin/python`，已含项目依赖与 pytest、PyYAML）；Pillow 只装在其中（`.venv/bin/pip install Pillow`），脚本头注释写明。
- 演示数据全部虚构：节点名如 `Tokyo 01`、`Singapore 02`、`Los Angeles 03`，订阅地址 `https://sub.example.com/...`，出口 IP 用文档保留段 `203.0.113.x`。不得出现任何真实节点、真实订阅或真实 IP。
- 图片不进 PyPI 包：`pyproject.toml:56-57` 的 sdist `include` 白名单不含 `docs/`，release 只上传 `dist/*`（`.github/workflows/release.yml` github-release 步骤），无需 `.gitattributes`。

## 4. 涉及模块

| 区域 | 行号锚点（基线 main 9fa2b6b） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `README.md` | `:3` 与 `:5` 之间；`:6` 简介之后 | 修改 | 插入徽章行；简介后插入三行示例与 GIF、说明 |
| `README.zh-CN.md` | 同上对应位置 | 修改 | 同步中文版 |
| `scripts/make-assets.py` | 新文件 | 新增 | 抓取真实输出 + 绘制 GIF 与预览图 |
| `docs/assets/demo.gif` | 新文件 | 新增 | 脚本产物 |
| `docs/assets/social-preview.png` | 新文件 | 新增 | 脚本产物 |
| `CHANGELOG.md` | `:3` 之前 | 修改 | 加 `## Unreleased` 一条 README 说明 |

脚本只 import、不修改的现有代码：`tests/conftest.py:25-37`（`FAKE_CORE`）、`:40-97`（`FakeKernel`）、`:99-170`（`FakeApi`）、`:172-238`（`FakePlatform`）；`src/mihomo_cli/cli.py:102`（`get_platform()` 构造 Ctx）、`:713`（`main(argv)`）；`src/mihomo_cli/actions.py:620`（`exit_ip`）；`src/mihomo_cli/state.py:65-72`（`resolve_home`，`--home` 优先）。

## 5. 方案

### 5.1 实现要点

**徽章**（插入 `README.md:3` 之后，中文版同）：

```markdown
[![Release](https://img.shields.io/github/v/release/jakoes-wu/mihomo-cli)](https://github.com/jakoes-wu/mihomo-cli/releases)
[![CI](https://github.com/jakoes-wu/mihomo-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/jakoes-wu/mihomo-cli/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mihomo-cli)](https://pypi.org/project/mihomo-cli/)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
```

**三行示例 + 动画**（插入简介段之后、特性列表之前）：

```sh
mihomo-cli profile add airport --url '<subscription>'   # add your proxy service
mihomo-cli node auto                                    # pick the fastest node
mihomo-cli                                              # interactive menu
```

GIF 用绝对地址 `https://raw.githubusercontent.com/jakoes-wu/mihomo-cli/main/docs/assets/demo.gif`（PyPI 页面渲染 README 时相对路径会失效），下方 `<sub>The nodes in the demo are examples.</sub>`；中文版“演示中的节点为示例。”

**抓取真实输出**（`capture_session()`）：

1. `tempfile.mkdtemp()` 建数据目录，结束时 `shutil.rmtree`。
2. `sys.path` 加入仓库 `src/` 与 `tests/`，import `conftest` 取 `FAKE_CORE / FakeKernel / FakeApi / FakePlatform`；写 `FAKE_CORE` 到 `paths.core` 并加执行位、建空 `paths.sock`（同 `tests/conftest.py:247-250`）。
3. 打桩（只在脚本进程内，用 `unittest.mock.patch`）：`mihomo_cli.cli.get_platform` → 返回同一个 `FakePlatform`；`actions.Ctx.api` → `FakeApi(kernel)`；`actions._tcp_ok` → `True`；`actions.exit_ip` → `{"ip": "203.0.113.24", "country": "SG"}`（与 `actions.py:620-636` 返回结构一致；国家与自动选中的 Singapore 01 对应）；并从环境变量里去掉测试故障注入开关 `MIHOMO_CLI_TEST_*`（同 `tests/conftest.py:244-246`）。
4. `kernel.sub_nodes[<示例订阅>] = [虚构节点…]`，`kernel.delays = {节点: 毫秒}`，固定数值保证可重复。
5. 依次调用 `cli.main(["--home", tmp, "--lang", "en", *args])`，用 `contextlib.redirect_stdout/stderr` 捕获，`SystemExit` 退出码非 0 即报错终止；设 `NO_COLOR=1`（防御性；表格宽度只由内容决定，cli 不读终端宽度）；每条命令后关闭 `cli.main` 挂到 `mihomo_cli` 日志器上的文件 handler。演示命令：
   - `profile add airport --url https://sub.example.com/...`
   - `node list --delay`
   - `node auto`
   - `mode global`
   - `status`
6. 输出里把临时目录路径替换成 `~/.local/share/mihomo-cli`。

**绘制**：移植 multi-claude `scripts/make-assets.py` 的 `load_fonts / make_gif / make_social`（配色、标题栏、逐字打出提示符、命令间停顿、末帧停留）；画布宽 760 像素（容纳 80 列以内的输出），高度按全部行数一次定好、不滚动（5 段命令共 25 行）。与原算法唯一的差别是调色板：所有帧共用一套取自末帧的 128 色调色板，取色样本垫上标题栏按钮纯色块——逐帧各自量化时，文字多的帧会把红黄绿按钮挤成灰色。预览图左侧标题 `mihomo-cli` + 一句标语，右侧取 `node list --delay` 那段输出。

**CLI**：`-h/--help`（含用法示例）、`--out DIR`（默认 `docs/assets/`）、`--print`（只打印抓到的会话、不画图，便于核对内容）。

### 5.2 接口变更

无接口变更（不改程序代码、配置、命令行接口；新增的是开发者脚本与文档图片）。

## 6. 备选方案与决策

- 手写演示文字：用户已否决（界面改了动画不会跟着变）。
- 用 vhs / asciinema 录制：需要额外安装工具且要真实运行内核和节点，否决。

## 7. 影响分析

- 程序行为：不改 `src/`，运行时零影响。脚本对 `cli.get_platform`、`Ctx.api` 等的打桩只在脚本进程内生效，不写任何真实路径（数据目录是临时目录，不调用 systemctl / sudo，`FakePlatform` 全部是内存操作）。
- 测试：脚本 import `tests/conftest.py`，以后有人改假内核接口时脚本可能跟着坏；这是开发工具，坏了重跑时就会报错，不影响 CI 与发布。
- 发布：图片不进 sdist / wheel（见 §3），包体积不变；README 改动随 PyPI 下次发布同步到 PyPI 页面。
- 隐私：画面只含虚构节点、example.com 订阅、203.0.113.x 出口，提交前用 `--print` 人工核一遍。

## 8. 回归测试

| 编号 | 方法 | 预期 |
| ---- | ---- | ---- |
| T-01 | `.venv/bin/python scripts/make-assets.py --print` | 打印 5 段命令与输出，退出码 0，无临时目录路径、无真实节点 / IP |
| T-02 | `.venv/bin/python scripts/make-assets.py` 连跑两次 | 两次生成的 `demo.gif`、`social-preview.png` 的 sha256 相同（可重复） |
| T-03 | 运行后检查 | `/tmp` 下无本脚本残留的临时目录；真实 `~/.local/share/mihomo-cli` 不存在或未被修改（运行前后对比 mtime） |
| T-04 | `-h`；`--out /tmp/x` | 帮助含示例、退出码 0；图片写到指定目录 |
| T-05 | 本地用 Read 查看 GIF 首帧与末帧、预览图 | 文字完整不截断、无乱码 |
| T-06 | `pytest -q`、`ruff check .` | 全过（脚本纳入 ruff） |
| T-07 | 推送分支后用分支的 raw 地址打开 GIF；合并到 main 后在 GitHub 网页看两份 README | 合并前：分支 raw 地址的 GIF 能播放；合并后：徽章显示、README 里 GIF 能播放、中英版一致（README 用 main 的绝对地址，合并前分支页面上图片显示不出来属预期） |

## 9. 日志 / 观测点

- 脚本每步打印 `[make-assets] capture <命令>` / `write <路径> (<KB> KB)`；任一命令退出码非 0 时打印命令与输出后以非 0 退出。
- `--print` 输出即抓到的完整会话，是核对画面内容的依据。
