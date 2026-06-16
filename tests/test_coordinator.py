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
    CONF_POWER,
    CONF_SPEED,
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
POW = "sensor.power"
SPD = "sensor.speed"


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

    # v0.5.53 — off-grace is now 180 s (covers brief stops mid-trip).
    # Advancing 4 min is enough to pass the grace + idle timeout.
    await _advance(hass, 4)
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

    # v0.5.53 — wait for the 180-s grace + close to elapse.
    await _advance(hass, 4)

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


async def test_trip_cost_uses_configured_home_tariff(hass: HomeAssistant) -> None:
    """Trip cost always uses CONF_ENERGY_PRICE, not the last-charge price.

    Earlier versions (<= v0.5.6) inherited the price from the most recent
    charge. That broke when the user logged a one-off free/expensive
    external charge: every subsequent trip got costed at €0 or at €0.50/kWh
    when in reality the energy mostly came from prior home charges.
    """
    entry = await _setup(hass)  # home tariff defaults to 0.15 €/kWh
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # An exotic charge — won't affect trip cost.
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
    # Cost = energy * configured home tariff (0.15), NOT the one-off charge's 0.50.
    assert trip.cost == pytest.approx(2.25)


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


async def test_late_zone_arrival_amends_destination_without_closing_journey(
    hass: HomeAssistant,
) -> None:
    """Arriving at a non-home zone (work, gym, etc.) amends destination only.

    Home is the natural journey terminator; arriving at 'Trabajo' should
    update the trip's destination so history reads correctly but must NOT
    close the journey — the user is still away from home.
    """
    from custom_components.ev_trip_logger.const import CONF_HOME_ZONE
    hass.states.async_set(LOC, "home")
    entry = await _setup(
        hass, **{CONF_LOCATION: LOC, CONF_HOME_ZONE: "zone.home"}
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Drive home → work, but location still says not_home when engine turns off.
    hass.states.async_set(LOC, "home")
    await _run_stage(
        hass, odo_start=1000, odo_end=1015, soc_end=75, location_end="not_home"
    )
    journey_before = coordinator.current_journey_id
    assert journey_before is not None
    assert coordinator.last_trip.destination == "not_home"

    # Geofence lag — 90 seconds later, device_tracker flips to 'Trabajo ele '
    # (custom zone with a trailing space, as the user's real HA reports it).
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
    hass.states.async_set(LOC, "Trabajo ele ")
    await hass.async_block_till_done()

    # Destination amended.
    assert coordinator.last_trip.destination == "Trabajo ele "
    # Journey stays OPEN — non-home zone is not a terminator.
    assert coordinator.current_journey_id == journey_before


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
    # 11.25 kWh × 0.15 €/kWh (configured home tariff) = 1.69 €
    # (Trip cost does NOT inherit the 0.30 from the one-off external charge.)
    assert t["cost"] == pytest.approx(1.69, abs=0.01)
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


async def test_trip_record_persists_regen_and_max_speed(hass: HomeAssistant) -> None:
    """Storage round-trips regen_kwh and max_speed_kmh on a TripRecord."""
    from custom_components.ev_trip_logger.storage import TripRecord, TripStorage

    entry = await _setup(hass)
    storage: TripStorage = hass.data[DOMAIN][entry.entry_id].storage
    record = TripRecord(
        started_at=dt_util.now() - timedelta(minutes=30),
        ended_at=dt_util.now(),
        duration_min=30.0,
        distance_km=22.0,
        max_speed_kmh=118.5,
        regen_kwh=1.84,
    )
    trip_id = await storage.async_insert(record)
    assert trip_id > 0

    fetched = await storage.async_get_last()
    assert fetched is not None
    assert fetched.regen_kwh == pytest.approx(1.84)
    assert fetched.max_speed_kmh == pytest.approx(118.5)


async def test_charge_record_persists_is_dcfc_flag(hass: HomeAssistant) -> None:
    """Storage round-trips the is_dcfc flag on a ChargeRecord."""
    from custom_components.ev_trip_logger.storage import ChargeRecord, TripStorage

    entry = await _setup(hass)
    storage: TripStorage = hass.data[DOMAIN][entry.entry_id].storage
    dc_charge = ChargeRecord(
        ended_at=dt_util.now(),
        kwh=30.0,
        price_per_kwh=0.40,
        total_cost=12.0,
        is_dcfc=True,
    )
    ac_charge = ChargeRecord(
        ended_at=dt_util.now() + timedelta(seconds=1),
        kwh=12.0,
        price_per_kwh=0.07,
        total_cost=0.84,
        is_dcfc=False,
    )
    await storage.async_insert_charge(dc_charge)
    await storage.async_insert_charge(ac_charge)

    recent = await storage.async_recent_charges(limit=10)
    flags = {c.kwh: c.is_dcfc for c in recent}
    assert flags[30.0] is True
    assert flags[12.0] is False


async def test_charges_aggregates_segment_by_ac_dc(hass: HomeAssistant) -> None:
    """Aggregates expose AC vs DC totals + avg prices separately."""
    from custom_components.ev_trip_logger.storage import ChargeRecord, TripStorage

    entry = await _setup(hass)
    storage: TripStorage = hass.data[DOMAIN][entry.entry_id].storage
    now = dt_util.now()
    # Two AC sessions @ €0.07/kWh, one DC session @ €0.40/kWh.
    await storage.async_insert_charge(ChargeRecord(
        ended_at=now - timedelta(days=3), kwh=10.0, price_per_kwh=0.07,
        total_cost=0.70, is_dcfc=False,
    ))
    await storage.async_insert_charge(ChargeRecord(
        ended_at=now - timedelta(days=2), kwh=20.0, price_per_kwh=0.07,
        total_cost=1.40, is_dcfc=False,
    ))
    await storage.async_insert_charge(ChargeRecord(
        ended_at=now - timedelta(days=1), kwh=30.0, price_per_kwh=0.40,
        total_cost=12.0, is_dcfc=True,
    ))

    agg = await storage.async_charges_aggregates_since(now - timedelta(days=30))
    # AC: 30 kWh / €2.10 → avg €0.07/kWh
    assert agg["ac_kwh"] == pytest.approx(30.0)
    assert agg["avg_ac_price_per_kwh"] == pytest.approx(0.07)
    # DC: 30 kWh / €12.00 → avg €0.40/kWh
    assert agg["dc_kwh"] == pytest.approx(30.0)
    assert agg["avg_dc_price_per_kwh"] == pytest.approx(0.40)
    # Combined average is the blended €0.235/kWh — proving the segmented
    # numbers are needed to see the truth.
    assert agg["avg_price_per_kwh"] == pytest.approx(14.10 / 60.0)


async def test_consumption_by_temp_bucket_groups_trips(hass: HomeAssistant) -> None:
    """Trips bucket by avg_temp_c into 5°C bins, distance-weighted."""
    from custom_components.ev_trip_logger.storage import TripRecord, TripStorage

    entry = await _setup(hass)
    storage: TripStorage = hass.data[DOMAIN][entry.entry_id].storage
    base = dt_util.now() - timedelta(days=10)
    # Winter trip: 5 °C, 20 kWh/100km, 50 km
    await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(minutes=30),
        duration_min=30.0, distance_km=50.0, consumption_kwh_100km=20.0,
        avg_temp_c=5.0,
    ))
    # Summer trip: 22 °C, 14 kWh/100km, 100 km
    await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(minutes=60),
        duration_min=60.0, distance_km=100.0, consumption_kwh_100km=14.0,
        avg_temp_c=22.0,
    ))

    buckets = await storage.async_consumption_by_temp_bucket(
        base - timedelta(days=1), bucket_size_c=5.0
    )
    # 5 °C falls into bucket [5,10) → consumption 20.
    assert buckets["by_bucket"]["5"] == pytest.approx(20.0)
    # 22 °C falls into bucket [20,25) → consumption 14.
    assert buckets["by_bucket"]["20"] == pytest.approx(14.0)
    assert buckets["sample_count"] == 2


