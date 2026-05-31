"""Trip detection state machine."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    STATE_NOT_HOME,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY,
    CONF_BATTERY_CAPACITY,
    CONF_CHARGE_SENSOR,
    CONF_CURRENCY,
    CONF_ENERGY_PRICE,
    CONF_HOME_ZONE,
    CONF_IDLE_TIMEOUT,
    CONF_LOCATION,
    CONF_MIN_TRIP_DISTANCE,
    CONF_ODOMETER,
    CONF_DCFC_THRESHOLD_KW,
    CONF_POWER,
    CONF_SPEED,
    CONF_TEMP,
    CONF_VEHICLE_ON,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_DCFC_THRESHOLD_KW,
    DEFAULT_CURRENCY,
    DEFAULT_ENERGY_PRICE,
    DEFAULT_HOME_ZONE,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MIN_TRIP_DISTANCE,
    EVENT_CHARGE_LOGGED,
    EVENT_TRIP_ENDED,
    EVENT_TRIP_STARTED,
)
from .storage import ChargeRecord, TripRecord, TripStorage

_LOGGER = logging.getLogger(__name__)

_INVALID_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""}
_LIVE_TICK = timedelta(seconds=30)
# Wait this long without further odo growth before committing a synthetic
# trip. Cloud-polling sources emit small odo deltas every ~1-2 min during a
# drive; the window must be longer than the polling interval so we don't
# fragment one drive into many micro-trips.
_SYNTH_COALESCE_WINDOW_S = 300
# How long after a trip closes we still accept a late device_tracker → home
# transition as "the trip ended at home" (and use it to close the journey
# and amend the trip's destination).
_HOME_ARRIVAL_GRACE_S = 600


@dataclass
class TripInProgress:
    """In-memory accumulator for an active trip."""

    started_at: datetime
    odometer_start: float | None
    soc_start: float | None
    location_start: str | None
    temp_samples: list[float] = field(default_factory=list)
    max_power: float = 0.0
    max_speed_kmh: float = 0.0
    # Trapezoidal-integrated negative-side battery power (kWh recovered via
    # regenerative braking). Updated on every power-sensor change while the
    # trip is open.
    regen_kwh: float = 0.0
    last_power_kw: float | None = None
    last_power_ts: datetime | None = None
    last_seen_odometer: float | None = None
    last_seen_soc: float | None = None


@dataclass
class ChargeInProgress:
    """In-memory accumulator for an active (auto-detected) charging session."""

    started_at: datetime
    soc_start: float | None
    last_seen_soc: float | None = None
    # Most-recent absolute power reading from the configured power sensor,
    # surfaced by the current_charge_* sensors so the dashboard can show
    # "charging at 7.2 kW right now". Captured even when no trip is active.
    last_power_kw: float | None = None


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
        self._charge_sensor = merged.get(CONF_CHARGE_SENSOR)
        self._location = merged.get(CONF_LOCATION)
        self._temp = merged.get(CONF_TEMP)
        self._speed = merged.get(CONF_SPEED)

        self._battery_capacity = float(
            merged.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
        )
        self._dcfc_threshold_kw = float(
            merged.get(CONF_DCFC_THRESHOLD_KW, DEFAULT_DCFC_THRESHOLD_KW)
        )
        self._min_distance = float(
            merged.get(CONF_MIN_TRIP_DISTANCE, DEFAULT_MIN_TRIP_DISTANCE)
        )
        self._idle_timeout = int(merged.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT))
        self._energy_price = float(merged.get(CONF_ENERGY_PRICE, DEFAULT_ENERGY_PRICE))
        self._currency = merged.get(CONF_CURRENCY, DEFAULT_CURRENCY)
        self._home_zone = merged.get(CONF_HOME_ZONE, DEFAULT_HOME_ZONE)

        self.current: TripInProgress | None = None
        self.last_trip: TripRecord | None = None
        self.last_charge: ChargeRecord | None = None
        self.current_charge: ChargeInProgress | None = None
        self.current_journey_id: int | None = None
        self.last_completed_journey_id: int | None = None
        # Snapshot of (timestamp, odo, soc) at the last reading while no trip
        # was open. Used to recover trips that cloud-polling integrations miss
        # because `vehicle_on` flips on and off between polls.
        self._last_idle_odo: tuple[datetime, float, float | None] | None = None

        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_metrics: CALLBACK_TYPE | None = None
        self._unsub_power: CALLBACK_TYPE | None = None
        self._unsub_temp: CALLBACK_TYPE | None = None
        self._unsub_speed: CALLBACK_TYPE | None = None
        self._unsub_charge: CALLBACK_TYPE | None = None
        self._unsub_idle: CALLBACK_TYPE | None = None
        self._unsub_live_tick: CALLBACK_TYPE | None = None
        # Pending synthetic-trip finalize timer + baseline (start_t, start_odo,
        # start_soc) of the in-progress synth trip being coalesced. When the
        # underlying integration (e.g. BYD cloud) reports odo updates in many
        # small increments while vehicle_on stays False, we accumulate them
        # into one trip instead of inserting one record per polling cycle.
        self._unsub_synth_finalize: CALLBACK_TYPE | None = None
        self._synth_baseline: tuple[datetime, float, float | None] | None = None
        self._unsub_location: CALLBACK_TYPE | None = None

        self._listeners: list[Callable[[], None]] = []
        self._trip_log_listeners: list[Callable[[], None]] = []

    @property
    def entry_id(self) -> str:
        return self.entry.entry_id

    @property
    def battery_capacity(self) -> float:
        return self._battery_capacity

    @property
    def currency(self) -> str:
        return self._currency

    def _is_at_home(self, location: str | None) -> bool:
        """Case-insensitive home check.

        Device trackers report the zone's friendly_name (e.g. 'home'), but
        the same name might be capitalised differently between sources
        ('Home' vs 'home'). All journey comparisons go through this helper.
        """
        if location is None:
            return False
        return location.strip().casefold() == self.home_zone.strip().casefold()

    @property
    def home_zone(self) -> str:
        """The string a device_tracker reports while inside the home zone.

        HA's device_tracker uses the zone's underlying *slug* (entity_id
        without the `zone.` prefix), NOT its friendly_name. If you rename
        `zone.home` to 'Rafelehouse' in the UI, the friendly_name changes
        but device_tracker still reports 'home'. So we always strip the
        prefix and compare against that slug.

        Accepts both the new selector format (`zone.<id>`) and the legacy
        free-text format (just the name) for backwards compatibility.
        """
        raw = self._home_zone or DEFAULT_HOME_ZONE
        if raw.startswith("zone."):
            return raw[len("zone."):]
        return raw

    @property
    def battery_level(self) -> float | None:
        """Current SoC % from the configured battery sensor, None if unreadable."""
        return self._read_float(self._battery)

    @property
    def exterior_temp(self) -> float | None:
        """Configured outside-temperature sensor value, None if unset/unreadable."""
        return self._read_float(self._temp) if self._temp else None

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

    def async_add_trip_log_listener(
        self, update: Callable[[], None]
    ) -> Callable[[], None]:
        """Subscribe to changes in the persisted trip log (close / delete)."""
        self._trip_log_listeners.append(update)

        def _remove() -> None:
            self._trip_log_listeners.remove(update)

        return _remove

    @callback
    def _notify_trip_log_listeners(self) -> None:
        for listener in list(self._trip_log_listeners):
            listener()

    async def async_start(self) -> None:
        """Wire up state listeners and seed from existing storage."""
        self.last_trip = await self.storage.async_get_last()
        self.last_charge = await self.storage.async_get_last_charge()
        # Resume an open journey if the last trip didn't end at home.
        # We test by destination because the retroactive-close happens at the
        # next stage's _open_trip, not at the previous close.
        if (
            self.last_trip is not None
            and self.last_trip.journey_id is not None
            and not self._is_at_home(self.last_trip.destination)
        ):
            self.current_journey_id = self.last_trip.journey_id
        # Seed the odo-jump snapshot from the last trip if available so we can
        # detect missed trips that happened while HA was down.
        if self.last_trip is not None and self.last_trip.odometer_end is not None:
            self._last_idle_odo = (
                self.last_trip.ended_at,
                self.last_trip.odometer_end,
                self.last_trip.soc_end,
            )
        self.last_completed_journey_id = (
            await self.storage.async_last_completed_journey_id(self.current_journey_id)
        )

        self._unsub_state = async_track_state_change_event(
            self.hass, [self._vehicle_on], self._async_vehicle_on_changed
        )
        self._unsub_metrics = async_track_state_change_event(
            self.hass, [self._odometer, self._battery], self._async_metric_changed
        )
        if self._power:
            self._unsub_power = async_track_state_change_event(
                self.hass, [self._power], self._async_power_changed
            )
        if self._temp:
            self._unsub_temp = async_track_state_change_event(
                self.hass, [self._temp], self._async_temp_changed
            )
        if self._speed:
            self._unsub_speed = async_track_state_change_event(
                self.hass, [self._speed], self._async_speed_changed
            )
        if self._charge_sensor:
            self._unsub_charge = async_track_state_change_event(
                self.hass, [self._charge_sensor], self._async_charge_sensor_changed
            )
        if self._location:
            self._unsub_location = async_track_state_change_event(
                self.hass, [self._location], self._async_location_changed
            )

        if self.hass.state == CoreState.running:
            self._maybe_resume_trip()
        else:
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._async_on_ha_started
            )

    @callback
    def _async_on_ha_started(self, _event: Event) -> None:
        self._maybe_resume_trip()

    def _maybe_resume_trip(self) -> None:
        """Open a trip at startup only when vehicle_on=on AND odo/soc are readable.

        Why: at startup, sensors restored from history may still report unknown
        before the integration loads. Opening a trip then would record a wrong
        odometer_start. We skip and rely on the next vehicle_on transition.
        """
        if self.current is not None:
            return
        if self._read_bool(self._vehicle_on) is not True:
            return
        if (
            self._read_float(self._odometer) is None
            or self._read_float(self._battery) is None
        ):
            _LOGGER.warning(
                "Vehicle is on at startup but odometer/battery are not ready; "
                "skipping auto-open to avoid recording a bogus trip"
            )
            return
        self._open_trip(dt_util.now())

    async def async_stop(self) -> None:
        for unsub in (
            self._unsub_state,
            self._unsub_metrics,
            self._unsub_power,
            self._unsub_temp,
            self._unsub_speed,
            self._unsub_charge,
            self._unsub_idle,
            self._unsub_live_tick,
            self._unsub_synth_finalize,
            self._unsub_location,
        ):
            if unsub:
                unsub()
        self._unsub_state = self._unsub_metrics = None
        self._unsub_power = self._unsub_temp = self._unsub_idle = None
        self._unsub_speed = None
        self._unsub_charge = self._unsub_live_tick = None
        self._unsub_synth_finalize = None
        self._unsub_location = None
        self._synth_baseline = None

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
                # Defer opening if metrics aren't ready yet — avoids recording
                # a bogus odometer_start. The next metric tick will not re-open
                # automatically, so the user only loses a trip if the BYD
                # entity reports on before its odometer/battery do, which is
                # very brief in practice.
                if (
                    self._read_float(self._odometer) is None
                    or self._read_float(self._battery) is None
                ):
                    _LOGGER.warning(
                        "vehicle_on=on but odometer/battery not ready; not opening trip"
                    )
                    return
                self._open_trip(now)
        elif self.current is not None:
            # Close immediately: each on/off cycle is one trip. Trip stats
            # (avg_speed, energy_kwh, consumption) are computed from the
            # actual on→off interval. The legacy idle_timeout debounce was
            # merging consecutive cycles (e.g. a home→work + work→shops
            # pair) into a single "trip" with bogus aggregate stats.
            self.hass.async_create_task(self._async_close_trip(now))

    @callback
    def _async_metric_changed(self, event: Event[EventStateChangedData]) -> None:
        """Notify listeners on odometer / battery change, even when idle.

        Also recovers a missed-resume: if vehicle_on is on but no trip is
        open (because _maybe_resume_trip ran while BYD hadn't yet repopulated
        odometer/battery after a HA restart), the first fresh metric arrival
        opens the trip retroactively. Without this, every HA restart during
        a real drive silently swallows the entire trip.
        """
        if (
            self.current is None
            and self._read_bool(self._vehicle_on) is True
            and self._read_float(self._odometer) is not None
            and self._read_float(self._battery) is not None
        ):
            self._open_trip(dt_util.now())
            return

        if self.current_charge is not None:
            soc = self._read_float(self._battery)
            if soc is not None:
                self.current_charge.last_seen_soc = soc
        self._notify_listeners()
        if self.current is None:
            self.hass.async_create_task(self._async_check_odo_jump())

    @callback
    def _async_location_changed(self, event: Event[EventStateChangedData]) -> None:
        """React to late device_tracker zone transitions.

        Cloud-polling integrations lag the geofence by 1–3 min: a trip that
        ends parked anywhere (home, work, gym, etc.) closes with whatever
        location was visible at vehicle_on=off — typically `not_home`. When
        the tracker finally settles on the real zone, we:

        1. Amend the last trip's destination to that zone (any known zone,
           not just home) so history matches reality.
        2. Specifically on `home` arrival, close the open journey too —
           home is the natural journey terminator; other zones aren't.
        """
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _INVALID_STATES:
            return
        loc = new_state.state
        # `not_home` means "outside every known zone" — not informative.
        if loc == STATE_NOT_HOME:
            return
        # While a trip is active, normal close will pick this up later.
        if self.current is not None:
            return
        self.hass.async_create_task(
            self._async_handle_late_zone_arrival(loc, new_state.last_updated)
        )

    async def _async_handle_late_zone_arrival(
        self, location: str, when: datetime
    ) -> None:
        if self.current is not None:
            return
        # 1) Amend the last trip's destination if within the grace window
        #    AND the recorded destination isn't already this zone.
        if self.last_trip is not None and self.last_trip.trip_id is not None:
            delta_s = (when - self.last_trip.ended_at).total_seconds()
            if (
                0 <= delta_s <= _HOME_ARRIVAL_GRACE_S
                and self.last_trip.destination != location
            ):
                await self.storage.async_update_trip_destination(
                    self.last_trip.trip_id, location
                )
                self.last_trip = replace(self.last_trip, destination=location)
        # 2) Home arrival also closes the open journey.
        if self._is_at_home(location) and self.current_journey_id is not None:
            self.last_completed_journey_id = self.current_journey_id
            self.current_journey_id = None
        self._notify_listeners()
        self._notify_trip_log_listeners()

    async def _async_check_odo_jump(self) -> None:
        """Coalesce consecutive odo growth into ONE synthetic trip.

        Catches trips that cloud-polling integrations miss because vehicle_on
        toggles between two polls. Cloud-polling sources (e.g. BYD) typically
        emit many small odo deltas during a drive — without coalescing we'd
        insert one trip per polling cycle. Instead, we hold the baseline at
        the last idle reading and (re)schedule a finalize timer; when the
        timer fires after _SYNTH_COALESCE_WINDOW_S of no new growth, we log a
        single trip covering the full accumulated delta.
        """
        odo = self._read_float(self._odometer)
        if odo is None:
            return
        now = dt_util.now()
        soc = self._read_float(self._battery)

        prev = self._last_idle_odo
        if prev is None:
            # First observation — establish baseline, don't insert anything.
            self._last_idle_odo = (now, odo, soc)
            return
        prev_t, prev_odo, prev_soc = prev
        delta = odo - prev_odo

        if delta < self._min_distance:
            # Sub-threshold growth — keep waiting. Do NOT advance the baseline:
            # the next reading must still be compared against the original
            # idle snapshot, otherwise we'd lose the cumulative distance.
            return

        # Above-threshold growth detected. Adopt or keep the synth baseline
        # (= last idle reading) and (re)schedule the finalize timer.
        if self._synth_baseline is None:
            self._synth_baseline = (prev_t, prev_odo, prev_soc)
        if self._unsub_synth_finalize is not None:
            self._unsub_synth_finalize()

        @callback
        def _finalize(_at: datetime) -> None:
            self._unsub_synth_finalize = None
            self.hass.async_create_task(self._async_finalize_synth_trip())

        self._unsub_synth_finalize = async_call_later(
            self.hass, _SYNTH_COALESCE_WINDOW_S, _finalize
        )

    async def _async_finalize_synth_trip(self) -> None:
        """Commit the coalesced synthetic trip after the debounce window."""
        baseline = self._synth_baseline
        if baseline is None:
            return
        # If a real trip opened in the meantime, abort — the live trip will
        # cover this distance and we'd double-count.
        if self.current is not None:
            self._synth_baseline = None
            return
        odo_now = self._read_float(self._odometer)
        if odo_now is None:
            return
        soc_now = self._read_float(self._battery)
        now = dt_util.now()
        prev_t, prev_odo, prev_soc = baseline
        delta = odo_now - prev_odo
        # Reset state regardless of outcome — next idle reading establishes
        # a fresh baseline.
        self._synth_baseline = None
        self._last_idle_odo = (now, odo_now, soc_now)
        if delta < self._min_distance:
            return
        await self._async_log_synthetic_trip(
            prev_t, now, prev_odo, odo_now, prev_soc, soc_now
        )

    async def _async_log_synthetic_trip(
        self,
        started_at: datetime,
        ended_at: datetime,
        odo_s: float,
        odo_e: float,
        soc_s: float | None,
        soc_e: float | None,
    ) -> None:
        distance = odo_e - odo_s
        duration_min = max(0.1, (ended_at - started_at).total_seconds() / 60.0)
        soc_used = (
            soc_s - soc_e
            if soc_s is not None and soc_e is not None and soc_s > soc_e
            else None
        )
        energy = (
            (soc_used / 100.0) * self._battery_capacity
            if soc_used is not None
            else None
        )
        consumption = (
            (energy / distance * 100.0) if energy and distance > 0 else None
        )
        avg_speed = (
            (distance / (duration_min / 60.0))
            if duration_min > 0 and distance > 0
            else None
        )
        if avg_speed is not None and avg_speed > 300:
            avg_speed = None
        price_per_kwh = (
            self.last_charge.price_per_kwh
            if self.last_charge is not None
            else self._energy_price
        )
        cost = energy * price_per_kwh if energy and energy > 0 else None
        location_start = self.last_trip.destination if self.last_trip else None
        location_end = self._read_str(self._location) if self._location else None
        started_from_home = self._is_at_home(location_start)
        is_at_home_end = self._is_at_home(location_end)
        if started_from_home and self.current_journey_id is not None:
            self.last_completed_journey_id = self.current_journey_id
            self.current_journey_id = None
        if self.current_journey_id is not None:
            journey_id: int | None = self.current_journey_id
        elif started_from_home:
            journey_id = await self.storage.async_next_journey_id()
        else:
            journey_id = None

        record = TripRecord(
            started_at=started_at,
            ended_at=ended_at,
            duration_min=duration_min,
            distance_km=distance,
            odometer_start=odo_s,
            odometer_end=odo_e,
            soc_start=soc_s,
            soc_end=soc_e,
            soc_used_pct=soc_used,
            energy_kwh=energy,
            consumption_kwh_100km=consumption,
            avg_speed_kmh=avg_speed,
            avg_temp_c=None,
            origin=location_start,
            destination=location_end,
            cost=cost,
            currency=self._currency if cost is not None else None,
            journey_id=journey_id,
        )
        trip_id = await self.storage.async_insert(record)
        record.trip_id = trip_id
        self.last_trip = record
        if is_at_home_end and journey_id is not None:
            self.last_completed_journey_id = journey_id
            self.current_journey_id = None
        else:
            self.current_journey_id = journey_id

        self.hass.bus.async_fire(
            EVENT_TRIP_ENDED,
            {
                "entry_id": self.entry_id,
                **record.to_dict(),
                "synthetic": True,
                "reason": "odometer jump (vehicle_on never reported on)",
            },
        )
        _LOGGER.info(
            "Synthetic trip #%s from odo jump: %.2f km / %.1f min",
            trip_id, distance, duration_min,
        )
        self._notify_listeners()
        self._notify_trip_log_listeners()

    @callback
    def _async_power_changed(self, event: Event[EventStateChangedData]) -> None:
        value = self._read_float(self._power)
        if value is None:
            return
        # Charge tracking: capture live power so current_charge sensors can
        # display "charging at X kW right now". Runs even with no trip open.
        if self.current_charge is not None:
            self.current_charge.last_power_kw = abs(value)
            self._notify_listeners()
        if self.current is None:
            return
        self.current.max_power = max(self.current.max_power, abs(value))
        # Trapezoidal regen integration: when the prior sample was negative
        # (battery discharging in reverse → regen) we accumulate the area
        # under that half of the curve. Convention here: discharge power
        # is positive (BYD reports it that way), so regen = -power values.
        now = dt_util.now()
        prev_kw = self.current.last_power_kw
        prev_ts = self.current.last_power_ts
        if prev_kw is not None and prev_ts is not None:
            dt_h = (now - prev_ts).total_seconds() / 3600.0
            if 0 < dt_h < 1.0:  # sanity-bound: skip samples with >1h gap
                # Take the negative portion of each endpoint (regen only).
                a = -min(prev_kw, 0.0)
                b = -min(value, 0.0)
                self.current.regen_kwh += (a + b) / 2.0 * dt_h
        self.current.last_power_kw = value
        self.current.last_power_ts = now
        self._notify_listeners()

    @callback
    def _async_speed_changed(self, event: Event[EventStateChangedData]) -> None:
        if self.current is None:
            return
        value = self._read_float(self._speed)
        if value is None:
            return
        # Sanity cap — BYD has occasionally reported sub-second odo jumps as
        # nonsense speeds elsewhere; the speed sensor is direct but still
        # bound it to a physically plausible ceiling.
        if value > 300:
            return
        if value > self.current.max_speed_kmh:
            self.current.max_speed_kmh = value
        self._notify_listeners()

    _AUTO_CHARGE_DEDUP_WINDOW_S = 300  # 5 min — skip auto if a manual charge just logged

    @callback
    def _async_charge_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _INVALID_STATES:
            return
        is_charging = new_state.state == STATE_ON
        now = dt_util.now()
        if is_charging:
            if self.current_charge is not None:
                return
            soc = self._read_float(self._battery)
            self.current_charge = ChargeInProgress(
                started_at=now, soc_start=soc, last_seen_soc=soc
            )
            _LOGGER.debug("Charge session opened at %s, soc=%s", now, soc)
            self._notify_listeners()
        elif self.current_charge is not None:
            self.hass.async_create_task(self._async_close_auto_charge(now))

    async def _async_close_auto_charge(self, now: datetime) -> None:
        active = self.current_charge
        if active is None:
            return
        self.current_charge = None
        soc_end = self._read_float(self._battery) or active.last_seen_soc
        if active.soc_start is None or soc_end is None or soc_end <= active.soc_start:
            _LOGGER.debug("Discarding auto-charge: SoC delta not positive")
            self._notify_listeners()
            return

        if self.last_charge is not None:
            elapsed = (now - self.last_charge.ended_at).total_seconds()
            if elapsed < self._AUTO_CHARGE_DEDUP_WINDOW_S:
                _LOGGER.debug(
                    "Skipping auto-charge: a charge was logged %.0fs ago (likely manual)",
                    elapsed,
                )
                self._notify_listeners()
                return

        kwh = (soc_end - active.soc_start) / 100.0 * self._battery_capacity
        # Location comes from the configured device_tracker (e.g. zone "home"); falls
        # back to "auto" so we can still tell auto-detected charges apart in the log.
        location = self._read_str(self._location) if self._location else None
        await self.async_log_charge_service(
            kwh=kwh,
            location=location or "auto",
            notes=f"auto-detected from {self._charge_sensor}",
            started_at=active.started_at,
        )

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
        # If a synth-trip finalize was pending, cancel it — the live trip
        # will own the distance from here on.
        if self._unsub_synth_finalize is not None:
            self._unsub_synth_finalize()
            self._unsub_synth_finalize = None
        self._synth_baseline = None
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
        # Heartbeat so duration / avg_speed tick forward even when no sensor
        # is changing (e.g. car stopped at a light).
        if self._unsub_live_tick is None:
            self._unsub_live_tick = async_track_time_interval(
                self.hass, self._async_live_tick, _LIVE_TICK
            )
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

    @callback
    def _async_live_tick(self, _now: datetime) -> None:
        if self.current is not None:
            self._notify_listeners()

    def _cancel_live_tick(self) -> None:
        if self._unsub_live_tick is not None:
            self._unsub_live_tick()
            self._unsub_live_tick = None

    async def _async_close_trip(self, now: datetime) -> None:
        active = self.current
        if active is None:
            return
        self._cancel_live_tick()

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
        avg_speed = (
            (distance / (duration_min / 60.0))
            if duration_min > 0 and distance > 0
            else None
        )
        if avg_speed is not None and avg_speed > 300:
            # Sub-second time deltas produce nonsense (e.g. 40 000 km/h when
            # you bump the odometer slider just after turning on). Cap it.
            avg_speed = None
        avg_temp = (
            sum(active.temp_samples) / len(active.temp_samples)
            if active.temp_samples
            else None
        )
        price_per_kwh = (
            self.last_charge.price_per_kwh
            if self.last_charge is not None
            else self._energy_price
        )
        cost_currency = (
            self.last_charge.currency
            if self.last_charge is not None and self.last_charge.currency
            else self._currency
        )
        cost = (
            energy * price_per_kwh
            if energy is not None and energy > 0
            else None
        )

        # Journey membership.
        # Heuristic for noisy GPS: device_tracker may say "not_home" when the
        # car parks just outside the home zone. If this stage *starts* at home
        # while a journey is still open (last stage ended away), the car must
        # have come home in between — retroactively close that journey and let
        # this stage open a fresh one.
        is_at_home_end = self._is_at_home(location_end)
        started_from_home = self._is_at_home(active.location_start)
        if started_from_home and self.current_journey_id is not None:
            _LOGGER.debug(
                "Retroactively closing journey %s — stage opened from home",
                self.current_journey_id,
            )
            self.last_completed_journey_id = self.current_journey_id
            self.current_journey_id = None

        if self.current_journey_id is not None:
            journey_id: int | None = self.current_journey_id
        elif started_from_home:
            journey_id = await self.storage.async_next_journey_id()
        else:
            journey_id = None

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
            max_speed_kmh=active.max_speed_kmh or None,
            regen_kwh=active.regen_kwh or None,
            avg_temp_c=avg_temp,
            origin=active.location_start,
            destination=location_end,
            cost=cost,
            currency=cost_currency if cost is not None else None,
            journey_id=journey_id,
        )

        trip_id = await self.storage.async_insert(record)
        record.trip_id = trip_id

        # Update journey state after insert: closed by arrival home, otherwise carry on.
        if is_at_home_end and journey_id is not None:
            self.last_completed_journey_id = journey_id
            self.current_journey_id = None
        else:
            self.current_journey_id = journey_id

        self.last_trip = record
        self.current = None

        self.hass.bus.async_fire(
            EVENT_TRIP_ENDED,
            {"entry_id": self.entry_id, **record.to_dict()},
        )
        _LOGGER.debug("Trip #%s closed: %.2f km / %.1f min", trip_id, distance, duration_min)
        # Reset odo-jump baseline to the trip-end values so the next idle
        # reading doesn't compare against a stale snapshot.
        self._last_idle_odo = (now, odometer_end, soc_end) if odometer_end is not None else None
        self._notify_listeners()
        self._notify_trip_log_listeners()

    async def async_start_trip_service(self) -> None:
        if self.current is None:
            self._open_trip(dt_util.now())

    async def async_end_trip_service(self) -> None:
        self._cancel_idle()
        if self.current is not None:
            await self._async_close_trip(dt_util.now())

    async def async_log_charge_service(
        self,
        *,
        kwh: float,
        price_per_kwh: float | None = None,
        total_cost: float | None = None,
        currency: str | None = None,
        location: str | None = None,
        notes: str | None = None,
        started_at: datetime | None = None,
        is_dcfc: bool | None = None,
    ) -> ChargeRecord:
        """Persist a charge session.

        Provide one of: total_cost, price_per_kwh. If neither, falls back to
        the configured home price. The missing one is derived from kwh.
        Location defaults to the configured device_tracker's state.

        `is_dcfc` defaults to a duration-based heuristic: when `started_at`
        is provided and avg power > _dcfc_threshold_kw, the session is
        classified as DC fast-charge. Callers can override explicitly.
        """
        now = dt_util.now()
        kwh = float(kwh)
        if total_cost is not None:
            total_cost = float(total_cost)
            price_per_kwh = total_cost / kwh if kwh else 0.0
        else:
            if price_per_kwh is None:
                price_per_kwh = self._energy_price
            price_per_kwh = float(price_per_kwh)
            total_cost = kwh * price_per_kwh

        if location is None and self._location:
            location = self._read_str(self._location)

        if is_dcfc is None and started_at is not None:
            duration_h = (now - started_at).total_seconds() / 3600.0
            if duration_h > 0:
                avg_kw = kwh / duration_h
                is_dcfc = avg_kw > self._dcfc_threshold_kw

        record = ChargeRecord(
            started_at=started_at,
            ended_at=now,
            kwh=kwh,
            price_per_kwh=price_per_kwh,
            total_cost=total_cost,
            currency=currency or self._currency,
            soc_end=self._read_float(self._battery),
            location=location,
            notes=notes,
            is_dcfc=is_dcfc,
        )
        charge_id = await self.storage.async_insert_charge(record)
        record.charge_id = charge_id
        self.last_charge = record

        self.hass.bus.async_fire(
            EVENT_CHARGE_LOGGED,
            {"entry_id": self.entry_id, **record.to_dict()},
        )
        _LOGGER.debug(
            "Charge #%s logged: %.2f kWh @ %.4f = %.2f",
            charge_id, record.kwh, record.price_per_kwh, record.total_cost,
        )
        self._notify_listeners()
        self._notify_trip_log_listeners()
        return record

    async def async_set_last_charge_price_service(
        self,
        *,
        price_per_kwh: float | None = None,
        total_cost: float | None = None,
        location: str | None = None,
        notes: str | None = None,
    ) -> ChargeRecord | None:
        """Override price / location of the last charge already in storage.

        Use case: auto-detect logged a charge with the home default price, but
        you actually paid a public-charger rate. Pass price_per_kwh or
        total_cost (one of them) and the kWh + timestamp stay; price + cost
        are recomputed.
        """
        updated = await self.storage.async_update_last_charge(
            price_per_kwh=price_per_kwh,
            total_cost=total_cost,
            location=location,
            notes=notes,
        )
        if updated is None:
            _LOGGER.warning("set_last_charge_price: no charge in storage to update")
            return None
        self.last_charge = updated
        self.hass.bus.async_fire(
            EVENT_CHARGE_LOGGED,
            {"entry_id": self.entry_id, **updated.to_dict()},
        )
        _LOGGER.debug(
            "Updated charge #%s: price=%.4f, total=%.2f, location=%s",
            updated.charge_id, updated.price_per_kwh, updated.total_cost,
            updated.location,
        )
        self._notify_listeners()
        self._notify_trip_log_listeners()
        return updated

    async def async_delete_last_charge_service(self) -> bool:
        deleted = await self.storage.async_delete_last_charge()
        if deleted:
            self.last_charge = await self.storage.async_get_last_charge()
            self._notify_listeners()
            self._notify_trip_log_listeners()
        return deleted

    async def async_delete_last_trip_service(self) -> bool:
        deleted = await self.storage.async_delete_last()
        if deleted:
            self.last_trip = await self.storage.async_get_last()
            self._notify_listeners()
            self._notify_trip_log_listeners()
        return deleted

    async def async_purge_trips_service(
        self, *, since: datetime | None = None, until: datetime | None = None
    ) -> int:
        """Delete every trip in [since, until]; refresh in-memory state."""
        count = await self.storage.async_purge_trips(since=since, until=until)
        if count:
            self.last_trip = await self.storage.async_get_last()
            # If the open journey's last stage is gone, reset journey state so
            # the next stage opens a fresh one instead of attaching to a
            # ghost id.
            if self.last_trip is None or (
                self.current_journey_id is not None
                and self.last_trip.journey_id != self.current_journey_id
            ):
                self.current_journey_id = None
            self.last_completed_journey_id = (
                await self.storage.async_last_completed_journey_id(
                    self.current_journey_id
                )
            )
            self._notify_listeners()
            self._notify_trip_log_listeners()
        return count

    async def async_log_manual_trip_service(
        self,
        *,
        started_at: datetime,
        ended_at: datetime,
        distance_km: float | None = None,
        odometer_start: float | None = None,
        odometer_end: float | None = None,
        soc_start: float | None = None,
        soc_end: float | None = None,
        max_power_kw: float | None = None,
        avg_temp_c: float | None = None,
        origin: str | None = None,
        destination: str | None = None,
    ) -> TripRecord:
        """Backfill a trip that the live detector missed.

        Required: started_at, ended_at, and one of distance_km / odometer
        bounds. Everything else is derived when possible (soc_used → energy →
        cost → consumption, duration → avg_speed) using the same formulas as
        the live close path.
        """
        if distance_km is None:
            if odometer_start is None or odometer_end is None:
                raise ValueError(
                    "Provide either distance_km or both odometer_start and odometer_end"
                )
            distance_km = float(odometer_end) - float(odometer_start)
        distance = float(distance_km)
        if distance < 0:
            raise ValueError("distance must be non-negative")

        duration_min = max(0.0, (ended_at - started_at).total_seconds() / 60.0)
        soc_used = (
            float(soc_start) - float(soc_end)
            if soc_start is not None and soc_end is not None
            else None
        )
        energy = (
            (soc_used / 100.0) * self._battery_capacity
            if soc_used is not None and soc_used > 0
            else None
        )
        consumption = (
            (energy / distance * 100.0)
            if energy is not None and distance > 0
            else None
        )
        avg_speed = (
            (distance / (duration_min / 60.0))
            if duration_min > 0 and distance > 0
            else None
        )
        if avg_speed is not None and avg_speed > 300:
            # Sub-second time deltas produce nonsense (e.g. 40 000 km/h when
            # you bump the odometer slider just after turning on). Cap it.
            avg_speed = None
        price_per_kwh = (
            self.last_charge.price_per_kwh
            if self.last_charge is not None
            else self._energy_price
        )
        cost_currency = (
            self.last_charge.currency
            if self.last_charge is not None and self.last_charge.currency
            else self._currency
        )
        cost = energy * price_per_kwh if energy is not None and energy > 0 else None

        record = TripRecord(
            started_at=started_at,
            ended_at=ended_at,
            duration_min=duration_min,
            distance_km=distance,
            odometer_start=float(odometer_start) if odometer_start is not None else None,
            odometer_end=float(odometer_end) if odometer_end is not None else None,
            soc_start=float(soc_start) if soc_start is not None else None,
            soc_end=float(soc_end) if soc_end is not None else None,
            soc_used_pct=soc_used,
            energy_kwh=energy,
            consumption_kwh_100km=consumption,
            avg_speed_kmh=avg_speed,
            max_power_kw=float(max_power_kw) if max_power_kw is not None else None,
            avg_temp_c=float(avg_temp_c) if avg_temp_c is not None else None,
            origin=origin,
            destination=destination,
            cost=cost,
            currency=cost_currency if cost is not None else None,
        )

        trip_id = await self.storage.async_insert(record)
        record.trip_id = trip_id

        self.last_trip = record
        self.hass.bus.async_fire(
            EVENT_TRIP_ENDED,
            {"entry_id": self.entry_id, **record.to_dict()},
        )
        _LOGGER.info(
            "Manual trip #%s logged: %.2f km / %.1f min", trip_id, distance, duration_min
        )
        self._notify_listeners()
        self._notify_trip_log_listeners()
        return record

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
        # Need at least ~1 minute of trip before avg_speed is meaningful;
        # otherwise tiny time deltas produce 40 000 km/h-level nonsense.
        avg_speed = (
            (distance / (duration_min / 60.0))
            if duration_min > 0 and distance > 0
            else None
        )
        if avg_speed is not None and avg_speed > 300:
            # Sub-second time deltas produce nonsense (e.g. 40 000 km/h when
            # you bump the odometer slider just after turning on). Cap it.
            avg_speed = None
        avg_temp = (
            sum(active.temp_samples) / len(active.temp_samples)
            if active.temp_samples
            else None
        )

        # Live cost: use the most recent charge price if any, else the home default.
        price_per_kwh = (
            self.last_charge.price_per_kwh
            if self.last_charge is not None
            else self._energy_price
        )
        cost = (energy * price_per_kwh) if energy and energy > 0 else None
        # Live score: same curve as TripRecord.score.
        score = None
        if consumption is not None and consumption > 0:
            score = max(0.0, min(10.0, 10.0 - max(0.0, consumption - 14.5) * 0.6))

        return {
            "distance_km": distance,
            "duration_min": duration_min,
            "avg_speed_kmh": avg_speed,
            "soc_used_pct": soc_used,
            "energy_kwh": energy,
            "consumption_kwh_100km": consumption,
            "avg_temp_c": avg_temp,
            "max_power_kw": active.max_power or None,
            "max_speed_kmh": active.max_speed_kmh or None,
            "regen_kwh": active.regen_kwh or None,
            "cost": cost,
            "score": score,
        }

    def current_charge_snapshot(self) -> dict[str, Any] | None:
        """Live charging metrics — mirror of LastChargeSensor while charging."""
        active = self.current_charge
        if active is None:
            return None
        soc_now = active.last_seen_soc if active.last_seen_soc is not None else active.soc_start
        if soc_now is None or active.soc_start is None or soc_now <= active.soc_start:
            kwh_so_far: float | None = 0.0
        else:
            kwh_so_far = (soc_now - active.soc_start) / 100.0 * self._battery_capacity
        # Live price: the user can correct it post-hoc on the last completed
        # charge; while in progress we project the configured home tariff.
        price_per_kwh = self._energy_price
        total_cost = kwh_so_far * price_per_kwh if kwh_so_far else 0.0
        duration_min = max(0.0, (dt_util.now() - active.started_at).total_seconds() / 60.0)
        # is_dcfc classification: while charging we compare last_power_kw if
        # available, falling back to running avg (kwh / hours). NULL until
        # we have enough signal to be confident.
        is_dcfc: bool | None = None
        if active.last_power_kw is not None:
            is_dcfc = active.last_power_kw > self._dcfc_threshold_kw
        elif duration_min > 1 and kwh_so_far and kwh_so_far > 0:
            avg_kw = kwh_so_far / (duration_min / 60.0)
            is_dcfc = avg_kw > self._dcfc_threshold_kw
        return {
            "kwh": round(kwh_so_far, 2) if kwh_so_far else 0.0,
            "total_cost": round(total_cost, 2),
            "price_per_kwh": price_per_kwh,
            "power_kw": active.last_power_kw,
            "duration_min": duration_min,
            "is_dcfc": is_dcfc,
            "soc_start": active.soc_start,
            "soc_now": soc_now,
        }
