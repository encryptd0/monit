from __future__ import annotations

from dataclasses import dataclass
from time import sleep, time

import psutil


@dataclass(slots=True)
class ProcessInfo:
    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    rss_memory: int
    virtual_memory: int
    threads: int
    open_files: int
    status: str
    runtime_seconds: float

@dataclass(slots=True)
class ProcessMetrics:
    top_cpu_processes: list[ProcessInfo]
    top_memory_processes: list[ProcessInfo]

def get_process_metrics(limit: int = 10) -> ProcessMetrics:
    processes: list[ProcessInfo] = []

    for process in psutil.process_iter():
        try:
            process.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    sleep(1)

    for process in psutil.process_iter():
        try:
            with process.oneshot():
                memory = process.memory_info()

                processes.append(
                    ProcessInfo(
                        pid=process.pid,
                        name=process.name(),
                        cpu_percent=round(
                            process.cpu_percent(None),
                            2,
                        ),
                        memory_percent=round(
                            process.memory_percent(),
                            2,
                        ),
                        rss_memory=memory.rss,
                        virtual_memory=memory.vms,
                        threads=process.num_threads(),
                        open_files=len(process.open_files()),
                        status=process.status(),
                        runtime_seconds=round(
                            time() - process.create_time(),
                            2,
                        ),
                    )
                )

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
        ):
            continue

    top_cpu = sorted(
        processes,
        key=lambda process: process.cpu_percent,
        reverse=True,
    )[:limit]

    top_memory = sorted(
        processes,
        key=lambda process: process.memory_percent,
        reverse=True,
    )[:limit]

    return ProcessMetrics(
        top_cpu_processes=top_cpu,
        top_memory_processes=top_memory,
    )
