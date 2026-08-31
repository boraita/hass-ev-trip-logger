"""Tests for the trip detection state machine and storage integration."""
from __future__ import annotations

from datetime import datetime, timedelta, UTC

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
    CONF_EVSE_POWER_SENSOR,
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
    CONF_LAST_TRIP_ENERGY_SENSOR,
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


async def test_a_cloud_dropout_mid_drive_does_not_split_the_trip(
    hass: HomeAssistant,
) -> None:
    """v0.8.49 — `vehicle_on=off` is not proof the ignition went off.

    On a cloud-polled car, losing the car reads exactly like switching it
    off: `vehicle_on` goes off and `speed` goes to 0 while the car is
    doing 90 km/h. Measured on the author's install (28/08): off at
    14:59:48, six minutes of silence, then everything lands at once —
    speed 80, odometer +10 km. The 180 s grace expired in the hole, so
    the trip closed and a second one opened, and one 35 km drive became
    three rows reading 29.5 / 16.5 / 7.5 kWh/100 km against a true 18.9.

    The odometer is the evidence the timer is not: if it advanced while
    the car claimed to be off, the car was driving. So the deferred close
    re-checks it and keeps deferring while the kilometres keep coming.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        entry = await _setup(hass, odo=1000.0, bat=80.0, on=True)
        coordinator = hass.data[DOMAIN][entry.entry_id]

        hass.states.async_set(VOK, STATE_ON)
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=5))
        hass.states.async_set(ODO, "1030")
        await hass.async_block_till_done()
        assert coordinator.current is not None, "a trip must be open"

        # The cloud drops the car mid-drive.
        hass.states.async_set(VOK, STATE_OFF)
        await hass.async_block_till_done()

        # Grace expires, but the odometer has moved: the car is driving.
        frozen.tick(timedelta(seconds=200))
        hass.states.async_set(ODO, "1040")
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.current is not None, (
            "the odometer moved while 'off' — that is a dropout, not an "
            "ignition off, and closing here is what split the drive"
        )

        # The car comes back and finishes the drive.
        hass.states.async_set(VOK, STATE_ON)
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=3))
        hass.states.async_set(ODO, "1055")
        await hass.async_block_till_done()

        # Now it really stops: off, and the odometer stays put.
        hass.states.async_set(VOK, STATE_OFF)
        await hass.async_block_till_done()
        frozen.tick(timedelta(seconds=200))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert coordinator.current is None, "a real stop still closes the trip"
    assert coordinator.last_trip is not None
    assert coordinator.last_trip.distance_km == pytest.approx(55.0), (
        "one trip covering the whole drive, dropout included"
    )


async def test_charge_energy_prefers_power_integration_with_good_coverage(
    hass: HomeAssistant,
) -> None:
    """v0.8.14 — a power-integration reading that disagrees with the old
    fixed ±30 % band should still win once it's earned trust via good
    sample coverage across the session.

    SoC 20%→25% at 75 kWh nominal capacity implies 3.75 kWh (kwh_soc).
    The car's power sensor reports a steady 10 kW draw for 30 min
    (3 contributing samples spanning the whole charging window) →
    5.0 kWh, 33 % above kwh_soc — outside the old fixed ±30 % band but
    inside the new coverage-earned ±40 % band.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        hass.states.async_set(POW, "0")
        hass.states.async_set(CHG, STATE_OFF)
        entry = await _setup(
            hass, bat=20.0, **{CONF_CHARGE_SENSOR: CHG, CONF_POWER: POW},
        )
        coordinator = hass.data[DOMAIN][entry.entry_id]

        hass.states.async_set(CHG, STATE_ON)
        await hass.async_block_till_done()
        assert coordinator.current_charge.soc_start == 20.0

        hass.states.async_set(POW, "-10", force_update=True)
        await hass.async_block_till_done()

        for _ in range(3):
            frozen.tick(timedelta(minutes=10))
            hass.states.async_set(POW, "-10", force_update=True)
            await hass.async_block_till_done()

        hass.states.async_set(BAT, "25")
        await hass.async_block_till_done()

        frozen.tick(timedelta(minutes=5))
        hass.states.async_set(CHG, STATE_OFF)
        await hass.async_block_till_done()

    assert coordinator.last_charge is not None
    assert coordinator.last_charge.kwh == pytest.approx(5.0, abs=0.01)
    assert coordinator.last_charge.energy_source == "power_integration"


def test_a_meter_that_recorded_zero_is_evidence_not_silence() -> None:
    """v0.8.42 — the bug the v0.8.41 backfill shipped with.

    `_integrate_evse_from_recorder` returns None for two situations that
    are nothing alike: no usable samples, and a sensor that recorded the
    whole session and never delivered a watt. v0.8.41 treated both as "no
    evidence" and skipped, but the second is the STRONGEST evidence that
    the stored figure came from somewhere other than this meter.

    Measured live after that release: 34 of 35 rows went unlabelled,
    because the author's wallbox records continuously through every away
    session and integrates to exactly 0.00 kWh — the common case, not an
    edge case.
    """
    from custom_components.ev_trip_logger.coordinator import (
        EvTripLoggerCoordinator as C,
    )

    # A wallbox recording through a 36 kWh away session, delivering
    # nothing: that figure is an operator invoice.
    assert C._decide_evse_source(36.16, None, sample_count=240) == "invoice"
    assert C._decide_evse_source(36.16, 0.0, sample_count=240) == "invoice"
    # The replay reproduces the figure → the sensor produced it. 15.74 is
    # the author's one home session, matched to three decimals.
    assert C._decide_evse_source(15.74, 15.74, sample_count=240) == "meter"
    assert C._decide_evse_source(15.74, 15.60, sample_count=240) == "meter"
    # No samples at all is purged history: genuinely unknown, which is
    # different from zero and must stay unlabelled.
    assert C._decide_evse_source(36.16, None, sample_count=0) is None
    # One sample with no peak information cannot be integrated over, so it
    # stays unlabelled too. v0.8.43 adds the peak, which settles it.
    assert C._decide_evse_source(36.16, None, sample_count=1) is None
    # Saw something that is not this figure — labelling that is how the
    # v0.8.30 "metered" pool went wrong.
    assert C._decide_evse_source(36.16, 20.0, sample_count=240) is None


def test_a_flat_zero_sensor_arrives_as_one_state_not_a_stream() -> None:
    """v0.8.43 — the recorder stores state CHANGES, not a stream.

    A wallbox idling at 0.0 W through a 20-minute session produces a
    single boundary row, not hundreds of samples. v0.8.42 required two
    samples before it would read a zero, so it threw away exactly the
    evidence the feature was built to read: measured live, 34 of 35 rows
    stayed unlabelled and the log said "could not be decided" for every
    away charge. This is the real shape of the author's data — one state,
    value 0.0, against a stored 36.16 kWh.

    A peak of zero settles it with no integration at all: the sensor was
    observed across the window and never delivered a watt, so the figure
    came from somewhere else.
    """
    from custom_components.ev_trip_logger.coordinator import (
        EvTripLoggerCoordinator as C,
    )

    assert C._decide_evse_source(
        36.16, None, sample_count=1, peak_kw=0.0
    ) == "invoice"
    # A single NON-zero sample cannot be integrated, and calling it an
    # invoice would invent one out of a sensor that may have been
    # delivering the whole time.
    assert C._decide_evse_source(
        36.16, None, sample_count=1, peak_kw=7.4
    ) is None
    # No states at all is still unknown, whatever the peak says.
    assert C._decide_evse_source(
        36.16, None, sample_count=0, peak_kw=0.0
    ) is None
    # A zero peak cannot override a replay that reproduces the figure —
    # meter is checked first, so a 0 kWh charge stays coherent.
    assert C._decide_evse_source(
        0.2, 0.2, sample_count=1, peak_kw=0.0
    ) == "meter"


