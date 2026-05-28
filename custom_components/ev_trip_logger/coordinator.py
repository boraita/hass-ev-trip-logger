"""Trip detection state machine."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY,
    CONF_BATTERY_CAPACITY,
    CONF_CURRENCY,
    CONF_ENERGY_PRICE,
    CONF_IDLE_TIMEOUT,
    CONF_LOCATION,
    CONF_MIN_TRIP_DISTANCE,
    CONF_ODOMETER,
    CONF_POWER,
    CONF_RANGE,
    CONF_TEMP,
    CONF_VEHICLE_ON,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CURRENCY,
    DEFAULT_ENERGY_PRICE,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MIN_TRIP_DISTANCE,
    EVENT_TRIP_ENDED,
    EVENT_TRIP_STARTED,
)
from .storage import TripRecord, TripStorage

_LOGGER = logging.getLogger(__name__)

_INVALID_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""}


@dataclass
class TripInProgress:
    """In-memory accumulator for an active trip."""

    started_at: datetime
    odometer_start: float | None
    soc_start: float | None
    location_start: str | None
    temp_samples: list[float] = field(default_factory=list)
    max_power: float = 0.0
    last_seen_odometer: float | None = None
    last_seen_soc: float | None = None


class EvTripLoggerCoordinator:
    """Tracks vehicle_on transitions and produces trip records."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        storage: TripStorage,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.storage = storage

        merged = {**entry.data, **entry.options}
        self._odometer = merged[CONF_ODOMETER]
        self._battery = merged[CONF_BATTERY]
        self._vehicle_on = merged[CONF_VEHICLE_ON]
        self._power = merged.get(CONF_POWER)
        self._range = merged.get(CONF_RANGE)
        self._location = merged.get(CONF_LOCATION)
        self._temp = merged.get(CONF_TEMP)

        self._battery_capacity = float(
            merged.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
        )
        self._min_distance = float(
            merged.get(CONF_MIN_TRIP_DISTANCE, DEFAULT_MIN_TRIP_DISTANCE)
        )
        self._idle_timeout = int(merged.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT))
        self._energy_price = float(merged.get(CONF_ENERGY_PRICE, DEFAULT_ENERGY_PRICE))
        self._currency = merged.get(CONF_CURRENCY, DEFAULT_CURRENCY)

        self.current: TripInProgress | None = None
        self.last_trip: TripRecord | None = None

        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_power: CALLBACK_TYPE | None = None
        self._unsub_temp: CALLBACK_TYPE | None = None
        self._unsub_idle: CALLBACK_TYPE | None = None

        self._listeners: list[Callable[[], None]] = []

    @property
    def entry_id(self) -> str:
        return self.entry.entry_id

    @property
    def battery_capacity(self) -> float:
        return self._battery_capacity

    @property
    def currency(self) -> str:
        return self._currency

    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        """Subscribe a sensor to coordinator updates."""
        self._listeners.append(update)

        def _remove() -> None:
            self._listeners.remove(update)

        return _remove

    @callback
    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def async_start(self) -> None:
        """Wire up state listeners and seed from existing storage."""
        self.last_trip = await self.storage.async_get_last()

        self._unsub_state = async_track_state_change_event(
            self.hass, [self._vehicle_on], self._async_vehicle_on_changed
        )
        if self._power:
            self._unsub_power = async_track_state_change_event(
                self.hass, [self._power], self._async_power_changed
            )
        if self._temp:
            self._unsub_temp = async_track_state_change_event(
                self.hass, [self._temp], self._async_temp_changed
            )

        if self._read_bool(self._vehicle_on) is True:
            self._open_trip(dt_util.now())

    async def async_stop(self) -> None:
        for unsub in (self._unsub_state, self._unsub_power, self._unsub_temp, self._unsub_idle):
            if unsub:
                unsub()
        self._unsub_state = self._unsub_power = self._unsub_temp = self._unsub_idle = None

    @callback
    def _async_vehicle_on_changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _INVALID_STATES:
            return
        is_on = new_state.state == STATE_ON
        now = dt_util.now()
        if is_on:
            self._cancel_idle()
            if self.current is None:
                self._open_trip(now)
        elif self.current is not None:
            self._schedule_close(now)

    @callback
    def _async_power_changed(self, event: Event[EventStateChangedData]) -> None:
        if self.current is None:
            return
        value = self._read_float(self._power)
        if value is None:
            return
        self.current.max_power = max(self.current.max_power, abs(value))
        self._notify_listeners()

    @callback
    def _async_temp_changed(self, event: Event[EventStateChangedData]) -> None:
        if self.current is None:
            return
        value = self._read_float(self._temp)
        if value is None:
            return
        self.current.temp_samples.append(value)
        self._notify_listeners()

    def _open_trip(self, now: datetime) -> None:
        odometer = self._read_float(self._odometer)
        soc = self._read_float(self._battery)
        location = self._read_str(self._location) if self._location else None
        temp = self._read_float(self._temp) if self._temp else None

        self.current = TripInProgress(
            started_at=now,
            odometer_start=odometer,
            soc_start=soc,
            location_start=location,
            temp_samples=[temp] if temp is not None else [],
            last_seen_odometer=odometer,
            last_seen_soc=soc,
        )
        _LOGGER.debug("Trip opened at %s odo=%s soc=%s", now, odometer, soc)
        self.hass.bus.async_fire(
            EVENT_TRIP_STARTED,
            {
                "entry_id": self.entry_id,
                "started_at": now.isoformat(),
                "odometer_start": odometer,
                "soc_start": soc,
                "location_start": location,
            },
        )
        self._notify_listeners()

    def _schedule_close(self, now: datetime) -> None:
        self._cancel_idle()

        @callback
        def _close(_at: datetime) -> None:
            self._unsub_idle = None
            self.hass.async_create_task(self._async_close_trip(dt_util.now()))

        self._unsub_idle = async_call_later(
            self.hass, self._idle_timeout * 60, _close
        )

    def _cancel_idle(self) -> None:
        if self._unsub_idle is not None:
            self._unsub_idle()
            self._unsub_idle = None

    async def _async_close_trip(self, now: datetime) -> None:
        active = self.current
        if active is None:
            return

        odometer_end = self._read_float(self._odometer) or active.last_seen_odometer
        soc_end = self._read_float(self._battery) or active.last_seen_soc
        location_end = self._read_str(self._location) if self._location else None

        distance = (
            (odometer_end - active.odometer_start)
            if odometer_end is not None and active.odometer_start is not None
            else 0.0
        )
        duration_min = max(0.0, (now - active.started_at).total_seconds() / 60.0)

        if distance < self._min_distance:
            _LOGGER.debug(
                "Discarding short trip distance=%.2f km < min=%.2f km",
                distance,
                self._min_distance,
            )
            self.current = None
            self._notify_listeners()
            return

        soc_used = (
            (active.soc_start - soc_end)
            if active.soc_start is not None and soc_end is not None
            else None
        )
        energy = (
            (soc_used / 100.0) * self._battery_capacity
            if soc_used is not None
            else None
        )
        consumption = (
            (energy / distance * 100.0)
            if energy is not None and distance > 0
            else None
        )
        avg_speed = (distance / (duration_min / 60.0)) if duration_min > 0 else None
        avg_temp = (
            sum(active.temp_samples) / len(active.temp_samples)
            if active.temp_samples
            else None
        )
        cost = (
            energy * self._energy_price
            if energy is not None and energy > 0
            else None
        )

        record = TripRecord(
            started_at=active.started_at,
            ended_at=now,
            duration_min=duration_min,
            distance_km=distance,
            odometer_start=active.odometer_start,
            odometer_end=odometer_end,
            soc_start=active.soc_start,
            soc_end=soc_end,
            soc_used_pct=soc_used,
            energy_kwh=energy,
            consumption_kwh_100km=consumption,
            avg_speed_kmh=avg_speed,
            max_power_kw=active.max_power or None,
            avg_temp_c=avg_temp,
            origin=active.location_start,
            destination=location_end,
            cost=cost,
            currency=self._currency if cost is not None else None,
        )

        trip_id = await self.storage.async_insert(record)
        record.trip_id = trip_id

        self.last_trip = record
        self.current = None

        self.hass.bus.async_fire(
            EVENT_TRIP_ENDED,
            {"entry_id": self.entry_id, **record.to_dict()},
        )
        _LOGGER.debug("Trip #%s closed: %.2f km / %.1f min", trip_id, distance, duration_min)
        self._notify_listeners()

    async def async_start_trip_service(self) -> None:
        if self.current is None:
            self._open_trip(dt_util.now())

    async def async_end_trip_service(self) -> None:
        self._cancel_idle()
        if self.current is not None:
            await self._async_close_trip(dt_util.now())

    async def async_delete_last_trip_service(self) -> bool:
        deleted = await self.storage.async_delete_last()
        if deleted:
            self.last_trip = await self.storage.async_get_last()
            self._notify_listeners()
        return deleted

    def _read_state(self, entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _INVALID_STATES:
            return None
        return state.state

    def _read_float(self, entity_id: str | None) -> float | None:
        raw = self._read_state(entity_id)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _read_str(self, entity_id: str | None) -> str | None:
        return self._read_state(entity_id)

    def _read_bool(self, entity_id: str | None) -> bool | None:
        raw = self._read_state(entity_id)
        if raw is None:
            return None
        return raw == STATE_ON

    def current_snapshot(self) -> dict[str, Any] | None:
        """Return live trip metrics for the sensor platform."""
        active = self.current
        if active is None:
            return None

        odometer_now = self._read_float(self._odometer) or active.last_seen_odometer
        soc_now = self._read_float(self._battery) or active.last_seen_soc
        if odometer_now is not None:
            active.last_seen_odometer = odometer_now
        if soc_now is not None:
            active.last_seen_soc = soc_now

        distance = (
            (odometer_now - active.odometer_start)
            if odometer_now is not None and active.odometer_start is not None
            else 0.0
        )
        duration_min = max(
            0.0, (dt_util.now() - active.started_at).total_seconds() / 60.0
        )
        soc_used = (
            (active.soc_start - soc_now)
            if active.soc_start is not None and soc_now is not None
            else None
        )
        energy = (
            (soc_used / 100.0) * self._battery_capacity
            if soc_used is not None
            else None
        )
        consumption = (
            (energy / distance * 100.0)
            if energy is not None and distance > 0
            else None
        )
        avg_speed = (
            (distance / (duration_min / 60.0)) if duration_min > 0 else None
        )
        avg_temp = (
            sum(active.temp_samples) / len(active.temp_samples)
            if active.temp_samples
            else None
        )

        return {
            "distance_km": distance,
            "duration_min": duration_min,
            "avg_speed_kmh": avg_speed,
            "soc_used_pct": soc_used,
            "energy_kwh": energy,
            "consumption_kwh_100km": consumption,
            "avg_temp_c": avg_temp,
            "max_power_kw": active.max_power or None,
        }
