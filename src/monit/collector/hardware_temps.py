from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import psutil


_CPU_CHIPS = (
    "coretemp",      # Intel
    "k10temp",       # AMD
    "zenpower",      # AMD (third-party driver)
    "cpu_thermal",   # ARM / Raspberry Pi
    "acpitz",        # generic ACPI thermal zone, last resort
)

# Package-level labels represent the whole die rather than one core.
_CPU_PACKAGE_LABELS = ("package id 0", "tctl", "tdie", "cpu")

@dataclass(slots=True)
class HardwareTemperatures:
    cpu_temperature: float | None
    fan_speed: int | None

class TemperatureSensor(Protocol):
    @property
    def label(self) -> str: ...

    @property
    def current(self) -> float: ...


def _cpu_temperature_from(
    sensors: Mapping[str, Sequence[TemperatureSensor]],
) -> float | None:
    for chip in _CPU_CHIPS:
        entries = sensors.get(chip)

        if not entries:
            continue

        for entry in entries:
            if (entry.label or "").strip().lower() in _CPU_PACKAGE_LABELS:
                return entry.current

        # No package-level reading on this chip, so use its hottest core.
        return max(entry.current for entry in entries)

    return None


def get_hardware_temperatures() -> HardwareTemperatures:
    cpu_temperature: float | None = None
    fan_speed: int | None = None

    try:
        cpu_temperature = _cpu_temperature_from(psutil.sensors_temperatures())

        if cpu_temperature is not None:
            cpu_temperature = round(cpu_temperature, 2)

    except (AttributeError, OSError):
        pass

    try:
        fans = psutil.sensors_fans()

        fan_speed = max(
            (
                fan.current
                for entries in fans.values()
                for fan in entries
            ),
            default=None,
        )

    except (AttributeError, OSError):
        pass

    return HardwareTemperatures(
        cpu_temperature=cpu_temperature,
        fan_speed=fan_speed,
    )
