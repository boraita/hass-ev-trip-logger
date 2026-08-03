"""Tests for the trip detection state machine and storage integration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    CONF_CABIN_TEMP_SENSOR,
    CONF_CHARGE_SENSOR,
    CONF_HVAC_SETPOINT_SENSOR,
    CONF_IDLE_TIMEOUT,
    CONF_LOCATION,
    CONF_MIN_TRIP_DISTANCE,
    CONF_NAME,
    CONF_ODOMETER,
    CONF_POWER,
    CONF_POWER_SIGN_INVERTED,
    CONF_SPEED,
    CONF_TIRE_PRESSURE_FL_SENSOR,
    CONF_TIRE_PRESSURE_FR_SENSOR,
    CONF_TIRE_PRESSURE_RL_SENSOR,
    CONF_TIRE_PRESSURE_RR_SENSOR,
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


async def test_stuck_trip_watchdog_closes_after_no_movement_and_off(
    hass: HomeAssistant,
) -> None:
    """v0.5.79 — when vehicle_on=off but the off-edge close was lost,
    the periodic watchdog force-closes the trip after the no-movement
    timeout and tags confidence.

    Reproduces the real bug: BYD's cloud poll dropped offline mid-drive,
    the off transition never reached the listener, and self.current
    stayed pinned at 'distance=11 km' for 30+ min even after the car
    had been off for 5+ min.
    """
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1015")
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()
    assert coordinator.current is not None

    # Backdate last_movement_ts to simulate 70 min of no movement.
    coordinator.current.started_at = dt_util.now() - timedelta(minutes=80)
    coordinator.current.last_movement_ts = dt_util.now() - timedelta(minutes=70)

    # Simulate a lost off-edge: the upstream finally publishes off, but
    # we cancel the debounced close that the listener queued so the
    # watchdog is the only thing left that could rescue the trip.
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    if coordinator._pending_close_unsub is not None:
        coordinator._pending_close_unsub()
        coordinator._pending_close_unsub = None

    # Fire the watchdog directly — exactly what async_track_time_interval
    # would do every 5 min.
    coordinator._async_check_stuck_trip(dt_util.now())
    await hass.async_block_till_done()

    assert coordinator.current is None
    assert coordinator.last_trip is not None
    assert coordinator.last_trip.confidence == "force_closed_no_movement"


async def test_stuck_trip_watchdog_closes_after_max_age_regardless(
    hass: HomeAssistant,
) -> None:
    """v0.5.79 — beyond 4 h the watchdog force-closes even when
    vehicle_on is still 'on'. The upstream's state machine is wedged
    and can't be trusted; close at the last real movement timestamp.
    """
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1020")
    hass.states.async_set(BAT, "60")
    await hass.async_block_till_done()
    assert coordinator.current is not None

    # Backdate to 5 h ago; vehicle_on still reads ON.
    now = dt_util.now()
    coordinator.current.started_at = now - timedelta(hours=5)
    coordinator.current.last_movement_ts = now - timedelta(hours=4, minutes=30)
    assert hass.states.get(VOK).state == STATE_ON

    coordinator._async_check_stuck_trip(now)
    await hass.async_block_till_done()

    assert coordinator.current is None
    assert coordinator.last_trip is not None
    assert coordinator.last_trip.confidence == "force_closed_max_age"


async def test_stuck_trip_watchdog_leaves_healthy_trip_alone(
    hass: HomeAssistant,
) -> None:
    """A trip with recent movement and vehicle_on=on stays open."""
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1005")
    await hass.async_block_till_done()
    assert coordinator.current is not None

    coordinator._async_check_stuck_trip(dt_util.now())
    await hass.async_block_till_done()

    assert coordinator.current is not None
    assert coordinator.last_trip is None


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

    # v0.5.62 — speed and consumption also show 0 in idle (cleaner UX:
    # "not driving → no consumption"). Only avg_temp_c stays None because
    # there's no physically defensible default for "no measurement".
    zero_keys = [
        "distance_km", "duration_min", "soc_used_pct", "energy_kwh",
        "max_power_kw", "avg_speed_kmh", "consumption_kwh_100km",
    ]
    for key in zero_keys:
        eid = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_current_{key}")
        assert eid is not None, f"missing {key}"
        state = hass.states.get(eid)
        assert state is not None and state.state not in ("unavailable", "unknown"), (
            f"{eid} should be available with 0, got {state.state if state else None}"
        )
        assert float(state.state) == 0.0

    # v0.5.74 — `current_trip_avg_temperature` in idle returns the
    # live exterior-temp reading from CONF_TEMP (or `unknown` if no
    # temp sensor is configured AND auto-detect found nothing).
    # _setup wires no temp sensor by default, so we still see
    # `unknown` here — the live-reading path only triggers when
    # CONF_TEMP / auto-detect resolves to a real entity.
    eid = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_current_avg_temp_c",
    )
    state = hass.states.get(eid)
    assert state.state == "unknown", (
        f"{eid} expected unknown (no temp sensor configured), "
        f"got {state.state}"
    )


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


async def test_secondary_home_zone_closes_and_opens_journey(
    hass: HomeAssistant,
) -> None:
    """v0.8.10 — a configured secondary home (second house, holiday
    home, …) closes a journey on arrival and opens one on departure,
    exactly like the primary home_zone.
    """
    from custom_components.ev_trip_logger.const import CONF_SECONDARY_HOME_ZONES

    hass.states.async_set(LOC, "casa_playa")
    entry = await _setup(hass, **{
        CONF_LOCATION: LOC,
        CONF_SECONDARY_HOME_ZONES: ["zone.casa_playa"],
    })
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Departing the secondary home opens a journey.
    await _run_stage(hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="not_home")
    j_id = coordinator.current_journey_id
    assert j_id is not None

    # Arriving back at the secondary home closes it.
    hass.states.async_set(LOC, "not_home")
    await _run_stage(hass, odo_start=1020, odo_end=1040, soc_end=65, location_end="casa_playa")
    assert coordinator.last_completed_journey_id == j_id
    assert coordinator.current_journey_id is None


async def test_secondary_home_zone_resolves_friendly_name(
    hass: HomeAssistant,
) -> None:
    """A secondary home zone can show up as either its slug (the normal
    device_tracker convention) or its friendly_name (what
    `_zone_from_coords`'s non-home branch returns) — both must match.
    """
    from custom_components.ev_trip_logger.const import CONF_SECONDARY_HOME_ZONES

    hass.states.async_set(
        "zone.casa_playa", "0",
        {"friendly_name": "Casa de la Playa", "latitude": 36.5, "longitude": -4.5},
    )
    entry = await _setup(hass, **{
        CONF_SECONDARY_HOME_ZONES: ["zone.casa_playa"],
    })
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert coordinator._is_at_any_home("casa_playa")  # slug
    assert coordinator._is_at_any_home("Casa de la Playa")  # friendly_name
    assert coordinator._is_at_any_home("CASA_PLAYA")  # case-insensitive
    assert not coordinator._is_at_any_home("work")
    assert not coordinator._is_at_any_home(None)


async def test_secondary_home_coords_parsed_correctly() -> None:
    """CONF_SECONDARY_HOME_COORDS free text: one 'lat,lon[,radius][,label]'
    per line; radius defaults, label defaults to a stable auto-generated
    name; blank lines / comments / malformed lines are skipped.
    """
    from custom_components.ev_trip_logger.coordinator import (
        _parse_secondary_home_coords,
    )
    from custom_components.ev_trip_logger.const import (
        DEFAULT_SECONDARY_HOME_RADIUS_M,
    )

    raw = """
    # holiday home, default radius + auto label
    36.5,-4.5

    40.0,-3.0,250,Casa de la playa
    not,a,coord
    """
    parsed = _parse_secondary_home_coords(raw)
    assert len(parsed) == 2
    assert parsed[0] == (36.5, -4.5, DEFAULT_SECONDARY_HOME_RADIUS_M, "secondary_home_1")
    assert parsed[1] == (40.0, -3.0, 250.0, "Casa de la playa")


async def test_secondary_home_coords_parsed_empty_for_blank_input() -> None:
    from custom_components.ev_trip_logger.coordinator import (
        _parse_secondary_home_coords,
    )

    assert _parse_secondary_home_coords(None) == []
    assert _parse_secondary_home_coords("") == []
    assert _parse_secondary_home_coords("   \n  # just a comment\n") == []


async def test_secondary_home_coord_label_matches_within_radius(
    hass: HomeAssistant,
) -> None:
    """v0.8.10 — free-typed coordinates (no registered HA zone) resolve
    to their label within radius, and None outside it or with nothing
    configured. This is the path used when the tracker names no zone
    and HA's own zone-matching (`_zone_from_coords`) finds nothing.
    """
    from custom_components.ev_trip_logger.const import CONF_SECONDARY_HOME_COORDS

    entry = await _setup(hass, **{
        CONF_SECONDARY_HOME_COORDS: "36.5,-4.5,200,Casa de la playa",
    })
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Well within the 200 m radius (~50 m away).
    assert coordinator._secondary_home_coord_label(36.5004, -4.5) == "Casa de la playa"
    # Far outside (~11 km away).
    assert coordinator._secondary_home_coord_label(36.6, -4.5) is None
    # Missing coordinates.
    assert coordinator._secondary_home_coord_label(None, None) is None
    # The label is also recognised by _is_at_any_home for journey checks.
    assert coordinator._is_at_any_home("Casa de la playa")
    assert coordinator._is_at_any_home("casa de la playa")  # case-insensitive


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
    # v0.5.76, WAC pool since v0.8.8 — 10 kWh @ 0.30 blended into the
    # pool ahead of the trip → 10×0.30 + 1.25×0.15 (home fallback for
    # the overflow) = 3.19 €. Each kWh is now priced at the rate it
    # was charged at.
    assert t["cost"] == pytest.approx(3.19, abs=0.01)
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


async def test_abrp_push_respects_power_sign_inverted(hass: HomeAssistant) -> None:
    """v0.8.1 — the ABRP push path must honour CONF_POWER_SIGN_INVERTED,
    not just _async_power_changed's local normalisation.

    Before the fix, `_async_maybe_send_abrp` re-read the raw power
    sensor and pre-negated it unconditionally, which cancelled
    `build_tlm`'s own negation and sent the raw sensor sign straight
    through to ABRP — ignoring the inversion flag entirely. For a
    source configured as sign-inverted (discharge reported negative,
    e.g. the user's BYD cloud sensor), that meant driving (discharge)
    reached ABRP as negative and charging reached it as positive —
    exactly backwards from ABRP's +discharge/-charge convention.
    """
    from types import SimpleNamespace

    entry = await _setup(hass, **{
        CONF_POWER: POW,
        CONF_POWER_SIGN_INVERTED: True,
    })
    coordinator = hass.data[DOMAIN][entry.entry_id]

    sent: list[dict] = []

    async def _fake_send(tlm):
        sent.append(tlm)
        return True

    # Stub in a fake client directly instead of going through real ABRP
    # config (CONF_ABRP_TOKEN/API_KEY) — that would build a real
    # AbrpClient on HA's shared aiohttp session, which is unnecessary
    # network-adjacent setup for what's purely a sign-arithmetic test.
    coordinator._abrp = SimpleNamespace(send=_fake_send)
    coordinator.abrp_push_enabled = True

    # Sign-inverted source: -20 kW raw while DRIVING (discharging).
    hass.states.async_set(POW, "-20")
    await hass.async_block_till_done()
    coordinator._abrp_last_send = 0.0
    await coordinator._async_maybe_send_abrp()
    assert len(sent) == 1
    # ABRP convention: discharge must arrive positive.
    assert sent[0]["power"] == pytest.approx(20.0)

    # Sign-inverted source: +8 kW raw while CHARGING.
    hass.states.async_set(POW, "8")
    await hass.async_block_till_done()
    coordinator._abrp_last_send = 0.0
    await coordinator._async_maybe_send_abrp()
    assert len(sent) == 2
    # ABRP convention: charge must arrive negative.
    assert sent[1]["power"] == pytest.approx(-8.0)


async def test_abrp_push_sends_calibrated_soh_not_modelled_estimate(
    hass: HomeAssistant,
) -> None:
    """v0.8.5 — ABRP's soh field must be the REAL calibrated SoH
    (calibrated / baseline capacity), not the age/mileage/climate
    *model* computed for the "expected vs actual" diagnostic sensor.
    Sending the generic model to ABRP as this car's actual health would
    misinform its range predictions with a number nobody measured.
    """
    from types import SimpleNamespace

    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 80.0})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    sent: list[dict] = []

    async def _fake_send(tlm):
        sent.append(tlm)
        return True

    coordinator._abrp = SimpleNamespace(send=_fake_send)
    coordinator.abrp_push_enabled = True

    # 76 kWh observed vs 80 kWh declared baseline -> 95 % real SoH.
    coordinator._battery_capacity_calibrated = 76.0

    coordinator._abrp_last_send = 0.0
    await coordinator._async_maybe_send_abrp()
    assert len(sent) == 1
    assert sent[0]["soh"] == pytest.approx(95.0)


async def test_abrp_push_sends_cabin_hvac_and_tire_pressures(
    hass: HomeAssistant,
) -> None:
    """v0.8.7 — cabin temp / HVAC setpoint pass through as-is (°C); tire
    pressures are converted from the source sensor's own unit to kPa
    (ABRP's unit) rather than assuming a fixed unit.
    """
    from types import SimpleNamespace

    CABIN = "sensor.cabin_temp"
    HVAC = "sensor.hvac_setpoint"
    TFL = "sensor.tire_fl"
    TFR = "sensor.tire_fr"
    TRL = "sensor.tire_rl"
    TRR = "sensor.tire_rr"

    hass.states.async_set(CABIN, "22.5", {"unit_of_measurement": "°C"})
    hass.states.async_set(HVAC, "21.0", {"unit_of_measurement": "°C"})
    hass.states.async_set(TFL, "2.2", {"unit_of_measurement": "bar"})
    hass.states.async_set(TFR, "31.9", {"unit_of_measurement": "psi"})
    hass.states.async_set(TRL, "220.0", {"unit_of_measurement": "kPa"})
    hass.states.async_set(TRR, "2200", {"unit_of_measurement": "hPa"})

    entry = await _setup(hass, **{
        CONF_CABIN_TEMP_SENSOR: CABIN,
        CONF_HVAC_SETPOINT_SENSOR: HVAC,
        CONF_TIRE_PRESSURE_FL_SENSOR: TFL,
        CONF_TIRE_PRESSURE_FR_SENSOR: TFR,
        CONF_TIRE_PRESSURE_RL_SENSOR: TRL,
        CONF_TIRE_PRESSURE_RR_SENSOR: TRR,
    })
    coordinator = hass.data[DOMAIN][entry.entry_id]

    sent: list[dict] = []

    async def _fake_send(tlm):
        sent.append(tlm)
        return True

    coordinator._abrp = SimpleNamespace(send=_fake_send)
    coordinator.abrp_push_enabled = True
    coordinator._abrp_last_send = 0.0
    await coordinator._async_maybe_send_abrp()

    assert len(sent) == 1
    tlm = sent[0]
    assert tlm["cabin_temp"] == pytest.approx(22.5)
    assert tlm["hvac_setpoint"] == pytest.approx(21.0)
    assert tlm["tire_pressure_fl"] == pytest.approx(220.0)  # 2.2 bar
    assert tlm["tire_pressure_fr"] == pytest.approx(219.9, abs=0.5)  # 31.9 psi
    assert tlm["tire_pressure_rl"] == pytest.approx(220.0)  # already kPa
    assert tlm["tire_pressure_rr"] == pytest.approx(220.0)  # 2200 hPa


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


async def test_fix_speed_stats_service_clears_impossible_avg_speed(
    hass: HomeAssistant,
) -> None:
    """v0.8.3 — fix_speed_stats backfills the avg > max sanity check onto
    trips persisted before the close-time guard existed. Only rows where
    avg_speed_kmh > max_speed_kmh (with the 5% margin) get cleared;
    everything else (including trips with no max_speed_kmh at all) is
    left untouched.
    """
    from custom_components.ev_trip_logger.const import SERVICE_FIX_SPEED_STATS
    from custom_components.ev_trip_logger.storage import TripRecord, TripStorage

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    storage: TripStorage = coordinator.storage
    base = dt_util.now() - timedelta(days=1)

    # Corrupted: avg (112.7) way above max (47) — the reported production case.
    bad_id = await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(minutes=4, seconds=18),
        duration_min=4.3, distance_km=8.0,
        avg_speed_kmh=112.7, max_speed_kmh=47.0,
    ))
    # Healthy: max well above avg, as any normal trip with stops looks.
    good_id = await storage.async_insert(TripRecord(
        started_at=base + timedelta(hours=1),
        ended_at=base + timedelta(hours=1, minutes=48),
        duration_min=48.0, distance_km=20.0,
        avg_speed_kmh=25.0, max_speed_kmh=133.0,
    ))
    # No max_speed_kmh recorded at all — nothing to compare against, must
    # not be touched even though its avg_speed_kmh looks high.
    no_max_id = await storage.async_insert(TripRecord(
        started_at=base + timedelta(hours=2),
        ended_at=base + timedelta(hours=2, minutes=5),
        duration_min=5.0, distance_km=10.0,
        avg_speed_kmh=120.0, max_speed_kmh=None,
    ))

    await hass.services.async_call(
        DOMAIN, SERVICE_FIX_SPEED_STATS, {}, blocking=True,
    )

    bad = await storage.async_get_trip_by_id(bad_id)
    good = await storage.async_get_trip_by_id(good_id)
    no_max = await storage.async_get_trip_by_id(no_max_id)
    assert bad.avg_speed_kmh is None
    assert good.avg_speed_kmh == pytest.approx(25.0)
    assert no_max.avg_speed_kmh == pytest.approx(120.0)


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


async def test_avg_speed_dropped_when_it_exceeds_max_speed(
    hass: HomeAssistant,
) -> None:
    """v0.8.3 — avg_speed_kmh > max_speed_kmh is physically impossible:
    the max is a running ceiling sampled over the exact same window the
    average covers. Seen in production from the stale-odometer-anchor
    bug (distance inflated with km driven before `started_at`, duration
    only covering the real short window) — e.g. a real trip logged
    112.7 km/h average against a genuine 47 km/h max. max_speed_kmh is
    tracked independently from live speed samples and stays trustworthy
    even when distance/duration are corrupted, so close-time must drop
    avg_speed rather than persist the impossible value.
    """
    entry = await _setup(hass, **{CONF_SPEED: SPD})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    assert coordinator.current is not None
    assert coordinator.current.odometer_start == 1000.0
    trip_started_at = coordinator.current.started_at

    # Genuine max speed seen live during the trip: a modest 40 km/h.
    hass.states.async_set(SPD, "40")
    await hass.async_block_till_done()
    assert coordinator.current.max_speed_kmh == pytest.approx(40.0)

    # Odometer jumps 10 km. Closing only 7.5 min after open makes the
    # naive distance/duration average 80 km/h — above the real 40 km/h
    # max, but still well under the blunt >300 km/h sanity cap, so only
    # the new avg-vs-max guard can catch it.
    hass.states.async_set(ODO, "1010")
    await hass.async_block_till_done()
    await coordinator._async_close_trip(trip_started_at + timedelta(minutes=7.5))

    last = await coordinator.storage.async_get_last()
    assert last is not None
    assert last.distance_km == pytest.approx(10.0)
    assert last.max_speed_kmh == pytest.approx(40.0)
    assert last.avg_speed_kmh is None


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


async def test_live_open_distrusts_stale_odometer(hass: HomeAssistant) -> None:
    """v0.8.3 — a valid-but-stale odometer reading must not anchor a new
    trip's odometer_start.

    Reproduces a real case: a short vehicle_on blip opens and closes
    before the cloud delivers a fresh odometer sample, so it gets
    discarded as noise (distance ~0, below min_trip_distance). The odo
    entity is left holding a value that is now old. When the NEXT
    vehicle_on=on edge fires, `hass.states.get(odometer)` still returns
    that old-but-valid value — indistinguishable from a fresh one by
    value alone. Before the fix, the live opener accepted it immediately,
    so the real trip's distance silently absorbed whatever km built up
    since that stale reading, while duration only covered the new
    edge's own short window — producing impossible average speeds.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        hass.states.async_set(ODO, "1000")
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

        # Odometer sample ages past the freshness window with no update —
        # simulates the cloud going quiet after a discarded short blip.
        frozen.tick(timedelta(seconds=120))  # > _ODOMETER_STALE_MAX_AGE_S (90s)

        # Ignition fires for what should be a real trip. The odometer
        # value is still "1000" (valid, not unknown/unavailable) but stale.
        hass.states.async_set(VOK, STATE_ON)
        await hass.async_block_till_done()
        assert coordinator.current is None
        assert coordinator._pending_open_unsub is not None

        # Cloud catches up with a fresh sample — only now must the trip
        # open, anchored on the fresh value rather than the stale "1000".
        hass.states.async_set(ODO, "1007")
        await hass.async_block_till_done()
        assert coordinator.current is not None
        assert coordinator.current.odometer_start == 1007.0
        assert coordinator._pending_open_unsub is None


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
    """v0.5.66 — fresh LFP car (low km, mid-warm climate) → SoH 95+%.

    `km` for the SoH model comes from SUM(distance_km) across logged
    trips, NOT the live odometer reading.
    """
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 82.5})
    coord = hass.data[DOMAIN][entry.entry_id]
    # Trip total = 26 500 km
    await coord.storage.async_insert(TripRecord(
        started_at=dt_util.now() - timedelta(days=10),
        ended_at=dt_util.now() - timedelta(days=10) + timedelta(minutes=30),
        duration_min=30.0, distance_km=26500.0,
        odometer_start=20000.0, odometer_end=46500.0,
        soc_used_pct=4.0, energy_kwh=3.3, consumption_kwh_100km=16.5,
    ))
    await hass.async_block_till_done()
    result = await coord.async_compute_expected_soh()
    assert result["inputs"]["chemistry"] == "lfp"
    assert result["inputs"]["km"] == pytest.approx(26500.0)
    # LFP year-1 knee (3.5) + cycle loss 26.5 × 0.04 ≈ 1.06 → ~95.4 %
    assert 93.0 <= result["expected_soh_pct"] <= 97.0


async def test_expected_soh_nmc_aged_with_dcfc(hass: HomeAssistant) -> None:
    """v0.5.66 — older NMC car with DCFC habit loses more than LFP."""
    from custom_components.ev_trip_logger.const import CONF_BATTERY_CHEMISTRY
    from custom_components.ev_trip_logger.storage import ChargeRecord, TripRecord

    entry = await _setup(hass, **{
        CONF_BATTERY_CAPACITY: 75.0, CONF_BATTERY_CHEMISTRY: "nmc",
    })
    coord = hass.data[DOMAIN][entry.entry_id]
    # Single big trip carrying the 100 000 km we want to assert against.
    await coord.storage.async_insert(TripRecord(
        started_at=dt_util.now() - timedelta(days=1000),
        ended_at=dt_util.now() - timedelta(days=1000) + timedelta(minutes=30),
        duration_min=30.0, distance_km=100000.0,
        odometer_start=0.0, odometer_end=100000.0,
        soc_used_pct=4.0, energy_kwh=3.3,
    ))
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
    """v0.5.66 — model never reports below 70% (warranty floor)."""
    from custom_components.ev_trip_logger.const import CONF_BATTERY_CHEMISTRY
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass, **{
        CONF_BATTERY_CAPACITY: 75.0, CONF_BATTERY_CHEMISTRY: "nca",
    })
    coord = hass.data[DOMAIN][entry.entry_id]
    await coord.storage.async_insert(TripRecord(
        started_at=dt_util.now() - timedelta(days=4000),
        ended_at=dt_util.now() - timedelta(days=4000) + timedelta(minutes=30),
        duration_min=30.0, distance_km=500000.0,
        odometer_start=0.0, odometer_end=500000.0,
        soc_used_pct=4.0, energy_kwh=3.3,
    ))
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
        duration_min=30.0, distance_km=26500.0,
        odometer_start=20000.0, odometer_end=46500.0,
        soc_used_pct=4.0, energy_kwh=3.3,
    ))
    await hass.async_block_till_done()
    # Should not raise.
    result = await coord.async_compute_expected_soh()
    assert result["expected_soh_pct"] is not None
    assert result["inputs"]["age_years"] > 0  # parsed correctly
    # v0.5.66 — km is the SUM(distance_km) of logged trips.
    assert result["inputs"]["km"] == pytest.approx(26500.0)
    assert result["confidence"] == "medium"  # has age, no climate


