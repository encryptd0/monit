from __future__ import annotations

from dataclasses import dataclass

import psutil


@dataclass(slots=True)
class VirtualMemory:
    total: int
    available: int
    used: int
    free: int
    percent: float


@dataclass(slots=True)
class SwapMemory:
    total: int
    used: int
    free: int
    percent: float


@dataclass(slots=True)
class MemoryMetrics:
    virtual_memory: VirtualMemory
    swap_memory: SwapMemory


def get_memory_metrics() -> MemoryMetrics:
    """Collect RAM and swap memory statistics."""
    virtual = psutil.virtual_memory()
    swap = psutil.swap_memory()

    return MemoryMetrics(
        virtual_memory=VirtualMemory(
            total=virtual.total,
            available=virtual.available,
            used=virtual.used,
            free=virtual.free,
            percent=round(virtual.percent, 2),
        ),
        swap_memory=SwapMemory(
            total=swap.total,
            used=swap.used,
            free=swap.free,
            percent=round(swap.percent, 2),
        ),
    )
