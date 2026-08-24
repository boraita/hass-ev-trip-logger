"""The big list attributes must be excluded from the recorder.

With a large recent window the trips/journeys/charges JSON blob exceeds the
recorder's 16 KB per-state attribute limit; declaring them unrecorded keeps the
entity state in history without the recorder warning / dropped attributes.
"""
from __future__ import annotations

import pytest

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


async def test_tracked_avg_retries_while_only_the_unit_is_missing(
    hass,  # type: ignore[no-untyped-def]
) -> None:
    """v0.8.23 — the startup retry gave up as soon as a VALUE existed.

    Measured on the real install: after a restart the avg sensor published
    at 20:48:13 and its source entity appeared at 20:48:16 — three seconds
    later. The sensor had a perfectly good mean by then, so the retry
    budget bailed out, and with a 1800 s refresh cadence it went on
    publishing `unit_of_measurement: None` for the next half hour. The
    recorder read that as a units change and refused to compile statistics
    for 22 sensors at once.

    A value without its unit is not "done starting up".
    """
    from custom_components.ev_trip_logger.sensor import TrackedAvgSensor

    from .test_coordinator import _setup
    from custom_components.ev_trip_logger.const import DOMAIN

    src = "sensor.byd_upstream_consumption"
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    sensor = TrackedAvgSensor(coordinator, src, days=30)
    sensor.hass = hass
    # A mean is already known, but the source has not been created yet —
    # exactly the state the real restart left the sensor in.
    sensor._mean = 19.46
    assert sensor.native_unit_of_measurement is None
    assert sensor._retry_needed() is True, (
        "a known value with an unknown unit must keep retrying"
    )

    # Source shows up with its unit: nothing left to wait for.
    hass.states.async_set(src, "25.5", {"unit_of_measurement": "kWh/100km"})
    assert sensor.native_unit_of_measurement == "kWh/100km"
    assert sensor._retry_needed() is False


async def test_elevation_has_no_phantom_current_trip_entities(
    hass,  # type: ignore[no-untyped-def]
) -> None:
    """v0.8.23 — elevation gain/loss are computed from the trip's GPS route
    AFTER it closes, so a `current_trip_` counterpart can never hold a
    value. Worse, neither had a translation, so both fell back to the
    device-class name and collided: the real install ended up with
    `sensor.<device>_distance` and `sensor.<device>_distance_2`, two
    permanently-unknown entities both called "Distance".
    """
    from custom_components.ev_trip_logger.sensor import _LAST_TRIP_ONLY

    assert "elevation_gain_m" in _LAST_TRIP_ONLY
    assert "elevation_loss_m" in _LAST_TRIP_ONLY


async def test_journey_sensors_surface_cost_lifo(
    hass,  # type: ignore[no-untyped-def]
) -> None:
    """v0.8.23 — v0.8.22 added `cost_lifo` to the journey SQL and to the
    dicts storage returns, but all three journey sensors rebuild their
    attribute payload key by key and none of them copied it across. The
    figure was computed on every refresh and dropped on the floor: on the
    real install every journey read `cost_lifo: None` while its own stages
    carried real values (journey 104: 0.15 + 0.47).
    """
    from custom_components.ev_trip_logger.const import DOMAIN
    from custom_components.ev_trip_logger.sensor import (
        CurrentJourneySensor,
        LastJourneySensor,
        RecentJourneysSensor,
    )

    from .test_coordinator import _setup

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    summary = {
        "journey_id": 104, "started_at": None, "ended_at": None,
        "distance_km": 10.0, "energy_kwh": 1.1,
        "cost": 0.62, "cost_lifo": 0.62, "stages": 3,
    }

    last = LastJourneySensor(coordinator)
    last._summary = dict(summary)
    assert "cost_lifo" in (last.extra_state_attributes or {})
    assert last.extra_state_attributes["cost_lifo"] == pytest.approx(0.62)

    cur = CurrentJourneySensor(coordinator)
    cur._closed = dict(summary)
    assert "cost_lifo" in cur._compute()

    recent = RecentJourneysSensor(coordinator)
    recent._journeys = [dict(summary)]
    out = recent.extra_state_attributes["journeys"][0]
    assert "cost_lifo" in out
    assert out["cost_lifo"] == pytest.approx(0.62)
