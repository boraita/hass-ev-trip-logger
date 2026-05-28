"""Regression: HA restart mid-trip while the vehicle source integration
hasn't yet repopulated odometer/battery must not silently swallow the trip.

Real-world repro: BYD Sealion 7, integration was running, HA restarted at
~15:58 UTC while the car was on, vehicle_on was restored to `on` but the
BYD entity hadn't fetched odometer/battery yet. The original
_maybe_resume_trip ran once, saw odometer=None, logged a warning, and
never tried again — the entire 21 km drive vanished from the log.

Fix: _async_metric_changed retries the open when metrics catch up.
"""
from __future__ import annotations

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
)


@pytest.fixture
async def entry_with_vehicle_on_no_metrics(hass: HomeAssistant):
    """Simulate the post-restart race: vehicle_on already on, odometer/battery
    still unknown when the integration loads."""
    hass.states.async_set("binary_sensor.fake_vehicle_on", "on")
    hass.states.async_set("sensor.fake_odometer", "unknown")
    hass.states.async_set("sensor.fake_battery", "unknown")

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
        unique_id="test-ev-resume-race",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_metric_catchup_opens_missed_trip(
    hass: HomeAssistant,
    entry_with_vehicle_on_no_metrics: MockConfigEntry,
) -> None:
    coordinator = hass.data[DOMAIN][entry_with_vehicle_on_no_metrics.entry_id]

    # Initial: vehicle_on=on but metrics unknown — resume must have skipped.
    assert coordinator.current is None, "trip wrongly opened with no metrics"

    # BYD finally publishes odometer + battery: this should open the trip.
    hass.states.async_set("sensor.fake_odometer", "25325")
    hass.states.async_set("sensor.fake_battery", "55")
    await hass.async_block_till_done()

    assert coordinator.current is not None, (
        "metric catch-up did not retroactively open the trip"
    )
    assert coordinator.current.odometer_start == pytest.approx(25325.0)
    assert coordinator.current.soc_start == pytest.approx(55.0)


async def test_metric_change_when_vehicle_off_does_not_open(
    hass: HomeAssistant,
) -> None:
    """Negative case: metric updates while vehicle is off must NOT spawn
    a phantom trip."""
    hass.states.async_set("binary_sensor.fake_vehicle_on", "off")
    hass.states.async_set("sensor.fake_odometer", "25325")
    hass.states.async_set("sensor.fake_battery", "55")

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
        unique_id="test-ev-no-phantom",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set("sensor.fake_odometer", "25326")
    await hass.async_block_till_done()

    assert coordinator.current is None, (
        "phantom trip opened while vehicle_on=off"
    )