async def test_charge_sensor_accepts_tesla_state_enum(hass: HomeAssistant) -> None:
    """v0.5.61 — Tesla's `sensor.<v>_charging_state` reports a string
    enum (Charging / Disconnected / Complete / ...). The integration
    must treat 'Charging' the same as binary_sensor 'on'.
    """
    from custom_components.ev_trip_logger.const import CONF_CHARGE_SENSOR

    CHG = "sensor.tesla_charging_state"
    hass.states.async_set(CHG, "Disconnected")
    entry = await _setup(hass, **{CONF_CHARGE_SENSOR: CHG})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.current_charge is None

    hass.states.async_set(CHG, "Charging")
    await hass.async_block_till_done()
    assert coordinator.current_charge is not None, (
        "Tesla 'Charging' state should open a charge session"
    )

    hass.states.async_set(CHG, "Complete")
    await hass.async_block_till_done()
    assert coordinator.current_charge is None, (
        "'Complete' should close the charge session"
    )


async def test_charge_sensor_legacy_binary_still_works(hass: HomeAssistant) -> None:
    """v0.5.61 — the new multi-vocab matcher must NOT regress the
    classic binary_sensor 'on' / 'off' path.
    """
    from custom_components.ev_trip_logger.const import CONF_CHARGE_SENSOR

    CHG = "binary_sensor.byd_charging"
    hass.states.async_set(CHG, STATE_OFF)
    entry = await _setup(hass, **{CONF_CHARGE_SENSOR: CHG})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    assert coordinator.current_charge is not None

    hass.states.async_set(CHG, STATE_OFF)
    await hass.async_block_till_done()
    assert coordinator.current_charge is None