async def test_current_charge_snapshot_during_session(hass: HomeAssistant) -> None:
    """current_charge_snapshot returns live kWh/cost/type while charging."""
    hass.states.async_set(CHG, STATE_OFF)
    entry = await _setup(hass, **{CONF_CHARGE_SENSOR: CHG})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(BAT, "40")
    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    assert coordinator.current_charge is not None

    # SoC climbs to 55 % → 11.25 kWh on a 75 kWh pack.
    coordinator.current_charge.last_seen_soc = 55.0
    snap = coordinator.current_charge_snapshot()
    assert snap is not None
    assert snap["kwh"] == pytest.approx(11.25)
    # Default home price 0.15 €/kWh × 11.25 ≈ 1.69
    assert snap["total_cost"] == pytest.approx(11.25 * 0.15, abs=0.01)
    # No power reading yet → classification stays unknown
    assert snap["is_dcfc"] in (None, False, True)  # don't over-assert; covered below


async def test_current_charge_dcfc_classification_from_live_power(
    hass: HomeAssistant,
) -> None:
    """Live charging power above threshold flips is_dcfc=True in real time."""
    hass.states.async_set(POW, "0")
    hass.states.async_set(CHG, STATE_OFF)
    entry = await _setup(hass, **{CONF_CHARGE_SENSOR: CHG, CONF_POWER: POW})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(BAT, "30")
    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()

    # AC at 7 kW → AC.
    hass.states.async_set(POW, "7")
    await hass.async_block_till_done()
    snap = coordinator.current_charge_snapshot()
    assert snap is not None
    assert snap["power_kw"] == pytest.approx(7.0)
    assert snap["is_dcfc"] is False

    # Plug into a DC fast-charger at 90 kW → DC.
    hass.states.async_set(POW, "90")
    await hass.async_block_till_done()
    snap = coordinator.current_charge_snapshot()
    assert snap["power_kw"] == pytest.approx(90.0)
    assert snap["is_dcfc"] is True


