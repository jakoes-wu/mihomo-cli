# 修复：Python 3.8 上交互菜单打不开

> 2026-10-21 注记：方案已定，代码待落地。问题来自 0.1.1 在编译机宿主（Ubuntu 20.04，Python 3.8.10）上的实测。
>
> **2026-10-21 落地注记**：代码已按本方案落地（未提交），代码评审第 1 轮无阻塞 / 高 / 中级问题，按评审的低级建议补了发布包依赖信息检查（§8.1 第 3 条末段、M-09）。验证位置：依赖约束 `pyproject.toml:33-37`；真实选择框测试 `tests/test_menu.py:78-91`；发版检查 `.github/workflows/release.yml:73-74`（依赖清单）、`:83-87`（依赖信息检查）、`:88-95`（真实选择框）。已通过：M-03（编译机 3.8 临时环境，prompt_toolkit 3.0.52 时新测试报 AttributeError、3.0.51 时通过）、M-04（3.8 解析为 questionary 2.1.0 + prompt_toolkit 3.0.51，全量 158 个测试通过）、M-05（3.11 解析为 2.1.1 + 3.0.53）、M-07（ruff 通过，158 个测试通过）、M-09。待验证：M-01 / M-02 等 CI，M-06 等用户在编译机装上修复版后操作，M-08 等下次发版。

## 1. 背景

在编译机上不带参数运行 `mihomo-cli`，先打印一屏概况，然后报错退出，交互菜单进不去：

```text
An unexpected error occurred.
  Detail: AttributeError: 'VSplit' object has no attribute 'content'
```

带子命令的用法（`status`、`node use` 等）不受影响。编译机安装环境 `~/.local/share/mihomo-cli-venv` 里的版本：Python 3.8.10、mihomo-cli 0.1.1、questionary 2.1.0、prompt_toolkit 3.0.52。

## 2. 目标 / 非目标

目标：

1. Python 3.8 上新装或重装 mihomo-cli 后，交互菜单能正常打开。
2. 测试与发版检查能抓到“真实选择框打不开”这类问题。

非目标：

- 不改菜单的功能和文案。
- 不放弃对 Python 3.8 的支持，也不换掉 questionary。
- 不负责修复用户已经装好的旧环境：旧环境需要重装或升级到修复版（见 §8.2）。

## 3. 假设与约束

- `pyproject.toml` 的依赖写法要支持 PEP 508 环境标记（`; python_version < "3.9"`），pip 和 pipx 都认这种写法。
- 发版流程 `release.yml` 里手抄了一份依赖清单，按 `.github/workflows/release.yml:72` 的注释，必须和 `pyproject.toml` 同步修改。

## 4. 根因分析

- **现象**：只要打开菜单（`src/mihomo_cli/cli.py:153-156` 在终端里调用 `run_menu`），第一个 `questionary.select` 就会抛 `AttributeError`。
- **出错位置**：questionary 2.1.0 的 `questionary/prompts/common.py:591`，`_fix_unecessary_blank_lines()` 按固定路径取界面元素：`default_container.get_children()[0].content.get_children()[1].content`。这条路径写死了 prompt_toolkit 默认界面的嵌套结构。
- **触发条件**：prompt_toolkit 3.0.52 改了这个嵌套结构，取到的第一个子元素变成了 `VSplit`，它没有 `.content` 属性。questionary 2.1.1 已把这里改成遍历 `ps.layout.find_all_windows()`，按类型查找（本机 `.venv` 中 `questionary/prompts/common.py:589-594`），不再依赖结构。
- **为什么只有 Python 3.8 出问题**：questionary 2.1.1 的元数据要求 `Requires-Python: >=3.9`，所以 Python 3.8 上 pip 最高只能装 2.1.0。prompt_toolkit 3.0.52 仍支持 3.8，会被装上。我们的依赖只写了 `questionary>=1.10`（`pyproject.toml:31`），没有限制 prompt_toolkit 的版本。
- **为什么测试没抓到**：`tests/test_menu.py:19-27` 把 `questionary.select/text/confirm/path` 全部换成了假对象，测试从来没有真正画过选择框。发版流程在 Ubuntu 20.04 容器里只跑了 `--version`、`--help`、`help node add`（`release.yml:79-81`），也没有打开选择框。