def test_is_charging_value_vocabulary() -> None:
    """v0.5.61 — explicit list of strings that count as 'charging'."""
    from custom_components.ev_trip_logger.coordinator import EvTripLoggerCoordinator

    f = EvTripLoggerCoordinator._is_charging_value
    # truthy values
    for s in ("on", "On", "ON",
              "Charging", "charging",
              "Starting", "starting",
              "true", "True", "1",
              "ac_charging", "DC_charging", "fast_charging"):
        assert f(s) is True, f"{s!r} should be charging"
    # falsy
    for s in ("off", "Off", "Disconnected", "Complete", "Stopped",
              "NoPower", "idle", "done", "false", "0", ""):
        assert f(s) is False, f"{s!r} should NOT be charging"
    assert f(None) is None


async def test_auto_detect_exterior_temp_sensor(hass: HomeAssistant) -> None:
    """v0.5.69 — when CONF_TEMP is unset, the integration looks for
    `sensor.<prefix>_exterior_temperature` (where <prefix> is derived
    from the configured odometer entity_id) and uses it automatically.
    """
    # Pre-register a temp sensor with the expected naming.
    EXT_TEMP = "sensor.odometer_exterior_temperature"  # prefix from "sensor.odometer"
    hass.states.async_set(EXT_TEMP, "22.5")
    # NOTE: _setup wires ODO = "sensor.odometer"; prefix derivation
    # strips the trailing "_odometer" → empty. To make this test
    # representative we use a non-trivial odometer name.
    OUR_ODO = "sensor.byd_car_odometer"
    hass.states.async_set(OUR_ODO, "1000")
    hass.states.async_set("sensor.byd_car_exterior_temperature", "22.5")

    data = {
        "name": "BYD",
        "odometer_sensor": OUR_ODO,
        "battery_sensor": BAT,
        "vehicle_on_sensor": VOK,
        "battery_capacity_kwh": 75.0,
        "min_trip_distance_km": 0.5,
        "idle_timeout_minutes": 1,
        # CONF_TEMP intentionally NOT set
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, title="BYD")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator._temp == "sensor.byd_car_exterior_temperature"