def test_evse_state_reader_handles_both_units() -> None:
    """W and kW in the same codebase, one reader.

    v0.8.43 lifted this out of the integrator so the provenance decision
    could ask about the peak without re-implementing unit handling and
    getting it subtly different — which is the kind of duplication that
    produced the 156.8 % efficiency two releases ago.
    """
    from custom_components.ev_trip_logger.coordinator import (
        EvTripLoggerCoordinator as C,
    )

    class _S:
        def __init__(self, state, unit=None):
            self.state = state
            self.attributes = {"unit_of_measurement": unit} if unit else {}

    assert C._evse_state_kw(_S("7400", "W")) == pytest.approx(7.4)
    assert C._evse_state_kw(_S("7.4", "kW")) == pytest.approx(7.4)
    assert C._evse_state_kw(_S("7.4")) == pytest.approx(7.4)
    assert C._evse_state_kw(_S("-3", "kW")) == 0.0, "clamped, not negative"
    assert C._evse_state_kw(_S("unavailable")) is None
    assert C._evse_peak_kw([_S("0", "W"), _S("0", "W")]) == 0.0
    assert C._evse_peak_kw([_S("0"), _S("7.4"), _S("3")]) == pytest.approx(7.4)
    assert C._evse_peak_kw([]) is None
    assert C._evse_peak_kw([_S("unavailable")]) is None


def test_provenance_tolerance_scales_with_the_figure() -> None:
    """A fixed kWh tolerance would misjudge both ends of the range.

    The author's home sessions run from 1.05 to 43.25 kWh, so the band
    has to be a ratio — with an absolute floor, because 10 % of 1.05 kWh
    is finer than recorder resampling can resolve.
    """
    from custom_components.ev_trip_logger.coordinator import (
        EvTripLoggerCoordinator as C,
    )

    # Small session: the 0.25 kWh floor carries it, 10 % would not.
    assert C._decide_evse_source(1.05, 0.90, sample_count=60) == "meter"
    # Large session: 10 % of 43.25 is 4.3, so a 2 kWh gap still matches.
    assert C._decide_evse_source(43.25, 41.25, sample_count=60) == "meter"
    # But not a gap of half the session.
    assert C._decide_evse_source(43.25, 21.0, sample_count=60) is None


async def test_acceptance_band_is_anchored_on_declared_not_calibrated(
    hass: HomeAssistant,
) -> None:
    """v0.8.39 — the band that admits a measurement must not move with it.

    A charge is tagged `power_integration` only if its integral lands
    within ±40 % of ΔSoC × capacity, and the capacity calibration is
    computed from exactly the charges carrying that tag. Anchoring the
    band on the calibrated figure therefore pre-filtered the pool by the
    number the pool derives, and the filter moved with the answer.

    v0.8.37's ceiling capped the upward direction. What is left is the
    downward one, which this test pins: a pack calibrated LOW rejects the
    higher integrals that would have corrected it, and locks itself in.

    Declared 82.5 kWh, calibrated down to 60.0, ΔSoC 50 %:
      * declared band   ±40 % of 41.25  →  [24.75, 57.75]
      * calibrated band ±40 % of 30.00  →  [18.00, 42.00]
    A 45 kWh integral sits inside the first and outside the second. It
    must be accepted, because the pack really is bigger than 60 kWh and
    this measurement is the evidence that says so.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        hass.states.async_set(POW, "0")
        hass.states.async_set(CHG, STATE_OFF)
        entry = await _setup(
            hass, bat=20.0,
            **{CONF_BATTERY_CAPACITY: 82.5, CONF_CHARGE_SENSOR: CHG,
               CONF_POWER: POW},
        )
        coordinator = hass.data[DOMAIN][entry.entry_id]
        coordinator._battery_capacity_calibrated = 60.0
        assert coordinator.battery_capacity == 60.0

        hass.states.async_set(CHG, STATE_ON)
        await hass.async_block_till_done()

        # 90 kW for 30 min = 45.0 kWh, 3 samples across the session.
        hass.states.async_set(POW, "-90", force_update=True)
        await hass.async_block_till_done()
        for _ in range(3):
            frozen.tick(timedelta(minutes=10))
            hass.states.async_set(POW, "-90", force_update=True)
            await hass.async_block_till_done()

        hass.states.async_set(BAT, "70")
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=5))
        hass.states.async_set(CHG, STATE_OFF)
        await hass.async_block_till_done()

    assert coordinator.last_charge is not None
    assert coordinator.last_charge.energy_source == "power_integration", (
        "45 kWh is inside the declared-capacity band; anchoring on the "
        "60 kWh calibration would have thrown away the one measurement "
        "able to correct it"
    )
    assert coordinator.last_charge.kwh == pytest.approx(45.0, abs=0.05)


async def test_acceptance_band_still_rejects_an_impossible_integral(
    hass: HomeAssistant,
) -> None:
    """The band is looser after v0.8.39, not absent.

    ΔSoC 50 % of a declared 82.5 kWh pack is 41.25 kWh, so the band runs
    to 57.75. A 90 kWh integral — more than the whole pack — is still
    refused, and the charge falls back to SoC math.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        hass.states.async_set(POW, "0")
        hass.states.async_set(CHG, STATE_OFF)
        entry = await _setup(
            hass, bat=20.0,
            **{CONF_BATTERY_CAPACITY: 82.5, CONF_CHARGE_SENSOR: CHG,
               CONF_POWER: POW},
        )
        coordinator = hass.data[DOMAIN][entry.entry_id]

        hass.states.async_set(CHG, STATE_ON)
        await hass.async_block_till_done()
        hass.states.async_set(POW, "-180", force_update=True)
        await hass.async_block_till_done()
        for _ in range(3):
            frozen.tick(timedelta(minutes=10))
            hass.states.async_set(POW, "-180", force_update=True)
            await hass.async_block_till_done()

        hass.states.async_set(BAT, "70")
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=5))
        hass.states.async_set(CHG, STATE_OFF)
        await hass.async_block_till_done()

    assert coordinator.last_charge is not None
    assert coordinator.last_charge.energy_source == "soc_delta"
    assert coordinator.last_charge.kwh == pytest.approx(41.25, abs=0.05)


