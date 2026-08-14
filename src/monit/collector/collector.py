from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .cpu import CPUMetrics, get_cpu_metrics
from .disk import DiskMetrics, get_disk_metrics
from .hardware_temps import HardwareTemperatures, get_hardware_temperatures
from .host_info import HostInfo, get_host_info
from .memory import MemoryMetrics, get_memory_metrics
from .network import NetworkInterface, get_network_metrics
from .process import ProcessMetrics, get_process_metrics

import json

@dataclass(slots=True)
class Metrics:
    timestamp: str
    cpu: CPUMetrics
    memory: MemoryMetrics
    disk: DiskMetrics
    network: list[NetworkInterface]
    host: HostInfo
    hardware: HardwareTemperatures
    processes: ProcessMetrics


def collect_metrics() -> Metrics:

    return Metrics(
        timestamp=datetime.now(timezone.utc).isoformat(),
        cpu=get_cpu_metrics(),
        memory=get_memory_metrics(),
        disk=get_disk_metrics(),
        network=get_network_metrics(),
        host=get_host_info(),
        hardware=get_hardware_temperatures(),
        processes=get_process_metrics(),
    )

def collect_metrics_json() -> str:
    return json.dumps(
        asdict(collect_metrics()),
    )