async def test_auto_detect_temp_does_not_override_explicit_conf(
    hass: HomeAssistant,
) -> None:
    """v0.5.69 — explicit CONF_TEMP wins over auto-detect."""
    hass.states.async_set("sensor.byd_car_exterior_temperature", "30.0")
    hass.states.async_set("sensor.my_custom_temp", "21.0")
    hass.states.async_set("sensor.byd_car_odometer", "1000")

    data = {
        "name": "BYD",
        "odometer_sensor": "sensor.byd_car_odometer",
        "battery_sensor": BAT,
        "vehicle_on_sensor": VOK,
        "battery_capacity_kwh": 75.0,
        "min_trip_distance_km": 0.5,
        "idle_timeout_minutes": 1,
        "exterior_temp_sensor": "sensor.my_custom_temp",  # explicit
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, title="BYD")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator._temp == "sensor.my_custom_temp"


async def test_auto_detect_last_trip_energy_sensor(hass: HomeAssistant) -> None:
    """v0.5.77 — prefix walk picks up `<prefix>_last_trip_energy` like
    BYD exposes. Generic: same suffix works for any integration.
    """
    OUR_ODO = "sensor.byd_car_odometer"
    hass.states.async_set(OUR_ODO, "1000")
    hass.states.async_set("sensor.byd_car_last_trip_energy", "1.65")
    hass.states.async_set("sensor.byd_car_last_trip_distance", "10.0")
    data = {
        "name": "BYD",
        "odometer_sensor": OUR_ODO,
        "battery_sensor": BAT,
        "vehicle_on_sensor": VOK,
        "battery_capacity_kwh": 75.0,
        "min_trip_distance_km": 0.5,
        "idle_timeout_minutes": 1,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, title="BYD")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    assert c._last_trip_energy_sensor == "sensor.byd_car_last_trip_energy"
    assert c._last_trip_distance_sensor == "sensor.byd_car_last_trip_distance"


async def test_vehicle_heal_overrides_inflated_energy(hass: HomeAssistant) -> None:
    """v0.5.77 — when the vehicle's last_trip_energy disagrees with the
    logger's estimate, the heal overrides energy_kwh + consumption +
    energy_source. This is the trip 163 fix path.
    """
    from custom_components.ev_trip_logger.const import (
        CONF_LAST_TRIP_ENERGY_SENSOR,
        CONF_LAST_TRIP_DISTANCE_SENSOR,
    )
    from custom_components.ev_trip_logger.storage import TripRecord

    VEH_E = "sensor.veh_last_trip_energy"
    VEH_D = "sensor.veh_last_trip_distance"
    hass.states.async_set(VEH_E, "1.65")
    hass.states.async_set(VEH_D, "10.0")
    entry = await _setup(hass, **{
        CONF_LAST_TRIP_ENERGY_SENSOR: VEH_E,
        CONF_LAST_TRIP_DISTANCE_SENSOR: VEH_D,
    })
    c = hass.data[DOMAIN][entry.entry_id]
    # Insert a trip with the inflated 2.60 power_integration value.
    now = dt_util.now()
    inflated = TripRecord(
        started_at=now - timedelta(minutes=30),
        ended_at=now - timedelta(minutes=5),
        duration_min=25.0,
        distance_km=10.0,
        energy_kwh=2.60,
        consumption_kwh_100km=26.0,
        energy_source="power_integration",
        cost=0.18,
        currency="EUR",
    )
    trip_id = await c.storage.async_insert(inflated)
    # Re-set vehicle sensors AFTER trip ended so last_changed is fresh.
    hass.states.async_set(VEH_E, "1.65")
    hass.states.async_set(VEH_D, "10.0")
    await c._async_heal_from_vehicle(trip_id)
    healed = await c.storage.async_get_trip_by_id(trip_id)
    assert healed.energy_kwh == pytest.approx(1.65)
    assert healed.consumption_kwh_100km == pytest.approx(16.5)
    assert healed.energy_source == "vehicle"