async def test_a_single_huge_gap_disqualifies_the_integral(
    hass: HomeAssistant,
) -> None:
    """v0.8.50 — coverage measured the SPAN, and was blind inside it.

    The v0.8.14 check is (last - first) / duration, so samples at both
    ends of a long session score 100 % however little happens between.

    Charge 74 was that shape: a solar-modulated home charge cycling on and
    off every 10-20 s, 14.3 h long, and one gap of **237 minutes**. Any
    gap over `_MAX_POWER_TRAPEZOID_DT_H` is dropped, so the energy that
    flowed during those four hours was never counted — the integral came
    out 26.25 kWh against 40.6 kWh on the wall meter, 32 % short, yet
    still *inside* the ±40 % plausibility band. It was adopted as a
    measurement and entered the capacity pool implying a 55.9 kWh pack.

    That "inside the band" part is what makes this test non-trivial: an
    integral short enough to fail the band would have been caught
    already. Here 30 kWh of well-sampled charging lands comfortably
    inside the band for a 40-point delta (`[19.8, 46.2]`), and only the
    45-minute hole afterwards can disqualify it.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        hass.states.async_set(POW, "0")
        hass.states.async_set(CHG, STATE_OFF)
        entry = await _setup(
            hass, bat=20.0,
            **{CONF_BATTERY_CAPACITY: 82.5, CONF_CHARGE_SENSOR: CHG,
               CONF_POWER: POW},
        )
        coordinator = hass.data[DOMAIN][entry.entry_id]

        hass.states.async_set(CHG, STATE_ON)
        await hass.async_block_till_done()

        # 20 kW sampled every 10 min for 90 min = 30.0 kWh, densely
        # covered and well inside the band.
        hass.states.async_set(POW, "-20", force_update=True)
        await hass.async_block_till_done()
        for _ in range(9):
            frozen.tick(timedelta(minutes=10))
            hass.states.async_set(POW, "-20", force_update=True)
            await hass.async_block_till_done()

        # The hole. Power kept flowing; nobody sampled it.
        frozen.tick(timedelta(minutes=45))
        hass.states.async_set(POW, "-20", force_update=True)
        await hass.async_block_till_done()

        hass.states.async_set(BAT, "60")
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=1))
        hass.states.async_set(CHG, STATE_OFF)
        await hass.async_block_till_done()

    assert coordinator.last_charge is not None
    assert coordinator.last_charge.kwh == pytest.approx(33.0, abs=0.5), (
        "falls back to SoC math: 40 points of a declared 82.5 kWh pack"
    )
    assert coordinator.last_charge.energy_source == "soc_delta", (
        "a 45-minute hole makes the integral incomplete, however wide the "
        "samples span and however plausible the total looks"
    )


async def test_charge_energy_distrusts_sparse_power_samples(
    hass: HomeAssistant,
) -> None:
    """v0.8.14 — a plausible-looking power-integration number must NOT be
    trusted when it's built from samples that only cover a fraction of
    the session (cloud dropout mid-DCFC-charge is common away from
    home). Even though the sparse reading lands close enough to
    kwh_soc to have passed the OLD ±30 % check, coverage now gates it
    out before that comparison ever runs.

    SoC 20%→25% at 75 kWh → kwh_soc = 3.75 kWh. 3 power samples all
    land in the first 5 minutes of a 40-minute session (~3.33 kWh
    integrated) then the sensor goes quiet for the remaining 35 min.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        hass.states.async_set(POW, "0")
        hass.states.async_set(CHG, STATE_OFF)
        entry = await _setup(
            hass, bat=20.0, **{CONF_CHARGE_SENSOR: CHG, CONF_POWER: POW},
        )
        coordinator = hass.data[DOMAIN][entry.entry_id]

        hass.states.async_set(CHG, STATE_ON)
        await hass.async_block_till_done()

        hass.states.async_set(POW, "-40", force_update=True)
        await hass.async_block_till_done()

        for minutes in (2, 2, 1):
            frozen.tick(timedelta(minutes=minutes))
            hass.states.async_set(POW, "-40", force_update=True)
            await hass.async_block_till_done()

        # Cloud goes quiet — no more power samples for the rest of the
        # session, even though it stays open another 35 minutes.
        frozen.tick(timedelta(minutes=35))
        hass.states.async_set(BAT, "25")
        await hass.async_block_till_done()
        hass.states.async_set(CHG, STATE_OFF)
        await hass.async_block_till_done()

    assert coordinator.last_charge is not None
    assert coordinator.last_charge.kwh == pytest.approx(3.75, abs=0.01)
    assert coordinator.last_charge.energy_source == "soc_delta"


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


async def test_open_journey_absorbs_next_stage_that_starts_at_home(
    hass: HomeAssistant,
) -> None:
    """An open journey absorbs the next stage even when it starts at home.

    v0.5.14 deliberately removed the "retroactively close the journey when a
    stage opens from home" band-aid: it conflated GPS-noise destinations with
    real home arrivals. The surviving invariant is that a journey closes only
    on a trip that *ends* at a home, so a stage starting at home while a
    journey is open joins it rather than minting a second one.

    Until v0.8.27 this test asserted the removed behaviour and passed anyway:
    the dwell guard was a bare `asyncio.sleep`, so `async_block_till_done`
    inside the next stage awaited it and the late-arrival amend closed
    journey 1 before the stage opened. Real HA never did that — the amend
    bails once a trip is active. The dwell path has its own coverage in
    `test_late_home_arrival_closes_journey_and_amends_destination`.
    """
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

    # Journey 1 stays open and swallows stage 2 — no second journey, and
    # nothing closed, because no trip has ended at home yet.
    assert coordinator.current_journey_id == jid_1
    assert coordinator.last_completed_journey_id is None

    # It closes on the arrival, which is the only thing that may close it.
    await _run_stage(hass, odo_start=1050, odo_end=1070, soc_end=50, location_end="home")
    assert coordinator.last_completed_journey_id == jid_1
    assert coordinator.current_journey_id is None


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

    # device_tracker finally flips to 'home'. The amend is held back by the
    # dwell guard, so advance the clock past it to let the check run.
    hass.states.async_set(LOC, "home")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
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

    # Geofence lag — device_tracker flips to 'Trabajo ele ' (custom zone with
    # a trailing space, as the user's real HA reports it). Advance past the
    # dwell guard so the deferred amend runs.
    hass.states.async_set(LOC, "Trabajo ele ")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
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
    from custom_components.ev_trip_logger.calc import (
        parse_secondary_home_coords,
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
    parsed = parse_secondary_home_coords(raw)
    assert len(parsed) == 2
    assert parsed[0] == (36.5, -4.5, DEFAULT_SECONDARY_HOME_RADIUS_M, "secondary_home_1")
    assert parsed[1] == (40.0, -3.0, 250.0, "Casa de la playa")


async def test_secondary_home_coords_parsed_empty_for_blank_input() -> None:
    from custom_components.ev_trip_logger.calc import (
        parse_secondary_home_coords,
    )

    assert parse_secondary_home_coords(None) == []
    assert parse_secondary_home_coords("") == []
    assert parse_secondary_home_coords("   \n  # just a comment\n") == []


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


async def test_set_last_charge_price_with_evse_energy_kwh(hass: HomeAssistant) -> None:
    """An away charge with no EVSE sensor can get evse_energy_kwh from an
    invoice after the fact — same field/formula the home auto-detect uses."""
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE,
        {"kwh": 18.0, "location": "Iberdrola Móstoles"},
        blocking=True,
    )
    assert coordinator.last_charge.evse_energy_kwh is None

    await hass.services.async_call(
        DOMAIN, "set_last_charge_price",
        {"total_cost": 9.0, "evse_energy_kwh": 20.0},
        blocking=True,
    )
    updated = coordinator.last_charge
    assert updated.total_cost == pytest.approx(9.0)
    assert updated.evse_energy_kwh == pytest.approx(20.0)
    assert updated.charging_efficiency_pct == pytest.approx(18.0 / 20.0 * 100.0, abs=0.1)


async def test_set_last_charge_price_by_id_with_evse_energy_kwh(
    hass: HomeAssistant,
) -> None:
    """Same as above but targeting an older charge via charge_id."""
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE,
        {"kwh": 10.0, "location": "public"},
        blocking=True,
    )
    charge_id = coordinator.last_charge.charge_id

    await hass.services.async_call(
        DOMAIN, "set_last_charge_price",
        {"charge_id": charge_id, "evse_energy_kwh": 11.0},
        blocking=True,
    )
    updated = coordinator.last_charge
    assert updated.charge_id == charge_id
    assert updated.evse_energy_kwh == pytest.approx(11.0)
    assert updated.charging_efficiency_pct == pytest.approx(10.0 / 11.0 * 100.0, abs=0.1)


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
    # v0.5.76, WAC pool since v0.8.8. v0.8.17 — the charge closed at
    # 80 % SoC, so the pool is re-anchored to the pack's real content
    # (0.80 × 75 = 60 kWh) instead of holding only the 10 kWh of this
    # session: the battery was not empty before it. The whole 11.25 kWh
    # withdrawal is therefore covered at the blended 0.30 → 3.38 €.
    # Previously the pool held 10 kWh and the 1.25 kWh overflow was
    # billed at the home tariff, which is the cheapest assumption
    # available and the least likely to be true.
    assert t["cost"] == pytest.approx(3.375, abs=0.01)
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

    state = hass.states.get("sensor.test_ev_recent_trips")
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

    state = hass.states.get("sensor.test_ev_recent_charges")
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
    # v0.8.48 — tr_c (10 km at 15.0) used to win this. It no longer
    # qualifies: an efficiency record ranks a RATE, and over 10 km the
    # 1 %-of-SoC quantisation in the numerator is ~40 % of the trip's
    # energy. Only tr_b (50 km) clears `_TOPS_MIN_KM_FOR_RATE`.
    #
    # The old assertion encoded the defect: on the author's real data this
    # list was topped by 3 km hops reading 1.83 kWh/100 km, which no
    # electric car does. It ranked noise, and ranked it by how noisy it was.
    assert [t["distance_km"] for t in tops["top_efficiency"]] == [50.0]
    assert tops["top_efficiency"][0]["consumption_kwh_100km"] == 18.0
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
    today_evt = next(e for e in events if e.start == today.date())
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


