# 修复：单节点测速走错接口、install 首次 sudo 超时、README 缺安装前置

> 2026-10-21 注记：三个问题都来自 0.1.0 在编译机宿主（Ubuntu 20.04，Python 3.8）上的实测。
>
> 2026-10-21 落地注记：代码已落地，回归 F-01～F-12 全部通过。验证位置：单节点测速 `src/mihomo_cli/api.py:138-150`；sudo 预检 `src/mihomo_cli/actions.py:718`（install）、`:750`（uninstall）；README 安装段两份同步。编译机复测中 doctor D09 共 12 次，11 次 OK，换装新版后第 1 次 FAIL 的原因未记录；F-10 由用户在 `sudo -k` 后手动执行，install 成功、服务 active。无未修子项。

## 1. 背景

0.1.0 发布后在一台 Ubuntu 20.04 宿主上按 README 安装，并导入一份含 9 个节点的本地文件代理服务实测，发现三个问题：

1. `doctor` 的 D09（当前节点延迟）对能正常上网的节点也报 FAIL：`ads-bandwagon-verizon-169: Resource not found`；`node test <名>` 对存在的节点报“当前代理服务没有节点 <名>”。不带名字的 `node test`、`node list --delay`、`node auto` 正常。
2. 首次 `mihomo-cli install` 提示输入 sudo 密码后报 `sudo install -m 0644 ... : timed out after 15s`，重跑一次才成功。
3. README 只写了 `pipx install mihomo-cli`。Ubuntu 20.04 默认没有 pipx，也没有 `python3-venv`（pipx 建虚拟环境要用）。

## 2. 目标 / 非目标

目标：

- `node test <名>` 与 D09 能对订阅 / 本地文件 / 手工节点测出单节点延迟。
- `install` 与 `uninstall` 在第一条 sudo 命令之前先完成密码输入，输密码的时间不计入命令超时。
- README（中英文）写明 pipx 的安装前置。

非目标：

- 不改组测速（`node test` 不带名字、`node list --delay`、`node auto`），它们走 `/group/PROXY/delay`，实测正常。
- 不改各条 sudo 命令自身的超时：预检之后凭据已缓存，后续 sudo 不再读密码。`service start/stop/restart` 只有一条 sudo 命令且超时 200 秒（`platform/linux.py:131`），足够输密码，本次不加预检。
- 不处理宿主 apt 本身损坏、装不了 pipx 的情形（属用户环境问题）。

## 3. 假设与约束

- 节点只会出现在 `main`（订阅 / 本地文件）与 `manual`（手工节点）两个 provider 里，PROXY 组成员即二者节点加 `REJECT`（`src/mihomo_cli/render.py:58-60`、`:118-126`）。手工节点名统一带 `[M] ` 前缀，与 `main` 同名的情况只在订阅节点本身以 `[M] ` 开头时出现；按先 `main` 后 `manual` 查找。
- `/providers/proxies/<provider>/<name>/healthcheck` 路由在最低支持版本 v1.19.0 与编译机实测版本 v1.19.32 的 `hub/route/provider.go:30-36` 中写法相同，都交给 `getProxyDelay`。

## 4. 根因分析

### 4.1 单节点测速

- 现象：D09 与 `node test <名>` 对存在的节点得到 404 `Resource not found`。
- 失效路径：`Api.proxy_delay` 请求 `GET /proxies/<名>/delay`（`src/mihomo_cli/api.py:138-142`）。内核该路由用 `findProxyByName` 只在 `tunnel.Proxies()` 里找（mihomo v1.19.32 `hub/route/proxies.go:46-55`），这张表只含配置文件顶层 `proxies` 与策略组，不含 provider 里的节点；而本程序的节点全部来自 provider（`main` / `manual`），所以一律 404。
- 调用点（全集，`grep -rn proxy_delay src` 共 2 处）：
  - `src/mihomo_cli/doctor.py:240`：D09，捕获 `ApiError` 后报 FAIL；
  - `src/mihomo_cli/actions.py:1154`：`node_test` 带名字分支，404 转成 `err.no_such_node`。
