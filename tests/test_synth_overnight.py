"""Regression: synthetic trips must not span an overnight charge.

Cloud-polling (e.g. BYD) goes quiet while the car is parked. With polling
paused overnight the logger sees one odometer reading in the evening and the
next in the morning, and backfills the morning drive as a *synthetic* trip
(vehicle_on never toggled). The idle baseline used as the trip's start must
track the latest parked reading — including the SoC the overnight charge left
behind — otherwise the trip is back-dated by hours and inherits the pre-charge
SoC, making soc_start < soc_end → negative usage → no energy/cost/score.
"""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ev_trip_logger.const import CONF_MIN_TRIP_DISTANCE, DOMAIN

from .test_coordinator import BAT, ODO, _advance, _setup


async def test_synthetic_trip_after_overnight_charge_uses_post_charge_soc(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass, **{CONF_MIN_TRIP_DISTANCE: 2.0})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await hass.async_block_till_done()

    # Evening: trip just ended, car parked at 57% — establishes the idle baseline.
    hass.states.async_set(BAT, "57")
    await hass.async_block_till_done()

    # Overnight charge to 80%, car not moving (odometer unchanged).
    for soc in (60, 70, 80):
        hass.states.async_set(BAT, str(soc))
        await hass.async_block_till_done()
        await _advance(hass, 30)

    # Morning: a single cloud poll surfaces the commute as a +17 km odo jump,
    # SoC 80 -> 77, with no vehicle_on toggle.
    hass.states.async_set(ODO, "1017")
    hass.states.async_set(BAT, "77")
    await hass.async_block_till_done()
    await _advance(hass, 6)  # flush the coalesce window

    trip = coordinator.last_trip
    assert trip is not None
    assert trip.distance_km == pytest.approx(17.0)
    # Post-charge SoC (80 -> 77), NOT the stale pre-charge 57.
    assert trip.soc_used_pct == pytest.approx(3.0)
    assert trip.energy_kwh == pytest.approx(3.0 / 100 * 75)  # 2.25 kWh
    assert trip.cost is not None