async def test_capacity_calibration_never_exceeds_the_nameplate(
    hass: HomeAssistant,
) -> None:
    """v0.8.37 — the ceiling is the declared capacity, not 1.5× it.

    The old bound allowed the calibration to claim 123.75 kWh on a car
    sold with 82.5. That is not a sanity guard: no measurement error of
    that size is worth adopting, and every value above the nameplate is
    already known to be wrong, because a pack does not gain capacity
    with use.

    This is the exact case that broke the author's install: a pool
    median of 85.16 kWh against an 82.5 kWh nameplate, adopted whole,
    which inflated every SoC-derived trip energy by 3.2 % and went to
    ABRP as a battery better than new.
    """
    from custom_components.ev_trip_logger.storage import ChargeRecord

    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 82.5})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Six charges implying 85.164 kWh — the real pool, to the decimal.
    for _ in range(6):
        await coordinator.storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=2),
            ended_at=dt_util.now() - timedelta(hours=1),
            kwh=33.214, price_per_kwh=0.3, total_cost=9.96,
            soc_start=26.0, soc_end=65.0,   # 39 % Δ → 85.16 kWh
        ))
    await coordinator._async_refresh_battery_capacity()

    assert coordinator.battery_capacity == pytest.approx(82.5), (
        "clamped to the nameplate, not the 85.16 the pool asked for"
    )


async def test_capacity_calibration_below_the_nameplate_is_adopted(
    hass: HomeAssistant,
) -> None:
    """The ceiling must not flatten a genuinely degraded pack.

    The asymmetry is deliberate: capacity loss is a real thing to
    measure and report, capacity gain is not.
    """
    from custom_components.ev_trip_logger.storage import ChargeRecord

    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 82.5})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    for _ in range(6):
        await coordinator.storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=2),
            ended_at=dt_util.now() - timedelta(hours=1),
            kwh=37.5, price_per_kwh=0.3, total_cost=11.25,
            soc_start=20.0, soc_end=70.0,   # 50 % Δ → 75.0 kWh
        ))
    await coordinator._async_refresh_battery_capacity()

    assert coordinator.battery_capacity == pytest.approx(75.0)
    assert coordinator.battery_soh_pct == pytest.approx(90.91, abs=0.01)


async def test_soh_is_capped_at_100_but_the_overshoot_stays_visible(
    hass: HomeAssistant,
) -> None:
    """v0.8.36 — a pack never holds more than it did new.

    A calibration above the baseline is the estimator running high, not a
    battery that improved, and publishing it as health inverts the
    meaning: a route planner fed 103 % reads a pack 3 % BETTER than new
    and plans a longer range than the car has. That happened on the
    author's install — 85.16 kWh calibrated against an 82.5 kWh
    nameplate, pushed to ABRP as 103.23 % on every telemetry frame while
    wall-meter evidence bounded the real pack at 79.8-83.4 kWh.

    Capping is a floor under the damage, not a fix for the estimator, so
    the uncapped ratio has to survive somewhere: it is the single best
    signal that the calibration is wrong.
    """
    from custom_components.ev_trip_logger.storage import ChargeRecord

    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 82.5})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # v0.8.37 closed the route this defect originally took (a calibration
    # above the nameplate is now clamped), so the surviving one is a
    # cohort baseline BELOW the nameplate: "observed as-new capacity for
    # cars like yours" can sit under what the manufacturer printed, and
    # then a perfectly legal calibration still divides out above 100 %.
    coordinator._cohort_baseline_kwh = 78.0
    for _ in range(6):
        await coordinator.storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=2),
            ended_at=dt_util.now() - timedelta(hours=1),
            kwh=41.25, price_per_kwh=0.3, total_cost=12.38,
            soc_start=20.0, soc_end=70.0,   # 50 % Δ → 82.5 kWh
        ))
    await coordinator._async_refresh_battery_capacity()

    assert coordinator.battery_capacity == pytest.approx(82.5, abs=0.01), (
        "the capacity estimate itself is untouched by this change"
    )
    assert coordinator.battery_soh_pct == 100.0, "health cannot exceed as-new"
    assert coordinator.battery_soh_pct_raw == pytest.approx(105.77, abs=0.01), (
        "the overshoot is the diagnostic and must not be swallowed"
    )


async def test_soh_below_100_is_published_unchanged(
    hass: HomeAssistant,
) -> None:
    """The cap must not touch a real degradation reading.

    A one-sided clamp that also flattened genuine losses would hide the
    thing the sensor exists to report.
    """
    from custom_components.ev_trip_logger.storage import ChargeRecord

    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 100.0})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    for _ in range(6):
        await coordinator.storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=2),
            ended_at=dt_util.now() - timedelta(hours=1),
            kwh=54.0, price_per_kwh=0.2, total_cost=10.8,
            soc_start=10.0, soc_end=70.0,   # 90 kWh implied
        ))
    await coordinator._async_refresh_battery_capacity()

    assert coordinator.battery_soh_pct == pytest.approx(90.0)
    assert coordinator.battery_soh_pct_raw == pytest.approx(90.0)


async def test_soh_reads_100_before_any_calibration_exists(
    hass: HomeAssistant,
) -> None:
    """No calibration is "not measured yet", not "degraded"."""
    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 82.5})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.battery_soh_pct == 100.0
    assert coordinator.battery_soh_pct_raw is None, (
        "None, not 100: nothing has been measured to compare against"
    )