async def test_purge_trips_service_deletes_in_range(hass: HomeAssistant) -> None:
    """purge_trips deletes trips by started_at range; in-memory state refreshes."""
    from custom_components.ev_trip_logger.const import SERVICE_PURGE_TRIPS
    from custom_components.ev_trip_logger.storage import TripRecord, TripStorage

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    storage: TripStorage = coordinator.storage

    base = dt_util.now() - timedelta(days=3)
    # 3 trips on 2 different days.
    await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(minutes=10),
        duration_min=10.0, distance_km=5.0,
    ))
    await storage.async_insert(TripRecord(
        started_at=base + timedelta(hours=1), ended_at=base + timedelta(hours=1, minutes=10),
        duration_min=10.0, distance_km=8.0,
    ))
    await storage.async_insert(TripRecord(
        started_at=base + timedelta(days=1), ended_at=base + timedelta(days=1, minutes=10),
        duration_min=10.0, distance_km=3.0,
    ))
    coordinator.last_trip = await storage.async_get_last()
    assert coordinator.last_trip is not None
    assert len(await storage.async_recent_trips(limit=10)) == 3

    # Purge the two trips on the base day.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_PURGE_TRIPS,
        {"since": base - timedelta(minutes=1),
         "until": base + timedelta(hours=2)},
        blocking=True,
    )
    remaining = await storage.async_recent_trips(limit=10)
    assert len(remaining) == 1
    assert remaining[0].distance_km == pytest.approx(3.0)


async def test_recent_trips_attr_exposes_full_schema(hass: HomeAssistant) -> None:
    """Recent_trips items must expose every captured field.

    Regression: v0.4.3 briefly stripped down the schema, breaking dashboards
    that consume max_power_kw / avg_speed_kmh / regen_kwh / soc_* / etc.
    Both `id` and `trip_id` must be present (dual alias for back-compat).
    """
    from custom_components.ev_trip_logger.storage import TripRecord, TripStorage

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    storage: TripStorage = coordinator.storage

    await storage.async_insert(TripRecord(
        started_at=dt_util.now() - timedelta(minutes=30),
        ended_at=dt_util.now(),
        duration_min=30.0,
        distance_km=22.0,
        odometer_start=10000.0, odometer_end=10022.0,
        soc_start=80.0, soc_end=72.0, soc_used_pct=8.0,
        energy_kwh=6.0,
        consumption_kwh_100km=27.3,
        avg_speed_kmh=44.0,
        max_speed_kmh=118.5,
        max_power_kw=85.0,
        regen_kwh=1.84,
        avg_temp_c=17.5,
        origin="home", destination="Trabajo ele ",
        cost=0.42, currency="EUR",
        journey_id=1,
    ))
    coordinator._notify_trip_log_listeners()
    await hass.async_block_till_done()

    state = hass.states.get(f"sensor.test_ev_recent_trips")
    assert state is not None
    trips = state.attributes.get("trips") or []
    assert len(trips) >= 1
    t = trips[0]

    # Dual-alias id and trip_id
    assert t["id"] is not None
    assert t["trip_id"] == t["id"]
    # Every captured field must be exposed
    for key in (
        "journey_id", "started_at", "ended_at",
        "distance_km", "duration_min",
        "odometer_start", "odometer_end",
        "soc_start", "soc_end", "soc_used_pct",
        "energy_kwh", "consumption_kwh_100km",
        "avg_speed_kmh", "max_speed_kmh", "max_power_kw", "regen_kwh",
        "avg_temp_c",
        "origin", "destination",
        "cost", "currency", "score",
    ):
        assert key in t, f"missing key: {key}"
    assert t["max_speed_kmh"] in (118, 119)  # banker's rounding 118.5→118
    assert t["regen_kwh"] == 1.84
    # v0.5.19 — `destination` is humanized (stripped); the raw
    # device_tracker state (trailing space included) lives in _raw.
    assert t["destination"] == "Trabajo ele"
    assert t["destination_raw"] == "Trabajo ele "


