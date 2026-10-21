# Changelog

## 0.1.2 - 2026-10-21

- Fix the interactive menu crashing on Python 3.8 (Ubuntu 20.04) with `AttributeError: 'VSplit' object has no
  attribute 'content'`: questionary 2.1.0, the last release for Python 3.8, does not work with prompt_toolkit
  3.0.52 or later, so prompt_toolkit is now kept below 3.0.52 on Python 3.8. Reinstall or upgrade to pick it up.
- README: badges, a three-line usage example and a terminal demo animation; `scripts/make-assets.py`
  regenerates the demo GIF and the social preview image from a real session against a fake core.
- README: How it works, Prerequisites, Supported platforms and FAQ sections, plus a platform badge.
- CONTRIBUTING.md and SECURITY.md; links in `README.md` are full URLs so they work on the PyPI
  project page, and CI checks this.

## 0.1.1 - 2026-10-21

- Fix `node test <name>` reporting "no such node" and `doctor` D09 failing: single-node latency now uses the
  provider healthcheck endpoint, since all nodes live in providers.
- `install` / `uninstall` ask for the sudo password up front, so typing it slowly no longer hits the
  15-second timeout of the first service command.
- README: how to install `pipx` on Ubuntu first.

## 0.1.0 - 2026-10-21

- First release: Linux (Ubuntu 20.04 / 22.04 / 24.04) command-line client built on the mihomo core.
- Interactive menu and direct commands; proxy services (subscription / local file / manual nodes),
  node switching, latency test and `node auto`, global / rule / direct modes, CN-sites switch.
- systemd service installed with a single sudo; TUN mode with systemd-resolved DNS takeover.
- Automatic backup before every change with rollback on failure; `backup restore`.
- `doctor` with 15 checks and `--fix`; `--json` output; English / Chinese help; shell completion.
