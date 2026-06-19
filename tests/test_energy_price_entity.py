"""Tests for the optional dynamic price entity (``energy_price_entity``).

The home tariff used for trip/charge cost can be driven by a live price
sensor (Octopus/Nordpool/PVPC/…) instead of the fixed ``energy_price_kwh``.
``_current_energy_price()`` reads that entity at cost-computation time and
falls back to the fixed price when it is unset, unavailable, or non-numeric.
"""
from __future__ import annotations

import pytest
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_trip_logger.const import (
    CONF_BATTERY,
    CONF_BATTERY_CAPACITY,
    CONF_ENERGY_PRICE,
    CONF_ENERGY_PRICE_ENTITY,
    CONF_NAME,
    CONF_ODOMETER,
    CONF_VEHICLE_ON,
    DOMAIN,
)

ODO = "sensor.odometer"
BAT = "sensor.battery"
VOK = "binary_sensor.vehicle_on"
PRICE = "sensor.precio_kwh"

FIXED = 0.20


async def _setup(hass: HomeAssistant, **overrides) -> MockConfigEntry:
    hass.states.async_set(ODO, "1000.0")
    hass.states.async_set(BAT, "80.0")
    hass.states.async_set(VOK, STATE_OFF)
    data = {
        CONF_NAME: "Test EV",
        CONF_ODOMETER: ODO,
        CONF_BATTERY: BAT,
        CONF_VEHICLE_ON: VOK,
        CONF_BATTERY_CAPACITY: 75.0,
        CONF_ENERGY_PRICE: FIXED,
    }
    data.update(overrides)
    entry = MockConfigEntry(domain=DOMAIN, data=data, title="Test EV")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _coord(hass: HomeAssistant, entry: MockConfigEntry):
    return hass.data[DOMAIN][entry.entry_id]


async def test_fixed_price_when_no_entity(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert _coord(hass, entry)._current_energy_price() == pytest.approx(FIXED)


async def test_entity_overrides_fixed_price(hass: HomeAssistant) -> None:
    hass.states.async_set(PRICE, "0.096")
    entry = await _setup(hass, **{CONF_ENERGY_PRICE_ENTITY: PRICE})
    assert _coord(hass, entry)._current_energy_price() == pytest.approx(0.096)


async def test_entity_tracks_live_changes(hass: HomeAssistant) -> None:
    hass.states.async_set(PRICE, "0.096")
    entry = await _setup(hass, **{CONF_ENERGY_PRICE_ENTITY: PRICE})
    coord = _coord(hass, entry)
    assert coord._current_energy_price() == pytest.approx(0.096)
    # A new tariff period (e.g. valle -> punta) is picked up live.
    hass.states.async_set(PRICE, "0.231")
    await hass.async_block_till_done()
    assert coord._current_energy_price() == pytest.approx(0.231)


async def test_fallback_when_entity_unavailable(hass: HomeAssistant) -> None:
    hass.states.async_set(PRICE, "unavailable")
    entry = await _setup(hass, **{CONF_ENERGY_PRICE_ENTITY: PRICE})
    assert _coord(hass, entry)._current_energy_price() == pytest.approx(FIXED)


async def test_fallback_when_entity_non_numeric(hass: HomeAssistant) -> None:
    hass.states.async_set(PRICE, "cheap")
    entry = await _setup(hass, **{CONF_ENERGY_PRICE_ENTITY: PRICE})
    assert _coord(hass, entry)._current_energy_price() == pytest.approx(FIXED)


async def test_fallback_when_entity_missing(hass: HomeAssistant) -> None:
    entry = await _setup(hass, **{CONF_ENERGY_PRICE_ENTITY: "sensor.does_not_exist"})
    assert _coord(hass, entry)._current_energy_price() == pytest.approx(FIXED)
