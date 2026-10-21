# Security Policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/jakoes-wu/mihomo-cli/security/advisories/new)
instead of opening a public issue. Include the version, your Linux distribution and the
steps to reproduce. You should receive a reply within a week.

## Scope notes

- Subscription URLs and node links contain secrets. They are stored only in the data directory
  (`~/.local/share/mihomo-cli` by default), which is mode `0700`, so other users cannot read it.
  They are masked in normal output; `mihomo-cli profile show --reveal` prints them in full.
- The core's management API is a Unix socket inside the data directory; its TCP controller is
  disabled. The proxy port listens on `127.0.0.1` only (`allow-lan: false`).
- The mihomo core is downloaded from the official GitHub releases (or the mirror you set) and checked
  against the published SHA-256 digest when one is available; a mismatch aborts the download.
- The core process runs as your own user with only `CAP_NET_ADMIN`, `CAP_NET_BIND_SERVICE` and
  `CAP_NET_RAW`. Only two short hooks of the service run as root: applying the TUN DNS takeover after
  start and reverting it after stop. mihomo-cli asks for `sudo` only to install or remove the service,
  start / stop / restart it, switch TUN on or off and apply `doctor --fix`; everyday commands refuse to
  run as root.