async def test_abrp_never_sends_a_soh_above_100(hass: HomeAssistant) -> None:
    """The payload is the whole reason the cap exists.

    ABRP holds its own as-new figure for the car model; a SoH above 100
    tells it this pack beats that, and it plans accordingly. This pins
    the field at its source rather than trusting the sensor to be the
    only consumer.
    """
    from custom_components.ev_trip_logger.storage import ChargeRecord

    entry = await _setup(hass, **{CONF_BATTERY_CAPACITY: 82.5})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator._cohort_baseline_kwh = 78.0
    for _ in range(6):
        await coordinator.storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=2),
            ended_at=dt_util.now() - timedelta(hours=1),
            kwh=41.25, price_per_kwh=0.3, total_cost=12.38,
            soc_start=20.0, soc_end=70.0,   # 50 % Δ → 82.5 kWh
        ))
    await coordinator._async_refresh_battery_capacity()
    assert coordinator.battery_soh_pct_raw > 100.0, "fixture must overshoot"

    sent: list[dict] = []

    class _CaptureClient:
        async def send(self, tlm: dict) -> bool:
            sent.append(tlm)
            return True

    # SoC is the one field the push refuses to send without.
    hass.states.async_set("sensor.fake_battery", "50.0")
    coordinator._battery = "sensor.fake_battery"
    coordinator._abrp = _CaptureClient()
    coordinator.abrp_push_enabled = True
    coordinator._abrp_last_send = 0.0
    coordinator._abrp_interval_s = 0
    await coordinator._async_maybe_send_abrp()

    assert sent, "no telemetry frame was built"
    assert sent[-1]["soh"] == 100.0
    assert sent[-1]["capacity"] == pytest.approx(82.5, abs=0.01), (
        "capacity is a separate defect, fixed separately; not silently "
        "changed by the SoH cap"
    )


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
    """v0.7.3 — `speed_stats` returns nearest-rank V95 + fraction
    of samples ≥ threshold. Empty/all-None → (None, None).
    """
    from custom_components.ev_trip_logger.calc import speed_stats

    # Trip 206-style samples: 30-tick deque (30 s cadence over ~15 min)
    # with mostly 40-60 km/h and a couple of highway bursts.
    # Kept as concatenation so each phase stays annotated on its own line.
    samples = (
        [0.0, 0.0, 0.0]            # 3 idle at lights  # noqa: RUF005
        + [40.0, 50.0, 55.0, 45.0] # urban
        + [70.0, 75.0]             # extra-urban
        + [95.0, 100.0, 105.0, 117.0, 110.0, 90.0]  # highway
        + [55.0, 45.0, 30.0]       # slowing to town
    )
    v95, highway = speed_stats(samples, highway_threshold_kmh=80.0)
    assert v95 is not None and 100.0 <= v95 <= 117.0
    # 6 samples ≥ 80 out of 18 → 33.3 %
    assert highway == pytest.approx(33.3, abs=0.1)

    # Empty deque → both None.
    assert speed_stats([], highway_threshold_kmh=80.0) == (None, None)
    # All zeros (car idle whole trip) → V95=0, highway=0.
    v95, highway = speed_stats([0.0] * 5, highway_threshold_kmh=80.0)
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
    from custom_components.ev_trip_logger.cohort import (
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

    base = datetime(2026, 6, 25, 18, 0, 0, tzinfo=UTC)
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

    t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
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


async def test_road_trip_second_dcfc_hop_within_2h_is_not_discarded(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — regression for the real 2026-08-19/20 data loss.

    Two DCFC stops 50 min apart with a drive in between (SoC falls from
    the first stop's end SoC before the second starts). The 2 h dedup
    used to `return` and delete the second session's kWh outright —
    ~150 kWh vanished across four days of a road trip. SoC dropping in
    between proves the car was driven, so the second stop must land as
    its own row.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        hass.states.async_set(CHG, STATE_OFF)
        entry = await _setup(hass, bat=20.0, **{CONF_CHARGE_SENSOR: CHG})
        coordinator = hass.data[DOMAIN][entry.entry_id]

        # --- stop #1: 20 % → 30 % (7.5 kWh at 75 kWh capacity)
        hass.states.async_set(CHG, STATE_ON)
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=10))
        hass.states.async_set(BAT, "30")
        await hass.async_block_till_done()
        hass.states.async_set(CHG, STATE_OFF)
        await hass.async_block_till_done()

        first = coordinator.last_charge
        assert first is not None
        assert first.kwh == pytest.approx(7.5)

        # --- drove 40 min: SoC falls below the first stop's end SoC
        frozen.tick(timedelta(minutes=40))
        hass.states.async_set(BAT, "25")
        await hass.async_block_till_done()

        # --- stop #2: 25 % → 60 % (26.25 kWh), 50 min after stop #1
        hass.states.async_set(CHG, STATE_ON)
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=15))
        hass.states.async_set(BAT, "60")
        await hass.async_block_till_done()
        hass.states.async_set(CHG, STATE_OFF)
        await hass.async_block_till_done()

        charges = await coordinator.storage.async_recent_charges(10)
        assert len(charges) == 2, "second DCFC hop was discarded"
        second = coordinator.last_charge
        assert second.charge_id != first.charge_id
        assert second.soc_start == 25.0
        assert second.soc_end == 60.0
        assert second.kwh == pytest.approx(26.25)


async def test_continuation_pulse_without_plug_proof_merges_not_drops(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — the 2026-08-17 case: one DCFC session the cloud split.

    `charging` flickered off for 2 min mid-session and back on, with no
    plug sensor to prove continuity. SoC never dropped, so it's the same
    session — the tail must be merged into the existing row instead of
    dropped on the floor.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        hass.states.async_set(CHG, STATE_OFF)
        entry = await _setup(hass, bat=40.0, **{CONF_CHARGE_SENSOR: CHG})
        coordinator = hass.data[DOMAIN][entry.entry_id]

        # --- pulse 1: 40 % → 55 % (11.25 kWh)
        hass.states.async_set(CHG, STATE_ON)
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=12))
        hass.states.async_set(BAT, "55")
        await hass.async_block_till_done()
        hass.states.async_set(CHG, STATE_OFF)
        await hass.async_block_till_done()

        first_id = coordinator.last_charge.charge_id
        assert coordinator.last_charge.kwh == pytest.approx(11.25)

        # --- 2 min gap (cloud dropout), then the session resumes
        frozen.tick(timedelta(minutes=2))
        hass.states.async_set(CHG, STATE_ON)
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=20))
        hass.states.async_set(BAT, "80")
        await hass.async_block_till_done()
        hass.states.async_set(CHG, STATE_OFF)
        await hass.async_block_till_done()

        charges = await coordinator.storage.async_recent_charges(10)
        assert len(charges) == 1, "continuation pulse fragmented the session"
        merged = coordinator.last_charge
        assert merged.charge_id == first_id
        # 40 % → 80 % of 75 kWh, accumulated across both pulses
        assert merged.kwh == pytest.approx(30.0)
        assert merged.soc_end == 80.0


