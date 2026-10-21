# Changelog

## Unreleased

- First release: Linux (Ubuntu 20.04 / 22.04 / 24.04) command-line client built on the mihomo core.
- Interactive menu and direct commands; proxy services (subscription / local file / manual nodes),
  node switching, latency test and `node auto`, global / rule / direct modes, CN-sites switch.
- systemd service installed with a single sudo; TUN mode with systemd-resolved DNS takeover.
- Automatic backup before every change with rollback on failure; `backup restore`.
- `doctor` with 15 checks and `--fix`; `--json` output; English / Chinese help; shell completion.