async def test_vehicle_heal_skipped_when_sensor_stale(hass: HomeAssistant) -> None:
    """v0.5.77 — the heal only fires when the sensor's last_changed is
    later than the trip's ended_at. If the sensor still carries the
    PREVIOUS trip's value (cloud hasn't updated yet), don't override.
    """
    from custom_components.ev_trip_logger.const import CONF_LAST_TRIP_ENERGY_SENSOR
    from custom_components.ev_trip_logger.storage import TripRecord

    VEH_E = "sensor.veh_last_trip_energy"
    # Set sensor BEFORE inserting the trip → its last_changed is older.
    hass.states.async_set(VEH_E, "1.65")
    await hass.async_block_till_done()
    entry = await _setup(hass, **{CONF_LAST_TRIP_ENERGY_SENSOR: VEH_E})
    c = hass.data[DOMAIN][entry.entry_id]
    now = dt_util.now()
    rec = TripRecord(
        started_at=now - timedelta(minutes=30),
        ended_at=now,  # trip ended AFTER the sensor was set
        duration_min=25.0,
        distance_km=10.0,
        energy_kwh=2.60,
        consumption_kwh_100km=26.0,
        energy_source="power_integration",
    )
    trip_id = await c.storage.async_insert(rec)
    await c._async_heal_from_vehicle(trip_id)
    after = await c.storage.async_get_trip_by_id(trip_id)
    assert after.energy_kwh == pytest.approx(2.60)  # untouched
    assert after.energy_source == "power_integration"


async def test_vehicle_heal_skipped_on_distance_mismatch(hass: HomeAssistant) -> None:
    """v0.5.77 — defends against the vehicle sensor referring to a
    different trip the logger missed: if vehicle distance disagrees
    by >1 km AND >20 %, skip the override.
    """
    from custom_components.ev_trip_logger.const import (
        CONF_LAST_TRIP_ENERGY_SENSOR,
        CONF_LAST_TRIP_DISTANCE_SENSOR,
    )
    from custom_components.ev_trip_logger.storage import TripRecord

    VEH_E = "sensor.veh_last_trip_energy"
    VEH_D = "sensor.veh_last_trip_distance"
    hass.states.async_set(VEH_E, "1.65")
    hass.states.async_set(VEH_D, "50.0")  # vehicle says 50 km, trip says 10
    entry = await _setup(hass, **{
        CONF_LAST_TRIP_ENERGY_SENSOR: VEH_E,
        CONF_LAST_TRIP_DISTANCE_SENSOR: VEH_D,
    })
    c = hass.data[DOMAIN][entry.entry_id]
    now = dt_util.now()
    rec = TripRecord(
        started_at=now - timedelta(minutes=30),
        ended_at=now - timedelta(minutes=5),
        duration_min=25.0,
        distance_km=10.0,
        energy_kwh=2.60,
        consumption_kwh_100km=26.0,
        energy_source="power_integration",
    )
    trip_id = await c.storage.async_insert(rec)
    hass.states.async_set(VEH_E, "1.65")
    hass.states.async_set(VEH_D, "50.0")
    await c._async_heal_from_vehicle(trip_id)
    after = await c.storage.async_get_trip_by_id(trip_id)
    assert after.energy_kwh == pytest.approx(2.60)
    assert after.energy_source == "power_integration"


async def test_auto_detect_vehicle_sensor_skips_own_entities(hass: HomeAssistant) -> None:
    """Issue #12 — when the vehicle integration's device slug collides
    with the logger's own device slug (e.g. both named "relampago"),
    `sensor.<prefix>_last_trip_energy` can be the LOGGER'S OWN output
    sensor rather than the vehicle's. Auto-detect must not adopt it —
    that would heal trips from data the logger just wrote itself.
    """
    ODO2 = "sensor.relampago_odometer"
    hass.states.async_set(ODO2, "1000.0")
    entry = await _setup(hass, **{CONF_ODOMETER: ODO2})
    c = hass.data[DOMAIN][entry.entry_id]

    collide = "sensor.relampago_last_trip_energy"
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, "fake_own_last_trip_energy",
        suggested_object_id="relampago_last_trip_energy",
        config_entry=entry,
    )
    hass.states.async_set(collide, "0.75")

    assert c._auto_detect_vehicle_sensor(
        ("_last_trip_energy", "_last_trip_kwh", "_trip_energy"),
        "last-trip energy",
    ) is None


async def test_auto_detect_temp_sensor_skips_own_entities(hass: HomeAssistant) -> None:
    """Same guard for the exterior-temp auto-detect prefix walk."""
    ODO2 = "sensor.relampago_odometer"
    hass.states.async_set(ODO2, "1000.0")
    entry = await _setup(hass, **{CONF_ODOMETER: ODO2})
    c = hass.data[DOMAIN][entry.entry_id]

    collide = "sensor.relampago_exterior_temperature"
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, "fake_own_exterior_temp",
        suggested_object_id="relampago_exterior_temperature",
        config_entry=entry,
    )
    hass.states.async_set(collide, "21.0")

    assert c._auto_detect_temp_sensor() is None


def test_speed_stats_v95_and_highway_ratio() -> None:
    """v0.7.3 — `_speed_stats` returns nearest-rank V95 + fraction
    of samples ≥ threshold. Empty/all-None → (None, None).
    """
    from custom_components.ev_trip_logger.coordinator import _speed_stats

    # Trip 206-style samples: 30-tick deque (30 s cadence over ~15 min)
    # with mostly 40-60 km/h and a couple of highway bursts.
    samples = (
        [0.0, 0.0, 0.0]            # 3 idle at lights
        + [40.0, 50.0, 55.0, 45.0] # urban
        + [70.0, 75.0]             # extra-urban
        + [95.0, 100.0, 105.0, 117.0, 110.0, 90.0]  # highway
        + [55.0, 45.0, 30.0]       # slowing to town
    )
    v95, highway = _speed_stats(samples, highway_threshold_kmh=80.0)
    assert v95 is not None and 100.0 <= v95 <= 117.0
    # 6 samples ≥ 80 out of 18 → 33.3 %
    assert highway == pytest.approx(33.3, abs=0.1)

    # Empty deque → both None.
    assert _speed_stats([], highway_threshold_kmh=80.0) == (None, None)
    # All zeros (car idle whole trip) → V95=0, highway=0.
    v95, highway = _speed_stats([0.0] * 5, highway_threshold_kmh=80.0)
    assert v95 == 0.0
    assert highway == 0.0


def test_classify_trip_character_thresholds() -> None:
    """v0.7.6 — highway_ratio_pct → 'highway' / 'mixed' / 'urban' /
    None mapping. Cutoffs at 25 % and 60 % mirror the intuition
    'anything under a quarter is basically city driving' and 'over
    60 % is unambiguously motorway'.
    """
    from custom_components.ev_trip_logger.sensor import (
        _classify_trip_character,
    )

    assert _classify_trip_character(None) is None
    assert _classify_trip_character(0.0) == "urban"
    assert _classify_trip_character(10.0) == "urban"
    assert _classify_trip_character(24.9) == "urban"
    assert _classify_trip_character(25.0) == "mixed"
    assert _classify_trip_character(45.0) == "mixed"
    assert _classify_trip_character(59.9) == "mixed"
    assert _classify_trip_character(60.0) == "highway"
    assert _classify_trip_character(85.0) == "highway"


async def test_trip_record_persists_v95_and_highway_ratio(
    hass: HomeAssistant,
) -> None:
    """v0.7.3 — V95 + highway_ratio round-trip through storage and
    show up in `_trip_to_attr` output for dashboard consumption.
    """
    from custom_components.ev_trip_logger.storage import TripRecord
    from custom_components.ev_trip_logger.sensor import _trip_to_attr

    entry = await _setup(hass)
    coord = hass.data[DOMAIN][entry.entry_id]
    rec = TripRecord(
        started_at=dt_util.now() - timedelta(minutes=30),
        ended_at=dt_util.now(),
        duration_min=30.0,
        distance_km=25.0,
        energy_kwh=4.5,
        consumption_kwh_100km=18.0,
        v95_speed_kmh=105.0,
        highway_ratio_pct=42.5,
    )
    trip_id = await coord.storage.async_insert(rec)
    fetched = await coord.storage.async_get_trip_by_id(trip_id)
    assert fetched is not None
    assert fetched.v95_speed_kmh == pytest.approx(105.0)
    assert fetched.highway_ratio_pct == pytest.approx(42.5)

    attr = _trip_to_attr(fetched)
    assert attr["v95_speed_kmh"] == pytest.approx(105.0)
    assert attr["highway_ratio_pct"] == pytest.approx(42.5)


