# monit

A Linux system metrics collector. It samples CPU, memory, disk, network, host,
sensor and process data every few seconds, writes them to `/var/log/monit` in
both a human-readable and a machine-readable form, and raises a desktop
notification when something crosses a threshold (a CPU above 95 °C, or CPU,
memory or disk at 90 %).

## Try it first

Rehearse the whole install in a throwaway container before touching a real
machine. It does a genuine root install, exercises it, then reverts everything:

```bash
./scripts/test-install.sh     # needs podman or docker
```

## Install

```bash
curl -LsSf https://raw.githubusercontent.com/encryptd0/monit/main/scripts/install.sh | bash
```

Run it as your normal user, not with `sudo`: it asks for elevation only for the
steps that need it. To enable the service at the same time:

```bash
curl -LsSf https://raw.githubusercontent.com/encryptd0/monit/main/scripts/install.sh | bash -s -- --enable
```

That runs three scripts in order:

| Script | Runs as | What it does |
| --- | --- | --- |
| `config.sh` | root | Creates the `monit` group and service account, the directories, and `/etc/monit/monit.env`. Adds you to the `monit` group so you can read the logs. |
| `install-tools.sh` | you | Installs `uv`, then `psutil`, and verifies both actually work. |
| `install-project.sh` | root | Installs the app into `/opt/monit`, links `/usr/local/bin/monit`, and writes the systemd unit. |

Each can be run on its own, and every path is overridable from the environment
(`MONIT_PREFIX`, `MONIT_LOG_DIR`, `MONIT_USER`, …).

You must log out and back in before your new `monit` group membership applies —
group membership is only read at login. `newgrp monit` works in the meantime.

## Use

```bash
monit                              # run in the foreground, Ctrl-C to stop
sudo systemctl enable --now monit  # or run it as a service
journalctl -u monit -f             # follow what it is doing
ls /var/log/monit                  # the metrics logs
```

Each run produces two files: `*-monit.log`, syslog-style lines meant to be read,
and `*-monit.jsonl`, the raw documents, one JSON object per line, meant to be
parsed. `grep -E 'WARN|CRIT' /var/log/monit/*.log` surfaces the interesting
cycles.

### Desktop notifications

A **system** service has no session D-Bus, so it can log alerts but cannot pop
them on screen. To get notifications, run the per-user service instead:

```bash
systemctl --user enable --now monit    # notifications work
sudo systemctl disable --now monit     # don't run both
```

Both units are installed; they are alternatives. The user service still writes
to `/var/log/monit`, because your account is in the `monit` group.

## Uninstall

```bash
sudo /path/to/monit/scripts/uninstall.sh           # keeps your logs and config
sudo /path/to/monit/scripts/uninstall.sh --purge   # removes everything
sudo /path/to/monit/scripts/uninstall.sh --dry-run # show what would happen
```

`--purge` also deletes `/var/log/monit`, `/etc/monit`, and the `monit` account
and group. Safe to run against a half-finished install.

## Logs

`logrotate` keeps the directory in check: rotated daily or at 50 MiB, seven
compressed generations kept. Without it the logs grow by roughly 100 MiB a day
at the default 5 second interval, so raise `COLLECTION_INTERVAL_SECONDS` in
`src/monit/__init__.py` if you want less.

## Development

```bash
uv sync                              # set up .venv
uv run monit                         # run the loop
uv run python tests/test_monit.py    # run the tests
uvx pyright                          # type-check (strict)
```

Requires Python 3.13 or newer. `psutil` is the only runtime dependency.