## 5. 复现步骤

前置：Python 3.8 环境（或任意 Python 版本，手动装 questionary 2.1.0）。

```bash
python3 -m venv /tmp/v && /tmp/v/bin/pip install 'questionary==2.1.0' 'prompt_toolkit==3.0.52'
cat > /tmp/repro.py <<'EOF'
import questionary
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
with create_pipe_input() as inp:
    inp.send_text("\r")
    print("result:", questionary.select("t", choices=["a", "b"], input=inp, output=DummyOutput()).ask())
EOF
/tmp/v/bin/python -I /tmp/repro.py      # AttributeError: 'VSplit' object has no attribute 'content'
/tmp/v/bin/pip install 'prompt_toolkit==3.0.51'
/tmp/v/bin/python -I /tmp/repro.py      # result: a
```

2026-10-21 已实测：编译机安装环境中必现；本机临时环境 questionary 2.1.0 配 3.0.52 必现，配 3.0.51 正常。

## 6. 影响版本

- mihomo-cli 0.1.0、0.1.1。
- 只影响 Python 3.8（典型是 Ubuntu 20.04 自带的 python3），并且装到了 prompt_toolkit ≥3.0.52 的环境。
- Python ≥3.9 上 pip 会装 questionary 2.1.1，不受影响。

## 7. 涉及模块

| 区域 | 行号锚点 | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `pyproject.toml` 依赖注释 | `:27` | 修改 | “只写下限”改为说明 3.8 上 prompt_toolkit 的例外 |
| `pyproject.toml` 依赖 | `:28-34` | 新增 | 加两条带环境标记的依赖 |
| `.github/workflows/release.yml` 安装检查 | `:73` | 修改 | 依赖清单同步加这两条 |
| `.github/workflows/release.yml` 安装检查 | 同一 `run` 块 `:81` 之后 | 新增 | 用假输入打开一次真实选择框 |
| `.github/workflows/release.yml` 安装检查 | 同一 `run` 块 `:81` 之后 | 新增 | 核发布包自带的依赖里有这两条约束（代码评审第 1 轮补充） |
| `tests/test_menu.py` 模块说明 | `:1` | 修改 | 说明多了一条不打补丁的真实选择框测试 |
| `tests/test_menu.py` | 文件末尾 `:72` 之后 | 新增 | 真实选择框冒烟测试 |
| `docs/feature/feature-mihomo-cli-v1.md` | `:44` | 修改 | 行尾加注记：3.8 上 prompt_toolkit 已限制在 3.0.52 以下，链到本文 |
| `CHANGELOG.md` | `## Unreleased` 下 `:3-9` | 新增 | 记一条修复 |

## 8. 方案

### 8.1 实现要点

1. `pyproject.toml:28-34` 的 `dependencies` 里，紧接 `"questionary>=1.10",`（`:31`）之后追加：

   ```toml
   # Python 3.8 只能装到 questionary 2.1.0，它与 prompt_toolkit 3.0.52 起的界面结构不兼容，菜单会崩
   "prompt_toolkit<3.0.52; python_version < '3.9'",
   # 3.9 起要求已修复的 questionary，避免旧环境沿用 2.1.0 后遇到新版 prompt_toolkit
   "questionary>=2.1.1; python_version >= '3.9'",
   ```

   同时把 `pyproject.toml:27` 的注释改为：一般只写下限，由 pip 按 `requires_python` 挑出 3.8 能装的最后版本；例外是 3.8 上 prompt_toolkit 必须低于 3.0.52（见 `docs/bugfix/fix-menu-py38.md`）。`docs/feature/feature-mihomo-cli-v1.md:44` 行尾加一句注记，说明 prompt_toolkit 3.0.52 这一解析结果已被本修复改为 3.0.51。

   第一条是修复本身。第二条防的是另一种情况：Python ≥3.9 的旧环境里如果残留 questionary 2.1.0，prompt_toolkit 又被升级，就会出同样的错；要求 2.1.1 以后可以排除这种组合，代价只有一行。