async def test_recent_charges_attr_exposes_full_schema(hass: HomeAssistant) -> None:
    """Recent_charges must include is_dcfc, started_at, soc_*, notes."""
    from custom_components.ev_trip_logger.storage import ChargeRecord, TripStorage

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    storage: TripStorage = coordinator.storage

    started = dt_util.now() - timedelta(minutes=20)
    await storage.async_insert_charge(ChargeRecord(
        started_at=started, ended_at=dt_util.now(),
        kwh=18.0, price_per_kwh=0.40, total_cost=7.20,
        currency="EUR", soc_start=30.0, soc_end=54.0,
        location="Repsol", notes="DC fast", is_dcfc=True,
    ))
    coordinator._notify_trip_log_listeners()
    await hass.async_block_till_done()

    state = hass.states.get(f"sensor.test_ev_recent_charges")
    assert state is not None
    charges = state.attributes.get("charges") or []
    assert len(charges) >= 1
    c = charges[0]

    assert c["id"] is not None
    assert c["charge_id"] == c["id"]
    for key in ("started_at", "ended_at", "kwh", "price_per_kwh", "total_cost",
                "currency", "soc_start", "soc_end", "location", "notes", "is_dcfc"):
        assert key in c, f"missing key: {key}"
    assert c["is_dcfc"] is True
    assert c["location"] == "Repsol"


async def test_v050_storage_round_trip_positions_and_aggregates(
    hass: HomeAssistant,
) -> None:
    """v0.5.0 backend covers: GPS positions + monthly history + daily km +
    trip patterns + avg trip metrics + tops + avg charge metrics."""
    from custom_components.ev_trip_logger.storage import (
        ChargeRecord,
        TripRecord,
        TripStorage,
    )

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    storage: TripStorage = coordinator.storage

    # Seed 3 trips on 2 different months and 1 different weekday.
    base_now = dt_util.now()
    tr_a = TripRecord(
        started_at=base_now - timedelta(days=40),
        ended_at=base_now - timedelta(days=40) + timedelta(minutes=30),
        duration_min=30.0,
        distance_km=20.0,
        energy_kwh=4.0,
        consumption_kwh_100km=20.0,
        avg_speed_kmh=40.0,
        cost=1.0,
    )
    tr_b = TripRecord(
        started_at=base_now - timedelta(days=5),
        ended_at=base_now - timedelta(days=5) + timedelta(minutes=60),
        duration_min=60.0,
        distance_km=50.0,
        energy_kwh=9.0,
        consumption_kwh_100km=18.0,
        avg_speed_kmh=50.0,
        cost=2.0,
    )
    tr_c = TripRecord(
        started_at=base_now - timedelta(days=1),
        ended_at=base_now - timedelta(days=1) + timedelta(minutes=20),
        duration_min=20.0,
        distance_km=10.0,
        energy_kwh=1.5,
        consumption_kwh_100km=15.0,
        avg_speed_kmh=30.0,
        cost=0.5,
    )
    id_a = await storage.async_insert(tr_a)
    id_b = await storage.async_insert(tr_b)
    id_c = await storage.async_insert(tr_c)
    assert id_a and id_b and id_c

    # ===== GPS positions =====
    samples = [
        (base_now - timedelta(seconds=60), 40.123, -3.567),
        (base_now - timedelta(seconds=30), 40.124, -3.568),
        (base_now, 40.125, -3.569),
    ]
    n = await storage.async_insert_positions(id_c, samples)
    assert n == 3
    fetched = await storage.async_trip_positions(id_c)
    assert len(fetched) == 3
    assert fetched[0]["lat"] == pytest.approx(40.123)
    assert fetched[-1]["lon"] == pytest.approx(-3.569)

    # ===== Monthly history =====
    mh = await storage.async_monthly_history(months=6)
    # tr_a is ~40 days ago (different month), tr_b/tr_c may straddle a
    # month boundary depending on when the test runs — just assert the
    # full window sums correctly across all returned months.
    assert len(mh) >= 1
    total_km = sum(m["distance_km"] for m in mh)
    assert total_km == pytest.approx(80.0)  # 20 + 50 + 10
    total_trips = sum(m["trips"] for m in mh)
    assert total_trips == 3

    # ===== Daily km window =====
    daily = await storage.async_daily_km_window(days=10)
    assert len(daily) == 11  # window + today inclusive
    km_total = sum(d["distance_km"] for d in daily)
    assert km_total >= 60.0

    # ===== Trip patterns =====
    patterns = await storage.async_trip_patterns(days=90)
    assert patterns["sample_count"] == 3
    assert sum(patterns["by_hour"].values()) == 3
    assert sum(patterns["by_weekday"].values()) == 3

    # ===== Avg trip metrics =====
    since = base_now - timedelta(days=10)
    avg = await storage.async_avg_trip_metrics(since=since)
    assert avg["count"] == 2  # tr_b + tr_c
    assert avg["avg_distance_km"] == pytest.approx(30.0)  # (50 + 10) / 2
    assert avg["driving_time_min"] == pytest.approx(80.0)  # 60 + 20

    # ===== Tops lists =====
    tops = await storage.async_tops_lists(limit=5)
    assert "longest" in tops and len(tops["longest"]) == 3
    assert tops["longest"][0]["distance_km"] == 50.0  # tr_b
    assert tops["top_efficiency"][0]["consumption_kwh_100km"] == 15.0  # tr_c is best
    assert tops["cheapest"][0]["cost"] == 0.5  # tr_c

    # ===== Avg charge metrics =====
    await storage.async_insert_charge(ChargeRecord(
        ended_at=base_now, kwh=18.0, price_per_kwh=0.07, total_cost=1.26,
    ))
    await storage.async_insert_charge(ChargeRecord(
        ended_at=base_now, kwh=22.0, price_per_kwh=0.07, total_cost=1.54,
    ))
    chg_avg = await storage.async_avg_charge_metrics(
        since=base_now - timedelta(days=30)
    )
    assert chg_avg["count"] == 2
    assert chg_avg["avg_kwh"] == pytest.approx(20.0)
    assert chg_avg["avg_cost"] == pytest.approx(1.40, abs=0.01)


