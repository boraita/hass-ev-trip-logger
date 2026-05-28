"""Tests for the trip detection state machine and storage integration."""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ev_trip_logger.const import (
    CONF_BATTERY,
    CONF_BATTERY_CAPACITY,
    CONF_CHARGE_SENSOR,
    CONF_IDLE_TIMEOUT,
    CONF_LOCATION,
    CONF_MIN_TRIP_DISTANCE,
    CONF_NAME,
    CONF_ODOMETER,
    CONF_VEHICLE_ON,
    DOMAIN,
    SERVICE_DELETE_LAST_CHARGE,
    SERVICE_DELETE_LAST_TRIP,
    SERVICE_END_TRIP,
    SERVICE_LOG_CHARGE,
)

CHG = "binary_sensor.byd_charging"
LOC = "device_tracker.byd_location"

ODO = "sensor.odometer"
BAT = "sensor.battery"
VOK = "binary_sensor.vehicle_on"


def _seed_states(hass: HomeAssistant, *, odo: float, bat: float, on: bool) -> None:
    hass.states.async_set(ODO, str(odo))
    hass.states.async_set(BAT, str(bat))
    hass.states.async_set(VOK, STATE_ON if on else STATE_OFF)


async def _setup(hass: HomeAssistant, **overrides) -> MockConfigEntry:
    _seed_states(
        hass,
        odo=overrides.pop("odo", 1000.0),
        bat=overrides.pop("bat", 80.0),
        on=overrides.pop("on", False),
    )
    data = {
        CONF_NAME: "Test EV",
        CONF_ODOMETER: ODO,
        CONF_BATTERY: BAT,
        CONF_VEHICLE_ON: VOK,
        CONF_BATTERY_CAPACITY: 75.0,
        CONF_MIN_TRIP_DISTANCE: 0.5,
        CONF_IDLE_TIMEOUT: 1,
    }
    data.update(overrides)
    entry = MockConfigEntry(domain=DOMAIN, data=data, title="Test EV")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _advance(hass: HomeAssistant, minutes: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done()


async def test_resume_trip_at_startup_when_metrics_ready(
    hass: HomeAssistant,
) -> None:
    """Vehicle on at startup with valid odo/soc → trip opens automatically."""
    entry = await _setup(hass, on=True)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert coordinator.current is not None
    assert coordinator.current.odometer_start == 1000.0
    assert coordinator.current.soc_start == 80.0


async def test_skip_auto_open_when_metrics_unknown(hass: HomeAssistant) -> None:
    """Vehicle on at startup but odo/soc still unknown → don't open a bogus trip."""
    hass.states.async_set(ODO, STATE_UNKNOWN)
    hass.states.async_set(BAT, STATE_UNKNOWN)
    hass.states.async_set(VOK, STATE_ON)
    data = {
        CONF_NAME: "Test EV",
        CONF_ODOMETER: ODO,
        CONF_BATTERY: BAT,
        CONF_VEHICLE_ON: VOK,
        CONF_BATTERY_CAPACITY: 75.0,
        CONF_MIN_TRIP_DISTANCE: 0.5,
        CONF_IDLE_TIMEOUT: 1,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, title="Test EV")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.current is None


async def test_vehicle_on_opens_trip(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.current is None

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()

    assert coordinator.current is not None
    assert coordinator.current.odometer_start == 1000.0
    assert coordinator.current.soc_start == 80.0


async def test_idle_timeout_closes_and_persists_trip(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()

    hass.states.async_set(ODO, "1015")
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()

    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    assert coordinator.current is not None  # still inside idle window

    await _advance(hass, 2)  # idle_timeout = 1 min

    assert coordinator.current is None
    assert coordinator.last_trip is not None
    assert coordinator.last_trip.distance_km == pytest.approx(15.0)
    assert coordinator.last_trip.soc_used_pct == pytest.approx(10.0)
    assert coordinator.last_trip.energy_kwh == pytest.approx(7.5)
    assert coordinator.last_trip.trip_id is not None


async def test_short_trip_is_discarded(hass: HomeAssistant) -> None:
    entry = await _setup(hass, **{CONF_MIN_TRIP_DISTANCE: 1.0})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1000.3")
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()

    await _advance(hass, 2)

    assert coordinator.current is None
    assert coordinator.last_trip is None


async def test_end_trip_service_closes_immediately(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1010")
    hass.states.async_set(BAT, "75")
    await hass.async_block_till_done()

    await hass.services.async_call(DOMAIN, SERVICE_END_TRIP, {}, blocking=True)

    assert coordinator.current is None
    assert coordinator.last_trip is not None
    assert coordinator.last_trip.distance_km == pytest.approx(10.0)


async def test_delete_last_trip_service_removes_record(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1010")
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, SERVICE_END_TRIP, {}, blocking=True)
    assert coordinator.last_trip is not None

    await hass.services.async_call(DOMAIN, SERVICE_DELETE_LAST_TRIP, {}, blocking=True)

    assert coordinator.last_trip is None


async def test_current_trip_distance_updates_live(hass: HomeAssistant) -> None:
    """While a trip is open, current_trip_distance reflects odometer changes immediately."""
    entry = await _setup(hass)
    registry = er.async_get(hass)
    current_distance_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_current_distance_km"
    )
    assert current_distance_id is not None

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    assert float(hass.states.get(current_distance_id).state) == pytest.approx(0.0)

    hass.states.async_set(ODO, "1007.5")
    await hass.async_block_till_done()
    assert float(hass.states.get(current_distance_id).state) == pytest.approx(7.5)


async def test_log_charge_with_total_cost_derives_price(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE,
        {"kwh": 20.0, "total_cost": 9.0, "location": "Iberdrola Mostoles"},
        blocking=True,
    )

    assert coordinator.last_charge is not None
    assert coordinator.last_charge.kwh == pytest.approx(20.0)
    assert coordinator.last_charge.total_cost == pytest.approx(9.0)
    assert coordinator.last_charge.price_per_kwh == pytest.approx(0.45)
    assert coordinator.last_charge.location == "Iberdrola Mostoles"


async def test_log_charge_without_price_uses_home_default(hass: HomeAssistant) -> None:
    # _setup() leaves CONF_ENERGY_PRICE at its default (0.15)
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE, {"kwh": 10.0}, blocking=True
    )

    assert coordinator.last_charge is not None
    assert coordinator.last_charge.price_per_kwh == pytest.approx(0.15)
    assert coordinator.last_charge.total_cost == pytest.approx(1.5)


async def test_trip_cost_uses_last_charge_price(hass: HomeAssistant) -> None:
    """A trip closed after a logged charge should use that charge's price."""
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Log a charge at 0.50 €/kWh (away from home, expensive).
    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE,
        {"kwh": 5.0, "price_per_kwh": 0.50},
        blocking=True,
    )

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1020")  # 20 km
    hass.states.async_set(BAT, "60")    # 20% used → 15 kWh
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, SERVICE_END_TRIP, {}, blocking=True)

    trip = coordinator.last_trip
    assert trip is not None
    assert trip.energy_kwh == pytest.approx(15.0)
    # Cost = energy * last-charge price (0.50), NOT the default 0.15.
    assert trip.cost == pytest.approx(7.5)


