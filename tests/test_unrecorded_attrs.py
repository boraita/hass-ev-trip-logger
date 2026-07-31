"""The big list attributes must be excluded from the recorder.

With a large recent window the trips/journeys/charges JSON blob exceeds the
recorder's 16 KB per-state attribute limit; declaring them unrecorded keeps the
entity state in history without the recorder warning / dropped attributes.
"""
from __future__ import annotations

from custom_components.ev_trip_logger.sensor import (
    RecentChargesSensor,
    RecentJourneysSensor,
    RecentTripsSensor,
)


def test_recent_list_attributes_are_unrecorded() -> None:
    assert "trips" in RecentTripsSensor._unrecorded_attributes
    assert "journeys" in RecentJourneysSensor._unrecorded_attributes
    assert "charges" in RecentChargesSensor._unrecorded_attributes


async def test_tracked_avg_unit_sticky_across_source_blips(
    hass, # type: ignore[no-untyped-def]
) -> None:
    """v0.5.48 — the avg sensor keeps the source's unit while the
    upstream integration blips unavailable; a None-and-back unit flip
    opens a units-changed Repair on the recorder statistics."""
    from custom_components.ev_trip_logger.sensor import TrackedAvgSensor

    from .test_coordinator import _setup
    from custom_components.ev_trip_logger.const import DOMAIN

    src = "sensor.byd_today_consumption"
    hass.states.async_set(src, "17.5", {"unit_of_measurement": "kWh/100km"})
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    sensor = TrackedAvgSensor(coordinator, src, days=7)
    sensor.hass = hass
    assert sensor.native_unit_of_measurement == "kWh/100km"

    # Upstream reload blip: state unavailable, attributes gone.
    hass.states.async_set(src, "unavailable", {})
    assert sensor.native_unit_of_measurement == "kWh/100km"  # sticky

    # Source comes back — still the same unit.
    hass.states.async_set(src, "17.6", {"unit_of_measurement": "kWh/100km"})
    assert sensor.native_unit_of_measurement == "kWh/100km"


async def test_tracked_avg_strips_car_integration_prefix(
    hass, # type: ignore[no-untyped-def]
) -> None:
    """v0.8.9 — a car integration's own prefix (e.g. BYD's "byd_") sits
    in front of the device title in the source entity_id, not exactly
    matching it. A plain startswith() never stripped it, producing a
    doubled slug like sensor.test_ev_byd_test_ev_energy_consumption.
    The device-title substring must be found and stripped wherever it
    falls, not only at position 0.
    """
    from custom_components.ev_trip_logger.sensor import TrackedAvgSensor

    from .test_coordinator import _setup
    from custom_components.ev_trip_logger.const import DOMAIN

    # _setup()'s entry title is "Test EV" -> device_prefix "test_ev".
    src = "sensor.byd_test_ev_energy_consumption"
    hass.states.async_set(src, "18.4", {"unit_of_measurement": "kWh/100km"})
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    sensor = TrackedAvgSensor(coordinator, src, days=7)
    assert sensor.entity_description.key == "energy_consumption_avg_7d"
