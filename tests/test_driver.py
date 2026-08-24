"""Tests for v0.5.43 driver capture and the zero-reading close fix."""
from __future__ import annotations

from datetime import timedelta, UTC

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ev_trip_logger.const import (
    CONF_BATTERY,
    CONF_BATTERY_CAPACITY,
    CONF_DRIVER_SENSOR,
    CONF_IDLE_TIMEOUT,
    CONF_MIN_TRIP_DISTANCE,
    CONF_NAME,
    CONF_ODOMETER,
    CONF_VEHICLE_ON,
    DOMAIN,
)

ODO = "sensor.odometer"
BAT = "sensor.battery"
VOK = "binary_sensor.vehicle_on"
DRV = "sensor.bt_connected_device"


async def _setup(hass: HomeAssistant, **overrides) -> MockConfigEntry:
    hass.states.async_set(ODO, str(overrides.pop("odo", 1000.0)))
    hass.states.async_set(BAT, str(overrides.pop("bat", 80.0)))
    hass.states.async_set(VOK, STATE_ON if overrides.pop("on", False) else STATE_OFF)
    data = {
        CONF_NAME: "Test EV",
        CONF_ODOMETER: ODO,
        CONF_BATTERY: BAT,
        CONF_VEHICLE_ON: VOK,
        CONF_BATTERY_CAPACITY: 75.0,
        CONF_MIN_TRIP_DISTANCE: 0.5,
        CONF_IDLE_TIMEOUT: 1,
        CONF_DRIVER_SENSOR: DRV,
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


async def _drive(hass: HomeAssistant, *, odo_end: str, bat_end: str) -> None:
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, odo_end)
    hass.states.async_set(BAT, bat_end)
    await hass.async_block_till_done()
    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    # v0.5.53 — vehicle-off grace is 180 s, advance past it so the
    # debounced close actually fires before assertions run.
    await _advance(hass, 4)


async def test_driver_captured_at_trip_open(hass: HomeAssistant) -> None:
    """Driver sensor state at ignition is persisted on the trip record."""
    hass.states.async_set(DRV, "Rafa iPhone")
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await _drive(hass, odo_end="1015", bat_end="70")

    assert coordinator.last_trip is not None
    assert coordinator.last_trip.driver == "Rafa iPhone"

    stored = await coordinator.storage.async_get_last()
    assert stored is not None
    assert stored.driver == "Rafa iPhone"


async def test_driver_none_states_ignored(hass: HomeAssistant) -> None:
    """'not_connected'-style states mean nobody identified — driver stays NULL."""
    hass.states.async_set(DRV, "not_connected")
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await _drive(hass, odo_end="1015", bat_end="70")

    assert coordinator.last_trip is not None
    assert coordinator.last_trip.driver is None


async def test_driver_resolved_late_via_close_read(hass: HomeAssistant) -> None:
    """BT pairs after ignition: unknown at open, resolved by trip close."""
    hass.states.async_set(DRV, "not_connected")
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    assert coordinator.current is not None
    assert coordinator.current.driver is None

    # Phone connects mid-trip.
    hass.states.async_set(DRV, "Maria Pixel")
    hass.states.async_set(ODO, "1015")
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()

    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    await _advance(hass, 4)

    assert coordinator.last_trip is not None
    assert coordinator.last_trip.driver == "Maria Pixel"


async def test_driver_dominant_wins_over_bt_race(hass: HomeAssistant) -> None:
    """v0.5.82 — when two drivers appear during a trip (passenger phone
    paired at ignition for 30 s, real driver's phone takes over for
    the remaining minutes), the LONGER-RUNNING value wins. This is
    the BT-race-at-open scenario that v0.5.43's 'first non-empty
    wins' got wrong.
    """
    # Open: passenger Elena's phone is already paired.
    hass.states.async_set(DRV, "Elena Pixel")
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    # The "first non-empty wins" world locks in Elena here.
    assert coordinator.current is not None
    assert coordinator.current.driver == "Elena Pixel"

    # 30 s later Rafa's phone takes over and Elena's drops.
    hass.states.async_set(DRV, "Rafa iPhone")
    hass.states.async_set(ODO, "1015")
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()
    # Rafa stays connected for the rest of the trip — advance time so
    # the live-tick accumulates Rafa-seconds on the dominant bucket.
    await _advance(hass, 5)

    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    await _advance(hass, 4)

    # Dominant resolution: Rafa held the sensor for ~5+ min vs Elena's
    # initial blip → Rafa wins on the persisted row even though Elena
    # was the open-time read.
    assert coordinator.last_trip is not None
    assert coordinator.last_trip.driver == "Rafa iPhone"


