from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_LOG_DIRECTORY = Path("/var/log/monit")
LOG_DIRECTORY_ENV_VAR = "MONIT_LOG_DIR"

WARNING_PERCENT = 80.0
CRITICAL_PERCENT = 90.0

# Fixed names, not per-run ones: logrotate owns the rotation, and it needs a
# stable path to rotate. See scripts/monit.logrotate.
LOG_FILE_NAME = "monit.log"
JSONL_FILE_NAME = "monit.jsonl"

_HOSTNAME = socket.gethostname()
_PID = os.getpid()

def resolve_log_directory() -> Path:
    override = os.environ.get(LOG_DIRECTORY_ENV_VAR)

    return Path(override) if override else DEFAULT_LOG_DIRECTORY

def ensure_log_directory(directory: Path | None = None) -> Path:
    target = directory if directory is not None else resolve_log_directory()

    try:
        target.mkdir(parents=True, exist_ok=True)
    except PermissionError as error:
        raise PermissionError(
            f"Cannot create log directory {target}: {error}. "
            f"Create it as root (install -d -o monit -g monit {target}) "
            f"or point {LOG_DIRECTORY_ENV_VAR} at a writable path."
        ) from error

    return target

def log_file_path(directory: Path | None = None) -> Path:
    target = directory if directory is not None else resolve_log_directory()

    return target / LOG_FILE_NAME

def jsonl_file_path(directory: Path | None = None) -> Path:

    target = directory if directory is not None else resolve_log_directory()

    return target / JSONL_FILE_NAME

def _severity(percent: float) -> str:
    if percent >= CRITICAL_PERCENT:
        return "CRIT"

    if percent >= WARNING_PERCENT:
        return "WARN"

    return "INFO"

def _human_bytes(value: float) -> str:
    amount = float(value)

    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024.0:
            return f"{amount:.1f}{unit}"

        amount /= 1024.0

    return f"{amount:.1f}PiB"

def _human_duration(seconds: float) -> str:
    total = int(seconds)

    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    if days:
        return f"{days}d{hours}h{minutes}m"

    if hours:
        return f"{hours}h{minutes}m"

    return f"{minutes}m"

def _syslog_timestamp(timestamp: str) -> str:
    """Render the collector timestamp as syslog does: 'Aug 20 07:58:36'."""
    try:
        moment = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return timestamp

    return f"{moment:%b} {moment.day:2d} {moment:%H:%M:%S}"


def _line(timestamp: str, severity: str, subsystem: str, message: str) -> str:
    return (
        f"{_syslog_timestamp(timestamp)} {_HOSTNAME} monit[{_PID}]: "
        f"{severity:<4} {subsystem}: {message}"
    )

