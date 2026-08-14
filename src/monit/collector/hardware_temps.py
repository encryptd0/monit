from __future__ import annotations

from dataclasses import dataclass

import psutil


@dataclass(slots=True)
class HardwareTemperatures:
    cpu_temperature: float | None
    fan_speed: int | None


def get_hardware_temperatures() -> HardwareTemperatures:
    """Collect hardware temperature and fan speed statistics."""
    cpu_temperature: float | None = None
    fan_speed: int | None = None

    try:
        sensors = psutil.sensors_temperatures()

        cpu_temperature = max(
            (
                sensor.current
                for entries in sensors.values()
                for sensor in entries
                if sensor.current is not None
            ),
            default=None,
        )

        if cpu_temperature is not None:
            cpu_temperature = round(cpu_temperature, 2)

    except (AttributeError, OSError):
        pass

    try:
        fans = psutil.sensors_fans()

        fan_speed = next(
            (
                fan.current
                for entries in fans.values()
                for fan in entries
            ),
            None,
        )

    except (AttributeError, OSError):
        pass

    return HardwareTemperatures(
        cpu_temperature=cpu_temperature,
        fan_speed=fan_speed,
    )