async def test_auto_detect_charge_records_session(hass: HomeAssistant) -> None:
    """Toggling the configured charge_sensor off→on→off persists a charge."""
    hass.states.async_set(CHG, STATE_OFF)
    entry = await _setup(hass, **{CONF_CHARGE_SENSOR: CHG})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.current_charge is None

    # Open charge session
    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    assert coordinator.current_charge is not None
    assert coordinator.current_charge.soc_start == 80.0

    # Battery climbs to 90% (7.5 kWh added at 75 kWh capacity)
    hass.states.async_set(BAT, "90")
    await hass.async_block_till_done()

    # Stop charging
    hass.states.async_set(CHG, STATE_OFF)
    await hass.async_block_till_done()

    assert coordinator.current_charge is None
    assert coordinator.last_charge is not None
    assert coordinator.last_charge.kwh == pytest.approx(7.5)
    assert coordinator.last_charge.location == "auto"
    assert "auto-detected" in (coordinator.last_charge.notes or "")


async def test_auto_detect_skips_when_recent_manual_charge_exists(
    hass: HomeAssistant,
) -> None:
    """If user just called log_charge, the auto-detect close shouldn't double-log."""
    hass.states.async_set(CHG, STATE_OFF)
    entry = await _setup(hass, **{CONF_CHARGE_SENSOR: CHG})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Manual log first
    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE,
        {"kwh": 8.0, "price_per_kwh": 0.45, "location": "manual"},
        blocking=True,
    )
    assert coordinator.last_charge is not None
    assert coordinator.last_charge.location == "manual"

    # Auto-detect cycle right after
    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(BAT, "90")
    await hass.async_block_till_done()
    hass.states.async_set(CHG, STATE_OFF)
    await hass.async_block_till_done()

    # last_charge should still be the manual one — no dup
    assert coordinator.last_charge.location == "manual"


