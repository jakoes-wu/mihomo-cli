# mihomo-cli

English | [简体中文](README.zh-CN.md)

A command-line, Clash-style proxy client for Linux machines without a desktop, powered by the
[mihomo](https://github.com/MetaCubeX/mihomo) core. Use it through an interactive menu or plain commands.

- Proxy services: an airport subscription, a local Clash/mihomo file, or hand-added node links (e.g. your own VPS)
- Switch nodes without restarting; latency test and automatic fastest-node pick
- Global / rule / direct modes, plus a switch for whether domestic (CN) sites go direct
- Background systemd service, installed with a single `sudo`; no root needed day to day
- Optional TUN mode for the whole machine, including programs that ignore proxy settings
- Every change is validated, backed up, and rolled back automatically if it fails
- `doctor` self-check with fixes, `--json` output, English / Chinese interface, shell completion

Supported: Linux (tested on Ubuntu 20.04 / 22.04 / 24.04), Python 3.8+. macOS and Windows are not supported yet.

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

## Notes

- The mihomo core (GPL-3.0) is not bundled; it is downloaded from the official releases and checked
  against the published SHA-256 digest when available.
- mihomo-cli runs as its own instance and does not touch other mihomo / Clash services. Only one TUN can
  be active on a machine; `tun on` refuses if another one is running.
- Do not run mihomo-cli with `sudo`; it calls sudo itself for the few steps that need it.

## License

MIT