async def test_v050_calendar_entity_emits_daily_events(hass: HomeAssistant) -> None:
    """Calendar produces one all-day event per day with trips/charges."""
    from custom_components.ev_trip_logger.calendar import EvActivityCalendar
    from custom_components.ev_trip_logger.storage import ChargeRecord, TripRecord

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    today = dt_util.now()
    # Two trips today + one charge yesterday.
    await coordinator.storage.async_insert(TripRecord(
        started_at=today.replace(hour=8, minute=0, second=0, microsecond=0),
        ended_at=today.replace(hour=8, minute=30, second=0, microsecond=0),
        duration_min=30.0, distance_km=12.0,
    ))
    await coordinator.storage.async_insert(TripRecord(
        started_at=today.replace(hour=18, minute=0, second=0, microsecond=0),
        ended_at=today.replace(hour=18, minute=30, second=0, microsecond=0),
        duration_min=30.0, distance_km=11.0,
    ))
    await coordinator.storage.async_insert_charge(ChargeRecord(
        started_at=today - timedelta(days=1),
        ended_at=today - timedelta(days=1) + timedelta(hours=2),
        kwh=10.0, price_per_kwh=0.07, total_cost=0.7,
    ))

    cal = EvActivityCalendar(coordinator)
    events = await cal.async_get_events(
        hass,
        today - timedelta(days=2),
        today + timedelta(days=1),
    )
    # Two days have activity → two events.
    assert len(events) == 2
    # Today event should mention 2 trips + 23 km.
    today_evt = [e for e in events if e.start == today.date()][0]
    assert "2 trips" in today_evt.summary
    assert "23" in today_evt.summary  # 12 + 11 km


async def test_live_open_retries_when_odometer_stale(hass: HomeAssistant) -> None:
    """v0.5.49 — vehicle_on=on arrives before odometer settles (BYD cloud
    poll lag). The opener must defer instead of bailing, and open the trip
    on the first retry once the odometer becomes readable.
    """
    # Battery is fresh, odometer is unknown — common pattern on BYD when
    # ignition fires between cloud-poll cycles.
    hass.states.async_set(ODO, STATE_UNKNOWN)
    hass.states.async_set(BAT, "80")
    hass.states.async_set(VOK, STATE_OFF)
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

    # Ignition fires. Odometer still stale → no trip yet, but a retry
    # is scheduled.
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    assert coordinator.current is None
    assert coordinator._pending_open_unsub is not None

    # Odometer settles after a few seconds (cloud poll lands).
    hass.states.async_set(ODO, "1000")
    await hass.async_block_till_done()
    # The metric-changed handler opens the trip immediately and cancels
    # the deferred retry.
    assert coordinator.current is not None
    assert coordinator.current.odometer_start == 1000.0
    assert coordinator._pending_open_unsub is None