- 编译机实测对照（内核 v1.19.32，管理套接字）：`/proxies/ads-lisa-90/delay` → 404；`/providers/proxies/main/ads-lisa-90/healthcheck` → 200 `{"delay":1140}`；同接口 `timeout=1` → 504 `Timeout`，节点名不存在 → 404，provider 名不对 → 404。
- 正确接口：`GET /providers/proxies/<provider>/<name>/healthcheck`（`hub/route/provider.go:30-36`），与 `/proxies/<名>/delay` 共用同一个处理函数 `getProxyDelay`，查询参数 `url`、`timeout` 与错误码（404 / 503 / 504）完全一致。
- 为何 0.1.0 回归没拦住：单元测试用 `FakeApi` 整体替换 `Api`（`tests/conftest.py:163-164`），真实请求路径从未被测；编译机 L1 只测了“坏节点时 D09 失败”（台架 `cases1.py:405`），没有“好节点时 D09 通过”的正向用例。

### 4.2 install 首次 sudo 超时

- 现象：首次 install 提示输入密码后约 15 秒报超时。
- 失效路径：`actions.install` 在 `src/mihomo_cli/actions.py:715` 提示需要 sudo 后，直接调用 `install_service`（`:718`），其第一条命令 `sudo install -m 0644 ...` 走 `_run` 的默认超时 `CMD_TIMEOUT_SEC = 15`（`src/mihomo_cli/platform/linux.py:31`、`:112`）。sudo 在这条命令里读密码，读密码的时间算进 15 秒。
- 对照：`tun on/off` 在 sudo 操作前先调 `sudo_validate()`（`sudo -v`，无超时，`actions.py:1277`、`:1328`），没有此问题。
- 同类：`actions.uninstall` 在 `:744` 提示后直接调 `uninstall_service`（`:747`）；unit 文件不存在时（例如首次 install 正是在 `sudo install` 这步超时）跳过 `disable`，第一条 sudo 命令是默认 15 秒超时的 `sudo systemctl daemon-reload`（`platform/linux.py:120-124`），同样会超时。

## 5. 复现步骤

单节点测速：

1. 任意 Linux 宿主 `pipx install mihomo-cli==0.1.0 && mihomo-cli install`；
2. `mihomo-cli profile add t --file <含 proxies 列表的 YAML>`（或 `--url` 订阅，或 `--node` 链接）；
3. `mihomo-cli doctor` → D09 FAIL `Resource not found`；`mihomo-cli node test <列表里任一节点名>` → “当前代理服务没有节点”。

install 超时：

1. 确认 sudo 凭据未缓存（`sudo -k`）；
2. `mihomo-cli install`，看到 sudo 密码提示后等待超过 15 秒再输入 → 报 `timed out after 15s`，退出码 5。

## 6. 影响版本

- 0.1.0，所有 Linux 部署形态。单节点测速问题与节点来源无关（订阅 / 本地文件 / 手工节点都 404）。
- install 超时只在 sudo 凭据未缓存时出现（首次安装最常见）。

## 7. 涉及模块

| 区域 | 行号锚点（main 当前状态） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/mihomo_cli/api.py` `Api.proxy_delay` | 138-142 | 修改 | 先在 `main`、`manual` 两个 provider 里定位节点，再请求 provider 节点测速接口；都找不到时抛 404 `ApiError` |
| `src/mihomo_cli/actions.py` `install` | 715-718 之间 | 新增 | 提示后、`install_service` 前调 `ctx.platform.sudo_validate()`，失败抛 `CliError(EXIT_SUDO, err.sudo_failed)` |
| `src/mihomo_cli/actions.py` `uninstall` | 744 与 745 之间 | 新增 | 同上，提示后、`uninstall_service` 前预检 sudo |
| `tests/test_api.py` | 新文件 | 新增 | 用 `httpx.MockTransport` 测真实 `Api.proxy_delay` 的请求路径、参数与 404 |
| `tests/test_actions.py` | 文件末尾追加 | 新增 | install / uninstall 预检失败时不调服务操作、退出码 5；成功时预检先于服务操作 |
| `tests/conftest.py` `FakePlatform.sudo_validate` | 207-208 | 修改 | 调用时向 `self.actions` 追加 `"sudo_validate"`（`install_service` / `uninstall_service` 已分别追加 `"install"` / `"uninstall"`，`:192-196`），供断言顺序 |
| `README.md`、`README.zh-CN.md` | 安装段 19-22 | 修改 | 补 pipx 前置安装命令 |
| `docs/feature/feature-mihomo-cli-v1.md` | 201 | 修改 | 测延迟一行的接口改为 provider 节点测速接口 |
| `CHANGELOG.md` | 顶部 | 新增 | `## 0.1.1` 条目 |

## 8. 方案

### 8.1 实现要点

**单节点测速**（替换 `src/mihomo_cli/api.py:138-142`）：

