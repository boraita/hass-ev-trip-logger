"""Every sensor's (device_class, state_class) pair must be one HA accepts.

HA validates the combination and refuses to record statistics for an invalid
one, logging "impossible considering device class". The pair is easy to break
by accident: a state-class is sometimes chosen for its *recorder* behaviour
(does a decrease count as a reset?) without re-checking it against the
device-class the sensor already declares.

`DEVICE_CLASS_STATE_CLASSES` is HA's own table, so this asserts against the
real rule rather than a copy of it that can drift.
"""
from __future__ import annotations

import pytest
from homeassistant.components.sensor import (
    DEVICE_CLASS_STATE_CLASSES,
    SensorDeviceClass,
    SensorStateClass,
)

# Imported at module level on purpose: `custom_components` resolves at
# collection time, and an in-function import of it fails once the test
# session is running (the loader no longer finds it on sys.path).
from custom_components.ev_trip_logger.const import DOMAIN
from custom_components.ev_trip_logger.sensor import AggregateSensor


def test_energy_rejects_measurement_state_class() -> None:
    """Guards the assumption the tests below rest on."""
    allowed = DEVICE_CLASS_STATE_CLASSES[SensorDeviceClass.ENERGY]
    assert SensorStateClass.MEASUREMENT not in allowed
    assert SensorStateClass.TOTAL in allowed


async def test_every_aggregate_sensor_pair_is_valid(hass) -> None:  # type: ignore[no-untyped-def]
    """Covers every period x key the aggregate roll-ups can be built with.

    The regression this pins: the "30d" rolling window downgraded
    TOTAL_INCREASING to MEASUREMENT so the recorder would stop flagging the
    legitimate decreases of a lookback window as invalid — but left
    `device_class` at ENERGY, which HA rejects outright. `regen_30d` and
    `discharge_30_days` were silently excluded from long-term statistics.
    """
    from .test_coordinator import _setup

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    periods = ("today", "week", "month", "year", "30d")
    keys = tuple(AggregateSensor._PERIODIC_KEYS_UNITS)
    checked = 0
    for period in periods:
        for key in keys:
            desc = AggregateSensor(
                coordinator, period=period, key=key
            ).entity_description
            if desc.device_class is None or desc.state_class is None:
                continue
            allowed = DEVICE_CLASS_STATE_CLASSES.get(desc.device_class)
            if allowed is None:
                continue
            assert desc.state_class in allowed, (
                f"{period}/{key}: state_class {desc.state_class} is invalid "
                f"for device_class {desc.device_class} "
                f"(HA allows {sorted(a.value for a in allowed)})"
            )
            checked += 1
    assert checked, "no pair was actually checked — the loop is not exercising"


@pytest.mark.parametrize("key", ["regen_kwh", "discharge_kwh"])
async def test_rolling_30d_energy_uses_total_not_measurement(
    hass,  # type: ignore[no-untyped-def]
    key: str,
) -> None:
    """A 30-day lookback sum legitimately decreases as old days age out, so
    TOTAL_INCREASING is wrong — but TOTAL, not MEASUREMENT, is the state
    class that both tolerates a decrease and is valid on an energy sensor.
    """
    from .test_coordinator import _setup

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    desc = AggregateSensor(
        coordinator, period="30d", key=key
    ).entity_description
    assert desc.device_class is SensorDeviceClass.ENERGY
    assert desc.state_class is SensorStateClass.TOTAL