2. `release.yml:73` 的 pip 安装行追加同样两条依赖（shell 里用双引号包住整条，例如 `"prompt_toolkit<3.0.52; python_version < '3.9'"`），保持与 `pyproject.toml` 一致。

3. 在现有同一个 `run: |` 块里、`release.yml:81` 之后接着写（直接用 `/opt/v`，不另起步骤）：用假输入打开一次真实选择框，返回值不是 `a` 就失败。回车后再送一个 Ctrl-C（`\x03`），万一回车没被选择框接收，`ask()` 会因 Ctrl-C 返回 `None` 并让断言失败，不会一直等输入：

   ```bash
   /opt/v/bin/python -I -c 'import questionary
   from prompt_toolkit.input import create_pipe_input
   from prompt_toolkit.output import DummyOutput
   with create_pipe_input() as i:
       i.send_text("\r\x03")
       assert questionary.select("t", choices=["a", "b"], input=i, output=DummyOutput()).ask() == "a"'
   ```

   这样 Ubuntu 20.04 容器（Python 3.8）会真正画一次选择框。

   同一处再加一项依赖信息检查（代码评审第 1 轮补充）：本包是 `--no-deps` 从 TestPyPI 装的，依赖靠上面的手抄清单提供，手抄清单和 `pyproject.toml` 不一致时选择框检查照样会过。所以用 `importlib.metadata.requires("mihomo-cli")` 读出发布包自带的依赖写到临时文件，分两行 `grep -F` 核 `prompt-toolkit<3.0.52; python_version <` 与 `questionary>=2.1.1; python_version >=`（连同 Python 版本条件一起核，防止条件被误删；不含引号，因为打包工具可能改引号风格；打包后名字会规范成 `prompt-toolkit`）。分两行写是因为 GitHub Actions 的 `bash -e` 下，`a && b` 里 `a` 失败不会让步骤失败。

4. `tests/test_menu.py` 末尾新增 `test_real_select_renders`：不打补丁，用 `create_pipe_input()` + `DummyOutput()` 调真实的 `questionary.select`，送回车加 Ctrl-C（`"\r\x03"`，理由同上，防止卡住），断言返回第一个选项。同时把 `:1` 的模块说明补上“另有一条不打补丁的真实选择框测试”。CI 的 3.8 矩阵（`ci.yml:35`）按新依赖安装后会跑到它；回退修复时它在 3.8 上必失败。

5. `CHANGELOG.md` 的 `## Unreleased` 下用英文加一条（与现有条目一致），意思是：Python 3.8 上交互菜单打开即崩（questionary 2.1.0 与 prompt_toolkit 3.0.52 不兼容），现在在 Python 3.8 上限制 prompt_toolkit < 3.0.52。

### 8.2 接口变更

- 安装依赖变化：Python 3.8 上 prompt_toolkit 被限制在 3.0.52 以下；Python ≥3.9 上 questionary 下限提到 2.1.1。命令行、配置文件、数据目录都没有变化。
- 已安装的旧环境：`pipx upgrade mihomo-cli` 或重新安装到修复版时，pip 会按新约束把 prompt_toolkit 降到 3.0.51。用 venv 手装的（如编译机）要用该 venv 的 pip 升级 mihomo-cli，或单独执行 `pip install 'prompt_toolkit<3.0.52'`。
- 不涉及 `docs/reference/*`。

## 9. 备选方案与决策

