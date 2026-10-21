# mihomo-cli

English | [简体中文](README.zh-CN.md)

[![Release](https://img.shields.io/github/v/release/jakoes-wu/mihomo-cli)](https://github.com/jakoes-wu/mihomo-cli/releases)
[![CI](https://github.com/jakoes-wu/mihomo-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/jakoes-wu/mihomo-cli/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mihomo-cli)](https://pypi.org/project/mihomo-cli/)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/jakoes-wu/mihomo-cli/blob/main/LICENSE)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)

A command-line, Clash-style proxy client for Linux machines without a desktop, powered by the
[mihomo](https://github.com/MetaCubeX/mihomo) core. Use it through an interactive menu or plain commands.

```sh
mihomo-cli profile add airport --url '<subscription>'   # add your proxy service
mihomo-cli node auto                                    # pick the fastest node
mihomo-cli                                              # interactive menu
```

![mihomo-cli demo](https://raw.githubusercontent.com/jakoes-wu/mihomo-cli/main/docs/assets/demo.gif)

<sub>The nodes in the demo are examples.</sub>

- Proxy services: an airport subscription, a local Clash/mihomo file, or hand-added node links (e.g. your own VPS)
- Switch nodes without restarting; latency test and automatic fastest-node pick
- Global / rule / direct modes, plus a switch for whether domestic (CN) sites go direct
- Background systemd service, installed with a single `sudo`; no root needed day to day
- Optional TUN mode for the whole machine, including programs that ignore proxy settings
- Every change is validated, backed up, and rolled back automatically if it fails
- `doctor` self-check with fixes, `--json` output, English / Chinese interface, shell completion

Supported: Linux with systemd (amd64 / arm64), Python 3.8+; macOS and Windows are not supported yet. Details in
[Supported platforms](#supported-platforms).

## How it works

```text
programs using the proxy ──HTTP / SOCKS──▶ 127.0.0.1:7897 ───┐
                                                             ├─▶ mihomo core ──▶ your node ──▶ websites
whole machine, optional ──TUN (mihomocli0)───────────────────┘   (systemd service, runs as you)
```

You tell mihomo-cli what you want — which proxy service, which node, which mode. It generates a new
`config.yaml` for the core, checks it with `mihomo -t`, backs up the current files and hot-reloads the core
through its local API socket. Your settings are written to `state.yaml` only after the core confirms the
change took effect; if the reload or that check fails, the previous files are put back.
Switching nodes or modes goes through the same API, without restarting the core.

The core runs as a systemd service under your own user, with only the network capabilities it needs
(`CAP_NET_ADMIN`, `CAP_NET_BIND_SERVICE`, `CAP_NET_RAW`), so everyday commands need no root. In rule mode,
private addresses go direct, domestic (CN) sites follow the `cn` switch, and everything else uses the proxy.

With `tun on`, the core creates the `mihomocli0` interface and system DNS (systemd-resolved) is pointed at the
core on 127.0.0.1, so programs that ignore proxy settings are covered too. `tun off`, stopping the service or
`uninstall` puts DNS back the way it was.

## Prerequisites

- Linux with systemd, on amd64 or arm64, and an account that can use `sudo` (needed only for a few
  steps: `install`, `uninstall`, `service start/stop/restart`, `tun on/off` and `doctor --fix`).
- Python 3.8+ with `pipx`. If `pipx` is not installed yet, install it first, then open a new terminal so
  `~/.local/bin` is on `PATH`:

  ```bash
  sudo apt update && sudo apt install -y pipx python3-venv
  pipx ensurepath
  ```

- Access to GitHub while installing (the core and geo data are downloaded from there), or a mirror, see below.
- A proxy service: an airport subscription URL, a Clash / mihomo YAML file, or node links such as `vless://`.
- For TUN mode only: `/dev/net/tun` and DNS managed by systemd-resolved (`resolvectl`).

## Install

```bash
pipx install mihomo-cli
mihomo-cli install            # downloads the mihomo core and geo data, installs the service (asks for sudo once)
```

If GitHub is slow to reach, pass a mirror that proxies `https://github.com`:

```bash
mihomo-cli install --mirror https://your-mirror.example/https://github.com
```

## Quick start

```bash
mihomo-cli profile add airport --url 'https://example.com/sub?token=...'
# or a single self-hosted node
mihomo-cli profile add vps --node 'vless://...#my-vps'

mihomo-cli node auto          # pick the fastest node
mihomo-cli status             # one-screen overview including exit IP
eval "$(mihomo-cli proxy-env on)"   # proxy the current shell (127.0.0.1:7897)
```

Run `mihomo-cli` with no arguments to open the menu. `mihomo-cli help <command>` shows help and examples.

## Commands

| Command | What it does |
| ---- | ---- |
| `status` | Service, core, proxy service, node, mode, CN switch, TUN, port, exit IP |
| `install` / `uninstall` | Install or remove the core and the systemd service |
| `core upgrade` | Replace the core; the old one is kept if verification fails |
| `service start/stop/restart/status/logs` | Control the background service |
| `profile list/show/add/rm/use/update/rename` | Manage proxy services |
| `node list/use/add/rm/restore/test/auto` | Manage nodes of the active proxy service |
| `mode [global/rule/direct]` | Show or set the mode |
| `cn [direct/proxy]` | Domestic sites direct or via proxy (rule mode) |
| `tun on/off/status` | Whole-machine TUN mode (on/off need sudo) |
| `proxy-env [on/off]` | Print shell statements for proxy variables |
| `conns` / `logs` | Active connections and core logs |
| `backup list/restore` | Automatic backups (latest 10) |
| `doctor [--fix]` | 15 checks with fix hints |
| `completion bash/zsh [--install]` | Shell completion and the `mproxy on/off` helper |

Global options: `--json`, `--lang zh|en`, `--home DIR` (or `MIHOMO_CLI_HOME`), `--version`.

Exit codes: 0 ok, 1 error, 2 usage, 3 core unreachable, 4 invalid config or rolled back, 5 needs sudo
or run as root, 6 unsupported platform, 7 rollback incomplete (manual action needed).

## Files

Everything lives in `~/.local/share/mihomo-cli` (mode 0700): `state.yaml` (what you asked for — the
source of truth), the generated `config.yaml`, the core in `bin/`, `backups/`, and `mihomo-cli.log`.
The service unit is `/etc/systemd/system/mihomo-cli.service`.

## Supported platforms

| | |
| ---- | ---- |
| Operating system | Linux with systemd. Tested with the service, TUN and DNS takeover on an Ubuntu 20.04 machine; each release is checked to pip-install and start (`--version` / `--help`) on Ubuntu 20.04 / 22.04 / 24.04. Other systemd distributions should work but are untested |
| CPU | amd64 (the core's `compatible` build, so older CPUs without AVX work too); arm64 (untested) |
| Python | 3.8 – 3.13 |
| macOS / Windows | Not supported yet; most commands exit with code 6 |
| Other proxies | Can run next to other mihomo / Clash instances on a different port; only one TUN per machine |

## Notes

- The mihomo core (GPL-3.0) is not bundled; it is downloaded from the official releases and checked
  against the published SHA-256 digest when available.
- mihomo-cli runs as its own instance and does not touch other mihomo / Clash services. Only one TUN can
  be active on a machine; `tun on` refuses if another one is running.
- Do not run mihomo-cli with `sudo`; it calls sudo itself for the few steps that need it.

## FAQ

**Something isn't working. Where do I start?** Run `mihomo-cli doctor`. It runs 15 checks — core, service,
config, port, nodes, exit IP, other proxy instances, TUN and DNS takeover, and more — and prints a fix hint
for most problems. `mihomo-cli doctor --fix` applies the fixes it can do by itself.

**A program ignores the proxy.** Proxy the current shell with `eval "$(mihomo-cli proxy-env on)"`, or run
`mihomo-cli completion bash --install` (or `zsh`), open a new terminal and use `mproxy on`.
Programs that ignore proxy variables need `mihomo-cli tun on`.

**I already run Clash or mihomo.** mihomo-cli is a separate instance with its own data directory and service.
If port 7897 is taken, choose another with `mihomo-cli install --port N`. Only one TUN can run at a time;
`tun on` refuses while another one is active.

**GitHub is slow or blocked.** Pass `--mirror <URL>` to `install` or `core upgrade` (see Install);
the mirror is remembered.

**A change broke something.** Every change is checked and rolled back automatically if it fails. To go back
to earlier settings, use `mihomo-cli backup list` and `mihomo-cli backup restore <name>` (the latest 10 are
kept; proxy service, node, mode and CN switch are restored, TUN, port and mirror stay as they are).

**Should domestic sites go direct?** In rule mode, `mihomo-cli cn direct` (the default) or `cn proxy`.
`mode global` sends everything through the proxy; `mode direct` sends nothing.

**How do I remove it?** `mihomo-cli uninstall` stops and removes the service and restores DNS; add `--purge`
to delete the data directory as well. Then `pipx uninstall mihomo-cli`.

## License

[MIT](https://github.com/jakoes-wu/mihomo-cli/blob/main/LICENSE)