async def test_live_open_retry_cancelled_on_off_edge(hass: HomeAssistant) -> None:
    """v0.5.49 — a brief on→off flap with odometer stale must NOT spawn a
    delayed trip. Without the off-edge cancel, the retry chain would fire
    minutes later and open a phantom trip while the car is parked.
    """
    hass.states.async_set(ODO, STATE_UNKNOWN)
    hass.states.async_set(BAT, "80")
    hass.states.async_set(VOK, STATE_OFF)
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

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    assert coordinator._pending_open_unsub is not None

    # User unlocked the car but didn't drive — vehicle_on flips back off.
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    assert coordinator._pending_open_unsub is None

    # Even if odometer settles later, no trip should open (no on-edge).
    hass.states.async_set(ODO, "1000")
    await _advance(hass, 5)
    assert coordinator.current is None


async def test_score_baseline_defaults_then_calibrates(hass: HomeAssistant) -> None:
    """v0.5.50 — score baseline stays at 14.5 until 10 eligible trips
    exist, then snaps to the P5 (clamped) and reshapes the score curve.
    """
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # No trips → default baseline.
    assert coordinator.score_baseline_kwh_100km == 14.5
    assert coordinator.score_baseline_trip_count == 0

    # Insert 9 eligible trips → still under the 10-trip threshold, fallback.
    base = dt_util.now()
    for i in range(9):
        await coordinator.storage.async_insert(TripRecord(
            started_at=base - timedelta(days=i + 1, hours=1),
            ended_at=base - timedelta(days=i + 1),
            duration_min=60.0,
            distance_km=20.0,
            energy_kwh=4.0,
            consumption_kwh_100km=10.0 + i,
        ))
    await coordinator._async_refresh_score_baseline()
    assert coordinator.score_baseline_kwh_100km == 14.5
    assert coordinator.score_baseline_trip_count == 9

    # One more → 10 eligible trips, P5 = best = 10.0. With the new
    # floor pinned at the 14.5 default, the calibration can only RAISE
    # the bar; a P5 of 10.0 below 14.5 keeps the anchor at 14.5.
    await coordinator.storage.async_insert(TripRecord(
        started_at=base - timedelta(days=20, hours=1),
        ended_at=base - timedelta(days=20),
        duration_min=60.0,
        distance_km=20.0,
        energy_kwh=4.0,
        consumption_kwh_100km=19.0,
    ))
    await coordinator._async_refresh_score_baseline()
    assert coordinator.score_baseline_kwh_100km == 14.5
    assert coordinator.score_baseline_trip_count == 10


async def test_score_baseline_never_drops_below_default(
    hass: HomeAssistant,
) -> None:
    """v0.5.50 — calibration may raise the bar but never lower it.

    User directive: 14.5 is the reference default. A fleet of
    weirdly-efficient trips (downhill / sensor errors / regen-heavy
    short trips) must NOT lower the anchor — otherwise every later
    normal trip would score worse than it deserves.
    """
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    base = dt_util.now()
    for i in range(15):
        await coordinator.storage.async_insert(TripRecord(
            started_at=base - timedelta(days=i + 1, hours=1),
            ended_at=base - timedelta(days=i + 1),
            duration_min=60.0,
            distance_km=20.0,
            energy_kwh=1.0,
            consumption_kwh_100km=5.5,  # impossibly efficient
        ))
    await coordinator._async_refresh_score_baseline()
    # P5 = 5.5 → clamped UP to the 14.5 default (never lower).
    assert coordinator.score_baseline_kwh_100km == 14.5


async def test_score_baseline_raises_anchor_for_thirsty_cars(
    hass: HomeAssistant,
) -> None:
    """v0.5.50 — for a car whose realistic best is worse than 14.5
    (Tesla in mountain terrain, large SUVs), the calibration MUST
    raise the 10/10 anchor to that car's actual best, otherwise the
    score stays unfairly low.
    """
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    base = dt_util.now()
    # 12 trips clustered around 17–22 kWh/100km — best = 17.0
    for i, cons in enumerate([17.0, 17.5, 17.5, 18.0, 18.5, 19.0, 19.5,
                              20.0, 20.5, 21.0, 21.5, 22.0]):
        await coordinator.storage.async_insert(TripRecord(
            started_at=base - timedelta(days=i + 1, hours=1),
            ended_at=base - timedelta(days=i + 1),
            duration_min=60.0,
            distance_km=20.0,
            energy_kwh=cons * 0.2,
            consumption_kwh_100km=cons,
        ))
    await coordinator._async_refresh_score_baseline()
    # P5 with n=12 → idx 0 → 17.0; above the 14.5 floor → adopted.
    assert coordinator.score_baseline_kwh_100km == pytest.approx(17.0)


