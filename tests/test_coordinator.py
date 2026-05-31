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


async def test_vehicle_off_closes_trip_immediately(hass: HomeAssistant) -> None:
    """Each on/off cycle is one trip — closes the moment vehicle_on flips off."""
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()

    hass.states.async_set(ODO, "1015")
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()

    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()

    # No idle window — closed immediately.
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


async def test_odo_jump_creates_synthetic_trip(hass: HomeAssistant) -> None:
    """A big odometer increase without vehicle_on going to on backfills a trip.

    The insert is debounced (see _SYNTH_COALESCE_WINDOW_S) — we advance time
    past the window to flush it.
    """
    entry = await _setup(hass, **{CONF_MIN_TRIP_DISTANCE: 2.0})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.last_trip is None

    # Baseline reading: odometer is at 1000, battery at 80 (from _setup)
    hass.states.async_set(BAT, "80")  # bump to refresh snapshot
    await hass.async_block_till_done()

    # Cloud poll happens later: odo suddenly +18 km, battery -8% — no vehicle_on toggle.
    hass.states.async_set(ODO, "1018")
    hass.states.async_set(BAT, "72")
    await hass.async_block_till_done()
    # Not committed yet — still inside the coalesce window.
    assert coordinator.last_trip is None

    # Advance past the coalesce window — finalize fires.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()

    assert coordinator.last_trip is not None
    assert coordinator.last_trip.distance_km == pytest.approx(18.0)
    assert coordinator.last_trip.soc_used_pct == pytest.approx(8.0)
    assert coordinator.last_trip.energy_kwh == pytest.approx(8.0 / 100 * 75)  # 6.0


async def test_odo_jump_below_threshold_does_nothing(hass: HomeAssistant) -> None:
    """Tiny odo movement (under min_trip_distance) does NOT create a synthetic trip."""
    entry = await _setup(hass, **{CONF_MIN_TRIP_DISTANCE: 2.0})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await hass.async_block_till_done()

    hass.states.async_set(ODO, "1001")  # +1 km, below 2.0 threshold
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()

    assert coordinator.last_trip is None


async def test_odo_jump_coalesces_consecutive_polls(hass: HomeAssistant) -> None:
    """Many small odo updates (cloud-polling) collapse into ONE synthetic trip.

    This is the real-world regression that motivated the coalesce window:
    BYD cloud polling emits +1 km updates every ~2 min during a drive. Without
    coalescing, we'd insert one trip per poll. With it, we insert one trip
    for the whole drive.
    """
    entry = await _setup(hass, **{CONF_MIN_TRIP_DISTANCE: 0.5})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    hass.states.async_set(BAT, "80")
    await hass.async_block_till_done()

    # Five consecutive +1 km polls 1 min apart while vehicle_on stays off.
    for km, bat in [(1001, 79), (1002, 78), (1003, 77), (1004, 76), (1005, 75)]:
        hass.states.async_set(ODO, str(km))
        hass.states.async_set(BAT, str(bat))
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1))
        await hass.async_block_till_done()
        # Still pending — every poll bumps the timer, finalize hasn't fired.
        assert coordinator.last_trip is None

    # Now wait past the coalesce window with no more polls.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()

    assert coordinator.last_trip is not None
    assert coordinator.last_trip.distance_km == pytest.approx(5.0)
    assert coordinator.last_trip.soc_used_pct == pytest.approx(5.0)


async def test_battery_energy_and_to_full_sensors(hass: HomeAssistant) -> None:
    """Battery-derived sensors track the source SoC live, even when idle."""
    entry = await _setup(hass)
    registry = er.async_get(hass)
    be_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_battery_energy"
    )
    ef_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_energy_to_full"
    )
    # Setup leaves BAT=80 and capacity=75 (CONF_BATTERY_CAPACITY default in _setup)
    assert float(hass.states.get(be_id).state) == pytest.approx(0.80 * 75)   # 60.0
    assert float(hass.states.get(ef_id).state) == pytest.approx(0.20 * 75)   # 15.0

    # Move battery and confirm the sensors track without any trip/charge open
    hass.states.async_set(BAT, "55")
    await hass.async_block_till_done()
    assert float(hass.states.get(be_id).state) == pytest.approx(0.55 * 75)   # 41.25
    assert float(hass.states.get(ef_id).state) == pytest.approx(0.45 * 75)   # 33.75


