from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from monit.logging.logger import CRITICAL_PERCENT, resolve_log_directory

CRITICAL_TEMPERATURE_CELSIUS = 95.0

# Only the tail of the metrics log is read; a long-running collector appends
# to it indefinitely and the newest line is the only one that matters.
TAIL_READ_BYTES = 65536

# A collection loop runs every few seconds; without a cooldown a sustained
# problem would raise a desktop notification on every single cycle.
NOTIFICATION_COOLDOWN_SECONDS = 300.0

NOTIFY_TIMEOUT_SECONDS = 5.0
NOTIFY_COMMAND = "notify-send"

_last_notified: dict[str, float] = {}


@dataclass(slots=True, frozen=True)
class Alert:
    key: str
    summary: str
    detail: str
    urgent: bool


def is_gui_session() -> bool:
    """Report whether a desktop session exists that can receive notifications.

    Notifications are delivered over the session D-Bus, so a bare TTY or SSH
    login has nothing to display them and must not be notified at all.
    """
    if os.environ.get("XDG_SESSION_TYPE", "").strip().lower() == "tty":
        return False

    # A Wayland compositor or X11 server must be reachable.
    if not (
        os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("DISPLAY")
    ):
        return False

    # The session bus carries the notification; it is either advertised
    # directly or discoverable in the runtime directory.
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        runtime_directory = os.environ.get("XDG_RUNTIME_DIR")

        if not runtime_directory:
            return False

        if not Path(runtime_directory, "bus").exists():
            return False

    return True


def desktop_environment() -> str:
    """Return the running desktop (GNOME, KDE, ...) or 'unknown'."""
    for variable in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION"):
        value = os.environ.get(variable, "").strip()

        if value:
            return value

    return "unknown"


def latest_metrics_file(directory: Path | None = None) -> Path | None:
    """Return the most recently written .jsonl metrics log, if any.

    The notifier usually runs in a different process from the collector, so
    it cannot assume the writer's session filename and picks the newest file.
    """
    target = directory if directory is not None else resolve_log_directory()

    try:
        # "*.jsonl" matches the live file but not logrotate's archives
        # (monit.jsonl.1, monit.jsonl.2.gz), which are stale by definition.
        candidates = list(target.glob("*.jsonl"))
    except OSError:
        return None

    if not candidates:
        return None

    return max(candidates, key=lambda path: path.stat().st_mtime)


def _read_last_line(path: Path) -> str | None:
    """Return the final non-empty line of a file without reading all of it."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()

            handle.seek(max(0, size - TAIL_READ_BYTES))
            tail = handle.read()
    except OSError:
        return None

    for line in reversed(tail.splitlines()):
        text = line.strip()

        if text:
            return text.decode("utf-8", errors="replace")

    return None


def read_latest_metrics(directory: Path | None = None) -> str | None:
    """Return the newest metrics document recorded in the log, if readable."""
    path = latest_metrics_file(directory)

    if path is None:
        return None

    line = _read_last_line(path)

    if line is None:
        return None

    # A partially flushed final line is not worth notifying on.
    try:
        json.loads(line)
    except ValueError:
        return None

    return line


def detect_alerts(json_data: str) -> list[Alert]:
    """Inspect a metrics document and describe anything worth notifying about."""
    data: dict[str, Any] = json.loads(json_data)

    host: dict[str, Any] = data.get("host", {})
    cpu: dict[str, Any] = data.get("cpu", {})
    memory: dict[str, Any] = data.get("memory", {})
    disk: dict[str, Any] = data.get("disk", {})
    hardware: dict[str, Any] = data.get("hardware", {})

    hostname: str = host.get("hostname", "unknown")

    alerts: list[Alert] = []

    temperature = hardware.get("cpu_temperature")

    if (
        temperature is not None
        and float(temperature) > CRITICAL_TEMPERATURE_CELSIUS
    ):
        alerts.append(
            Alert(
                key="cpu_temperature",
                summary="CPU temperature critical",
                detail=(
                    f"{hostname} is at {float(temperature):.1f}°C, "
                    f"above the {CRITICAL_TEMPERATURE_CELSIUS:.0f}°C limit."
                ),
                urgent=True,
            )
        )

    cpu_usage = float(cpu.get("usage_percent", 0.0))

    if cpu_usage >= CRITICAL_PERCENT:
        alerts.append(
            Alert(
                key="cpu_usage",
                summary="CPU usage critical",
                detail=f"{hostname} CPU is at {cpu_usage:.1f}%.",
                urgent=False,
            )
        )

    virtual_memory: dict[str, Any] = memory.get("virtual_memory", {})
    memory_percent = float(virtual_memory.get("percent", 0.0))

    if memory_percent >= CRITICAL_PERCENT:
        alerts.append(
            Alert(
                key="memory_usage",
                summary="Memory usage critical",
                detail=f"{hostname} memory is at {memory_percent:.1f}%.",
                urgent=False,
            )
        )

    disk_usage: dict[str, Any] = disk.get("usage", {})
    disk_percent = float(disk_usage.get("percent", 0.0))

    if disk_percent >= CRITICAL_PERCENT:
        alerts.append(
            Alert(
                key="disk_usage",
                summary="Disk space critical",
                detail=f"{hostname} root filesystem is {disk_percent:.1f}% full.",
                urgent=False,
            )
        )

    return alerts


def _in_cooldown(key: str, now: float) -> bool:
    previous = _last_notified.get(key)

    return (
        previous is not None
        and now - previous < NOTIFICATION_COOLDOWN_SECONDS
    )


def send_notification(alert: Alert) -> bool:
    """Display one alert on the desktop. Returns False if it could not be sent."""
    if shutil.which(NOTIFY_COMMAND) is None:
        return False

    try:
        result = subprocess.run(
            [
                NOTIFY_COMMAND,
                "--app-name=monit",
                f"--urgency={'critical' if alert.urgent else 'normal'}",
                alert.summary,
                alert.detail,
            ],
            check=False,
            capture_output=True,
            timeout=NOTIFY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    return result.returncode == 0


def notify(
    json_data: str | None = None,
    force: bool = False,
    directory: Path | None = None,
) -> list[Alert]:
    """Raise desktop notifications for problems recorded in the metrics log.

    Reads the newest line of the .jsonl log when no document is supplied, so
    every notification corresponds to a line that was actually logged and can
    be checked against it afterwards. Passing a document in is for tests.

    Does nothing at all outside a GUI session. Returns the alerts that were
    actually displayed, which excludes any still inside their cooldown.
    """
    # Checked first: there is nothing to notify on a headless or
    # terminal-only session, so no log needs to be read.
    if not is_gui_session():
        return []

    document = (
        json_data if json_data is not None else read_latest_metrics(directory)
    )

    if document is None:
        return []

    now = time.monotonic()
    delivered: list[Alert] = []

    for alert in detect_alerts(document):
        if not force and _in_cooldown(alert.key, now):
            continue

        if send_notification(alert):
            _last_notified[alert.key] = now
            delivered.append(alert)

    return delivered


