# mihomo-cli

[English](README.md) | 简体中文

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

支持：Linux（在 Ubuntu 20.04 / 22.04 / 24.04 上测试），Python 3.8 及以上。暂不支持 macOS 和 Windows。

## 安装

没有 pipx 时先装上，然后新开一个终端，让 `~/.local/bin` 进入 `PATH`：

```bash
sudo apt update && sudo apt install -y pipx python3-venv
pipx ensurepath
```

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

## 说明

- mihomo 内核（GPL-3.0）不随包分发，从官方发布页下载，能拿到官方 SHA-256 时会核对。
- mihomo-cli 是独立实例，不改动机器上其它 mihomo / Clash 服务。同一台机器只能开一个 TUN，
  已有其它 TUN 时 `tun on` 会拒绝。
- 不要用 `sudo` 运行 mihomo-cli；需要权限的几步它会自己调用 sudo。

## 许可证

MIT