async def test_driver_stats_groups_by_driver(hass: HomeAssistant) -> None:
    """async_driver_stats aggregates km/hours per driver with unknown bucket."""
    hass.states.async_set(DRV, "Rafa iPhone")
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await _drive(hass, odo_end="1015", bat_end="70")
    hass.states.async_set(DRV, "Maria Pixel")
    await _drive(hass, odo_end="1035", bat_end="60")

    rows = await coordinator.storage.async_driver_stats(
        dt_util.now() - timedelta(days=1)
    )
    by_driver = {r["driver"]: r for r in rows}
    assert by_driver["Rafa iPhone"]["distance_km"] == pytest.approx(15.0)
    assert by_driver["Rafa iPhone"]["trips"] == 1
    assert by_driver["Maria Pixel"]["distance_km"] == pytest.approx(20.0)


async def test_zero_soc_at_close_is_not_discarded(hass: HomeAssistant) -> None:
    """A legitimate 0 % SoC reading at close must not fall back to stale data."""
    entry = await _setup(hass, bat=10.0)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(DRV, "Rafa iPhone")
    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()

    hass.states.async_set(ODO, "1060")
    hass.states.async_set(BAT, "0")
    await hass.async_block_till_done()

    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    await _advance(hass, 4)

    assert coordinator.last_trip is not None
    assert coordinator.last_trip.soc_end == pytest.approx(0.0)
    assert coordinator.last_trip.soc_used_pct == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# v0.5.44 — driver resolution for reconstructed trips + zone-from-coords
# ---------------------------------------------------------------------------

def _dt(h: int, m: int = 0):
    from datetime import datetime
    return datetime(2026, 6, 11, h, m, tzinfo=UTC)


def test_pick_driver_longest_overlap_wins() -> None:
    from custom_components.ev_trip_logger.coordinator import _pick_driver_for_window
    timeline = [
        (_dt(5, 0), "none"),
        (_dt(5, 43), "Elena"),
        (_dt(6, 5), "Rafa"),
        (_dt(6, 10), "none"),
    ]
    # Window 05:52–06:14: Elena 13 min, Rafa 5 min.
    assert _pick_driver_for_window(timeline, _dt(5, 52), _dt(6, 14)) == "Elena"


def test_pick_driver_state_active_at_window_start() -> None:
    from custom_components.ev_trip_logger.coordinator import _pick_driver_for_window
    # Single change before the window — still drives the whole window.
    timeline = [(_dt(5, 0), "Rafa")]
    assert _pick_driver_for_window(timeline, _dt(6, 0), _dt(6, 30)) == "Rafa"


def test_pick_driver_catches_pre_trip_flicker_with_widened_window() -> None:
    """v0.5.97 — when AA/BT pairs briefly BEFORE the trip starts and
    drops before ignition, the in-trip overlap is 0 but the wider
    pre-window the recorder fallback uses still picks up the identity.

    Trip window: 17:25–17:45 (the trip-191 case). Sensor sequence:
    Rafa on at 17:20:19, off at 17:20:39 — entirely BEFORE the trip.
    With the narrow [trip-start, trip-end] window the picker returns
    None. With the widened [start-5min, end+2min] used by v0.5.97,
    Rafa wins because his only valid segment overlaps the widened
    window.
    """
    from custom_components.ev_trip_logger.coordinator import (
        _pick_driver_for_window,
    )
    from datetime import timedelta as _td
    trip_start = _dt(17, 25)
    trip_end = _dt(17, 45)
    # Single connect-then-disconnect 5 min before trip start.
    timeline = [
        (_dt(17, 0), "none"),
        (_dt(17, 20), "Rafa"),
        (trip_start - _td(minutes=4, seconds=30), "none"),
    ]
    # Narrow window — what the old code did. Misses Rafa entirely.
    assert _pick_driver_for_window(timeline, trip_start, trip_end) is None
    # Widened window — what _async_driver_during now passes after
    # extending the recorder query by 5 / 2 min on each side. Rafa
    # held the sensor for ~5 min inside the widened window.
    assert _pick_driver_for_window(
        timeline,
        trip_start - _td(minutes=5),
        trip_end + _td(minutes=2),
    ) == "Rafa"


def test_pick_driver_ignores_none_and_invalid_states() -> None:
    from custom_components.ev_trip_logger.coordinator import _pick_driver_for_window
    timeline = [
        (_dt(5, 0), "none"),
        (_dt(5, 30), "unavailable"),
        (_dt(5, 45), "not_connected"),
    ]
    assert _pick_driver_for_window(timeline, _dt(5, 0), _dt(6, 0)) is None
    assert _pick_driver_for_window([], _dt(5, 0), _dt(6, 0)) is None


async def test_zone_from_coords_resolves_stale_tracker(hass: HomeAssistant) -> None:
    """GPS endpoint inside zone.home resolves to 'home' even when the
    tracker state is a stale 'not_home' (paused cloud polling)."""
    from homeassistant.setup import async_setup_component

    # Real zone component: async_active_zone reads its internal registry,
    # not manually-injected states. zone.home derives from core config.
    assert await async_setup_component(
        hass,
        "zone",
        {
            "zone": [
                {
                    "name": "Trabajo ele",
                    "latitude": 37.20,
                    "longitude": -3.70,
                    "radius": 100,
                }
            ]
        },
    )
    await hass.async_block_till_done()

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    home_lat = hass.config.latitude
    home_lon = hass.config.longitude
    assert coordinator._zone_from_coords(home_lat, home_lon) == "home"
    assert coordinator._zone_from_coords(37.2000, -3.7000) == "Trabajo ele"
    assert coordinator._zone_from_coords(36.0, -4.0) is None
    assert coordinator._zone_from_coords(None, -3.6) is None


