from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import psutil

@dataclass(slots=True)
class CPUFrequency:
    current: Optional[float]
    minimum: Optional[float]
    maximum: Optional[float]


@dataclass(slots=True)
class CPUMetrics:
    usage_percent: float
    per_core_usage: list[float]
    physical_cores: Optional[int]
    logical_cores: Optional[int]
    frequency: CPUFrequency
    load_average: Optional[tuple[float, float, float]]


def get_cpu_metrics() -> CPUMetrics:

    freq = psutil.cpu_freq()

    try:
        load_avg = (
            psutil.getloadavg()
            if hasattr(psutil, "getloadavg")
            else None
        )
    except OSError:
        load_avg = None

    load_average: tuple[float, float, float] | None

    if load_avg is not None:
        load_average = (
            round(load_avg[0], 2),
            round(load_avg[1], 2),
            round(load_avg[2], 2),
        )
    else:
        load_average = None

    return CPUMetrics(
        usage_percent=round(
            psutil.cpu_percent(interval=1),
            2,
        ),
        per_core_usage=[
            round(value, 2)
            for value in psutil.cpu_percent(
                interval=None,
                percpu=True,
            )
        ],
        physical_cores=psutil.cpu_count(logical=False),
        logical_cores=psutil.cpu_count(logical=True),
        frequency=CPUFrequency(
            current=(
                round(freq.current, 2)
                if freq
                else None
            ),
            minimum=(
                round(freq.min, 2)
                if freq
                else None
            ),
            maximum=(
                round(freq.max, 2)
                if freq
                else None
            ),
        ),
        load_average=load_average,
    )