async def test_current_trip_cost_and_score_live(hass: HomeAssistant) -> None:
    """current_trip_cost and current_trip_score should update during a trip."""
    entry = await _setup(hass)
    registry = er.async_get(hass)
    cost_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_current_trip_cost")
    score_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_current_trip_score")
    assert cost_id is not None and score_id is not None

    # Idle defaults
    assert float(hass.states.get(cost_id).state) == 0.0
    assert hass.states.get(score_id).state == "unknown"

    # Open a trip and drive 20 km, drop 10 % SoC (energy 7.5 kWh at 75 kWh capacity).
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1020")
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()

    expected_energy = 0.10 * 75   # 7.5 kWh
    expected_consumption = expected_energy / 20.0 * 100   # 37.5 kWh/100km
    expected_score = max(0.0, min(10.0, 10 - max(0.0, expected_consumption - 14.5) * 0.6))
    expected_cost = expected_energy * 0.15   # home default

    assert float(hass.states.get(cost_id).state) == pytest.approx(expected_cost, abs=0.01)
    assert float(hass.states.get(score_id).state) == pytest.approx(expected_score, abs=0.05)


async def test_current_trip_sensors_idle_show_zero_not_unavailable(
    hass: HomeAssistant,
) -> None:
    """When no trip is open, current_* additive sensors show 0; ratios stay None."""
    entry = await _setup(hass)
    registry = er.async_get(hass)

    additive_keys = ["distance_km", "duration_min", "soc_used_pct", "energy_kwh", "max_power_kw"]
    for key in additive_keys:
        eid = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_current_{key}")
        assert eid is not None, f"missing {key}"
        state = hass.states.get(eid)
        assert state is not None and state.state not in ("unavailable", "unknown"), (
            f"{eid} should be available with 0, got {state.state if state else None}"
        )
        assert float(state.state) == 0.0

    ratio_keys = ["avg_speed_kmh", "consumption_kwh_100km", "avg_temp_c"]
    for key in ratio_keys:
        eid = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_current_{key}")
        state = hass.states.get(eid)
        # Available but value is None → HA renders as "unknown"
        assert state.state == "unknown", f"{eid} expected unknown, got {state.state}"


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


async def _run_stage(
    hass: HomeAssistant, *, odo_start: float, odo_end: float, soc_end: float,
    location_end: str,
) -> None:
    """Helper: open vehicle_on, set odo+battery+location, close via end_trip service."""
    hass.states.async_set(ODO, str(odo_start))
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, str(odo_end))
    hass.states.async_set(BAT, str(soc_end))
    hass.states.async_set(LOC, location_end)
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, SERVICE_END_TRIP, {}, blocking=True)
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()


async def test_recent_journeys_lists_only_completed(hass: HomeAssistant) -> None:
    """recent_journeys should exclude journeys still in progress."""
    hass.states.async_set(LOC, "home")
    entry = await _setup(hass, **{CONF_LOCATION: LOC})
    registry = er.async_get(hass)

    # 1 completed journey: home → work → home
    await _run_stage(hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="work")
    hass.states.async_set(LOC, "work")
    await _run_stage(hass, odo_start=1020, odo_end=1040, soc_end=65, location_end="home")

    # 1 open journey: home → work (no return)
    hass.states.async_set(LOC, "home")
    await _run_stage(hass, odo_start=1040, odo_end=1060, soc_end=55, location_end="work")

    rj_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_recent_journeys"
    )
    state = hass.states.get(rj_id)
    assert int(state.state) == 1, f"only 1 completed journey expected, got {state.state}"
    journeys = state.attributes["journeys"]
    assert journeys[0]["stages"] == 2
    assert journeys[0]["distance_km"] == pytest.approx(40.0)


async def test_current_journey_includes_active_stage_live(
    hass: HomeAssistant,
) -> None:
    """While driving stage 2 of a journey, current_journey should sum stage 1 + active distance."""
    hass.states.async_set(LOC, "home")
    entry = await _setup(hass, **{CONF_LOCATION: LOC})
    registry = er.async_get(hass)
    cj_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_current_journey"
    )

    # Stage 1: home → work (20 km closed)
    await _run_stage(hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="work")

    # Stage 2: opens at work, currently 8 km in but not closed yet
    hass.states.async_set(LOC, "work")
    hass.states.async_set(ODO, "1020")
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1028")
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()

    state = hass.states.get(cj_id)
    assert state is not None
    assert int(state.state) == 2  # 1 closed + 1 active stage
    assert state.attributes["distance_km"] == pytest.approx(28.0)  # 20 + 8 live
    assert state.attributes["stage_active"] is True


