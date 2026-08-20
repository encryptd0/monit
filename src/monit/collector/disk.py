from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

import psutil

@dataclass(slots=True)
class PreviousDiskIO:
    read_bytes: int
    write_bytes: int
    read_count: int
    write_count: int

_previous_io: PreviousDiskIO | None = None
_previous_timestamp: float | None = None

@dataclass(slots=True)
class DiskUsage:
    total: int
    used: int
    free: int
    percent: float

@dataclass(slots=True)
class DiskIO:
    read_bytes_per_sec: float
    write_bytes_per_sec: float
    read_operations_per_sec: float
    write_operations_per_sec: float

@dataclass(slots=True)
class DiskMetrics:
    usage: DiskUsage
    io: DiskIO
    latency_ms: float | None

def get_disk_metrics() -> DiskMetrics:
    global _previous_io
    global _previous_timestamp

    usage = psutil.disk_usage("/")
    current_io = psutil.disk_io_counters()
    
    if current_io is None:
        raise RuntimeError("Could not retrieve disk I/O counters")
        
    current_timestamp = monotonic()

    read_bytes_per_sec = 0.0
    write_bytes_per_sec = 0.0
    read_operations_per_sec = 0.0
    write_operations_per_sec = 0.0

    if (
        _previous_io is not None
        and _previous_timestamp is not None
    ):
        elapsed = current_timestamp - _previous_timestamp

        if elapsed > 0:
            read_bytes_per_sec = round(
                (
                    current_io.read_bytes
                    - _previous_io.read_bytes
                )
                / elapsed,
                2,
            )

            write_bytes_per_sec = round(
                (
                    current_io.write_bytes
                    - _previous_io.write_bytes
                )
                / elapsed,
                2,
            )

            read_operations_per_sec = round(
                (
                    current_io.read_count
                    - _previous_io.read_count
                )
                / elapsed,
                2,
            )

            write_operations_per_sec = round(
                (
                    current_io.write_count
                    - _previous_io.write_count
                )
                / elapsed,
                2,
            )

    _previous_io = PreviousDiskIO(
        read_bytes=current_io.read_bytes,
        write_bytes=current_io.write_bytes,
        read_count=current_io.read_count,
        write_count=current_io.write_count,
    )
    _previous_timestamp = current_timestamp

    return DiskMetrics(
        usage=DiskUsage(
            total=usage.total,
            used=usage.used,
            free=usage.free,
            percent=round(usage.percent, 2),
        ),
        io=DiskIO(
            read_bytes_per_sec=read_bytes_per_sec,
            write_bytes_per_sec=write_bytes_per_sec,
            read_operations_per_sec=read_operations_per_sec,
            write_operations_per_sec=write_operations_per_sec,
        ),
        latency_ms=None,
    )