async def test_battery_capacity_falls_back_to_declared_until_enough_charges(
    hass: HomeAssistant,
) -> None:
    """v0.5.51 — until 5 eligible charges exist, capacity = declared spec."""
    from custom_components.ev_trip_logger.storage import ChargeRecord

    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 80.0})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.battery_capacity == 80.0

    # Insert 4 eligible charges → still under the floor.
    for _ in range(4):
        await coordinator.storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=2),
            ended_at=dt_util.now() - timedelta(hours=1),
            kwh=42.0, price_per_kwh=0.2, total_cost=8.4,
            soc_start=10.0, soc_end=70.0,  # 60 % Δ → 70 kWh implied
        ))
    await coordinator._async_refresh_battery_capacity()
    assert coordinator.battery_capacity == 80.0  # still declared


async def test_battery_capacity_calibrates_from_real_charges(
    hass: HomeAssistant,
) -> None:
    """v0.5.51 — with ≥5 eligible charges, capacity = median(kwh/ΔSoC).

    Declared 100 kWh but real charges imply ~70 kWh → property returns 70.
    Score / energy / consumption all recompute against the calibrated
    value the next time they're read.
    """
    from custom_components.ev_trip_logger.storage import ChargeRecord

    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 100.0})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    for _ in range(6):
        await coordinator.storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=2),
            ended_at=dt_util.now() - timedelta(hours=1),
            kwh=42.0, price_per_kwh=0.2, total_cost=8.4,
            soc_start=10.0, soc_end=70.0,  # 60 % Δ → 70 kWh implied
        ))
    await coordinator._async_refresh_battery_capacity()
    assert coordinator.battery_capacity == pytest.approx(70.0)


async def test_battery_capacity_clamped_to_declared_bounds(
    hass: HomeAssistant,
) -> None:
    """v0.5.51 — a string of corrupted charges suggesting absurd capacity
    must not overwrite the declared value beyond ±50 %.
    """
    from custom_components.ev_trip_logger.storage import ChargeRecord

    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 80.0})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # 5 charges that imply 30 kWh — below the floor (40 = 80 * 0.5).
    for _ in range(5):
        await coordinator.storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=2),
            ended_at=dt_util.now() - timedelta(hours=1),
            kwh=18.0, price_per_kwh=0.2, total_cost=3.6,
            soc_start=10.0, soc_end=70.0,  # 60 % Δ → 30 kWh implied
        ))
    await coordinator._async_refresh_battery_capacity()
    # Clamped at the floor (50 % of declared 80 = 40 kWh), not the
    # raw 30 kWh that the corrupted charges would have implied.
    assert coordinator.battery_capacity == pytest.approx(40.0)


async def test_brief_off_during_trip_does_not_close(hass: HomeAssistant) -> None:
    """v0.5.53 — vehicle_on=off followed by on within the grace must
    NOT close the trip. Covers red-light / pickup-stop scenarios.
    """
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1015")
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()
    assert coordinator.current is not None

    # User stops at a red light — vehicle_on briefly drops off.
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    # Trip still open (grace hasn't expired).
    assert coordinator.current is not None
    assert coordinator._pending_close_unsub is not None

    # 30 s later the light turns green and vehicle_on goes back on.
    await _advance(hass, 0.5)
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    # Grace cancelled; trip still open.
    assert coordinator.current is not None
    assert coordinator._pending_close_unsub is None


async def test_expected_soh_lfp_low_km_high_soh(hass: HomeAssistant) -> None:
    """v0.5.57 — fresh LFP car (low km, mid-warm climate) → SoH 95+%."""
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 82.5})
    coord = hass.data[DOMAIN][entry.entry_id]
    # Seed 1 trip so first_odometer_seen has a value.
    await coord.storage.async_insert(TripRecord(
        started_at=dt_util.now() - timedelta(days=10),
        ended_at=dt_util.now() - timedelta(days=10) + timedelta(minutes=30),
        duration_min=30.0, distance_km=20.0,
        odometer_start=20000.0, odometer_end=20020.0,
        soc_used_pct=4.0, energy_kwh=3.3, consumption_kwh_100km=16.5,
    ))
    hass.states.async_set("sensor.odometer", "26500")
    await hass.async_block_till_done()
    result = await coord.async_compute_expected_soh()
    assert result["inputs"]["chemistry"] == "lfp"
    # v0.5.60 — km comes from the odometer reading directly (pack
    # lifetime), not "since install".
    assert result["inputs"]["km"] == pytest.approx(26500.0)
    # LFP year-1 knee (3.5) + cycle loss 26.5 × 0.04 ≈ 1.06 → ~95.4 %
    assert 93.0 <= result["expected_soh_pct"] <= 97.0