async def test_auto_detect_charge_uses_device_tracker_location(
    hass: HomeAssistant,
) -> None:
    """If a device_tracker is configured, auto-charge inherits its current zone."""
    hass.states.async_set(CHG, STATE_OFF)
    hass.states.async_set(LOC, "home")
    entry = await _setup(hass, **{CONF_CHARGE_SENSOR: CHG, CONF_LOCATION: LOC})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(BAT, "90")
    await hass.async_block_till_done()
    hass.states.async_set(CHG, STATE_OFF)
    await hass.async_block_till_done()

    assert coordinator.last_charge is not None
    assert coordinator.last_charge.location == "home"


async def test_manual_log_charge_defaults_location_to_device_tracker(
    hass: HomeAssistant,
) -> None:
    """Manual log_charge with no location should pick it up from device_tracker."""
    hass.states.async_set(LOC, "Iberdrola Mostoles")
    entry = await _setup(hass, **{CONF_LOCATION: LOC})

    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE,
        {"kwh": 25.0, "price_per_kwh": 0.45},  # no location passed
        blocking=True,
    )

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.last_charge.location == "Iberdrola Mostoles"


async def test_auto_detect_discards_negative_delta(hass: HomeAssistant) -> None:
    """If SoC doesn't increase between charge on and off, drop the session."""
    hass.states.async_set(CHG, STATE_OFF)
    entry = await _setup(hass, **{CONF_CHARGE_SENSOR: CHG})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    # battery actually drops (sensor glitch)
    hass.states.async_set(BAT, "78")
    await hass.async_block_till_done()
    hass.states.async_set(CHG, STATE_OFF)
    await hass.async_block_till_done()

    assert coordinator.current_charge is None
    assert coordinator.last_charge is None


async def test_delete_last_charge_service(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE, {"kwh": 10.0, "price_per_kwh": 0.20},
        blocking=True,
    )
    assert coordinator.last_charge is not None

    await hass.services.async_call(
        DOMAIN, SERVICE_DELETE_LAST_CHARGE, {}, blocking=True
    )
    assert coordinator.last_charge is None


async def test_recent_trips_sensor_lists_trips_in_attributes(
    hass: HomeAssistant,
) -> None:
    """recent_trips attribute should contain serialized trips with score + cost."""
    entry = await _setup(hass)
    registry = er.async_get(hass)
    rt_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_recent_trips"
    )
    assert rt_id is not None
    assert float(hass.states.get(rt_id).state) == 0  # no trips yet

    # Log a charge so the trip has a price > default
    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE,
        {"kwh": 10.0, "price_per_kwh": 0.30},
        blocking=True,
    )

    # Drive a trip
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1030")  # 30 km
    hass.states.async_set(BAT, "65")    # 15% drop → 11.25 kWh
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, SERVICE_END_TRIP, {}, blocking=True)
    await hass.async_block_till_done()

    state = hass.states.get(rt_id)
    assert state is not None
    assert int(state.state) == 1
    trips = state.attributes.get("trips")
    assert trips is not None and len(trips) == 1
    t = trips[0]
    assert t["distance_km"] == pytest.approx(30.0)
    assert t["energy_kwh"] == pytest.approx(11.25)
    assert t["consumption_kwh_100km"] == pytest.approx(37.5)
    assert t["cost"] == pytest.approx(3.38, abs=0.01)
    assert t["score"] is not None  # depends on consumption


async def test_today_aggregate_sensor_refreshes_on_trip_close(
    hass: HomeAssistant,
) -> None:
    """End-to-end: closing a trip immediately bumps the today-distance aggregate."""
    entry = await _setup(hass)
    registry = er.async_get(hass)
    today_distance_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_total_today_distance"
    )
    assert today_distance_id is not None
    initial = hass.states.get(today_distance_id)
    assert initial is not None
    assert float(initial.state) == pytest.approx(0.0)

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1012.5")
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, SERVICE_END_TRIP, {}, blocking=True)
    await hass.async_block_till_done()

    refreshed = hass.states.get(today_distance_id)
    assert refreshed is not None
    assert float(refreshed.state) == pytest.approx(12.5)