- **用 `python_requires` 放弃 3.8**：否决。README 与 CI 都承诺支持 3.8，Ubuntu 20.04 自带的就是 3.8。
- **在 mihomo-cli 里给 questionary 2.1.0 打运行时补丁**（替换 `_fix_unecessary_blank_lines`）：否决。会依赖第三方库的私有函数，比改依赖约束更脆弱。
- **只把 questionary 钉在 2.1.0、prompt_toolkit 钉在 3.0.51（不分 Python 版本）**：否决。3.9 及以上本来没有问题，没必要锁住它们的升级。

## 10. 影响分析

- **正向**：依赖约束只影响 pip 解析结果。Python 3.8 上 prompt_toolkit 从 3.0.52 退到 3.0.51；`rich`、`click` 不依赖 prompt_toolkit，不受影响。questionary 2.1.0 要求 `prompt_toolkit>=2.0,<4.0`，3.0.51 在范围内。
- **反向**：仓库里直接 import prompt_toolkit 的地方只有新增的测试，以及发版检查里的那段代码；菜单代码 `src/mihomo_cli/menu.py` 只用 questionary 的公开接口（`select/text/confirm/path/Choice`），没有用到 3.0.52 才有的接口。
- **运行时**：没有进程、频率或网络方面的变化；新增测试只在内存里画一次选择框，耗时可以忽略。
- **部署形态**：Python ≥3.9 的新安装会得到 questionary ≥2.1.1，与现在的实际解析结果一致。
- **CI**：3.8 矩阵安装的 prompt_toolkit 会变成 3.0.51；新增测试在所有 Python 版本上都会跑。

## 11. 回归测试

| 编号 | 场景 | 方法 | 预期 |
| ---- | ---- | ---- | ---- |
| M-01 | 真实选择框（3.8） | CI 3.8 矩阵跑 `pytest -q` | `test_real_select_renders` 通过 |
| M-02 | 真实选择框（3.9–3.13） | CI 其余矩阵 | 同上，全部通过 |
| M-03 | 修复确实生效 | 编译机临时目录的 3.8 venv（同 M-04）`pip install '.[test]'` 后，单独 `pip install prompt_toolkit==3.0.52` 跑新增测试；再装回 3.0.51 重跑 | 3.0.52 时失败（AttributeError）；3.0.51 时通过 |
| M-04 | 依赖解析（3.8） | 编译机临时目录建 3.8 venv，`pip install` 本仓库快照 | 装到 questionary 2.1.0 + prompt_toolkit <3.0.52 |
| M-05 | 依赖解析（≥3.9） | 本机 3.11 临时 venv，`pip install .` | questionary ≥2.1.1，prompt_toolkit 不受限 |
| M-06 | 编译机真实菜单 | 用户在编译机终端不带参数运行 `mihomo-cli`（装修复版后） | 进入菜单，方向键可选，选“退出”或 Ctrl-C 正常退出 |
| M-07 | 原有测试 | `ruff check src tests` + `pytest -q` | 全部通过 |
| M-08 | 发版检查 | 下次发版时看 `release.yml` 的 Ubuntu 20.04 安装检查 | 选择框步骤与依赖信息检查都通过 |
| M-09 | 依赖信息检查本身 | 本机构建安装包后用 `--no-deps` 装进临时 venv，照抄 `release.yml` 里的检查行执行；再把要找的字符串改成不存在的值重跑 | 前者退出码 0；后者非 0 |

M-04 只在编译机临时目录里建 venv，不碰正在运行的 mihomo-cli 安装环境。M-06 需要真终端，由用户执行。M-08 要等发版才能验证。

## 12. 日志 / 观测点

- 出错时 `cli.py:749-752` 会打印 `An unexpected error occurred.` 和异常类型；设置 `MIHOMO_CLI_DEBUG=1` 可以看到完整调用栈。修复后这个报错不应再出现。
- 查看安装环境中的版本：`<venv>/bin/pip list | grep -iE "questionary|prompt"`。