async def test_trip_record_persists_idle_minutes_field(
    hass: HomeAssistant,
) -> None:
    """v0.7.2 (a.k.a. v0.6.6 idle-tracking) — `idle_minutes` is a
    persisted column on trips; `_trip_to_attr` derives idle_ratio_pct,
    idle_energy_kwh_est, and moving_consumption_kwh_100km from it
    plus the coordinator's `_idle_power_estimate_kw`. Verifies the
    full chain without depending on live-tick timing.
    """
    from custom_components.ev_trip_logger.storage import TripRecord
    from custom_components.ev_trip_logger.sensor import _trip_to_attr

    entry = await _setup(hass)
    coord = hass.data[DOMAIN][entry.entry_id]
    coord._idle_power_estimate_kw = 2.5
    now = dt_util.now()
    # Trip 205-style: 19 km, 4.95 kWh, 120 min total, 95 min idle.
    rec = TripRecord(
        started_at=now - timedelta(minutes=120),
        ended_at=now,
        duration_min=120.0,
        distance_km=19.0,
        energy_kwh=4.95,
        consumption_kwh_100km=26.1,
        idle_minutes=95.0,
    )
    trip_id = await coord.storage.async_insert(rec)
    fetched = await coord.storage.async_get_trip_by_id(trip_id)
    assert fetched is not None
    assert fetched.idle_minutes == pytest.approx(95.0)

    attr = _trip_to_attr(
        fetched, score_baseline=14.5, idle_power_estimate_kw=2.5,
    )
    # Idle ratio: 95 / 120 × 100 = 79.2 %
    assert attr["idle_ratio_pct"] == pytest.approx(79.2, abs=0.05)
    # Idle energy estimate: 95 / 60 × 2.5 = 3.96 kWh
    assert attr["idle_energy_kwh_est"] == pytest.approx(3.96, abs=0.02)
    # Moving consumption: (4.95 - 3.96) / 19 × 100 = 5.2 kWh/100km
    assert attr["moving_consumption_kwh_100km"] == pytest.approx(5.2, abs=0.2)
    # Headline consumption stays at 26.1 — `cost_at_avg_tariff`-style
    # split: the dashboard now has BOTH numbers and the user can see
    # the moving-only figure.
    assert attr["consumption_kwh_100km"] == pytest.approx(26.1)


async def test_trip_to_attr_handles_missing_idle(hass: HomeAssistant) -> None:
    """v0.7.2 — synth / recovered trips have idle_minutes=None.
    The derived metrics must come back as None (not 0, not a crash).
    """
    from custom_components.ev_trip_logger.storage import TripRecord
    from custom_components.ev_trip_logger.sensor import _trip_to_attr

    rec = TripRecord(
        started_at=dt_util.now() - timedelta(minutes=30),
        ended_at=dt_util.now(),
        duration_min=30.0,
        distance_km=20.0,
        energy_kwh=4.0,
        consumption_kwh_100km=20.0,
        idle_minutes=None,  # synth path
    )
    attr = _trip_to_attr(rec, idle_power_estimate_kw=2.5)
    assert attr["idle_minutes"] is None
    assert attr["idle_ratio_pct"] is None
    assert attr["idle_energy_kwh_est"] is None
    assert attr["moving_consumption_kwh_100km"] is None


async def test_recent_avg_tariff_falls_back_to_home_price_when_no_charges(
    hass: HomeAssistant,
) -> None:
    """v0.6.4 — `recent_avg_tariff_per_kwh` returns the configured
    home tariff when the storage aggregate has no recent charges
    (empty cache → fallback). Prevents the dashboard `cost_at_avg_
    tariff` from rendering 0 € on a fresh install just because no
    charges are logged yet.
    """
    from custom_components.ev_trip_logger.const import CONF_ENERGY_PRICE

    entry = await _setup(hass, **{CONF_ENERGY_PRICE: 0.07})
    coord = hass.data[DOMAIN][entry.entry_id]
    # No charges in storage → refresh sets cache to None → property
    # falls back to home tariff.
    await coord._async_refresh_avg_tariff_cache()
    assert coord._avg_tariff_cache_per_kwh is None
    assert coord.recent_avg_tariff_per_kwh == pytest.approx(0.07)


async def test_recent_avg_tariff_uses_weighted_avg_over_recent_charges(
    hass: HomeAssistant,
) -> None:
    """v0.6.4 — when recent charges exist, the cache holds the
    kWh-weighted average. Verifies that the "free charge" case that
    triggered this feature (charge 22 at €0.00 mixed with home charges
    at €0.07) returns a sensible blended number, not zero.
    """
    from custom_components.ev_trip_logger.const import CONF_ENERGY_PRICE
    from custom_components.ev_trip_logger.storage import ChargeRecord

    entry = await _setup(hass, **{CONF_ENERGY_PRICE: 0.07})
    coord = hass.data[DOMAIN][entry.entry_id]
    now = dt_util.now()
    # Two charges: 10 kWh @ 0.07 + 5 kWh @ 0.00 → weighted avg
    # = 0.70 / 15 = 0.0467 €/kWh.
    await coord.storage.async_insert_charge(ChargeRecord(
        ended_at=now - timedelta(days=2),
        kwh=10.0, price_per_kwh=0.07, total_cost=0.70, currency="EUR",
    ))
    await coord.storage.async_insert_charge(ChargeRecord(
        ended_at=now - timedelta(days=1),
        kwh=5.0, price_per_kwh=0.00, total_cost=0.00, currency="EUR",
    ))
    await coord._async_refresh_avg_tariff_cache()
    assert coord._avg_tariff_cache_per_kwh == pytest.approx(0.0467, abs=0.001)
    assert coord.recent_avg_tariff_per_kwh == pytest.approx(0.0467, abs=0.001)


async def test_cohort_baseline_anchors_soh_against_observed_new(
    hass: HomeAssistant,
) -> None:
    """v0.6.3 — when CONF_VEHICLE_MODEL picks a model from the seeded
    cohort JSON, the SoH 100 % anchor uses that cohort's observed
    "new" capacity instead of nameplate. Independent of
    `battery_capacity` (which still routes through nameplate /
    live-calibration). Tessie pattern: lets the dashboard tell the
    user "you're at 99 % vs cohort" even when their car's nameplate
    is optimistic.
    """
    from custom_components.ev_trip_logger.const import CONF_VEHICLE_MODEL

    entry = await _setup(hass, **{
        CONF_BATTERY_CAPACITY: 82.5,  # nameplate
        CONF_VEHICLE_MODEL: "byd_sealion_7_premium",
    })
    coord = hass.data[DOMAIN][entry.entry_id]
    # Cohort `cohort_new_kwh` for this model is 80.6 (from the shipped
    # JSON). Picking it shifts the baseline AWAY from nameplate.
    assert coord.battery_capacity_baseline == pytest.approx(80.6)
    assert coord.vehicle_model_key == "byd_sealion_7_premium"
    # Sanity: `battery_capacity` (for SoC math) stays at nameplate
    # until live-calibration lands. The cohort baseline only routes
    # through SoH.
    assert coord.battery_capacity == pytest.approx(82.5)

    # No cohort picked → falls back to nameplate.
    entry2 = await _setup(hass, **{
        CONF_BATTERY_CAPACITY: 82.5,
    })
    coord2 = hass.data[DOMAIN][entry2.entry_id]
    assert coord2.battery_capacity_baseline == pytest.approx(82.5)
    assert coord2.vehicle_model_key is None

    # Unknown key → silently falls back to nameplate (no crash, no
    # warning spam — keeps the integration usable for vehicles the
    # seed list doesn't cover yet).
    entry3 = await _setup(hass, **{
        CONF_BATTERY_CAPACITY: 82.5,
        CONF_VEHICLE_MODEL: "made_up_model_xyz",
    })
    coord3 = hass.data[DOMAIN][entry3.entry_id]
    assert coord3.battery_capacity_baseline == pytest.approx(82.5)