async def test_journey_chains_stages_until_home(hass: HomeAssistant) -> None:
    """Home → work → shops → home should produce a single 3-stage journey."""
    hass.states.async_set(LOC, "home")  # starting at home
    entry = await _setup(hass, **{CONF_LOCATION: LOC})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await _run_stage(hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="work")
    assert coordinator.current_journey_id is not None
    assert coordinator.last_completed_journey_id is None
    j_id = coordinator.current_journey_id

    # Continue to shops
    hass.states.async_set(LOC, "work")  # start of next stage = location_end of last
    await _run_stage(hass, odo_start=1020, odo_end=1025, soc_end=72, location_end="shops")
    assert coordinator.current_journey_id == j_id

    # Return home
    hass.states.async_set(LOC, "shops")
    await _run_stage(hass, odo_start=1025, odo_end=1045, soc_end=65, location_end="home")
    assert coordinator.current_journey_id is None
    assert coordinator.last_completed_journey_id == j_id

    summary = await coordinator.storage.async_journey_summary(j_id)
    assert summary is not None
    assert summary["stages"] == 3
    assert summary["distance_km"] == pytest.approx(45.0)  # 20 + 5 + 20


async def test_journey_does_not_open_when_starting_outside_home(
    hass: HomeAssistant,
) -> None:
    """A stage starting away from home does not open a journey (orphan)."""
    hass.states.async_set(LOC, "work")
    entry = await _setup(hass, **{CONF_LOCATION: LOC})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await _run_stage(hass, odo_start=1000, odo_end=1010, soc_end=75, location_end="shops")

    assert coordinator.current_journey_id is None
    assert coordinator.last_trip is not None
    assert coordinator.last_trip.journey_id is None


async def test_journey_retroactively_closes_when_next_stage_starts_at_home(
    hass: HomeAssistant,
) -> None:
    """If device_tracker missed the 'home arrival', the next stage starting at
    home should close the previous open journey."""
    hass.states.async_set(LOC, "home")
    entry = await _setup(hass, **{CONF_LOCATION: LOC})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Stage 1: home → not_home (journey opens, never closes because dest != home)
    await _run_stage(hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="not_home")
    jid_1 = coordinator.current_journey_id
    assert jid_1 is not None
    assert coordinator.last_completed_journey_id is None

    # Stage 2: starts at home (car obviously came back, but GPS missed it)
    hass.states.async_set(LOC, "home")
    await _run_stage(hass, odo_start=1020, odo_end=1050, soc_end=60, location_end="not_home")

    # journey 1 should have been retroactively closed
    assert coordinator.last_completed_journey_id == jid_1
    # journey 2 should be open with this stage
    assert coordinator.current_journey_id is not None
    assert coordinator.current_journey_id != jid_1


async def test_home_zone_resolves_zone_entity_to_friendly_name(
    hass: HomeAssistant,
) -> None:
    """home_zone = 'zone.home' must compare against device_tracker reporting 'home'."""
    from custom_components.ev_trip_logger.const import CONF_HOME_ZONE
    hass.states.async_set(
        "zone.home", "0", {"friendly_name": "home", "latitude": 40, "longitude": -3}
    )
    hass.states.async_set(LOC, "home")
    entry = await _setup(hass, **{CONF_LOCATION: LOC, CONF_HOME_ZONE: "zone.home"})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.home_zone == "home"

    # Drive a journey: home → not_home → home
    await _run_stage(hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="not_home")
    hass.states.async_set(LOC, "not_home")
    await _run_stage(hass, odo_start=1020, odo_end=1040, soc_end=65, location_end="home")

    assert coordinator.last_completed_journey_id is not None


async def test_home_zone_uses_slug_not_friendly_name(
    hass: HomeAssistant,
) -> None:
    """zone.home renamed to 'Rafelehouse' must still match device_tracker reporting 'home'.

    HA's device_tracker uses the zone's underlying slug (entity_id minus the
    'zone.' prefix), not its friendly_name. If we resolved the configured
    `zone.<id>` to its friendly_name we'd compare against the renamed label
    and journeys would never close.
    """
    from custom_components.ev_trip_logger.const import CONF_HOME_ZONE
    # User renamed zone.home to 'Rafelehouse' in the UI but device_tracker
    # still reports 'home' as state.
    hass.states.async_set(
        "zone.home",
        "0",
        {"friendly_name": "Rafelehouse", "latitude": 40, "longitude": -3},
    )
    hass.states.async_set(LOC, "home")
    entry = await _setup(hass, **{CONF_LOCATION: LOC, CONF_HOME_ZONE: "zone.home"})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # Must resolve to the slug, NOT the friendly_name.
    assert coordinator.home_zone == "home"
    assert coordinator._is_at_home("home")
    assert coordinator._is_at_home("HOME")  # case-insensitive belt-and-braces
    assert coordinator._is_at_home("  home  ")
    assert not coordinator._is_at_home("Rafelehouse")
    assert not coordinator._is_at_home("work")

    # Drive a home → not_home → home cycle and verify journey opens AND closes
    # even though the zone's friendly_name doesn't equal the device_tracker state.
    await _run_stage(hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="not_home")
    assert coordinator.current_journey_id is not None
    hass.states.async_set(LOC, "not_home")
    await _run_stage(hass, odo_start=1020, odo_end=1040, soc_end=65, location_end="home")
    assert coordinator.current_journey_id is None
    assert coordinator.last_completed_journey_id is not None


