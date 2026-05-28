"""End-to-end test for the log_manual_trip service.

Exercises the same path the user will hit via Developer Tools → Services:
config flow → integration setup → call ev_trip_logger.log_manual_trip → assert
the trip lands in storage with the expected derived metrics.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_trip_logger.const import (
    CONF_BATTERY,
    CONF_BATTERY_CAPACITY,
    CONF_CURRENCY,
    CONF_ENERGY_PRICE,
    CONF_IDLE_TIMEOUT,
    CONF_MIN_TRIP_DISTANCE,
    CONF_NAME,
    CONF_ODOMETER,
    CONF_VEHICLE_ON,
    DOMAIN,
    SERVICE_LOG_MANUAL_TRIP,
)


_FAKE_SENSORS = {
    "sensor.fake_odometer": "25300",
    "sensor.fake_battery": "61",
    "binary_sensor.fake_vehicle_on": "off",
}


@pytest.fixture
async def configured_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Stand up an ev_trip_logger entry with deterministic fake sensors."""
    for entity_id, state in _FAKE_SENSORS.items():
        hass.states.async_set(entity_id, state)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test EV",
        data={
            CONF_NAME: "Test EV",
            CONF_ODOMETER: "sensor.fake_odometer",
            CONF_BATTERY: "sensor.fake_battery",
            CONF_VEHICLE_ON: "binary_sensor.fake_vehicle_on",
            CONF_BATTERY_CAPACITY: 82.56,
            CONF_MIN_TRIP_DISTANCE: 0.5,
            CONF_IDLE_TIMEOUT: 2,
            CONF_ENERGY_PRICE: 0.13,
            CONF_CURRENCY: "EUR",
        },
        unique_id="test-ev",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_log_manual_trip_persists_and_derives_metrics(
    hass: HomeAssistant, configured_entry: MockConfigEntry
) -> None:
    """Replicates the user's lost trip: 25300→25318 km, 61%→56%, ~34 min."""
    started_at = datetime(2026, 5, 28, 13, 7, 41, tzinfo=timezone.utc)
    ended_at = datetime(2026, 5, 28, 13, 42, 4, tzinfo=timezone.utc)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_LOG_MANUAL_TRIP,
        {
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "odometer_start": 25300,
            "odometer_end": 25318,
            "soc_start": 61,
            "soc_end": 56,
            "origin": "home",
            "destination": "albayzin",
        },
        blocking=True,
    )

    coordinator = hass.data[DOMAIN][configured_entry.entry_id]
    stored = await coordinator.storage.async_get_last()
    assert stored is not None, "manual trip was not persisted"

    assert stored.distance_km == pytest.approx(18.0)
    assert stored.soc_used_pct == pytest.approx(5.0)
    # 5% of 82.56 kWh battery
    assert stored.energy_kwh == pytest.approx(4.128, rel=1e-3)
    # 4.128 kWh / 18 km × 100
    assert stored.consumption_kwh_100km == pytest.approx(22.93, rel=1e-2)
    # 18 km / (34.38 min / 60) ≈ 31.4 km/h
    assert stored.avg_speed_kmh == pytest.approx(31.4, rel=1e-2)
    # 4.128 kWh × 0.13 €/kWh
    assert stored.cost == pytest.approx(0.5366, rel=1e-3)
    assert stored.currency == "EUR"
    assert stored.origin == "home"
    assert stored.destination == "albayzin"


async def test_log_manual_trip_distance_only(
    hass: HomeAssistant, configured_entry: MockConfigEntry
) -> None:
    """Distance can be passed directly without odometer bounds."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_LOG_MANUAL_TRIP,
        {
            "started_at": "2026-05-28T13:07:41+00:00",
            "ended_at": "2026-05-28T13:42:04+00:00",
            "distance_km": 18,
        },
        blocking=True,
    )

    coordinator = hass.data[DOMAIN][configured_entry.entry_id]
    stored = await coordinator.storage.async_get_last()
    assert stored is not None
    assert stored.distance_km == pytest.approx(18.0)
    # No soc bounds → no energy/cost
    assert stored.soc_used_pct is None
    assert stored.energy_kwh is None
    assert stored.cost is None


async def test_log_manual_trip_updates_last_trip_sensor(
    hass: HomeAssistant, configured_entry: MockConfigEntry
) -> None:
    """After backfilling, sensor.test_ev_last_trip_distance should reflect it."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_LOG_MANUAL_TRIP,
        {
            "started_at": "2026-05-28T13:07:41+00:00",
            "ended_at": "2026-05-28T13:42:04+00:00",
            "distance_km": 18,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.test_ev_last_trip_distance")
    assert state is not None
    assert float(state.state) == pytest.approx(18.0)