async def test_expected_soh_nmc_aged_with_dcfc(hass: HomeAssistant) -> None:
    """v0.5.57 — older NMC car with DCFC habit loses more than LFP."""
    from custom_components.ev_trip_logger.const import CONF_BATTERY_CHEMISTRY
    from custom_components.ev_trip_logger.storage import ChargeRecord, TripRecord

    entry = await _setup(hass, **{
        CONF_BATTERY_CAPACITY: 75.0, CONF_BATTERY_CHEMISTRY: "nmc",
    })
    coord = hass.data[DOMAIN][entry.entry_id]
    # First trip = baseline odo at 0.
    await coord.storage.async_insert(TripRecord(
        started_at=dt_util.now() - timedelta(days=1000),
        ended_at=dt_util.now() - timedelta(days=1000) + timedelta(minutes=30),
        duration_min=30.0, distance_km=20.0,
        odometer_start=0.0, odometer_end=20.0,
        soc_used_pct=4.0, energy_kwh=3.3,
    ))
    hass.states.async_set("sensor.odometer", "100000")
    # 20% DCFC ratio
    for is_dcfc, kwh in [(False, 100.0), (False, 100.0), (False, 100.0), (True, 75.0)]:
        await coord.storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(days=10),
            ended_at=dt_util.now() - timedelta(days=10) + timedelta(minutes=30),
            kwh=kwh, price_per_kwh=0.2, total_cost=kwh * 0.2,
            is_dcfc=is_dcfc,
        ))
    await hass.async_block_till_done()
    result = await coord.async_compute_expected_soh()
    assert result["inputs"]["chemistry"] == "nmc"
    assert result["inputs"]["km"] == pytest.approx(100000.0)
    # Expect substantial loss: ~4 (knee) + ~9 (calendar) + 10 (cycle) +
    # small DCFC penalty → < 80
    assert result["expected_soh_pct"] < 85.0
    assert result["factors"]["cycle"] == pytest.approx(10.0, abs=0.1)


async def test_expected_soh_floor_caps_at_70(hass: HomeAssistant) -> None:
    """v0.5.57 — model never reports below 70% (warranty floor)."""
    from custom_components.ev_trip_logger.const import CONF_BATTERY_CHEMISTRY
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass, **{
        CONF_BATTERY_CAPACITY: 75.0, CONF_BATTERY_CHEMISTRY: "nca",
    })
    coord = hass.data[DOMAIN][entry.entry_id]
    await coord.storage.async_insert(TripRecord(
        started_at=dt_util.now() - timedelta(days=4000),
        ended_at=dt_util.now() - timedelta(days=4000) + timedelta(minutes=30),
        duration_min=30.0, distance_km=20.0,
        odometer_start=0.0, odometer_end=20.0,
        soc_used_pct=4.0, energy_kwh=3.3,
    ))
    hass.states.async_set("sensor.odometer", "500000")
    await hass.async_block_till_done()
    result = await coord.async_compute_expected_soh()
    assert result["expected_soh_pct"] == 70.0  # clamped to floor


async def test_expected_soh_handles_date_only_first_registered(hass: HomeAssistant) -> None:
    """v0.5.59 — DateSelector returns 'YYYY-MM-DD' which we parse to
    a naive datetime. Subtracting against dt_util.now() (tz-aware) used
    to crash with TypeError. Test the fix is in place.
    """
    from custom_components.ev_trip_logger.const import (
        CONF_VEHICLE_FIRST_REGISTERED,
    )
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass, **{CONF_VEHICLE_FIRST_REGISTERED: "2024-12-15"})
    coord = hass.data[DOMAIN][entry.entry_id]
    await coord.storage.async_insert(TripRecord(
        started_at=dt_util.now() - timedelta(days=10),
        ended_at=dt_util.now() - timedelta(days=10) + timedelta(minutes=30),
        duration_min=30.0, distance_km=20.0,
        odometer_start=20000.0, odometer_end=20020.0,
        soc_used_pct=4.0, energy_kwh=3.3,
    ))
    hass.states.async_set("sensor.odometer", "26500")
    await hass.async_block_till_done()
    # Should not raise.
    result = await coord.async_compute_expected_soh()
    assert result["expected_soh_pct"] is not None
    assert result["inputs"]["age_years"] > 0  # parsed correctly
    # v0.5.60 — km is the odometer directly, not km-since-install.
    assert result["inputs"]["km"] == pytest.approx(26500.0)
    assert result["confidence"] == "medium"  # has age, no climate