def test_cohort_baseline_options_returns_seeded_models() -> None:
    """v0.6.3 — the helper used by the config-flow dropdown returns
    the same seeded model list every call, sorted by human label.
    Used as a smoke test: a typo in the JSON or a missing seed key
    fails loud here before the config-flow renders a broken form.
    """
    from custom_components.ev_trip_logger.coordinator import (
        cohort_baseline_options,
    )

    options = cohort_baseline_options()
    keys = {k for k, _ in options}
    # Multi-vendor coverage — never hard-pin to one make.
    assert "byd_sealion_7_premium" in keys
    assert "tesla_model_3_lr" in keys
    assert "hyundai_ioniq_5_lr" in keys
    assert "vw_id4_pro" in keys
    # Sorted alphabetically by label (deterministic for diff-friendly
    # config-flow UI).
    labels = [lbl for _, lbl in options]
    assert labels == sorted(labels)


async def test_orphan_gap_honors_user_min_distance(
    hass: HomeAssistant,
) -> None:
    """v0.5.100 — _detect_orphan_gap respects CONF_MIN_TRIP_DISTANCE.

    User sets min=2.0 in options because a 1-km re-park maneuver
    shouldn't count as a trip. Before v0.5.100 the orphan path used
    its own hardcoded floor of 0.3 km and would still fire on the
    1-km gap, producing a phantom orphan_odo_only row. Now the floor
    is max(0.3, user_min), so the orphan path skips gaps below the
    user's threshold.
    """
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass, **{
        CONF_MIN_TRIP_DISTANCE: 2.0,
    })
    coordinator = hass.data[DOMAIN][entry.entry_id]
    base = dt_util.now()
    last = TripRecord(
        started_at=base - timedelta(hours=2),
        ended_at=base - timedelta(minutes=30),
        duration_min=10.0, distance_km=6.0,
        odometer_start=26818.0, odometer_end=26824.0,
    )
    last.trip_id = await coordinator.storage.async_insert(last)
    coordinator.last_trip = last

    # 1-km gap (re-park) — below user threshold, must NOT trigger orphan.
    assert coordinator._detect_orphan_gap(base, 26825.0) is None
    # 2.5-km gap — above threshold, orphan still detected.
    payload = coordinator._detect_orphan_gap(base, 26826.5)
    assert payload is not None
    assert payload[1] == pytest.approx(2.5)


async def test_recover_segments_via_vehicle_on_handles_sparse_odo(
    hass: HomeAssistant,
) -> None:
    """v0.5.99 — vehicle_on-driven segmentation must split two separate
    missed drives even when the recorder only has 3 odometer samples.

    The trip-193 case in miniature: cloud-polled odometer reports at
    18:08 (post-trip-192), 18:20 (mid mini-drive A), 19:07 (mid mini-
    drive B). The old odometer-walker coalesced these into ONE big
    segment (no plateau-finalize between samples because no sample
    arrived during the 47-min idle) AND skipped it because the
    segment started 26 s before trip 192's recorded end. v0.5.99
    walks vehicle_on edges instead, producing two clean segments.
    """
    from types import SimpleNamespace

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    base = datetime(2026, 6, 25, 18, 0, 0, tzinfo=timezone.utc)
    # Mock state objects with the recorder's expected shape.
    def _s(ts_min, ts_sec, state):
        ts = base + timedelta(minutes=ts_min, seconds=ts_sec)
        return SimpleNamespace(state=str(state), last_updated=ts,
                               attributes={})

    vehicle_on_states = [
        _s(8, 26, "off"),     # 18:08:26 trip 192 closes
        _s(18, 34, "on"),     # 18:18:34 drive A starts
        _s(22, 25, "off"),    # 18:22:25 drive A ends
        _s(66, 44, "on"),     # 19:06:44 drive B starts
        _s(69, 26, "off"),    # 19:09:26 drive B ends
    ]
    odometer_states = [
        _s(8, 0, "26824"),    # last known just before window
        _s(20, 17, "26825"),  # +1 km after drive A
        _s(67, 50, "26826"),  # +1 km after drive B
    ]

    async def _fake_executor(func, *args, **kwargs):
        # state_changes_during_period(hass, start, end, entity_id)
        eid = args[3]
        if eid == VOK:
            states = [s for s in vehicle_on_states
                      if args[1] <= s.last_updated <= args[2]]
            return {VOK: states}
        if eid == ODO:
            states = [s for s in odometer_states
                      if args[1] <= s.last_updated <= args[2]]
            return {ODO: states}
        return {eid: []}

    # Recorder access uses get_instance(hass).async_add_executor_job.
    # Patch the returned instance.
    fake_recorder = SimpleNamespace(async_add_executor_job=_fake_executor)
    segments = await coordinator._recover_segments_via_vehicle_on(
        since=base,
        until=base + timedelta(hours=2),
        recorder=fake_recorder,
    )
    # Both drives recovered, each ≥ min_distance.
    assert len(segments) == 2
    s1, s2 = segments
    assert s1[0] == base + timedelta(minutes=18, seconds=34)
    assert s1[1] == base + timedelta(minutes=22, seconds=25)
    assert s1[2] == pytest.approx(26824.0)
    assert s1[3] == pytest.approx(26825.0)
    assert s2[0] == base + timedelta(minutes=66, seconds=44)
    assert s2[1] == base + timedelta(minutes=69, seconds=26)
    assert s2[2] == pytest.approx(26825.0)
    assert s2[3] == pytest.approx(26826.0)


async def test_orphan_yields_to_recovered_live_trips(hass: HomeAssistant) -> None:
    """v0.5.98 — when the recorder still has the precise vehicle_on
    edges of a missed drive, the orphan_odo_only synthetic window
    must NOT be inserted. The user's trip-193 case: a real 4-min /
    1-km re-park happened between trip 192 (closed) and trip 193's
    detection at the next ignition. The pre-v0.5.98 path produced a
    30-min orphan_odo_only row; recovery has the real timestamps so
    it inserts a proper live row and the orphan must yield.
    """
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # Pre-seed a closed last_trip so _detect_orphan_gap has something
    # to anchor against.
    now = dt_util.now()
    last = TripRecord(
        started_at=now - timedelta(hours=2),
        ended_at=now - timedelta(minutes=45),
        duration_min=10.0, distance_km=6.0,
        odometer_start=26818.0, odometer_end=26824.0,
        soc_start=64.0, soc_end=61.0, soc_used_pct=3.0,
        energy_kwh=2.48, consumption_kwh_100km=41.2,
    )
    last.trip_id = await coordinator.storage.async_insert(last)
    coordinator.last_trip = last

    # Force recover_missing_trips_service to claim it inserted 1 row.
    recovery_calls: list[tuple[datetime, datetime]] = []
    inserts_before = (await coordinator.storage.async_recent_trips(50))

    async def _fake_recover(*, since, until):
        recovery_calls.append((since, until))
        return 1

    coordinator.async_recover_missing_trips_service = _fake_recover  # type: ignore[assignment]

    # Drive the wrapper directly — bypasses _open_trip plumbing so the
    # test asserts only on the orphan/recovery decision.
    await coordinator._async_insert_orphan_with_recovery(
        last, now, 26825.0, 61.0, 1.0,
    )
    assert len(recovery_calls) == 1
    assert recovery_calls[0][0] == last.ended_at
    inserts_after = (await coordinator.storage.async_recent_trips(50))
    # No new row beyond `last` — the orphan was suppressed because
    # recovery claimed to have covered the gap.
    assert len(inserts_after) == len(inserts_before)


async def test_orphan_falls_back_when_recovery_empty(hass: HomeAssistant) -> None:
    """v0.5.98 — when recovery returns 0 (no recorder evidence of a
    real drive) the orphan_odo_only insert still fires as the
    residual catch for true odo-drift / catch-up snapshots.
    """
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    now = dt_util.now()
    last = TripRecord(
        started_at=now - timedelta(hours=2),
        ended_at=now - timedelta(minutes=10),
        duration_min=10.0, distance_km=6.0,
        odometer_start=26818.0, odometer_end=26824.0,
        soc_start=64.0, soc_end=61.0, soc_used_pct=3.0,
    )
    last.trip_id = await coordinator.storage.async_insert(last)
    coordinator.last_trip = last

    async def _empty_recover(*, since, until):
        return 0

    coordinator.async_recover_missing_trips_service = _empty_recover  # type: ignore[assignment]
    inserts_before = (await coordinator.storage.async_recent_trips(50))

    await coordinator._async_insert_orphan_with_recovery(
        last, now, 26825.0, 61.0, 1.0,
    )
    inserts_after = (await coordinator.storage.async_recent_trips(50))
    assert len(inserts_after) == len(inserts_before) + 1
    new_row = inserts_after[0]
    assert new_row.confidence == "orphan_odo_only"
    assert new_row.odometer_end == pytest.approx(26825.0)