async def test_late_home_arrival_closes_journey_and_amends_destination(
    hass: HomeAssistant,
) -> None:
    """If device_tracker reports 'home' AFTER vehicle_on=off, close the journey.

    Cloud-polling integrations lag the geofence by 1-3 min. A trip that ends
    in the home driveway closes with destination='not_home' because location
    hasn't updated yet. When the location finally flips to 'home', we
    retroactively (a) amend the trip's destination, (b) close the open
    journey.
    """
    from custom_components.ev_trip_logger.const import CONF_HOME_ZONE
    hass.states.async_set(LOC, "home")
    entry = await _setup(
        hass, **{CONF_LOCATION: LOC, CONF_HOME_ZONE: "zone.home"}
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Drive home → not_home (journey 1 stays open).
    await _run_stage(
        hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="not_home"
    )
    assert coordinator.current_journey_id is not None
    assert coordinator.last_trip.destination == "not_home"

    # Drive back. Vehicle turns off but location sensor STILL says not_home
    # (geofence lag). Trip closes with destination='not_home'.
    hass.states.async_set(LOC, "not_home")
    await _run_stage(
        hass, odo_start=1020, odo_end=1040, soc_end=65, location_end="not_home"
    )
    assert coordinator.current_journey_id is not None  # still open
    assert coordinator.last_trip.destination == "not_home"
    assert coordinator.last_trip.trip_id is not None

    # 90 seconds later, device_tracker finally flips to 'home'.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
    hass.states.async_set(LOC, "home")
    await hass.async_block_till_done()

    # Journey closed retroactively, destination amended.
    assert coordinator.current_journey_id is None
    assert coordinator.last_completed_journey_id is not None
    assert coordinator.last_trip.destination == "home"


async def test_home_zone_accepts_legacy_plain_string(hass: HomeAssistant) -> None:
    """Users on the old text-field config (e.g. 'home' as string) still work."""
    from custom_components.ev_trip_logger.const import CONF_HOME_ZONE
    hass.states.async_set(LOC, "home")
    entry = await _setup(hass, **{CONF_LOCATION: LOC, CONF_HOME_ZONE: "home"})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.home_zone == "home"


async def test_journey_zone_is_configurable(hass: HomeAssistant) -> None:
    """A custom home zone name closes journeys instead of literal 'home'."""
    from custom_components.ev_trip_logger.const import CONF_HOME_ZONE
    hass.states.async_set(LOC, "casa")
    entry = await _setup(
        hass, **{CONF_LOCATION: LOC, CONF_HOME_ZONE: "casa"}
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await _run_stage(hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="oficina")
    j_id = coordinator.current_journey_id
    assert j_id is not None

    hass.states.async_set(LOC, "oficina")
    await _run_stage(hass, odo_start=1020, odo_end=1040, soc_end=65, location_end="casa")
    assert coordinator.last_completed_journey_id == j_id
    assert coordinator.current_journey_id is None


async def test_set_last_charge_price_updates_in_place(hass: HomeAssistant) -> None:
    """The new service overrides price + location without losing kwh / timestamp."""
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Auto-style: log at home price
    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE,
        {"kwh": 30.0, "location": "auto"},
        blocking=True,
    )
    original = coordinator.last_charge
    assert original.price_per_kwh == pytest.approx(0.15)  # home default

    # Override: pretend the user actually paid 0.45 €/kWh at a public charger
    await hass.services.async_call(
        DOMAIN, "set_last_charge_price",
        {"price_per_kwh": 0.45, "location": "Iberdrola Móstoles"},
        blocking=True,
    )
    updated = coordinator.last_charge
    assert updated.charge_id == original.charge_id  # same row
    assert updated.kwh == pytest.approx(original.kwh)  # unchanged
    assert updated.price_per_kwh == pytest.approx(0.45)
    assert updated.total_cost == pytest.approx(30.0 * 0.45)
    assert updated.location == "Iberdrola Móstoles"


async def test_set_last_charge_price_with_total_cost(hass: HomeAssistant) -> None:
    """Override via total_cost recomputes price_per_kwh from kwh."""
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE,
        {"kwh": 20.0, "price_per_kwh": 0.20},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "set_last_charge_price",
        {"total_cost": 11.20},
        blocking=True,
    )
    updated = coordinator.last_charge
    assert updated.total_cost == pytest.approx(11.20)
    assert updated.price_per_kwh == pytest.approx(11.20 / 20.0)


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