def format_log(json_data: str) -> str:
    """Format one collected metrics document as syslog-style log lines."""
    data: dict[str, Any] = json.loads(json_data)

    timestamp: str = data.get("timestamp", "")
    host: dict[str, Any] = data.get("host", {})
    cpu: dict[str, Any] = data.get("cpu", {})
    memory: dict[str, Any] = data.get("memory", {})
    disk: dict[str, Any] = data.get("disk", {})
    hardware: dict[str, Any] = data.get("hardware", {})
    network: list[dict[str, Any]] = data.get("network", [])
    processes: dict[str, Any] = data.get("processes", {})

    logs: list[str] = []

    # Host
    logs.append(
        _line(
            timestamp,
            "INFO",
            "host",
            f"hostname={host.get('hostname', 'unknown')} "
            f"ip={host.get('ip_address', 'unknown')} "
            f"os={host.get('operating_system', 'unknown')} "
            f"kernel={host.get('kernel_version', 'unknown')} "
            f"arch={host.get('machine', 'unknown')} "
            f"uptime={_human_duration(host.get('uptime_seconds', 0.0))}",
        )
    )

    # CPU
    cpu_usage = float(cpu.get("usage_percent", 0.0))
    frequency: dict[str, Any] = cpu.get("frequency", {})
    load_average = cpu.get("load_average")

    cpu_message = (
        f"usage={cpu_usage:.1f}% "
        f"cores={cpu.get('physical_cores', 0)}phys/"
        f"{cpu.get('logical_cores', 0)}log"
    )

    current_frequency = frequency.get("current")

    if current_frequency is not None:
        cpu_message += f" freq={float(current_frequency):.0f}MHz"

    if load_average is not None:
        one, five, fifteen = load_average
        cpu_message += f" load={one:.2f},{five:.2f},{fifteen:.2f}"

    logs.append(_line(timestamp, _severity(cpu_usage), "cpu", cpu_message))

    # Memory and swap
    virtual_memory: dict[str, Any] = memory.get("virtual_memory", {})
    swap_memory: dict[str, Any] = memory.get("swap_memory", {})

    memory_percent = float(virtual_memory.get("percent", 0.0))

    logs.append(
        _line(
            timestamp,
            _severity(memory_percent),
            "memory",
            f"usage={memory_percent:.1f}% "
            f"used={_human_bytes(virtual_memory.get('used', 0))} "
            f"available={_human_bytes(virtual_memory.get('available', 0))} "
            f"total={_human_bytes(virtual_memory.get('total', 0))}",
        )
    )

    swap_percent = float(swap_memory.get("percent", 0.0))

    logs.append(
        _line(
            timestamp,
            _severity(swap_percent),
            "swap",
            f"usage={swap_percent:.1f}% "
            f"used={_human_bytes(swap_memory.get('used', 0))} "
            f"total={_human_bytes(swap_memory.get('total', 0))}",
        )
    )

    # Disk
    disk_usage: dict[str, Any] = disk.get("usage", {})
    disk_io: dict[str, Any] = disk.get("io", {})

    disk_percent = float(disk_usage.get("percent", 0.0))

    logs.append(
        _line(
            timestamp,
            _severity(disk_percent),
            "disk",
            f"mount=/ usage={disk_percent:.1f}% "
            f"used={_human_bytes(disk_usage.get('used', 0))} "
            f"free={_human_bytes(disk_usage.get('free', 0))} "
            f"total={_human_bytes(disk_usage.get('total', 0))}",
        )
    )

    logs.append(
        _line(
            timestamp,
            "INFO",
            "disk.io",
            f"read={_human_bytes(disk_io.get('read_bytes_per_sec', 0.0))}/s "
            f"write={_human_bytes(disk_io.get('write_bytes_per_sec', 0.0))}/s "
            f"read_ops={float(disk_io.get('read_operations_per_sec', 0.0)):.1f}/s "
            f"write_ops={float(disk_io.get('write_operations_per_sec', 0.0)):.1f}/s",
        )
    )

    # Hardware sensors
    temperature = hardware.get("cpu_temperature")
    fan_speed = hardware.get("fan_speed")

    if temperature is not None or fan_speed is not None:
        thermal_message = ""

        if temperature is not None:
            thermal_message += f"cpu_temp={float(temperature):.1f}C "

        if fan_speed is not None:
            thermal_message += f"fan={fan_speed}rpm"

        logs.append(
            _line(timestamp, "INFO", "thermal", thermal_message.strip())
        )

    # Network
    for interface in network:
        address: dict[str, Any] = interface.get("address", {})
        io: dict[str, Any] = interface.get("io", {})

        state = "UP" if interface.get("is_up", False) else "DOWN"

        logs.append(
            _line(
                timestamp,
                "INFO" if state == "UP" else "WARN",
                "net",
                f"iface={interface.get('name', 'unknown')} state={state} "
                f"ip={address.get('ip_address') or '-'} "
                f"mtu={interface.get('mtu', 0)} "
                f"speed={interface.get('speed_mbps', 0)}Mbps "
                f"rx={_human_bytes(io.get('bytes_received_per_sec', 0.0))}/s "
                f"tx={_human_bytes(io.get('bytes_sent_per_sec', 0.0))}/s "
                f"err_in={io.get('errors_in', 0)} err_out={io.get('errors_out', 0)} "
                f"drop_in={io.get('dropped_in', 0)} drop_out={io.get('dropped_out', 0)}",
            )
        )

    # Top processes
    top_cpu: list[dict[str, Any]] = processes.get("top_cpu_processes", [])
    top_memory: list[dict[str, Any]] = processes.get("top_memory_processes", [])

    for process in top_cpu[:5]:
        logs.append(
            _line(
                timestamp,
                "INFO",
                "proc.cpu",
                f"pid={process.get('pid', 0)} "
                f"name={process.get('name', 'unknown')} "
                f"cpu={float(process.get('cpu_percent', 0.0)):.1f}% "
                f"threads={process.get('threads', 0)} "
                f"status={process.get('status', 'unknown')}",
            )
        )

    for process in top_memory[:5]:
        logs.append(
            _line(
                timestamp,
                "INFO",
                "proc.mem",
                f"pid={process.get('pid', 0)} "
                f"name={process.get('name', 'unknown')} "
                f"mem={float(process.get('memory_percent', 0.0)):.1f}% "
                f"rss={_human_bytes(process.get('rss_memory', 0))} "
                f"status={process.get('status', 'unknown')}",
            )
        )

    return "\n".join(logs)

def write_log(json_data: str, directory: Path | None = None) -> Path:

    target = ensure_log_directory(directory)
    path = log_file_path(target)

    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{format_log(json_data)}\n")

    document = json.dumps(json.loads(json_data), separators=(",", ":"))

    with jsonl_file_path(target).open("a", encoding="utf-8") as jsonl_file:
        jsonl_file.write(f"{document}\n")

    return path
