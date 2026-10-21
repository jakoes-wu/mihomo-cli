# Contributing

[English](#english) | [简体中文](#简体中文)

## English

Thanks for your interest in mihomo-cli!

### Ground rules

- mihomo-cli must keep working on Python 3.8; avoid newer APIs such as `str.removeprefix` or `list[str]`
  annotations at runtime.
- Settings changes go through the existing helpers in `src/mihomo_cli/actions.py` / `state.py`: changes
  that need a reload of a running core use `reload()` (check with `mihomo -t`, back up, reload, verify,
  then commit or roll back); when the core is not running, `apply_offline()`; changes that only rewrite
  files use `save_state()` / `write_config()`. Do not add another way to write `state.yaml` or
  `config.yaml`.
- Platform differences belong in `src/mihomo_cli/platform/`.
- User-facing text lives in `src/mihomo_cli/i18n.py` as a Chinese / English pair; add both.
- Never commit real IPs, subscription URLs, node links or keys. Use example addresses such as
  `203.0.113.x` in tests, docs and the demo.

### Development

```bash
pip install -e '.[test]' ruff
ruff check src tests
pytest -q
```

The tests use a fake core and a fake system layer (`tests/conftest.py`); they never call `systemctl` or
`sudo`, and the tests that change settings work in a temporary data directory.

If the commands shown in the demo change, regenerate the images on macOS with
`.venv/bin/python scripts/make-assets.py` (needs Pillow in `.venv`) and commit `docs/assets/`.

### Pull requests

1. Open an issue first for larger changes.
2. Add or update tests for the behavior you change.
3. Update both `README.md` and `README.zh-CN.md` when user-facing behavior changes, and add an entry under
   `## Unreleased` in `CHANGELOG.md`.
4. `README.md` is also the PyPI project page, so its links must be full URLs
   (`https://github.com/jakoes-wu/mihomo-cli/blob/main/...`) or in-page anchors, never relative paths,
   which 404 on PyPI; CI checks this. `README.zh-CN.md` is not restricted.

### Releasing

1. Open a release PR that moves the `Unreleased` section of `CHANGELOG.md` under `## X.Y.Z - YYYY-MM-DD`.
   The version comes from the git tag; there is no version string to bump.
2. After it is merged, tag the merge commit `vX.Y.Z` and push the tag. The `release` workflow runs the
   tests, builds, publishes to TestPyPI, installs it on Ubuntu 20.04 / 22.04 / 24.04, waits for approval
   of the `pypi` environment, publishes to PyPI and creates the GitHub release with that CHANGELOG section.
3. Check that `pipx install mihomo-cli` installs the new version.

## 简体中文

感谢关注 mihomo-cli！

### 基本约定

- 必须继续支持 Python 3.8，运行时不要用 `str.removeprefix`、`list[str]` 注解等新写法。
- 改设置走 `src/mihomo_cli/actions.py` / `state.py` 里现有的辅助函数：内核运行中、需要重载才生效的改动用 `reload()`
  （`mihomo -t` 校验、备份、重载、核验，再提交或回滚）；内核没运行时用 `apply_offline()`；只需改写文件的
  用 `save_state()` / `write_config()`。不要另写一套写 `state.yaml` 或 `config.yaml` 的方法。
- 平台差异放在 `src/mihomo_cli/platform/`。
- 面向用户的文字放在 `src/mihomo_cli/i18n.py`，中英成对，两种都要写。
- 不要提交真实 IP、订阅地址、节点链接或密钥。测试、文档和演示里用 `203.0.113.x` 这类示例地址。

### 开发

```bash
pip install -e '.[test]' ruff
ruff check src tests
pytest -q
```

测试用假内核和假系统层（`tests/conftest.py`），不调用 `systemctl`、`sudo`；会改设置的测试都在临时数据目录里进行。

演示里的命令有变化时，在 macOS 上用 `.venv/bin/python scripts/make-assets.py` 重新出图（`.venv` 里需要
Pillow），并提交 `docs/assets/`。

### Pull Request

1. 较大的改动请先开 issue 讨论。
2. 改了什么行为，就补上或更新对应测试。
3. 用户可见的行为变化要同时更新 `README.md` 和 `README.zh-CN.md`，并在 `CHANGELOG.md` 的
   `## Unreleased` 下记一条。
4. `README.md` 同时是 PyPI 项目页的正文，里面的链接一律写完整 URL
   （`https://github.com/jakoes-wu/mihomo-cli/blob/main/...`）或页内锚点，不写相对路径，否则在 PyPI 上会 404；
   CI 会检查。`README.zh-CN.md` 不受此限。

### 发布

1. 开一个发版 PR，把 `CHANGELOG.md` 的 `Unreleased` 段移到 `## X.Y.Z - YYYY-MM-DD` 下。版本号取自 git 标签，
   代码里没有要改的版本号。
2. 合并后给合并提交打 `vX.Y.Z` 标签并推送。`release` 工作流会依次跑测试、构建、发 TestPyPI、在
   Ubuntu 20.04 / 22.04 / 24.04 上安装检查、等待 `pypi` 环境人工批准、发 PyPI，并用该版本的 CHANGELOG 段
   创建 GitHub Release。
3. 用 `pipx install mihomo-cli` 确认装到新版本。