async def test_vehicle_on_mid_charge_defers_trip_until_cable_stops(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — regression for the real 2026-08-19 trip/charge collision.

    Turning the car on during a DCFC stop (screens, AC, planning the next
    leg) used to force-close the charge and open a trip on the spot: the
    session lost everything after that moment, and the trip anchored
    `soc_start` to a SoC that was still climbing, ending with a negative
    `soc_used`. Nothing should open until the cable stops.
    """
    hass.states.async_set(CHG, STATE_OFF)
    entry = await _setup(hass, bat=20.0, **{CONF_CHARGE_SENSOR: CHG})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    assert coordinator.current_charge is not None

    # Driver sits in the car while it keeps charging.
    hass.states.async_set(BAT, "40")
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()

    assert coordinator.current is None, "trip opened while the cable was live"
    assert coordinator.current_charge is not None, "charge was truncated"

    # Charge runs to completion, then the cable stops.
    hass.states.async_set(BAT, "96")
    await hass.async_block_till_done()
    hass.states.async_set(CHG, STATE_OFF)
    await hass.async_block_till_done()

    charge = coordinator.last_charge
    assert charge is not None
    assert charge.soc_end == 96.0
    # 20 % → 96 % of 75 kWh, i.e. the full session, not a truncated slice
    assert charge.kwh == pytest.approx(57.0)

    # ...and the deferred trip opens now, anchored to the real end SoC.
    assert coordinator.current is not None
    assert coordinator.current.soc_start == 96.0


async def test_stuck_charge_sensor_does_not_block_a_moving_car(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — the defer must not strand a trip when `charging` sticks on.

    If the cloud leaves the charge sensor at 'on' after the cable is out,
    odometer movement is the ground truth: ≥1 km since the session opened
    means the car is driving and the trip has to open.
    """
    hass.states.async_set(CHG, STATE_OFF)
    entry = await _setup(hass, bat=50.0, **{CONF_CHARGE_SENSOR: CHG})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    assert coordinator.current_charge is not None
    assert coordinator.current_charge.odometer_at_start == 1000.0

    # Charge sensor never goes off, but the car covers ground.
    hass.states.async_set(ODO, "1004")
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()

    assert coordinator.current is not None, "trip stranded by a stuck sensor"


async def test_soc_rise_without_charge_suppresses_estimated_consumption(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — don't echo the rolling average back as a measurement.

    A trip whose SoC *rose* with no charge in its window (mountain regen,
    or SoC samples landing late after a coverage gap) has no usable
    energy measurement, so `energy_kwh` falls through to
    `distance × rolling-average kWh/100km`. Publishing a consumption from
    that just restates the average as if it had been measured — and then
    feeds the average it came from. The raw energy figure stays; the
    per-100 km number goes.
    """
    entry = await _setup(hass, bat=80.0)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Trip 1 — ordinary drive, seeds the rolling average.
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1050")
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    await _advance(hass, 4)
    assert coordinator.last_trip.consumption_kwh_100km is not None

    # Trip 2 — SoC climbs from 70 % to 74 % across the drive.
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1060")
    hass.states.async_set(BAT, "74")
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    await _advance(hass, 4)

    trip = coordinator.last_trip
    assert trip.soc_used_pct == pytest.approx(-4.0)
    assert trip.kwh_charged_during is None
    assert trip.energy_source == "estimated"
    assert trip.energy_kwh is not None, "raw energy estimate should survive"
    assert trip.consumption_kwh_100km is None, "fabricated kWh/100km published"
    assert trip.low_confidence is True


async def test_set_trip_recomputes_soc_used_and_consumption(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — a correction must not leave the row contradicting itself.

    `set_trip` is a raw column patch: fixing `soc_start` used to leave
    `soc_used_pct` quoting the old delta, and fixing `energy_kwh` left the
    kWh/100km built on the old figure. Both are derived from this row
    alone, so both get recomputed after the patch.
    """
    from custom_components.ev_trip_logger.const import SERVICE_SET_TRIP

    entry = await _setup(hass, bat=80.0)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1100")
    hass.states.async_set(BAT, "60")
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    await _advance(hass, 4)

    trip = coordinator.last_trip
    assert trip.soc_used_pct == pytest.approx(20.0)
    trip_id = trip.trip_id

    # The charge before this trip really ended at 95 %, not 80 %.
    await hass.services.async_call(
        DOMAIN, SERVICE_SET_TRIP,
        {"trip_id": trip_id, "soc_start": 95.0, "energy_kwh": 26.25},
        blocking=True,
    )

    fixed = (await coordinator.storage.async_recent_trips(1))[0]
    assert fixed.soc_start == 95.0
    assert fixed.soc_used_pct == pytest.approx(35.0), "stale soc_used_pct"
    # 26.25 kWh over 100 km
    assert fixed.consumption_kwh_100km == pytest.approx(26.25)


async def test_set_trip_normalises_timestamps_to_local(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — mixed UTC offsets broke the "most recent" ordering.

    Rows are ISO TEXT and every recency query orders by that text, so a
    row written with a '+00:00' stamp sorted as if it had happened
    `utcoffset` earlier and jumped ahead of trips that really preceded
    it. Whatever offset the caller passes, storage keeps local time.
    """
    entry = await _setup(hass, bat=80.0)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1020")
    hass.states.async_set(BAT, "75")
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    await _advance(hass, 4)

    trip_id = coordinator.last_trip.trip_id
    utc_stamp = datetime(2026, 8, 19, 11, 55, 50, tzinfo=UTC)
    await coordinator.storage.async_update_trip(
        trip_id, {"started_at": utc_stamp},
    )

    fixed = (await coordinator.storage.async_recent_trips(1))[0]
    assert fixed.started_at == utc_stamp, "the instant must not move"
    assert fixed.started_at.utcoffset() == dt_util.as_local(
        utc_stamp
    ).utcoffset(), "stored offset should be local, not UTC"


async def test_post_charge_anchor_absorbs_only_one_soc_step(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — parked drain must not be billed to the next drive.

    The post-charge anchor only ever RAISES `soc_start`, so every percent
    it adds becomes trip energy. With the old 2 % budget over a 12 h
    window, a car that sat overnight and lost 2 % to standby had that
    1.65 kWh charged to the next drive. Only one integer step is
    attributable to sensor staleness.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        entry = await _setup(hass, bat=80.0)
        coordinator = hass.data[DOMAIN][entry.entry_id]

        # A charge ends at 80 %.
        await hass.services.async_call(
            DOMAIN, SERVICE_LOG_CHARGE,
            {"kwh": 30.0, "price_per_kwh": 0.20},
            blocking=True,
        )
        assert coordinator.last_charge.soc_end == 80.0

        # Sits for an hour and loses 2 % to standby.
        frozen.tick(timedelta(hours=1))
        hass.states.async_set(BAT, "78")
        hass.states.async_set(ODO, "1001")  # fresh odometer for the open
        await hass.async_block_till_done()

        hass.states.async_set(VOK, STATE_ON)
        await hass.async_block_till_done()

        # 79, not 80: one step of staleness, the other point was drain.
        assert coordinator.current.soc_start == 79.0


async def test_post_charge_anchor_gives_up_after_a_long_park(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — past the quantization window, trust the live reading."""
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        entry = await _setup(hass, bat=80.0)
        coordinator = hass.data[DOMAIN][entry.entry_id]

        await hass.services.async_call(
            DOMAIN, SERVICE_LOG_CHARGE,
            {"kwh": 30.0, "price_per_kwh": 0.20},
            blocking=True,
        )

        frozen.tick(timedelta(hours=6))
        hass.states.async_set(BAT, "78")
        hass.states.async_set(ODO, "1001")  # fresh odometer for the open
        await hass.async_block_till_done()
        hass.states.async_set(VOK, STATE_ON)
        await hass.async_block_till_done()

        assert coordinator.current.soc_start == 78.0


async def test_charge_inside_trip_window_is_added_to_trip_energy(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — `kwh_charged_during` was measured and then ignored.

    A charge inside the trip's window has already partly refilled the
    battery, so the raw SoC delta understates what the car burned.
    """
    entry = await _setup(hass, bat=60.0)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    assert coordinator.current.soc_start == 60.0

    # 10 kWh top-up mid-leg.
    await hass.services.async_call(
        DOMAIN, SERVICE_LOG_CHARGE,
        {"kwh": 10.0, "price_per_kwh": 0.50},
        blocking=True,
    )

    hass.states.async_set(ODO, "1120")
    hass.states.async_set(BAT, "50")
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    await _advance(hass, 4)

    trip = coordinator.last_trip
    assert trip.kwh_charged_during == pytest.approx(10.0)
    # SoC delta alone says 10 % of 75 kWh = 7.5; the car really used 17.5
    assert trip.energy_kwh == pytest.approx(17.5)
    assert trip.energy_source == "soc_plus_charge"
    assert trip.consumption_kwh_100km == pytest.approx(14.6, abs=0.1)


async def test_zero_crossing_power_integral_is_exact_not_trapezoid(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — |P| has a kink at zero that a trapezoid cuts across.

    +40 kW followed by -30 kW eight minutes later: the true area under
    |P| is two triangles, (40²+30²)/(2·70)·dt = 2.380 kWh. A trapezoid
    over the magnitudes gives (40+30)/2·dt = 4.666 kWh — 1.96× too much,
    and that over-count fed `discharge_kwh`, the month/year discharge
    totals, and the trip's own energy whenever the SoC delta was under
    one integer step.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        hass.states.async_set(POW, "0")
        entry = await _setup(hass, **{CONF_POWER: POW})
        coordinator = hass.data[DOMAIN][entry.entry_id]

        hass.states.async_set(VOK, STATE_ON)
        await hass.async_block_till_done()
        hass.states.async_set(POW, "40")
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=8))
        hass.states.async_set(POW, "-30")
        await hass.async_block_till_done()

        active = coordinator.current
        # exact sub-areas, matching the maths regen already used
        assert active.energy_from_power_kwh == pytest.approx(2.381, abs=0.01)
        assert active.regen_kwh == pytest.approx(0.857, abs=0.01)
        # gross = discharge + regen must still close
        discharge = active.energy_from_power_kwh - active.regen_kwh
        assert discharge == pytest.approx(1.524, abs=0.01)


async def test_regen_trapezoid_is_clamped_like_the_gross_term(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 — the per-trapezoid ceiling guarded only the gross term.

    A cloud back-fill delivering two consecutive -100 kW samples 20 min
    apart added 33.3 kWh of regen to an 82.5 kWh pack — permanently, into
    the year totals — while the gross side was correctly capped at 5.
    """
    import freezegun

    with freezegun.freeze_time(dt_util.utcnow()) as frozen:
        hass.states.async_set(POW, "0")
        entry = await _setup(hass, **{CONF_POWER: POW})
        coordinator = hass.data[DOMAIN][entry.entry_id]

        hass.states.async_set(VOK, STATE_ON)
        await hass.async_block_till_done()
        hass.states.async_set(POW, "-100")
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=20))
        hass.states.async_set(POW, "-100.5")
        await hass.async_block_till_done()

        assert coordinator.current.regen_kwh <= 5.0


async def test_heal_history_service_is_wired(hass: HomeAssistant) -> None:
    """v0.8.17 — the new action must be callable through HA, not just as
    a storage method: schema, registration and coordinator plumbing.
    """
    from custom_components.ev_trip_logger.const import SERVICE_HEAL_HISTORY

    entry = await _setup(hass, bat=80.0)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1040")
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    await _advance(hass, 4)
    assert coordinator.last_trip is not None

    assert hass.services.has_service(DOMAIN, SERVICE_HEAL_HISTORY)
    await hass.services.async_call(
        DOMAIN, SERVICE_HEAL_HISTORY, {}, blocking=True,
    )

    # Idempotent: a second pass must not move anything.
    before = (await coordinator.storage.async_recent_trips(1))[0]
    await hass.services.async_call(
        DOMAIN, SERVICE_HEAL_HISTORY, {}, blocking=True,
    )
    after = (await coordinator.storage.async_recent_trips(1))[0]
    assert after.energy_kwh == before.energy_kwh
    assert after.soc_start == before.soc_start
    assert after.cost == before.cost


async def test_capacity_hint_is_published_before_the_startup_cost_heal(
    hass: HomeAssistant,
) -> None:
    """v0.8.17 regression — the WAC re-anchor must not be off at startup.

    The hint started out as a side effect of reading the
    `battery_capacity` property, and nothing reads it before the startup
    cost heal: that replay ran with no capacity, fell back to the old
    additive pool, and wrote costs the next replay overwrote with
    different numbers — so trip costs moved after every restart.
    """
    entry = await _setup(hass, bat=80.0)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert coordinator.storage.capacity_hint_kwh == pytest.approx(75.0)

    # And a replay is stable across repeats now that it is set.
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1060")
    hass.states.async_set(BAT, "65")
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    await _advance(hass, 4)

    first = (await coordinator.storage.async_recent_trips(1))[0].cost
    await coordinator.storage.async_recompute_trip_costs_from_charges(
        default_price=coordinator._current_energy_price(),
    )
    second = (await coordinator.storage.async_recent_trips(1))[0].cost
    assert second == pytest.approx(first)


EVSE_POW = "sensor.wallbox_power"


async def test_auto_evse_backfill_scheduled_when_live_integral_empty(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.8.19 — a home charge that closed with no live AC integral gets its
    EVSE window replayed from the recorder, shortly after the close.

    This is the common case, not an edge one: the charge sensor is
    cloud-polled, so `current_charge` frequently doesn't exist while the
    wallbox is delivering and `evse_energy_kwh` stayed NULL on every row.
    """
    hass.states.async_set(CHG, STATE_OFF)
    hass.states.async_set(EVSE_POW, "0", {"unit_of_measurement": "kW"})
    entry = await _setup(
        hass, **{CONF_CHARGE_SENSOR: CHG, CONF_EVSE_POWER_SENSOR: EVSE_POW},
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    calls: list[int] = []

    async def _fake_backfill(*, charge_id: int, **_kw):
        calls.append(charge_id)
        return

    monkeypatch.setattr(
        coordinator, "async_backfill_charge_evse_service", _fake_backfill,
    )

    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(BAT, "90")
    await hass.async_block_till_done()
    hass.states.async_set(CHG, STATE_OFF)
    await hass.async_block_till_done()

    charge_id = coordinator.last_charge.charge_id
    assert charge_id is not None
    # Deferred, so the recorder has time to commit the tail of the session.
    assert calls == []

    await _advance(hass, 1)
    assert calls == [charge_id]


async def test_no_auto_evse_backfill_without_a_wallbox_sensor(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `evse_power_sensor` configured → nothing to replay, no call."""
    hass.states.async_set(CHG, STATE_OFF)
    entry = await _setup(hass, **{CONF_CHARGE_SENSOR: CHG})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    calls: list[int] = []

    async def _fake_backfill(*, charge_id: int, **_kw):
        calls.append(charge_id)
        return

    monkeypatch.setattr(
        coordinator, "async_backfill_charge_evse_service", _fake_backfill,
    )

    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(BAT, "90")
    await hass.async_block_till_done()
    hass.states.async_set(CHG, STATE_OFF)
    await hass.async_block_till_done()

    await _advance(hass, 1)
    assert calls == []


async def test_pending_evse_backfill_cancelled_on_stop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.8.19 — an options change reloads the entry; a replay still in the
    queue must not fire against the stopped coordinator's storage.
    """
    hass.states.async_set(CHG, STATE_OFF)
    hass.states.async_set(EVSE_POW, "0", {"unit_of_measurement": "kW"})
    entry = await _setup(
        hass, **{CONF_CHARGE_SENSOR: CHG, CONF_EVSE_POWER_SENSOR: EVSE_POW},
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    calls: list[int] = []

    async def _fake_backfill(*, charge_id: int, **_kw):
        calls.append(charge_id)
        return

    monkeypatch.setattr(
        coordinator, "async_backfill_charge_evse_service", _fake_backfill,
    )

    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(BAT, "90")
    await hass.async_block_till_done()
    hass.states.async_set(CHG, STATE_OFF)
    await hass.async_block_till_done()

    assert coordinator._pending_evse_backfills
    await coordinator.async_stop()
    assert not coordinator._pending_evse_backfills

    await _advance(hass, 1)
    assert calls == []


async def test_odometer_catchup_does_not_release_the_charge_guard(
    hass: HomeAssistant,
) -> None:
    """v0.8.23 — the real 2026-08-23 corruption.

    v0.8.17 defers opening a trip while the charge session is still
    delivering, with an escape hatch: if the odometer has moved >=1 km
    since the session opened, the charge sensor must be stuck 'on' and the
    trip has to open anyway.

    That hatch fired for the wrong reason. Polling had been paused for
    4h31m; when it resumed at 16:47:08 the odometer caught up 46 km, the
    charge was detected, and `vehicle_on` went high — all inside the same
    second. The hatch saw "+46 km since the session opened" and released
    the guard, so a trip opened while the car was drawing 87 kW at a DC
    charger. That trip then swallowed the charge and reported 197 km at
    20.0 kWh/100km instead of 24.8.

    46 km in under a second is not movement, it is a stale reading landing.
    Only a delta the car could plausibly have covered in the elapsed time
    counts as movement.
    """
    hass.states.async_set(CHG, STATE_OFF)
    hass.states.async_set(ODO, "31028")
    hass.states.async_set(POW, "0")
    entry = await _setup(hass, **{CONF_CHARGE_SENSOR: CHG, CONF_POWER: POW})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    assert coordinator.current_charge is not None
    coordinator.current_charge.odometer_at_start = 31028.0

    # 87 kW going INTO the battery (negative = charging after the sign
    # normalisation), and the odometer catches up 46 km at the same time.
    hass.states.async_set(POW, "-87")
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "31074")
    await hass.async_block_till_done()
    assert coordinator._charge_still_delivering() is True, (
        "a car taking 87 kW is not driving away from the charger"
    )

    # Cable out, sensor stuck 'on', car actually rolling: the hatch is for
    # exactly this and must still fire.
    hass.states.async_set(POW, "12")     # discharging
    await hass.async_block_till_done()
    assert coordinator._charge_still_delivering() is False


async def test_vehicle_heal_rejects_an_implausible_vehicle_energy(
    hass: HomeAssistant,
) -> None:
    """v0.8.26 — the vehicle-native heal had no plausibility check.

    Real case, 2026-08-23: a 197 km motorway leg ran 65 % -> 6 % of an
    82.68 kWh pack, i.e. 48.8 kWh, and the driver's own figure was
    24.8 kWh/100km. The car's `last_trip_energy` sensor read 22.27 kWh —
    11.3 kWh/100km, physically impossible for that drive. The heal's two
    guards (sensor newer than the trip, distance within tolerance) both
    passed, so it overrode the SoC-derived energy with the bad number, and
    a reload re-applied it to a SECOND trip whose 185 km was inside the
    12 km distance tolerance.

    SoC is the cross-check the guards were missing: it comes from the
    physical pack and cannot be off by half.
    """
    hass.states.async_set(ODO, "1000")
    hass.states.async_set(BAT, "65")
    entry = await _setup(hass, bat=65.0, **{
        CONF_LAST_TRIP_ENERGY_SENSOR: "sensor.car_last_trip_energy",
    })
    coordinator = hass.data[DOMAIN][entry.entry_id]

    from custom_components.ev_trip_logger.storage import TripRecord

    trip_id = await coordinator.storage.async_insert(TripRecord(
        started_at=dt_util.now() - timedelta(hours=2),
        ended_at=dt_util.now() - timedelta(minutes=30),
        duration_min=90.0, distance_km=197.0,
        soc_start=65.0, soc_end=6.0, soc_used_pct=59.0,
        energy_kwh=48.78, energy_source="soc",
        consumption_kwh_100km=24.8,
    ))

    # The car claims less than half of what the battery says it used.
    hass.states.async_set("sensor.car_last_trip_energy", "22.27")
    await hass.async_block_till_done()
    await coordinator._async_heal_from_vehicle(trip_id)

    t = await coordinator.storage.async_get_trip_by_id(trip_id)
    assert t.energy_kwh == pytest.approx(48.78), "SoC must win over a bad sensor"
    assert t.energy_source == "soc"

    # A vehicle figure that AGREES with SoC is still adopted: it is the
    # more precise measurement, which is why the heal exists.
    hass.states.async_set("sensor.car_last_trip_energy", "47.10")
    await hass.async_block_till_done()
    await coordinator._async_heal_from_vehicle(trip_id)

    t = await coordinator.storage.async_get_trip_by_id(trip_id)
    assert t.energy_kwh == pytest.approx(47.10)
    assert t.energy_source == "vehicle"


async def test_charge_attrs_expose_rate_and_context(hass: HomeAssistant) -> None:
    """v0.8.32 — the charges tab needs the rate and the reasons for it.

    `peak_charge_power_kw` and `temperature_c` had been persisted since
    v0.6.0 and v0.6.5 but were never serialised, so no dashboard could
    read them. Duration and average power are derived here instead of
    stored, being pure functions of the two timestamps and kwh.
    """
    from custom_components.ev_trip_logger.sensor import _charge_to_attr
    from custom_components.ev_trip_logger.storage import ChargeRecord

    started = dt_util.now() - timedelta(minutes=21)
    rec = ChargeRecord(
        started_at=started, ended_at=started + timedelta(minutes=21),
        kwh=13.23, price_per_kwh=0.3696, total_cost=4.89,
        soc_start=37.0, soc_end=54.0, is_dcfc=True,
        peak_charge_power_kw=40.37, temperature_c=28.4,
        evse_energy_kwh=16.3, charging_efficiency_pct=81.2,
    )
    rec.km_before = 197.0
    rec.min_before = 143.0
    attr = _charge_to_attr(rec)

    assert attr["duration_min"] == pytest.approx(21.0)
    # 13.23 kWh over 21 min = 37.8 kW.
    assert attr["avg_power_kw"] == pytest.approx(37.8, abs=0.1)
    assert attr["peak_charge_power_kw"] == pytest.approx(40.4)
    assert attr["temperature_c"] == pytest.approx(28.4)
    assert attr["km_before"] == pytest.approx(197.0)
    assert attr["min_before"] == pytest.approx(143.0)


async def test_charge_attrs_survive_a_charge_with_no_start_time(
    hass: HomeAssistant,
) -> None:
    """`log_charge` can write a row with no `started_at`.

    Duration and average power must come back None rather than raise or
    report zero — a rate of 0 kW would read as "the charger delivered
    nothing", which is a different claim from "we do not know how long
    it took". `km_before` is None for the same reason: it is only
    populated on the `recent_charges` path.
    """
    from custom_components.ev_trip_logger.sensor import _charge_to_attr
    from custom_components.ev_trip_logger.storage import ChargeRecord

    attr = _charge_to_attr(ChargeRecord(
        ended_at=dt_util.now(), kwh=20.0,
        price_per_kwh=0.2, total_cost=4.0,
    ))
    assert attr["duration_min"] is None
    assert attr["avg_power_kw"] is None
    assert attr["peak_charge_power_kw"] is None
    assert attr["km_before"] is None
    assert attr["min_before"] is None
    assert attr["kwh"] == pytest.approx(20.0)


async def test_set_charger_power_service_is_wired(hass: HomeAssistant) -> None:
    """v0.8.33 — the rating reaches storage through the public service.

    Passing it alone, with no pricing field, has to be accepted: that is
    the whole use case — the charge auto-logged days ago and you are
    filling in what the unit said.
    """
    from custom_components.ev_trip_logger.storage import ChargeRecord

    entry = await _setup(hass)
    coord = hass.data[DOMAIN][entry.entry_id]
    cid = await coord.storage.async_insert_charge(ChargeRecord(
        ended_at=dt_util.now(), kwh=42.23, price_per_kwh=0.30,
        total_cost=12.67, peak_charge_power_kw=41.0, is_dcfc=True,
    ))

    await hass.services.async_call(
        DOMAIN, "set_last_charge_price",
        {"charge_id": cid, "charger_power_kw": 150},
        blocking=True,
    )
    await hass.async_block_till_done()

    got = await coord.storage.async_get_charge_by_id(cid)
    assert got.charger_power_kw == pytest.approx(150.0)
    assert got.total_cost == pytest.approx(12.67), "no pricing field was passed"


async def test_charge_attrs_expose_the_charger_rating(hass: HomeAssistant) -> None:
    """It has to reach the dashboard, or the verdict cannot be rendered."""
    from custom_components.ev_trip_logger.sensor import _charge_to_attr
    from custom_components.ev_trip_logger.storage import ChargeRecord

    attr = _charge_to_attr(ChargeRecord(
        ended_at=dt_util.now(), kwh=40.0, price_per_kwh=0.3, total_cost=12.0,
        peak_charge_power_kw=41.0, charger_power_kw=150.0,
    ))
    assert attr["charger_power_kw"] == pytest.approx(150.0)
    assert attr["peak_charge_power_kw"] == pytest.approx(41.0)

    # Absent stays absent rather than becoming zero: "not recorded" and
    # "a 0 kW charger" must not render the same.
    bare = _charge_to_attr(ChargeRecord(
        ended_at=dt_util.now(), kwh=40.0, price_per_kwh=0.3, total_cost=12.0,
    ))
    assert bare["charger_power_kw"] is None