def test_is_zoneless() -> None:
    from custom_components.ev_trip_logger.coordinator import _is_zoneless
    assert _is_zoneless("not_home")
    assert _is_zoneless("Unknown")
    assert _is_zoneless(None)
    assert _is_zoneless("  ")
    assert not _is_zoneless("home")
    assert not _is_zoneless("Trabajo ele ")


async def test_charge_merge_is_conservative_without_plug_continuity(
    hass: HomeAssistant,
) -> None:
    """v0.5.45 — without recorder proof that the cable stayed plugged,
    a new charging session inserts its OWN row instead of merging into
    (and corrupting) the previous one."""
    from homeassistant.util import dt as dt_util
    from custom_components.ev_trip_logger.const import (
        CONF_CHARGE_SENSOR,
        CONF_PLUG_SENSOR,
    )
    from custom_components.ev_trip_logger.storage import ChargeRecord

    CHG = "binary_sensor.charging"
    PLUG = "binary_sensor.plug"
    hass.states.async_set(CHG, STATE_OFF)
    hass.states.async_set(PLUG, STATE_ON)
    entry = await _setup(
        hass, **{CONF_CHARGE_SENSOR: CHG, CONF_PLUG_SENSOR: PLUG}
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Previous charge ended 3 h ago at 80 %; battery now 60 (car drove).
    old = ChargeRecord(
        started_at=dt_util.now() - timedelta(hours=8),
        ended_at=dt_util.now() - timedelta(hours=3),
        kwh=20.0, price_per_kwh=0.07, total_cost=1.4,
        soc_start=50.0, soc_end=80.0,
    )
    old.charge_id = await coordinator.storage.async_insert_charge(old)
    coordinator.last_charge = old

    hass.states.async_set(BAT, "60")
    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()
    hass.states.async_set(CHG, STATE_OFF)
    await hass.async_block_till_done()

    charges = await coordinator.storage.async_recent_charges(5)
    assert len(charges) == 2  # new row, NOT merged
    newest = charges[0]
    assert newest.charge_id != old.charge_id
    assert newest.soc_end == pytest.approx(70.0)
    # The old row is untouched.
    untouched = next(c for c in charges if c.charge_id == old.charge_id)
    assert untouched.kwh == pytest.approx(20.0)
    assert untouched.soc_end == pytest.approx(80.0)


async def test_charge_pulse_with_proven_continuity_merges(
    hass: HomeAssistant,
) -> None:
    """v0.5.47 — a second pulse within 2 h of the session start used to
    be DROPPED by the time dedup before the merge could run. With plug
    continuity proven, it must now merge into the previous row."""
    from homeassistant.util import dt as dt_util
    from custom_components.ev_trip_logger.const import (
        CONF_CHARGE_SENSOR,
        CONF_PLUG_SENSOR,
    )
    from custom_components.ev_trip_logger.storage import ChargeRecord

    CHG = "binary_sensor.charging"
    PLUG = "binary_sensor.plug"
    hass.states.async_set(CHG, STATE_OFF)
    hass.states.async_set(PLUG, STATE_ON)
    entry = await _setup(
        hass, bat=70.0, **{CONF_CHARGE_SENSOR: CHG, CONF_PLUG_SENSOR: PLUG}
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Session started 1 h ago, first pulse closed 30 min ago at 70 %.
    old = ChargeRecord(
        started_at=dt_util.now() - timedelta(hours=1),
        ended_at=dt_util.now() - timedelta(minutes=30),
        kwh=10.0, price_per_kwh=0.07, total_cost=0.7,
        soc_start=60.0, soc_end=70.0,
    )
    old.charge_id = await coordinator.storage.async_insert_charge(old)
    coordinator.last_charge = old

    # Recorder isn't available in tests — prove continuity directly.
    async def _stayed_connected(_since):
        return True
    coordinator._async_plug_stayed_connected_since = _stayed_connected

    # Balancing pulse 30 min later: 70 -> 75 %.
    hass.states.async_set(CHG, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(BAT, "75")
    await hass.async_block_till_done()
    hass.states.async_set(CHG, STATE_OFF)
    await hass.async_block_till_done()

    charges = await coordinator.storage.async_recent_charges(5)
    assert len(charges) == 1  # merged, not dropped, not a new row
    merged = charges[0]
    assert merged.charge_id == old.charge_id
    assert merged.kwh == pytest.approx(10.0 + 5.0 / 100 * 75.0)  # +3.75
    assert merged.soc_end == pytest.approx(75.0)  # absolute, not 70+5+...
