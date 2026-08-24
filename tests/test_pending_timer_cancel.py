"""Every deferred writer must be cancellable on unload (v0.8.27).

Three timers in the coordinator fire well after the event that armed them
and then write through `self.storage`. An options change reloads the entry,
so a timer that survives unload writes through a stopped coordinator. Two
of the three had already been fixed this way — `_cancel_pending_open`
(v0.5.49) and `_cancel_pending_evse_backfills` (v0.8.19); these are the
remaining ones.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ev_trip_logger.const import (
    CONF_LAST_TRIP_ENERGY_SENSOR,
    CONF_LOCATION,
    DOMAIN,
)

from .test_coordinator import BAT, LOC, ODO, VOK, _run_stage, _setup


async def test_pending_dwell_check_is_cancelled_on_unload(
    hass: HomeAssistant,
) -> None:
    """A late-zone-arrival amend must not fire after the entry unloads.

    The dwell guard holds the amend back for `_LOCATION_DWELL_MIN_S` (60 s)
    to filter GPS flaps. Reloading inside that window used to leave the
    check running: it was a bare `asyncio.sleep` in an untracked task, so
    nothing could cancel it and it amended the trip through a stopped
    coordinator's storage.
    """
    hass.states.async_set(LOC, "home")
    entry = await _setup(hass, **{CONF_LOCATION: LOC})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await _run_stage(
        hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="not_home"
    )
    assert coordinator.last_trip.destination == "not_home"

    # Geofence catches up: the dwell timer is armed but has not fired.
    hass.states.async_set(LOC, "home")
    await hass.async_block_till_done()
    assert coordinator._pending_dwell_unsub is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coordinator._pending_dwell_unsub is None

    # Past the dwell window the amend must stay unfired.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
    await hass.async_block_till_done()
    assert coordinator.last_trip.destination == "not_home"


async def test_newer_zone_arrival_replaces_the_pending_dwell_check(
    hass: HomeAssistant,
) -> None:
    """Two arrivals inside the window leave ONE pending check, the latest.

    The old bare-`sleep` version armed a parallel task per arrival, so a
    tracker walking home → work → home ran three amends against three
    stale `when` timestamps.
    """
    hass.states.async_set(LOC, "home")
    entry = await _setup(hass, **{CONF_LOCATION: LOC})
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await _run_stage(
        hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="not_home"
    )

    hass.states.async_set(LOC, "work")
    await hass.async_block_till_done()
    first = coordinator._pending_dwell_unsub
    assert first is not None

    hass.states.async_set(LOC, "home")
    await hass.async_block_till_done()
    assert coordinator._pending_dwell_unsub is not None
    assert coordinator._pending_dwell_unsub is not first

    # Only the last reading is amended to — 'work' never lands.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
    await hass.async_block_till_done()
    assert coordinator.last_trip.destination == "home"


async def test_pending_vehicle_heal_is_cancelled_on_unload(
    hass: HomeAssistant,
) -> None:
    """The vehicle-native energy heal must not fire after unload.

    `_schedule_vehicle_heal` waits `_VEHICLE_TRIP_HEAL_DELAY_S` (240 s) for
    the car's cloud integration to publish `last_trip_energy`, then rewrites
    the row. Its cancel handle used to be discarded outright.
    """
    energy_sensor = "sensor.vehicle_last_trip_energy"
    hass.states.async_set(energy_sensor, "12.0")
    entry = await _setup(
        hass, **{CONF_LAST_TRIP_ENERGY_SENSOR: energy_sensor}
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await _run_stage(
        hass, odo_start=1000, odo_end=1020, soc_end=75, location_end="not_home"
    )
    assert coordinator._pending_vehicle_heals

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not coordinator._pending_vehicle_heals


async def test_pending_close_debounce_is_cancelled_on_unload(
    hass: HomeAssistant,
) -> None:
    """The off-edge close debounce must not fire after unload.

    `vehicle_on` off arms a `_VEHICLE_OFF_GRACE_S` timer so a flap can
    reclaim the trip. The handle was kept but `async_stop` never dropped
    it, so an unload inside the grace window closed a trip — insert
    included — through a stopped coordinator's storage.
    """
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set(VOK, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "1020")
    hass.states.async_set(BAT, "70")
    await hass.async_block_till_done()
    assert coordinator.current is not None

    hass.states.async_set(VOK, STATE_OFF)
    await hass.async_block_till_done()
    assert coordinator._pending_close_unsub is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coordinator._pending_close_unsub is None
