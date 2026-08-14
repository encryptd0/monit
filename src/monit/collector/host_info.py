from __future__ import annotations

from dataclasses import dataclass
import platform
import socket
import time

import psutil


@dataclass(slots=True)
class HostInfo:
    hostname: str
    fqdn: str
    ip_address: str
    operating_system: str
    os_version: str
    kernel_version: str
    architecture: str
    machine: str
    processor: str
    boot_time: float
    uptime_seconds: float


def get_host_info() -> HostInfo:
    """Collect host information."""
    hostname = socket.gethostname()

    try:
        ip_address = next(
            address.address
            for addresses in psutil.net_if_addrs().values()
            for address in addresses
            if (
                address.family == socket.AF_INET
                and not address.address.startswith("127.")
            )
        )
    except StopIteration:
        ip_address = "Unknown"

    boot_time = psutil.boot_time()

    return HostInfo(
        hostname=hostname,
        fqdn=socket.getfqdn(),
        ip_address=ip_address,
        operating_system=platform.system(),
        os_version=platform.version(),
        kernel_version=platform.release(),
        architecture=platform.architecture()[0],
        machine=platform.machine(),
        processor=platform.processor(),
        boot_time=boot_time,
        uptime_seconds=round(time.time() - boot_time, 2),
    )