async def test_orphan_duration_capped_when_padded_by_downtime(
    hass: HomeAssistant,
) -> None:
    """v0.8.1 — a real missed drive (SoC drop tracks the km, so it's
    classified 'orphan' not 'orphan_odo_only') must not inherit hours
    of parked/HA-offline time as its duration.

    Reproduces the 2026-07-18 case: HA restarts briefly, the live path
    never sees the vehicle_on edges, and recorder recovery finds
    nothing (nothing was recorded while HA was down either). The old
    behaviour used the full last_trip.ended_at -> now span (3 h here)
    as duration_min, producing an absurd ~1.7 km/h average for a real
    5 km drive. It must now be capped to the longest duration
    compatible with _ORPHAN_MIN_PLAUSIBLE_AVG_KMH (15 km/h floor).
    """
    from custom_components.ev_trip_logger.storage import TripRecord

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    now = dt_util.now()
    last = TripRecord(
        started_at=now - timedelta(hours=5),
        ended_at=now - timedelta(hours=3),
        duration_min=10.0, distance_km=6.0,
        odometer_start=26818.0, odometer_end=26824.0,
        soc_start=64.0, soc_end=60.0,
    )
    last.trip_id = await coordinator.storage.async_insert(last)
    coordinator.last_trip = last

    async def _empty_recover(*, since, until):
        return 0

    coordinator.async_recover_missing_trips_service = _empty_recover  # type: ignore[assignment]
    inserts_before = (await coordinator.storage.async_recent_trips(50))

    # 5 km gap, 1% SoC drop — ratio 1.0 vs the 15 kWh/100km default
    # expectation (0.2%/km * 5 km = 1.0%), so this classifies 'orphan'.
    await coordinator._async_insert_orphan_with_recovery(
        last, now, 26829.0, 59.0, 5.0,
    )
    inserts_after = (await coordinator.storage.async_recent_trips(50))
    assert len(inserts_after) == len(inserts_before) + 1
    new_row = inserts_after[0]
    assert new_row.confidence == "orphan"
    # Capped to 5 km / 15 km/h == 20 min, not the naive 180 min span.
    assert new_row.duration_min == pytest.approx(20.0)
    assert new_row.avg_speed_kmh == pytest.approx(15.0)
    assert new_row.started_at == now - timedelta(minutes=20.0)
    assert new_row.odometer_end == pytest.approx(26829.0)


async def test_orphan_trip_resolves_home_arrival_and_adopts_last_trip(
    hass: HomeAssistant,
) -> None:
    """v0.8.5 — an orphan window that actually lands back at home must
    close the journey and become last_trip, not silently hardcode
    destination=None and leave last_trip stuck on the trip before it.
    """
    from custom_components.ev_trip_logger.storage import TripRecord

    hass.states.async_set(LOC, "home")
    entry = await _setup(hass, **{CONF_LOCATION: LOC})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    now = dt_util.now()
    last = TripRecord(
        started_at=now - timedelta(hours=2),
        ended_at=now - timedelta(hours=3) + timedelta(hours=2, minutes=50),
        duration_min=10.0, distance_km=6.0,
        odometer_start=26818.0, odometer_end=26824.0,
        soc_start=64.0, soc_end=60.0,
        destination="work",
    )
    last.trip_id = await coordinator.storage.async_insert(last)
    coordinator.last_trip = last

    async def _empty_recover(*, since, until):
        return 0

    coordinator.async_recover_missing_trips_service = _empty_recover  # type: ignore[assignment]

    # 5 km gap, 1% SoC drop — classifies 'orphan' (real missed drive).
    await coordinator._async_insert_orphan_with_recovery(
        last, now, 26829.0, 59.0, 5.0,
    )
    inserts_after = await coordinator.storage.async_recent_trips(50)
    new_row = inserts_after[0]
    assert new_row.confidence == "orphan"
    assert new_row.destination == "home"
    assert new_row.journey_id is not None
    assert coordinator.last_completed_journey_id == new_row.journey_id
    assert coordinator.current_journey_id is None
    assert coordinator.last_trip is not None
    assert coordinator.last_trip.trip_id == new_row.trip_id


async def test_disconnect_orphan_resolves_home_arrival_and_adopts_last_trip(
    hass: HomeAssistant,
) -> None:
    """v0.8.5 — same fix for the disconnect-orphan path: a disconnect
    gap that ended with the vehicle back home must resolve the real
    destination, close the journey, and become last_trip — reproduces
    the 2026-07-30 case where last_trip_* sensors stayed stuck on the
    PREVIOUS trip after a disconnect-orphan closed more recently.
    """
    from custom_components.ev_trip_logger.storage import TripRecord

    hass.states.async_set(LOC, "home")
    entry = await _setup(hass, **{CONF_LOCATION: LOC})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    now = dt_util.now()
    last = TripRecord(
        started_at=now - timedelta(hours=3),
        ended_at=now - timedelta(hours=2, minutes=20),
        duration_min=10.0, distance_km=4.0,
        odometer_start=28323.0, odometer_end=28327.0,
        soc_start=36.0, soc_end=37.0,
        destination="not_home",
    )
    last.trip_id = await coordinator.storage.async_insert(last)
    coordinator.last_trip = last

    await coordinator._async_insert_disconnect_orphan(
        last.ended_at, now, 28327.0, 28344.0, 37.0, 55.0,
    )
    inserts_after = await coordinator.storage.async_recent_trips(50)
    new_row = inserts_after[0]
    assert new_row.confidence == "orphan_disconnect"
    assert new_row.destination == "home"
    # Origin ("not_home") wasn't home, but landing back at home still
    # stitches a one-stage journey — same rule _async_close_trip uses.
    assert new_row.journey_id is not None
    assert coordinator.last_completed_journey_id == new_row.journey_id
    assert coordinator.current_journey_id is None
    assert coordinator.last_trip is not None
    assert coordinator.last_trip.trip_id == new_row.trip_id
    assert coordinator.last_trip.destination == "home"


def test_integrate_evse_from_recorder_masks_idle_windows() -> None:
    """v0.5.95 — backfill integrator masks samples to charge_sensor=on
    windows and converts W → kW transparently.

    Setup: a 2-hour charge window with the wallbox reporting 7000 W
    flat. The car says charging=on for the first hour, charging=off
    for the second. Integrated energy should be ≈ 7 kWh (the masked
    second hour drops out) — not 14 kWh (no mask) and not 0 (wrong
    unit handling).
    """
    from custom_components.ev_trip_logger.coordinator import (
        EvTripLoggerCoordinator,
    )

    class _S:
        def __init__(self, state, when, unit=None):
            self.state = state
            self.last_updated = when
            self.last_changed = when
            self.attributes = (
                {"unit_of_measurement": unit} if unit else {}
            )

    t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    evse = [
        _S("7000", t0 + timedelta(minutes=m), unit="W")
        for m in range(0, 121, 10)
    ]
    charge_states = [
        _S("on", t0),
        _S("off", t0 + timedelta(hours=1)),
    ]
    kwh = EvTripLoggerCoordinator._integrate_evse_from_recorder(
        evse_states=evse,
        charge_states=charge_states,
        window_start=t0,
        window_end=t0 + timedelta(hours=2),
    )
    # 7 kW × 1 h, masked to first hour only.
    assert kwh == pytest.approx(7.0, abs=0.05)

    # Same samples, NO mask → 7 kW × 2 h ≈ 14 kWh.
    full = EvTripLoggerCoordinator._integrate_evse_from_recorder(
        evse_states=evse,
        charge_states=[],
        window_start=t0,
        window_end=t0 + timedelta(hours=2),
    )
    assert full == pytest.approx(14.0, abs=0.05)

    # Empty → None (don't write a phantom zero onto the charge row).
    assert EvTripLoggerCoordinator._integrate_evse_from_recorder(
        evse_states=[],
        charge_states=charge_states,
        window_start=t0,
        window_end=t0 + timedelta(hours=2),
    ) is None
