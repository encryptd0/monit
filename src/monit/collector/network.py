from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

import psutil
import socket


@dataclass(slots=True)
class PreviousNetworkIO:
    bytes_sent: int
    bytes_received: int
    packets_sent: int
    packets_received: int


_previous_counters: dict[str, PreviousNetworkIO] = {}
_previous_timestamp: float | None = None


@dataclass(slots=True)
class NetworkAddress:
    ip_address: str | None
    netmask: str | None
    broadcast: str | None
    mac_address: str | None


@dataclass(slots=True)
class NetworkIO:
    bytes_sent_per_sec: float
    bytes_received_per_sec: float
    packets_sent_per_sec: float
    packets_received_per_sec: float
    errors_in: int
    errors_out: int
    dropped_in: int
    dropped_out: int


@dataclass(slots=True)
class NetworkInterface:
    name: str
    is_up: bool
    speed_mbps: int
    mtu: int
    address: NetworkAddress
    io: NetworkIO


def get_network_metrics() -> list[NetworkInterface]:
    """Collect network interface statistics."""
    global _previous_counters
    global _previous_timestamp

    interfaces: list[NetworkInterface] = []

    stats = psutil.net_if_stats()
    addresses = psutil.net_if_addrs()
    counters = psutil.net_io_counters(pernic=True)

    current_timestamp = monotonic()

    elapsed = (
        current_timestamp - _previous_timestamp
        if _previous_timestamp is not None
        else 0.0
    )

    for interface_name, interface_stats in stats.items():
        ipv4 = None
        netmask = None
        broadcast = None
        mac = None

        for address in addresses.get(interface_name, []):
            if address.family == socket.AF_INET:
                ipv4 = address.address
                netmask = address.netmask
                broadcast = address.broadcast

            elif address.family == getattr(socket, "AF_PACKET", None):
                mac = address.address

        io = counters.get(interface_name)

        bytes_sent_per_sec = 0.0
        bytes_received_per_sec = 0.0
        packets_sent_per_sec = 0.0
        packets_received_per_sec = 0.0

        if (
            io is not None
            and interface_name in _previous_counters
            and elapsed > 0
        ):
            previous = _previous_counters[interface_name]

            bytes_sent_per_sec = round(
                (io.bytes_sent - previous.bytes_sent) / elapsed,
                2,
            )

            bytes_received_per_sec = round(
                (io.bytes_recv - previous.bytes_received) / elapsed,
                2,
            )

            packets_sent_per_sec = round(
                (io.packets_sent - previous.packets_sent) / elapsed,
                2,
            )

            packets_received_per_sec = round(
                (io.packets_recv - previous.packets_received) / elapsed,
                2,
            )

        interfaces.append(
            NetworkInterface(
                name=interface_name,
                is_up=interface_stats.isup,
                speed_mbps=interface_stats.speed,
                mtu=interface_stats.mtu,
                address=NetworkAddress(
                    ip_address=ipv4,
                    netmask=netmask,
                    broadcast=broadcast,
                    mac_address=mac,
                ),
                io=NetworkIO(
                    bytes_sent_per_sec=bytes_sent_per_sec,
                    bytes_received_per_sec=bytes_received_per_sec,
                    packets_sent_per_sec=packets_sent_per_sec,
                    packets_received_per_sec=packets_received_per_sec,
                    errors_in=io.errin if io else 0,
                    errors_out=io.errout if io else 0,
                    dropped_in=io.dropin if io else 0,
                    dropped_out=io.dropout if io else 0,
                ),
            )
        )

        if io is not None:
            _previous_counters[interface_name] = PreviousNetworkIO(
                bytes_sent=io.bytes_sent,
                bytes_received=io.bytes_recv,
                packets_sent=io.packets_sent,
                packets_received=io.packets_recv,
            )

    _previous_timestamp = current_timestamp

    return interfaces