```python
def proxy_delay(self, name, url=DELAY_URL, timeout_ms=DELAY_TIMEOUT_MS) -> int:
    # 节点都在 provider 里，/proxies/<名>/delay 只查顶层节点表，必然 404（见方案 4.1）
    timeout_ms = min(int(timeout_ms), DELAY_TIMEOUT_MAX_MS)
    for prov in ("main", "manual"):
        if name in self.provider_node_names(prov):
            data = self._request("GET", "/providers/proxies/%s/%s/healthcheck" % (_q(prov), _q(name)),
                                 timeout=timeout_ms / 1000.0 + 5, params={"url": url, "timeout": timeout_ms})
            return int(data.get("delay", 0))
    raise ApiError("GET", "/providers/proxies/*/" + _q(name), 404, "Resource not found")
```

- 两个调用方不改：`node_test` 依旧把 404 转为“没有该节点”、其它错误记 0（超时）；D09 依旧把 `ApiError` 报 FAIL（超时时消息为 `Timeout`）。
- `provider_node_names` 对不存在的 provider 返回空列表（`api.py:99-111`），自建 VPS 类没有 `main` 时自然跳过。
- 多两次只读 `GET /providers/proxies/<p>`，走本机套接字，耗时可忽略。

**install 预检 sudo**（插入 `src/mihomo_cli/actions.py:715` 与 `:716` 之间）：

```python
    if not ctx.platform.sudo_validate():
        raise CliError(EXIT_SUDO, t("err.sudo_failed"))
```

`sudo -v` 用 `subprocess.call` 不设超时（`platform/linux.py:169-173`），用户输完密码后凭据缓存，随后 `install_service` 里的 sudo 命令不再提示。复用已有文案 `err.sudo_failed`，不新增文案。

**uninstall 预检 sudo**：同样两行，插入 `src/mihomo_cli/actions.py:744`（提示）与 `:745`（`try:`）之间。

**README**（两份同步，在现有 `pipx install mihomo-cli` / `mihomo-cli install` 代码块之前**新起一个代码块**，前面加一句“没有 pipx 时先装”）：

```bash
sudo apt update && sudo apt install -y pipx python3-venv
pipx ensurepath                      # 让 ~/.local/bin 进 PATH，重开终端生效
```

**功能方案**：`docs/feature/feature-mihomo-cli-v1.md:201` 的 `GET /proxies/<名>/delay` 改为 `GET /providers/proxies/<main|manual>/<名>/healthcheck`（`hub/route/provider.go:30-36`），并加一句“节点在 provider 里，`/proxies/<名>` 查不到”。

### 8.2 接口变更

无对外接口变更：命令、参数、输出格式、退出码都不变。`node test <名>` 与 D09 从“总是失败”变为按实际延迟返回；install / uninstall 在 sudo 失败时的退出码仍是 5，只是失败点从服务操作提前到预检，提示文案由 `err.service_failed` 变为 `err.sudo_failed`。仓库无 `docs/reference/`，不涉及 sibling 回补检查。

## 9. 备选方案与决策

- 单节点测速改用组测速 `/group/PROXY/delay` 再取该节点：会把整组都测一遍，节点多时慢且产生无关流量，否决。
- 把 `install_service` 第一条 sudo 命令的超时加大：仍把输密码时间算进命令超时，且不同 sudo 命令要各自调大，不如统一预检，否决。

## 10. 影响分析

- `Api.proxy_delay` 只有 2 个调用方（见 4.1），返回值与异常类型不变；新增的 `provider_node_names` 调用是只读请求，不加锁、不改状态。内核不可达时 `_request` 照旧抛 `ApiUnavailable`，调用方原有处理（D09 前有 `api_ok()` 检查，`node_test` 前有 `kernel_running`）不变。
- 节点名查找：订阅节点恰好以 `[M] ` 开头且与手工节点同名时，测的是 `main` 里那个。两者在 PROXY 组里本就同名，`node use` 也无法区分，不在本次范围。
- install：预检只在每次 install 末尾、需要装服务时执行一次；sudo 凭据已缓存时 `sudo -v` 立即返回、不提示。root 运行在 `cli.py:719-723` 已被拒绝（`err.root`，退出码 5），走不到预检。三次输错密码时 `sudo -v` 返回 1，提前以退出码 5 结束；按 Ctrl-C 时 Python 收到 `KeyboardInterrupt`，由 `cli.py:746-747` 以退出码 130 结束。两种情况下此时内核与地区数据库已下载、`state.yaml` 与配置已写好，与原来 `install_service` 失败时的落盘状态相同，重跑 install 会跳过已下载的部分。
- uninstall：预检加在最前面，凭据已缓存时无感；预检失败时什么都没动（服务、主目录都保留），比原来“停了一半再失败”更干净。
- `get_provider` 遇到非 404 错误（如内核内部错误）会原样抛 `ApiError`（`api.py:99-105`）：`node_test` 带名字时把它记为 0、显示为超时（`actions.py:1155-1158`），D09 报 FAIL 并带内核消息。与修复前对非 404 错误的处理一致。
- 文档改动不影响代码。

