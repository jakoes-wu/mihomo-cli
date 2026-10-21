# mihomo-cli

[English](README.md) | **简体中文**

[![Release](https://img.shields.io/github/v/release/jakoes-wu/mihomo-cli)](https://github.com/jakoes-wu/mihomo-cli/releases)
[![CI](https://github.com/jakoes-wu/mihomo-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/jakoes-wu/mihomo-cli/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mihomo-cli)](https://pypi.org/project/mihomo-cli/)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)

给没有桌面环境的 Linux 机器用的命令行版简易 Clash，以 [mihomo](https://github.com/MetaCubeX/mihomo) 为内核。
既可以用交互菜单，也可以直接敲命令。

```sh
mihomo-cli profile add airport --url '<订阅地址>'   # 添加代理服务
mihomo-cli node auto                                # 自动选最快的节点
mihomo-cli                                          # 打开交互菜单
```

![mihomo-cli 演示](https://raw.githubusercontent.com/jakoes-wu/mihomo-cli/main/docs/assets/demo.gif)

<sub>演示中的节点为示例。</sub>

- 代理服务：机场订阅、本地 Clash / mihomo 配置文件，或手工添加的节点链接（比如自建 VPS）
- 切换节点不重启、不断网；测延迟，一键选最快节点
- 全局 / 规则 / 直连三种模式，另有“国内网站走不走代理”开关
- 后台 systemd 服务，安装时输一次 sudo 密码，日常不需要 root
- 可选 TUN 模式：整台机器（包括不认代理设置的程序）都走代理
- 每次改配置都先校验、先备份，失败自动恢复原状
- `doctor` 自检并可自动修复，`--json` 输出，中英文界面，shell 补全

支持：带 systemd 的 Linux（amd64 / arm64），Python 3.8 及以上；暂不支持 macOS 和 Windows。详见[支持的平台](#支持的平台)。

## 工作原理

```text
走代理的程序 ──HTTP / SOCKS──▶ 127.0.0.1:7897 ──┐
                                                ├─▶ mihomo 内核 ──▶ 你的节点 ──▶ 网站
整台机器（可选）──TUN 网卡 mihomocli0───────────┘   （systemd 服务，以你的用户身份运行）
```

你告诉 mihomo-cli 想要什么——用哪个代理服务、哪个节点、哪种模式。它生成新的内核配置 `config.yaml`，先用
`mihomo -t` 校验，再备份当前文件，然后通过内核的本地管理接口热重载。内核确认改动生效后，才把你的设置写进
`state.yaml`；重载失败或核验不通过，就把原来的文件换回去。切换节点、切换模式也走同一个接口，不重启内核。

内核作为 systemd 服务以你自己的用户身份运行，只额外拿到联网需要的几项权限（`CAP_NET_ADMIN`、
`CAP_NET_BIND_SERVICE`、`CAP_NET_RAW`），所以日常命令不需要 root。规则模式下，局域网等私有地址直连，
国内网站按 `cn` 开关走直连或代理，其余走代理。

`tun on` 之后，内核创建 `mihomocli0` 网卡，系统 DNS（systemd-resolved）改为指向本机 127.0.0.1 上的内核，
不认代理设置的程序也会被接管。`tun off`、停止服务或 `uninstall` 时，DNS 会恢复原样。

## 准备工作

- 带 systemd 的 Linux，amd64 或 arm64；账号能用 `sudo`（只有少数几步需要：`install`、`uninstall`、
  `service start/stop/restart`、`tun on/off`、`doctor --fix`）。
- Python 3.8 及以上，并装好 `pipx`。没有 pipx 时先装上，然后新开一个终端，让 `~/.local/bin` 进入 `PATH`：

  ```bash
  sudo apt update && sudo apt install -y pipx python3-venv
  pipx ensurepath
  ```

- 安装时能访问 GitHub（内核和地区数据从那里下载），或者用镜像，见下文。
- 一个代理服务：机场订阅地址、Clash / mihomo 的 YAML 配置文件，或 `vless://` 之类的节点链接。
- 只有 TUN 模式需要：存在 `/dev/net/tun`，且 DNS 由 systemd-resolved 管理（有 `resolvectl`）。

## 安装

```bash
pipx install mihomo-cli
mihomo-cli install            # 下载 mihomo 内核与地区数据库，安装后台服务（输一次 sudo 密码）
```

访问 GitHub 慢时，可以指定一个代理 `https://github.com` 的镜像：

```bash
mihomo-cli install --mirror https://your-mirror.example/https://github.com
```

## 快速上手

```bash
mihomo-cli profile add airport --url 'https://example.com/sub?token=...'
# 或者只加一台自建节点
mihomo-cli profile add vps --node 'vless://...#my-vps'

mihomo-cli node auto          # 选最快的节点
mihomo-cli status             # 一屏概览，含出口 IP
eval "$(mihomo-cli proxy-env on)"   # 当前终端走代理（127.0.0.1:7897）
```

不带参数运行 `mihomo-cli` 进入菜单。`mihomo-cli help <命令>` 查看说明和示例。

## 命令一览

| 命令 | 作用 |
| ---- | ---- |
| `status` | 后台服务、内核、代理服务、节点、模式、国内开关、TUN、端口、出口 IP |
| `install` / `uninstall` | 安装或删除内核与后台服务 |
| `core upgrade` | 更换内核；校验不通过时原内核不变 |
| `service start/stop/restart/status/logs` | 控制后台服务 |
| `profile list/show/add/rm/use/update/rename` | 管理代理服务 |
| `node list/use/add/rm/restore/test/auto` | 管理当前代理服务的节点 |
| `mode [global/rule/direct]` | 查看或切换模式 |
| `cn [direct/proxy]` | 规则模式下国内网站直连或走代理 |
| `tun on/off/status` | 整机 TUN 模式（开 / 关需要 sudo） |
| `proxy-env [on/off]` | 输出终端代理环境变量语句 |
| `conns` / `logs` | 当前连接与内核日志 |
| `backup list/restore` | 自动备份（保留最近 10 份） |
| `doctor [--fix]` | 15 项自检与修复建议 |
| `completion bash/zsh [--install]` | shell 补全与 `mproxy on/off` 函数 |

全局选项：`--json`、`--lang zh|en`、`--home 目录`（或环境变量 `MIHOMO_CLI_HOME`）、`--version`。

退出码：0 成功，1 一般错误，2 用法错误，3 内核不可达，4 配置校验失败或已回滚，5 需要 sudo 或被以 root 运行，
6 平台不支持，7 回滚未完成、需要手动处理。

## 文件位置

全部放在 `~/.local/share/mihomo-cli`（权限 0700）：`state.yaml`（记录你想要的状态，是权威来源）、
生成的 `config.yaml`、`bin/` 里的内核、`backups/` 和 `mihomo-cli.log`。服务单元是
`/etc/systemd/system/mihomo-cli.service`。

## 支持的平台

| | |
| ---- | ---- |
| 操作系统 | 带 systemd 的 Linux。在一台 Ubuntu 20.04 机器上测试过后台服务、TUN 和 DNS 接管；每次发版都在 Ubuntu 20.04 / 22.04 / 24.04 上检查能否 pip 安装并启动（`--version` / `--help`）。其它带 systemd 的发行版应该可用，但没有测试 |
| CPU | amd64（用内核的 `compatible` 版本，不支持 AVX 的老 CPU 也能跑）；arm64（未测试） |
| Python | 3.8 – 3.13 |
| macOS / Windows | 暂不支持，多数命令以退出码 6 结束 |
| 其它代理 | 可以和机器上其它 mihomo / Clash 实例并存，端口错开即可；同一台机器只能开一个 TUN |

## 说明

- mihomo 内核（GPL-3.0）不随包分发，从官方发布页下载，能拿到官方 SHA-256 时会核对。
- mihomo-cli 是独立实例，不改动机器上其它 mihomo / Clash 服务。同一台机器只能开一个 TUN，
  已有其它 TUN 时 `tun on` 会拒绝。
- 不要用 `sudo` 运行 mihomo-cli；需要权限的几步它会自己调用 sudo。
- 报告安全漏洞请看 [SECURITY.md](SECURITY.md)。

## 常见问题

**出问题了，从哪里查起？** 运行 `mihomo-cli doctor`。它检查 15 项——内核、后台服务、配置、端口、节点、
出口 IP、其它代理实例、TUN 与 DNS 接管等——多数问题会给出修复提示。`mihomo-cli doctor --fix` 会自动修好能自己修的项。

**某个程序不走代理。** 让当前终端走代理：`eval "$(mihomo-cli proxy-env on)"`；或者先运行
`mihomo-cli completion bash --install`（zsh 同理），新开一个终端后用 `mproxy on`。不认代理环境变量的程序，需要
`mihomo-cli tun on`。

**机器上已经有 Clash 或 mihomo 了。** mihomo-cli 是独立实例，有自己的数据目录和服务。7897 端口被占用时，用
`mihomo-cli install --port N` 换一个端口。同一时间只能开一个 TUN，已有其它 TUN 时 `tun on` 会拒绝。

**GitHub 访问慢或打不开。** 给 `install` 或 `core upgrade` 加 `--mirror <地址>`（见“安装”一节），镜像地址会被记住。

**改完之后出问题了。** 每次改动都会先校验，失败会自动回滚。想退回更早的设置，用 `mihomo-cli backup list` 和
`mihomo-cli backup restore <名称>`（保留最近 10 份；恢复代理服务、节点、模式和国内开关，TUN、端口和镜像地址不变）。

**国内网站要不要直连？** 规则模式下用 `mihomo-cli cn direct`（默认）或 `cn proxy`。`mode global` 全部走代理，
`mode direct` 全部直连。

**怎么卸载？** `mihomo-cli uninstall` 停止并删除后台服务，同时恢复 DNS；加 `--purge` 会连数据目录一起删除。
最后 `pipx uninstall mihomo-cli`。

## 参与贡献

欢迎提 issue 和 pull request，请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。本地检查：

```bash
pip install -e '.[test]' ruff
ruff check src tests
pytest -q
```

## 许可证

[MIT](https://github.com/jakoes-wu/mihomo-cli/blob/main/LICENSE)