## 11. 回归测试

| 编号 | 层级 | 内容 | 判据 |
| ---- | ---- | ---- | ---- |
| F-01 | L0 | `tests/test_api.py`：构造 `Api` 后替换 `api._client = httpx.Client(transport=httpx.MockTransport(h), base_url="http://mihomo")`；`proxy_delay` 节点在 `main` | 请求路径 `/providers/proxies/main/<编码后名>/healthcheck`，带 `url`、`timeout` 参数，返回 delay |
| F-02 | L0 | 同上，节点只在 `manual`（含空格、`[M] ` 前缀、中文名） | 路径用 `manual`，名字百分号编码正确 |
| F-03 | L0 | 同上，两个 provider 都没有该节点 / `main` 不存在（404） | 抛 `ApiError` 且 `status == 404`；不请求 healthcheck |
| F-04 | L0 | 同上，healthcheck 分别返回 504、503 | 抛 `ApiError`，`status` 分别为 504、503 |
| F-04b | L0 | 同上，传输层抛 `httpx.ConnectError` | 抛 `ApiUnavailable` |
| F-05 | L0 | `tests/test_actions.py`：install，预置 `paths.core` 文件与地区数据库文件（或打桩 `core_dl.install_geodata`）、端口无冲突，`sudo_ok=False` | 抛退出码 5，`platform.actions` 不含 `install` |
| F-05b | L0 | 同上，`sudo_ok=True` | `platform.actions` 中 `sudo_validate` 在 `install` 之前 |
| F-05c | L0 | uninstall，`sudo_ok=False` / `True` | False：退出码 5、不含 `uninstall`；True：`sudo_validate` 在 `uninstall` 之前 |
| F-06 | L0 | 全量 `pytest` 与 `ruff` | 全部通过，原有用例无回退 |
| F-07 | L3 编译机宿主 | 装入本分支构建的 wheel 后：`doctor` | D09 OK 并显示毫秒数 |
| F-08 | L3 编译机宿主 | `node test ads-lisa-90`；`node test 不存在的名` | 前者显示延迟；后者“没有该节点”，退出码 1 |
| F-09 | L3 编译机宿主 | `node test`（不带名）、`node list --delay`、`node auto` | 与修复前一致，9 个节点都有延迟 |
| F-10 | L3 编译机宿主 | 用户在终端 `sudo -k` 后运行 install，提示后等 20 秒以上再输密码 | 安装成功，服务 active |
| F-11 | L0 | 两份 README 安装段 | 中英文命令一致 |
| F-12 | L1 编译机一次性容器 | `ubuntu:20.04`、`22.04`、`24.04` 各一个 `docker run --rm`，容器里以 root 执行、省掉 `sudo`，其余按 README 原样执行 `apt update && apt install -y pipx python3-venv` → `pipx ensurepath` → `pipx install <本分支 wheel>`（20.04 的 pipx 0.12 装本地 wheel 需 `--spec` 写法，从 PyPI 按包名装不需要）→ `~/.local/bin/mihomo-cli --version` | 三个镜像都输出本分支版本号；`ensurepath` 只改 shell 启动文件，容器内用绝对路径调用，重开终端生效这一步不在容器里验 |

F-10 需要用户本人输密码，由用户执行。

## 12. 日志 / 观测点

- D09 输出：成功为 `<节点> <N> ms`；超时为 `<节点>: Timeout`；节点被删为 `<节点>: Resource not found`；内核测速出错（503）为 `<节点>: An error occurred in the delay test` 或内核给出的错误文本。
- `node test <名>` 的 `--json` 输出 `{<名>: <毫秒>}`，0 表示超时。
- install / uninstall：sudo 预检失败时输出 `err.sudo_failed` 文案，退出码 5；成功后仍记 `[install] home=... user=...`（`actions.py:723`）。
