"""Trip detection state machine."""
from __future__ import annotations

import logging
import asyncio
from collections import deque
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
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .abrp import AbrpClient, build_tlm
from .const import (
    ABRP_MIN_SEND_INTERVAL_S,
    CONF_ABRP_API_KEY,
    CONF_ABRP_CAR_MODEL,
    CONF_ABRP_PUSH_INTERVAL_S,
    CONF_ABRP_TOKEN,
    DEFAULT_ABRP_PUSH_INTERVAL_S,
    CONF_BATTERY,
    CONF_BATTERY_CAPACITY,
    CONF_CHARGE_SENSOR,
    CONF_CURRENCY,
    CONF_ENERGY_PRICE,
    CONF_HOME_ZONE,
    CONF_IDLE_TIMEOUT,
    CONF_LOCATION,
    CONF_MIN_TRIP_DISTANCE,
    CONF_PLUG_SENSOR,
    CONF_POLLING_PAUSED_SENSOR,
    CONF_ODOMETER,
    CONF_DCFC_THRESHOLD_KW,
    CONF_IDLE_TRIP_TIMEOUT_MIN,
    CONF_POWER,
    CONF_SPEED,
    CONF_TEMP,
    CONF_RECENT_LIMIT,
    CONF_VEHICLE_ON,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_DCFC_THRESHOLD_KW,
    DEFAULT_IDLE_TRIP_TIMEOUT_MIN,
    DEFAULT_CURRENCY,
    DEFAULT_ENERGY_PRICE,
    DEFAULT_HOME_ZONE,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MIN_TRIP_DISTANCE,
    DEFAULT_RECENT_LIMIT,
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
# How long after a trip closes we still accept a late device_tracker
# transition as "the trip actually ended at <that zone>" (and use it to
# amend the trip's destination, plus close the journey when the new
# destination is home). 30 min (was 10 min in v0.5.13) gives slow
# cloud-polling trackers enough room without making spurious home flaps
# probable — random GPS noise 30 min after parking is rare.
_HOME_ARRIVAL_GRACE_S = 1800
# Idle watchdog inside open trips. If neither the odometer changes nor the
# speed sensor reports > 0 for this many seconds, the trip is force-closed
# even if vehicle_on still reads ON. Catches cloud-polling sources (BYD, …)
# that miss off→on cycles and leave a single bogus "trip" spanning multiple
# real drives.
_IDLE_INSIDE_TRIP_S = 600
# v0.5.13 — bounded SoC ring buffer used by resolve_soc_start to retrieve
# the freshest pre-vehicle_on reading. At 30 s cadence (Tesla streaming)
# 64 entries cover ~32 min of history; at BYD-class 1.5 min cadence they
# cover ~1.5 h. Memory cost is ≤ 6 KB.
_SOC_BUFFER_MAX = 64
# How far back to look for a pre-vehicle_on SoC sample. Anything older is
# treated as "stale enough to be the wrong anchor".
_PRE_ON_LOOKBACK = timedelta(minutes=5)
# Outer window after charge-end where last_charge.soc_end can still be
# the trip anchor. 12 h covers "charged overnight, drove off in the
# morning" — the canonical bug. Beyond that, vampire drain has likely
# made soc_end an unreliable anchor regardless. Within the window we
# add two more gates: no trip recorded since the charge ended, and the
# current SoC must not have dropped >2 % below soc_end (else the car
# either sat too long or someone discharged it externally).
_POST_CHARGE_ANCHOR_WINDOW = timedelta(hours=12)
_POST_CHARGE_DRAIN_BUDGET_PCT = 2.0
# Max age the synth-trip baseline can be before we discard it. Without
# this, `_async_check_odo_jump` happily uses `_last_idle_odo` from
# yesterday as the start anchor for today's drive, producing 10 h
# phantom trips that span overnight charging (SoC rises across the
# "trip", soc_used goes negative, energy ends up NULL or absurd).
_MAX_SYNTH_BASELINE_AGE = timedelta(hours=2)
# Debounce for vehicle_on off-edge. BYD cloud-poll occasionally sends a
# flicker (on→off→on within 1-2 s). Without this, two _async_close_trip
# tasks queue: the first closes properly, the second sees self.current
# is None and silently returns — but the next vehicle_on=on then opens
# a fresh trip from the SAME ignition event, fragmenting one real drive
# into two short trips with a near-zero gap. The debounce coalesces
# off-edges in this window into one close.
_VEHICLE_ON_OFF_DEBOUNCE_S = 3.0
# v0.5.25 — bounded GPS ring buffer fed by EVERY poll event (any
# metric or location state change). At BYD-typical cadence (8-10 min)
# 256 samples cover ~40 h. Live trips also append per-tick samples,
# but the ring buffer is what powers synthetic-trip GPS and the
# pre-trip start anchor.
_GPS_BUFFER_MAX = 256
# Look-back window for seeding active.gps_samples at trip open. A
# sample within the last N minutes is recent enough to qualify as
# "the car's position at trip start" — anything older is stale.
_PRE_TRIP_GPS_LOOKBACK_S = 600  # 10 min


def _haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance between two lat/lon pairs in km. Mean
    Earth radius 6371 km. Cheap (no external deps).
    """
    from math import radians, sin, cos, sqrt, atan2
    r1 = radians(lat1)
    r2 = radians(lat2)
    dphi = radians(lat2 - lat1)
    dlam = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(r1) * cos(r2) * sin(dlam / 2) ** 2
    return 2 * 6371.0 * atan2(sqrt(a), sqrt(1 - a))


def _route_distance_km(
    samples: list[tuple[datetime, float, float]] | list[tuple[float, float, float]],
) -> float | None:
    """Sum haversine segments across a sequence of (ts, lat, lon).
    None if fewer than 2 points.
    """
    if not samples or len(samples) < 2:
        return None
    total = 0.0
    for i in range(1, len(samples)):
        _, lat1, lon1 = samples[i - 1]
        _, lat2, lon2 = samples[i]
        total += _haversine_km(lat1, lon1, lat2, lon2)
    return total
# Minimum time a location zone must persist before we treat it as a real
# arrival. Cloud-polled device_trackers occasionally bounce (e.g.
# home→not_home→home in 40 s) when geofence math wobbles near the home
# boundary. v0.5.14's late-zone-arrival used to amend the destination
# on the first flap, fragmenting a single drive into two trips.
_LOCATION_DWELL_MIN_S = 60.0
# Sanity bounds for the v0.5.13 power-integration backup. Cloud-polled
# integrations (BYD especially) occasionally replay a stale power sample
# with a huge gap to the next one; without these guards a 200 kW × 0.5 h
# trapezoid added 100 kWh to a single trip on 2026-06-05 and produced
# absurd consumption numbers. Real-world peaks are ~230 kW DC fast-charge
# (regen far less); anything above _MAX_PLAUSIBLE_POWER_KW is rejected,
# and any gap > _MAX_POWER_TRAPEZOID_DT_H drops that trapezoid entirely.
_MAX_PLAUSIBLE_POWER_KW = 250.0
# v0.5.15 — relaxed from 3 → 20 min. The BYD audit (audit_soc_lag.py)
# measured a median power-sample cadence of ~8 min, p90 ~18 min: the
# v0.5.14 cap of 3 min rejected EVERY trapezoid for that car and left
# short trips with NULL energy. 20 min covers ~p90 of natural cadence
# without re-opening the v0.5.13 spike regression: the magnitude cap
# (250 kW) bounds any single trapezoid to ~83 kWh in the worst case,
# and a single such cap-pinned interval cannot persist across multiple
# polls of a real drive.
_MAX_POWER_TRAPEZOID_DT_H = 20.0 / 60.0
# Per-trapezoid contribution clamp. Even within the gap bound, a spike
# pair pinned at the magnitude cap shouldn't add more than this in one
# tick — at ~5 kWh per trapezoid we're already at "highway cruise full
# throttle for 10 min" territory, beyond which the sample is junk.
_MAX_POWER_TRAPEZOID_CONTRIBUTION_KWH = 5.0


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
    # Last observed evidence of actual movement: timestamp of either an
    # odometer change or a speed > 0 reading. Used to force-close trips
    # that BYD (or other cloud-polling integrations) leave hanging open
    # because vehicle_on stays "on" through real stops. Updated on every
    # metric-change tick; checked by the idle watchdog.
    last_movement_ts: datetime | None = None
    # GPS samples accumulated during the trip — list of (ts, lat, lon).
    # Persisted to trip_positions on close so the dashboard can render the
    # route map. Sampled by the live-tick callback so cadence is bound to
    # _LIVE_TICK (30 s by default).
    gps_samples: list[tuple[datetime, float, float]] = field(default_factory=list)
    # v0.5.13: provenance of soc_start, set by resolve_soc_start.
    soc_start_source: str | None = None
    # v0.5.13: independent kWh estimator via ∫|power| dt. When a power
    # sensor is configured, every _async_power_changed tick adds a
    # trapezoid; on close we compare this against the SoC-derived energy
    # and pick the more pessimistic (= larger) value so consumption is
    # never under-reported due to stale SoC.
    energy_from_power_kwh: float = 0.0
    last_abs_power_kw: float | None = None


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
        self._plug_sensor = merged.get(CONF_PLUG_SENSOR)
        # v0.5.35 — optional polling-pause sensor. When ON during a
        # synth-trip window we tag confidence as
        # 'reconstructed_polling_paused'.
        self._polling_paused_sensor = merged.get(CONF_POLLING_PAUSED_SENSOR)
        # v0.5.31 — ABRP wiring. Only instantiate the client when BOTH
        # token and api_key are present; otherwise the feature stays
        # off completely (no requests, no logs).
        abrp_token = (merged.get(CONF_ABRP_TOKEN) or "").strip()
        abrp_api_key = (merged.get(CONF_ABRP_API_KEY) or "").strip()
        self._abrp_car_model = (merged.get(CONF_ABRP_CAR_MODEL) or "").strip() or None
        if abrp_token and abrp_api_key:
            self._abrp: AbrpClient | None = AbrpClient(
                async_get_clientsession(hass), abrp_api_key, abrp_token,
            )
            _LOGGER.info(
                "ABRP enabled (car_model=%s)", self._abrp_car_model or "unset",
            )
        else:
            self._abrp = None
        # Monotonic timestamp of the last successful ABRP push. Used to
        # throttle: with BYD's bursty cloud-poll cadence (multiple
        # metric_changed events within ~1 s), we'd otherwise spam ABRP.
        self._abrp_last_send: float = 0.0
        # v0.5.32 — user-configurable throttle (min seconds between
        # consecutive pushes). Clamped to [5, 600].
        self._abrp_interval_s: int = max(5, min(600, int(
            merged.get(CONF_ABRP_PUSH_INTERVAL_S, DEFAULT_ABRP_PUSH_INTERVAL_S)
        )))
        # v0.5.32 — runtime kill-switch driven by the new
        # switch.<device>_abrp_push entity. Defaults to ON so users
        # who don't have an automation get the same UX as before;
        # automations replicating the old plugin (vehicle_on=on →
        # push on, charging V2C → push off) toggle this flag.
        self.abrp_push_enabled: bool = True
        self._location = merged.get(CONF_LOCATION)
        self._temp = merged.get(CONF_TEMP)
        self._speed = merged.get(CONF_SPEED)

        self._battery_capacity = float(
            merged.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
        )
        self._dcfc_threshold_kw = float(
            merged.get(CONF_DCFC_THRESHOLD_KW, DEFAULT_DCFC_THRESHOLD_KW)
        )
        # How long to wait without movement (odo change or speed > 0) before
        # force-closing an open trip. Configurable so cloud-polling
        # integrations with slow odo cadence (e.g. Tesla Fleet ~5 min)
        # can use a longer threshold than those with 1-min cadence.
        self._idle_trip_timeout_s = max(
            60,
            int(merged.get(CONF_IDLE_TRIP_TIMEOUT_MIN, DEFAULT_IDLE_TRIP_TIMEOUT_MIN)) * 60,
        )
        self._min_distance = float(
            merged.get(CONF_MIN_TRIP_DISTANCE, DEFAULT_MIN_TRIP_DISTANCE)
        )
        self._idle_timeout = int(merged.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT))
        self._recent_limit = max(1, int(merged.get(CONF_RECENT_LIMIT, DEFAULT_RECENT_LIMIT)))
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
        # v0.5.16 — vehicle_on off-edge debounce. Holds the most recent
        # off-edge timestamp so a follow-up on→off within
        # _VEHICLE_ON_OFF_DEBOUNCE_S can be detected and re-coalesced.
        self._pending_close_unsub: CALLBACK_TYPE | None = None

        # Reverse-geocode cache keyed on rounded (lat, lon) → friendly label.
        # Rounded to 4 decimal places (~10 m), which dedupes hits in the same
        # parking spot across many trips. Cleared on integration reload.
        self._geocode_cache: dict[tuple[float, float], str] = {}
        # v0.5.13: ring buffer of (timestamp, soc%) — populated by
        # _async_metric_changed whenever the battery entity reports a
        # fresh sample, regardless of trip state. Used by
        # _resolve_soc_start to anchor trip start to the freshest
        # pre-vehicle_on reading available (cf. design doc § 1).
        self._soc_history: deque[tuple[datetime, float]] = deque(maxlen=_SOC_BUFFER_MAX)
        # v0.5.25 — every poll event (battery / odo / location tick)
        # snapshots the location entity into this ring buffer. Used to:
        # (a) seed active.gps_samples at trip open so even the first
        #     poll has a real start anchor,
        # (b) reconstruct the route of synthetic trips that never had
        #     a live tick, and
        # (c) persist intermediate route points to trip_positions for
        #     the dashboard map.
        self._gps_history: deque[tuple[datetime, float, float]] = deque(
            maxlen=_GPS_BUFFER_MAX
        )
        self._listeners: list[Callable[[], None]] = []
        self._trip_log_listeners: list[Callable[[], None]] = []

    @property
    def entry_id(self) -> str:
        return self.entry.entry_id

    @property
    def battery_capacity(self) -> float:
        return self._battery_capacity

    @property
    def recent_limit(self) -> int:
        """How many rows the recent_* list sensors expose."""
        return self._recent_limit

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

    async def _async_lat_lon_at(
        self, entity_id: str, when: datetime
    ) -> tuple[float, float] | None:
        """Resolve lat/lon attrs of `entity_id` from recorder history at `when`.

        Looks 30 min before / 5 min after the target moment. Picks the
        most recent state whose timestamp is ≤ `when` (the value the
        car likely had at trip start/end). Returns None on any failure
        (recorder unavailable, no states in window, missing attrs).
        """
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.history import (  # noqa: PLC0415
                state_changes_during_period,
            )
        except Exception:  # pragma: no cover — recorder always present
            return None
        start = when - timedelta(minutes=30)
        end = when + timedelta(minutes=5)
        try:
            recorder = get_instance(self.hass)
            result = await recorder.async_add_executor_job(
                state_changes_during_period,
                self.hass, start, end, entity_id,
            )
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.debug(
                "Recorder GPS lookup failed for %s @ %s: %s",
                entity_id, when, exc,
            )
            return None
        states = result.get(entity_id, []) if isinstance(result, dict) else []
        if not states:
            return None
        # Sort by timestamp; pick the latest state ≤ when, else fall
        # back to the earliest > when (better than nothing).
        try:
            sorted_states = sorted(states, key=lambda s: s.last_updated)
        except Exception:
            sorted_states = list(states)
        candidates = [s for s in sorted_states if s.last_updated <= when]
        pick = candidates[-1] if candidates else sorted_states[0]
        try:
            lat = float(pick.attributes.get("latitude"))
            lon = float(pick.attributes.get("longitude"))
        except (TypeError, ValueError):
            return None
        return (lat, lon)

    async def _async_backfill_gps(self) -> None:
        """One-shot: fill in start_lat/lon and end_lat/lon for trips with
        NULL GPS coords by querying the recorder for the location entity's
        history at each trip's started_at / ended_at.

        Bounded to 50 trips per startup (the same cap as the geocode
        backfill it chains into). The location entity history typically
        only survives ~10 days in HA's recorder, so older trips will
        return no result — those stay unresolved. After GPS is filled,
        the geocode backfill resolves them to street/town.
        """
        if not self._location:
            return
        try:
            pending = await self.storage.async_trips_missing_gps(limit=50)
        except Exception as err:  # pragma: no cover — defensive
            _LOGGER.debug("GPS backfill: list query failed: %s", err)
            return
        if not pending:
            return
        _LOGGER.info("GPS backfill: %d trip(s) to resolve", len(pending))
        filled = 0
        for row in pending:
            try:
                started = datetime.fromisoformat(row["started_at"])
                ended = datetime.fromisoformat(row["ended_at"])
            except (TypeError, ValueError):
                continue
            start_coords = await self._async_lat_lon_at(self._location, started)
            end_coords = await self._async_lat_lon_at(self._location, ended)
            if not (start_coords or end_coords):
                continue
            await self.storage.async_update_trip_gps(
                row["id"],
                start_lat=start_coords[0] if start_coords else None,
                start_lon=start_coords[1] if start_coords else None,
                end_lat=end_coords[0] if end_coords else None,
                end_lon=end_coords[1] if end_coords else None,
            )
            filled += 1
        if filled:
            _LOGGER.info(
                "GPS backfill: filled %d trip(s); kicking geocode backfill",
                filled,
            )
            # Trigger the address resolver for the newly-coord'd rows.
            self.hass.async_create_task(self._async_backfill_geocodes())

    async def _async_backfill_geocodes(self) -> None:
        """Fill in start_address/end_address on historical trips with GPS.

        Rate-limited to ~1 req/sec per Nominatim's usage policy. Stops at
        50 trips per startup to avoid hammering the API. Idempotent: trips
        that already have an address are skipped by the SQL.
        """
        try:
            pending = await self.storage.async_trips_needing_geocode(limit=50)
        except Exception as err:
            _LOGGER.debug("Geocode backfill: list query failed: %s", err)
            return
        if not pending:
            return
        _LOGGER.info("Geocode backfill: %d trip(s) to resolve", len(pending))
        filled = 0
        for row in pending:
            start_addr = None
            end_addr = None
            if row.get("start_lat") is not None and not row.get("start_address"):
                start_addr = await self._async_reverse_geocode(
                    row["start_lat"], row["start_lon"]
                )
                await asyncio.sleep(1.0)  # Nominatim politeness
            if row.get("end_lat") is not None and not row.get("end_address"):
                end_addr = await self._async_reverse_geocode(
                    row["end_lat"], row["end_lon"]
                )
                await asyncio.sleep(1.0)
            if start_addr or end_addr:
                await self.storage.async_update_trip_addresses(
                    row["id"], start_address=start_addr, end_address=end_addr,
                )
                filled += 1
        if filled:
            _LOGGER.info("Geocode backfill: filled %d trip(s)", filled)
            self.last_trip = await self.storage.async_get_last()
            self._notify_trip_log_listeners()

    async def _async_reverse_geocode(
        self, lat: float | None, lon: float | None
    ) -> str | None:
        """Resolve lat/lon to a short human-readable label via Nominatim.

        Cached by rounded (lat, lon) so repeat trips to the same parking
        spot don't repeatedly hit the API. Best-effort: returns None on
        any failure (rate limit, timeout, network down) without blocking
        the trip-close path.
        """
        if lat is None or lon is None:
            return None
        # v0.5.16 — sharper key (~1.1 m at the equator). The previous
        # 4-decimal rounding (~11 m) collapsed adjacent shops/parking
        # spots to the same key, so trip B inherited trip A's address.
        # That label was then PERSISTED via async_update_trip_addresses,
        # producing the "mezcla direcciones" reports.
        key = (round(float(lat), 5), round(float(lon), 5))
        if key in self._geocode_cache:
            return self._geocode_cache[key]
        try:
            session = async_get_clientsession(self.hass)
            params = {
                "lat": str(lat), "lon": str(lon),
                "format": "json", "zoom": "17", "addressdetails": "1",
                # Spanish-language preference so labels match the user's locale;
                # Nominatim still falls back to local name when es isn't set.
                "accept-language": "es",
            }
            headers = {
                "User-Agent": (
                    "hass-ev-trip-logger/0.5.35 "
                    "(https://github.com/boraita/hass-ev-trip-logger)"
                ),
            }
            async with session.get(
                "https://nominatim.openstreetmap.org/reverse",
                params=params, headers=headers, timeout=8,
            ) as resp:
                if resp.status != 200:
                    # 403/429 are Nominatim explicitly rejecting us
                    # (UA block, rate-limit). Log loud so the user knows
                    # — silent debug previously hid systemic failures.
                    if resp.status in (403, 429):
                        _LOGGER.warning(
                            "Nominatim rejected reverse geocode "
                            "(%s,%s): HTTP %d — back off / verify UA",
                            lat, lon, resp.status,
                        )
                    return None
                data = await resp.json()
        except Exception as exc:  # pragma: no cover — network can fail
            _LOGGER.debug("Reverse geocode failed (%s,%s): %s", lat, lon, exc)
            return None
        a = data.get("address") or {}
        # Prefer the most specific recognisable place; fall back through
        # standard OSM hierarchy.
        primary = (
            a.get("amenity") or a.get("shop") or a.get("tourism")
            or a.get("building") or a.get("road")
            or a.get("neighbourhood") or a.get("suburb")
            or a.get("hamlet") or a.get("village") or a.get("town")
            or a.get("city") or data.get("name")
        )
        locality = a.get("city") or a.get("town") or a.get("village") or a.get("suburb")
        label = primary or data.get("display_name", "").split(",")[0].strip()
        if primary and locality and primary != locality:
            label = f"{primary}, {locality}"
        # v0.5.14 — never cache or return an empty string. The previous
        # behaviour persisted "" via COALESCE, the backfill SQL didn't
        # repick it (WHERE … IS NULL excludes ""), and the dashboard's
        # `start_address or origin` evaluated "" as falsy → fell to
        # `not_home`. End result: trips locked into a permanent
        # not_home label even after Nominatim had been queried.
        if not label:
            return None
        self._geocode_cache[key] = label
        return label

    def _trip_cost_price_per_kwh(self) -> float:
        """€/kWh used to compute trip cost — ALWAYS the configured home tariff.

        Trips don't carry the price of "the last charge" forward. An external
        DC-fast at €0.40 or a free public charger were one-off events; the
        car's battery holds a mix of home + external energy and trip cost is
        more honestly modelled as `energy × home tariff`. Individual charge
        records keep their actual price (free / home / DC) in their own row.
        """
        return float(self._energy_price)

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
        # Robust journey resume — derive from the actual trip log rather
        # than from `last_trip.destination` (which can be wrong if the
        # device_tracker lagged at close time or if an earlier amend
        # corrupted it). The storage query finds the first journey-
        # tagged trip after the most recent home-arrival; if any, that
        # journey is still open.
        self.current_journey_id = await self.storage.async_resolve_open_journey_id(
            self.home_zone
        )
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

        # One-shot heal: re-cost every trip from its preceding charge's
        # price. Catches users whose CONF_ENERGY_PRICE was 0 at trip-close
        # time, or whose set_last_charge_price corrections never
        # propagated. Idempotent and cheap.
        try:
            healed = await self.storage.async_recompute_trip_costs_from_charges(
                default_price=self._energy_price
            )
            if healed:
                _LOGGER.info("Startup heal: recomputed cost on %d trip(s)", healed)
                self.last_trip = await self.storage.async_get_last()
        except Exception as err:  # pragma: no cover — defensive
            _LOGGER.debug("Trip cost heal failed (non-fatal): %s", err)

        # v0.5.20 — one-shot GPS backfill from recorder history first,
        # which then chains into the geocode backfill so trips logged
        # before v0.5.3 (synth, no GPS) get street/town labels too. The
        # backfill is bounded (50 rows max) and idempotent. If the
        # recorder no longer holds the device_tracker history for an
        # older trip (default retention 10 days), that row stays
        # unresolved — the geocoder backfill will pick it up later
        # if/when coords become available another way.
        if self._location:
            self.hass.async_create_task(self._async_backfill_gps())
        else:
            self.hass.async_create_task(self._async_backfill_geocodes())

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
            self._maybe_resume_charge()
        else:
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._async_on_ha_started
            )

    @callback
    def _async_on_ha_started(self, _event: Event) -> None:
        self._maybe_resume_trip()
        self._maybe_resume_charge()

    def _maybe_resume_trip(self) -> None:
        """Open a trip at startup only when vehicle_on=on AND odo/soc are readable.

        Why: at startup, sensors restored from history may still report unknown
        before the integration loads. Opening a trip then would record a wrong
        odometer_start. We skip and rely on the next vehicle_on transition.

        v0.5.18 — require a FRESH vehicle_on edge before resuming. HA
        caches the last sensor state across restarts via the recorder;
        if the off-edge happened during the downtime, we'd open a
        phantom trip from a stale "on" reading. Audit on 2026-06-08
        showed trip #126 likely opened this way at 21:40 after a
        restart, stayed open 10 h through the overnight charge.
        """
        if self.current is not None:
            return
        st = self.hass.states.get(self._vehicle_on)
        if st is None or st.state != STATE_ON:
            return
        last_changed = st.last_changed
        if last_changed is not None:
            age = (dt_util.now() - last_changed).total_seconds()
            if age > 300:  # 5 min — anything older is presumed stale
                _LOGGER.info(
                    "Skipping resume: vehicle_on=on but last edge was %.1f "
                    "min ago — likely the off-edge happened during HA "
                    "downtime",
                    age / 60.0,
                )
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

    def _maybe_resume_charge(self) -> None:
        """Resume a ChargeInProgress at startup if a charge is already on.

        Why: ChargeInProgress lives only in memory. If HA (or this
        integration) restarts mid-charge, the charging=on event has
        already fired before the listener registered, and the next
        charging=off arrives to a coordinator that thinks no charge is
        open → handler silently no-ops and the entire session is lost.
        Mirroring _maybe_resume_trip closes this gap.

        We use the sensor's last_changed as `started_at` (the true edge
        from off→on), and the current SoC as a best-effort `soc_start`
        — slightly off from the real charge-start SoC, but vastly
        better than dropping the session.
        """
        if self._charge_sensor is None:
            return
        if self.current_charge is not None:
            return
        st = self.hass.states.get(self._charge_sensor)
        if st is None or st.state != STATE_ON:
            return
        soc = self._read_float(self._battery)
        started = st.last_changed or dt_util.now()
        self.current_charge = ChargeInProgress(
            started_at=started, soc_start=soc, last_seen_soc=soc
        )
        _LOGGER.info(
            "Resumed mid-charge session at startup (started %s, soc≈%s)",
            started.isoformat(), soc,
        )

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
            # v0.5.16 — if a debounced close is pending from a recent
            # off-edge, this on event means it was a flicker (BYD cloud-
            # poll sometimes sends on→off→on within 1-2 s). Cancel the
            # pending close, leave the trip open, return.
            if self._pending_close_unsub is not None:
                self._pending_close_unsub()
                self._pending_close_unsub = None
                _LOGGER.info(
                    "vehicle_on=on cancelled a pending close — flicker absorbed"
                )
                return
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
                # v0.5.16 — mutual exclusion: a charge session must end
                # before a trip opens. Chain via an async helper so the
                # close completes BEFORE _open_trip, letting
                # _resolve_soc_start consume the freshly-closed
                # last_charge.soc_end as the new trip's anchor.
                if self.current_charge is not None:
                    _LOGGER.info(
                        "vehicle_on=on with charge in progress — "
                        "closing charge before opening trip"
                    )
                    self.hass.async_create_task(
                        self._async_close_charge_then_open_trip(now)
                    )
                else:
                    self._open_trip(now)
        elif self.current is not None:
            # v0.5.16 — debounced close. Captures the off timestamp so
            # the trip's ended_at reflects the actual off-edge, not the
            # debounce expiry. If a fresh on arrives before the timer
            # fires, the close is cancelled above and the trip stays
            # open.
            if self._pending_close_unsub is not None:
                self._pending_close_unsub()
                self._pending_close_unsub = None
            off_ts = now

            @callback
            def _debounced_close(_at: datetime) -> None:
                self._pending_close_unsub = None
                self.hass.async_create_task(self._async_close_trip(off_ts))

            self._pending_close_unsub = async_call_later(
                self.hass, _VEHICLE_ON_OFF_DEBOUNCE_S, _debounced_close
            )

    @callback
    def _async_metric_changed(self, event: Event[EventStateChangedData]) -> None:
        """Notify listeners on odometer / battery change, even when idle.

        Also recovers a missed-resume: if vehicle_on is on but no trip is
        open (because _maybe_resume_trip ran while BYD hadn't yet repopulated
        odometer/battery after a HA restart), the first fresh metric arrival
        opens the trip retroactively. Without this, every HA restart during
        a real drive silently swallows the entire trip.
        """
        # Feed the SoC ring buffer whenever the battery entity emits a
        # fresh sample. Resolved later by _resolve_soc_start.
        new_state = event.data.get("new_state")
        if new_state is not None and new_state.entity_id == self._battery:
            try:
                soc_val = float(new_state.state)
            except (TypeError, ValueError):
                soc_val = None
            if soc_val is not None:
                self._soc_history.append((dt_util.now(), soc_val))

        # v0.5.25 — every cloud poll snapshot the location entity. Most
        # cloud-polled integrations refresh battery, odometer and
        # location together, so a metric tick is a strong signal the
        # tracker is also fresh. Captures the position even when no
        # trip is open yet (synth-trip case).
        self._capture_location_sample()

        # v0.5.31 — opportunistically push to ABRP. Throttled inside
        # _async_maybe_send_abrp so a burst of metric updates only
        # generates one outbound request. No-op when ABRP isn't
        # configured.
        if self._abrp is not None:
            self.hass.async_create_task(self._async_maybe_send_abrp())

        # v0.5.30 (issue #4) — relax: open the trip as soon as the
        # ODOMETER is readable. BYD's cloud-polled sensors arrive
        # offset (odo and battery seconds-to-minutes apart), so the
        # old "both must be non-None" gate left real trips to the
        # synthetic path. soc_start is filled in by _resolve_soc_start
        # which already handles None gracefully (charge-end anchor,
        # ring buffer, current value fallbacks). Battery's
        # subsequent ticks update last_seen_soc as usual.
        if (
            self.current is None
            and self._read_bool(self._vehicle_on) is True
            and self._read_float(self._odometer) is not None
        ):
            self._open_trip(dt_util.now())
            return

        if self.current_charge is not None:
            soc = self._read_float(self._battery)
            if soc is not None:
                self.current_charge.last_seen_soc = soc

        # Idle watchdog — any odo change while a trip is open counts as
        # evidence the car is actually moving. The entity_id check lets us
        # ignore battery-only ticks for the odometer signal (`new_state` was
        # already read at the top of this handler to feed the SoC buffer).
        # v0.5.15 — ALSO update last_seen_odometer / last_seen_soc here so
        # the trip-close path doesn't depend on `current_snapshot` having
        # been called by a sensor poll between the last data tick and the
        # vehicle_on=off event. Cloud-polled cars can go several minutes
        # between sensor polls; we now own the latest values directly.
        if self.current is not None and new_state is not None:
            if new_state.entity_id == self._odometer:
                self.current.last_movement_ts = dt_util.now()
                try:
                    self.current.last_seen_odometer = float(new_state.state)
                except (TypeError, ValueError):
                    pass
            elif new_state.entity_id == self._battery:
                try:
                    self.current.last_seen_soc = float(new_state.state)
                except (TypeError, ValueError):
                    pass

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

        v0.5.16 — dwell guard: we defer the amend by _LOCATION_DWELL_MIN_S
        and re-check the state at that point. If the zone changed again
        in the interim (a flap), no amend fires. This stops the 40 s
        home→not_home→home GPS glitch from fragmenting a single drive.
        """
        # v0.5.25 — every location tick feeds the GPS ring buffer
        # (used to seed trip start anchors and to populate synthetic
        # trip routes). Runs even when the rest of this handler short-
        # circuits below — the route capture is unconditional.
        self._capture_location_sample()
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
        when = new_state.last_updated

        async def _deferred() -> None:
            await asyncio.sleep(_LOCATION_DWELL_MIN_S)
            # Re-read NOW — if the zone changed back, this was a flap.
            current_loc = self._read_str(self._location)
            if current_loc != loc:
                _LOGGER.debug(
                    "Ignoring location flap: %s → %s within %.0f s",
                    loc, current_loc, _LOCATION_DWELL_MIN_S,
                )
                return
            await self._async_handle_late_zone_arrival(loc, when)

        self.hass.async_create_task(_deferred())

    async def _async_handle_late_zone_arrival(
        self, location: str, when: datetime
    ) -> None:
        if self.current is not None:
            return
        # Amend the last trip's destination if a late tracker resolution
        # arrives within the grace window. The journey closes ONLY as a
        # consequence of that amendment producing a home destination —
        # never on a standalone home flap. v0.5.13 used to close the
        # journey on ANY home reading while idle, which spuriously
        # closed journeys when the car sat at a remote location for
        # days and the device_tracker briefly flapped (cloud GPS noise,
        # geofence overshoot, restart of a presence integration, etc.).
        amended_to_home = False
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
                amended_to_home = self._is_at_home(location)
        if amended_to_home and self.current_journey_id is not None:
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

        v0.5.30 (issue #4) — suppress synth when vehicle_on is ON.
        The synthetic path is for "trip we missed" (vehicle_on never
        flipped on). If the ignition is on, the live path should
        handle the trip (retroactive _open_trip in metric_changed has
        been relaxed to fire on odo alone). Without this guard, BYD's
        offset cloud polls would still let synth race against the
        live open and record duplicate / wrong rows.
        """
        if self._read_bool(self._vehicle_on) is True:
            return
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
        # v0.5.16 — if the baseline is older than _MAX_SYNTH_BASELINE_AGE,
        # the user has either parked overnight or HA has been idle a
        # long time. Reusing such a stale prev_t as `started_at` produces
        # phantom 10 h trips that span overnight charging (SoC went UP
        # during the "trip", making soc_used negative). Treat it as a
        # fresh observation instead.
        if (now - prev_t) > _MAX_SYNTH_BASELINE_AGE:
            _LOGGER.debug(
                "Synth baseline stale (%.1f h old) — resetting to now",
                (now - prev_t).total_seconds() / 3600.0,
            )
            self._last_idle_odo = (now, odo, soc)
            return
        delta = odo - prev_odo

        if delta < self._min_distance:
            # Sub-threshold growth — the car hasn't meaningfully moved since the
            # baseline. Keep the odometer baseline (so cumulative distance across
            # sparse polls isn't lost) but advance its TIMESTAMP and SoC. While
            # the car sits parked — especially while it charges overnight — the
            # "last idle" moment moves forward and the battery refills. Freezing
            # the original snapshot back-dated the next real drive by hours and
            # made it inherit the pre-charge SoC, so a morning commute logged as
            # a multi-hour trip spanning the charge with soc_start < soc_end →
            # negative usage → no energy/cost/score. Refresh time+SoC, keep odo.
            self._last_idle_odo = (now, prev_odo, soc)
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
        price_per_kwh = self._trip_cost_price_per_kwh()
        cost = energy * price_per_kwh if energy and energy > 0 else None
        location_start = self.last_trip.destination if self.last_trip else None
        location_end = self._read_str(self._location) if self._location else None
        # v0.5.19 — sample the device_tracker's GPS coords at finalize
        # time. Synth trips previously persisted with NULL lat/lon,
        # which blocked the Nominatim backfill → every synth trip
        # ended up labelled "not_home" because there was nothing for
        # the geocoder to resolve. We only have the end coords (the
        # tracker's current position); start coords stay NULL since
        # we can't time-travel the device_tracker without HA recorder
        # lookups. The geocoder will still produce an end_address,
        # which the dashboard now prefers over the literal "not_home".
        # v0.5.25 — pull the full route from the GPS ring buffer.
        # Every cloud poll between started_at and ended_at fed a
        # sample; we now have a chronological list of (ts,lat,lon)
        # that covers the entire trip even though the live tick never
        # ran (synth = no self.current).
        route: list[tuple[datetime, float, float]] = [
            (ts, la, lo)
            for ts, la, lo in self._gps_history
            if started_at <= ts <= ended_at
        ]
        start_lat: float | None = route[0][1] if route else None
        start_lon: float | None = route[0][2] if route else None
        end_lat: float | None = route[-1][1] if route else None
        end_lon: float | None = route[-1][2] if route else None
        if end_lat is None and self._location:
            # Fall back to the tracker's current value when the buffer
            # is empty (e.g. brand-new install with no history yet).
            loc_state = self.hass.states.get(self._location)
            if loc_state is not None:
                try:
                    end_lat = float(loc_state.attributes.get("latitude"))
                    end_lon = float(loc_state.attributes.get("longitude"))
                except (TypeError, ValueError):
                    end_lat = end_lon = None
        started_from_home = self._is_at_home(location_start)
        is_at_home_end = self._is_at_home(location_end)
        # Same invariant as _async_close_trip — open journeys absorb
        # every stage until a home arrival closes them. No retroactive
        # closures (the band-aid that conflated GPS noise with real
        # home arrivals).
        journey_id: int | None
        if self.current_journey_id is not None:
            journey_id = self.current_journey_id
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
            start_lat=start_lat,
            start_lon=start_lon,
            end_lat=end_lat,
            end_lon=end_lon,
            gps_distance_km=(
                round(_route_distance_km(route), 2)
                if route and len(route) >= 2 else None
            ),
            # v0.5.35 — synth path. If a polling-pause sensor is wired
            # and it was ON at any point in the window, the upstream
            # manufacturer integration was sleeping → tag for low
            # confidence so the dashboard can warn the user.
            confidence=self._synth_confidence(started_at, ended_at),
        )

        # v0.5.27 — same charges-window attribution as the live close.
        prev_end_synth = self.last_trip.ended_at if self.last_trip else None
        if prev_end_synth is not None and prev_end_synth < started_at:
            before_s = await self.storage.async_charges_in_window(
                prev_end_synth, started_at,
            )
            record.kwh_charged_before = (
                round(before_s["kwh"], 2) if before_s["kwh"] > 0 else None
            )
        during_s = await self.storage.async_charges_in_window(started_at, ended_at)
        record.kwh_charged_during = (
            round(during_s["kwh"], 2) if during_s["kwh"] > 0 else None
        )

        trip_id = await self.storage.async_insert(record)
        record.trip_id = trip_id

        # v0.5.25 — persist the route so the dashboard map can render
        # intermediate waypoints, not just start/end. Same table that
        # live trips use via _async_close_trip.
        if route:
            await self.storage.async_insert_positions(trip_id, route)

        # v0.5.19 — geocode the end coord so dashboards can show a
        # street/town instead of "not_home". Same pattern as in
        # _async_close_trip's _geocode_async helper.
        if end_lat is not None and end_lon is not None:
            async def _geocode_synth() -> None:
                end_addr = await self._async_reverse_geocode(end_lat, end_lon)
                if end_addr:
                    await self.storage.async_update_trip_addresses(
                        trip_id, end_address=end_addr,
                    )
                    if self.last_trip and self.last_trip.trip_id == trip_id:
                        self.last_trip = replace(
                            self.last_trip, end_address=end_addr,
                        )
                    self._notify_trip_log_listeners()

            self.hass.async_create_task(_geocode_synth())

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
            # v0.5.16 — cable power is NOT trip energy. When a charge
            # session is in progress, return early so we don't integrate
            # the AC inrush or DC delivery into the trip's regen / power
            # estimator. The bug (confirmed empirically) is BYD reports
            # charging as negative power → the trip's regen_kwh and
            # energy_from_power_kwh balloon while the cable runs, then
            # `max(energy_soc, energy_pwr)` picks the inflated value at
            # close. Symptom: a stationary trip + charge gives a huge
            # max_power and impossible consumption.
            return
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
        prev_abs = self.current.last_abs_power_kw
        abs_now = abs(value)
        if prev_kw is not None and prev_ts is not None:
            dt_h = (now - prev_ts).total_seconds() / 3600.0
            # v0.5.15 — magnitude cap (250 kW) and a per-trapezoid
            # contribution clamp keep spikes from inflating the trip,
            # while the dt_h bound is generous enough (20 min) to
            # accept BYD-class cloud cadences instead of dropping every
            # sample. See _MAX_POWER_TRAPEZOID_DT_H comment for the
            # tradeoff.
            within_bounds = (
                0 < dt_h <= _MAX_POWER_TRAPEZOID_DT_H
                and abs(prev_kw) <= _MAX_PLAUSIBLE_POWER_KW
                and abs(value) <= _MAX_PLAUSIBLE_POWER_KW
            )
            if within_bounds:
                # Take the negative portion of each endpoint (regen only).
                a = -min(prev_kw, 0.0)
                b = -min(value, 0.0)
                self.current.regen_kwh += (a + b) / 2.0 * dt_h
                # v0.5.13 — independent kWh estimator. Trapezoid over
                # |power| gives the trip's gross throughput; we compare
                # against SoC-derived energy on close and keep the more
                # pessimistic value. Independent of any SoC sensor lag.
                #
                # v0.5.14 — bounds already enforced via within_bounds
                # above. Without them, a single BYD cloud-replay sample
                # with abs(power) ≈ 200 kW and dt_h ≈ 0.5 h would inject
                # ~100 kWh into one trip and blow up consumption.
                # v0.5.15 — per-trapezoid contribution clamp belt-and-
                # braces: even with both endpoints inside the magnitude
                # cap, a single tick shouldn't push more than ~5 kWh.
                if prev_abs is not None:
                    delta = (prev_abs + abs_now) / 2.0 * dt_h
                    if delta > _MAX_POWER_TRAPEZOID_CONTRIBUTION_KWH:
                        _LOGGER.warning(
                            "Capping outsized power trapezoid: %.2f kWh "
                            "(prev=%.1f kW, now=%.1f kW, dt=%.1f min)",
                            delta, prev_abs, abs_now, dt_h * 60.0,
                        )
                        delta = _MAX_POWER_TRAPEZOID_CONTRIBUTION_KWH
                    self.current.energy_from_power_kwh += delta
        self.current.last_power_kw = value
        self.current.last_power_ts = now
        self.current.last_abs_power_kw = abs_now
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
        # Movement signal — any non-zero speed resets the idle watchdog.
        if value > 0:
            self.current.last_movement_ts = dt_util.now()
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
            # v0.5.18 — if a trip is "open" when a legitimate
            # charging=on arrives, the trip is almost certainly stuck
            # (vehicle_on never reported off, or a stale resume opened
            # a phantom). The physical reality is "user is plugged in,
            # so they're not driving". Force-close the trip first, then
            # open the charge. This reverses the v0.5.16 "ignore" rule,
            # which dropped legitimate overnight charges when a trip
            # was phantom-open. The trip's ended_at uses the
            # last_movement_ts (best-known last drive moment) so the
            # trip's duration isn't inflated by the upcoming charge.
            if self.current is not None:
                ts = (
                    self.current.last_movement_ts
                    if self.current.last_movement_ts is not None
                    else now
                )
                _LOGGER.info(
                    "charging=on with trip open — force-closing trip "
                    "(presumed stuck) at %s before opening charge",
                    ts.isoformat(),
                )
                self.hass.async_create_task(
                    self._async_close_then_open_charge(ts, now)
                )
                return
            soc = self._read_float(self._battery)
            self.current_charge = ChargeInProgress(
                started_at=now, soc_start=soc, last_seen_soc=soc
            )
            _LOGGER.debug("Charge session opened at %s, soc=%s", now, soc)
            self._notify_listeners()
        elif self.current_charge is not None:
            self.hass.async_create_task(self._async_close_auto_charge(now))

    async def _async_close_then_open_charge(
        self, close_ts: datetime, now: datetime
    ) -> None:
        """Force-close the stuck trip, then open a fresh charge session.

        Used when charging=on fires while self.current is non-None,
        which is physically impossible (EV cannot drive while charging).
        The trip is almost certainly a phantom from a prior stale resume
        or a missed off-edge. We close it cleanly using the last-known
        movement time so the trip's duration matches reality.
        """
        await self._async_close_trip(close_ts)
        if self.current_charge is None:
            soc = self._read_float(self._battery)
            self.current_charge = ChargeInProgress(
                started_at=now, soc_start=soc, last_seen_soc=soc
            )
            _LOGGER.debug(
                "Charge session opened after trip force-close: soc=%s", soc
            )
            self._notify_listeners()

    async def _async_maybe_send_abrp(self) -> None:
        """Build a TLM payload from current sensor readings and push to ABRP.

        Throttled by ABRP_MIN_SEND_INTERVAL_S so a metric burst (BYD's
        cloud-poll can emit several state changes within a second)
        doesn't flood the endpoint. Skipped entirely if the client
        isn't configured.

        Sign note: our power sensor is in kW with the standard EV
        convention **+discharge / -charge**. ABRP wants kW with the
        SAME convention. `build_tlm` historically negated for byd-
        vehicle's raw cloud reading, so we pre-negate the W value here
        to cancel that negation and pass through the correct sign.
        """
        if self._abrp is None or not self.abrp_push_enabled:
            return
        import time as _time  # noqa: PLC0415 — local to keep import light
        now_mono = _time.monotonic()
        if now_mono - self._abrp_last_send < self._abrp_interval_s:
            return
        # Snapshot every value we feed into ABRP up-front (read once).
        soc = self._read_float(self._battery)
        power_kw = self._read_float(self._power) if self._power else None
        # build_tlm expects W with BYD-gl convention (+charge/-discharge)
        # and will negate. Our power_kw is +discharge/-charge, so
        # convert to W and negate → after build_tlm's negation we get
        # back to our (and ABRP's) +discharge/-charge convention.
        power_w_for_tlm: float | None = None
        if power_kw is not None:
            power_w_for_tlm = -float(power_kw) * 1000.0
        speed = self._read_float(self._speed) if self._speed else None
        odo = self._read_float(self._odometer)
        ext_temp = self._read_float(self._temp) if self._temp else None
        lat: float | None = None
        lon: float | None = None
        if self._location:
            loc_state = self.hass.states.get(self._location)
            if loc_state is not None:
                try:
                    lat = float(loc_state.attributes.get("latitude"))
                    lon = float(loc_state.attributes.get("longitude"))
                except (TypeError, ValueError):
                    lat = lon = None
        is_charging: bool | None = None
        if self._charge_sensor:
            is_charging = self._read_bool(self._charge_sensor)
        is_parked: bool | None = None
        veh_on = self._read_bool(self._vehicle_on)
        if veh_on is not None:
            is_parked = not veh_on
        tlm = build_tlm(
            soc=soc,
            power_w=power_w_for_tlm,
            speed=speed,
            lat=lat, lon=lon,
            is_charging=is_charging,
            is_parked=is_parked,
            ext_temp=ext_temp,
            est_range=None,  # no generic range sensor in our config
            odometer=odo,
            car_model=self._abrp_car_model,
        )
        # Need at least SoC to be a useful sample.
        if tlm.get("soc") is None:
            return
        try:
            ok = await self._abrp.send(tlm)
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.debug("ABRP push raised: %s", exc)
            return
        if ok:
            self._abrp_last_send = now_mono

    def _synth_confidence(
        self, started_at: datetime, ended_at: datetime
    ) -> str:
        """Decide the confidence tag for a synthetic trip.

        Returns 'reconstructed' by default; if the configured
        polling-pause sensor was ON anywhere inside [started_at,
        ended_at], returns 'reconstructed_polling_paused' (lowest
        confidence — the manufacturer integration was sleeping, so
        even the route's odometer ticks are sparse).
        """
        if not self._polling_paused_sensor:
            return "reconstructed"
        # Check the CURRENT state — a synth window is short (≤ a few
        # cloud-poll intervals) and the pause flag is usually sticky.
        # A more rigorous check would walk recorder history; we accept
        # the small chance of a brief unpause inside the window in
        # exchange for staying off the executor.
        if self._read_bool(self._polling_paused_sensor) is True:
            return "reconstructed_polling_paused"
        return "reconstructed"

    @callback
    def _capture_location_sample(self) -> None:
        """Snapshot the location entity's lat/lon into the GPS buffer.

        Cheap: skipped silently if no location entity configured, if
        the entity is not reporting valid coords, or if the new sample
        is identical to the last one in the buffer (no point storing
        duplicate cloud-cached values).
        """
        if not self._location:
            return
        state = self.hass.states.get(self._location)
        if state is None:
            return
        try:
            lat = float(state.attributes.get("latitude"))
            lon = float(state.attributes.get("longitude"))
        except (TypeError, ValueError):
            return
        now = dt_util.now()
        if self._gps_history:
            _, prev_lat, prev_lon = self._gps_history[-1]
            # ~1 m at the equator — drop duplicates from cloud cache
            # without losing real movement.
            if abs(lat - prev_lat) < 1e-5 and abs(lon - prev_lon) < 1e-5:
                return
        self._gps_history.append((now, lat, lon))
        # If a trip is open, also feed the live samples list so the
        # eventual close persists a dense route — bypasses the 30 s
        # live_tick cadence whenever a cloud poll lands faster.
        if self.current is not None:
            self.current.gps_samples.append((now, lat, lon))

    async def _async_close_charge_then_open_trip(self, now: datetime) -> None:
        """Force-close any open charge, THEN open the trip.

        Used when vehicle_on=on fires while current_charge is non-None.
        Awaiting the close persists last_charge so _resolve_soc_start
        for the new trip can use the freshly-captured soc_end as anchor
        (the most accurate option per the SoC-source design).
        """
        await self._async_close_auto_charge(now)
        if self.current is None:
            self._open_trip(now)


    async def _async_close_auto_charge(self, now: datetime) -> None:
        active = self.current_charge
        if active is None:
            return
        self.current_charge = None
        soc_end = self._read_float(self._battery) or active.last_seen_soc
        # Need at least 2 % SoC delta to count — cloud-polling can wobble by 1 %
        # while sitting plugged in (battery balancing, etc.) and we don't want
        # phantom 0.8 kWh "charges" stomping the user's price corrections.
        if (
            active.soc_start is None
            or soc_end is None
            or (soc_end - active.soc_start) < 2
        ):
            _LOGGER.debug("Discarding auto-charge: SoC delta < 2%%")
            self._notify_listeners()
            return

        if self.last_charge is not None:
            # Compare against `started_at` so a manual correction hours after
            # the original auto-detect doesn't open the window. Also widen
            # the dedup horizon to 2 h.
            ref_ts = self.last_charge.started_at or self.last_charge.ended_at
            elapsed = (now - ref_ts).total_seconds()
            # Time-based dedup: a real new charging session shouldn't follow
            # the previous one within 2 hours. price_locked alone must NOT
            # block insertion — that flag is for protecting the prior
            # record's price from auto-update, not for vetoing future
            # charges (that bug in v0.5.4 swallowed an entire overnight
            # session because the previous charge had been corrected).
            if elapsed < 7200:  # 2 h
                _LOGGER.debug(
                    "Skipping auto-charge: previous charge %.0fs ago "
                    "(price_locked=%s)",
                    elapsed, self.last_charge.price_locked,
                )
                self._notify_listeners()
                return

        kwh = (soc_end - active.soc_start) / 100.0 * self._battery_capacity
        extra_soc = float(soc_end) - float(active.soc_start)

        # Merge into the previous charge when the cable is STILL physically
        # connected. Multiple `charging` on/off pulses (battery balancing,
        # scheduled charging windows, sentry top-ups) inside one plugged
        # interval are the same session — we shouldn't fragment them.
        if (
            self._plug_sensor is not None
            and self._read_bool(self._plug_sensor) is True
            and self.last_charge is not None
        ):
            merged = await self.storage.async_extend_last_charge(
                extra_kwh=kwh, ended_at=now, extra_soc_pct=extra_soc,
            )
            if merged is not None:
                self.last_charge = merged
                _LOGGER.info(
                    "Merged %.2f kWh into charge #%s (cable still plugged in)",
                    kwh, merged.charge_id,
                )
                self.hass.bus.async_fire(
                    EVENT_CHARGE_LOGGED,
                    {"entry_id": self.entry_id, **merged.to_dict()},
                )
                self._notify_listeners()
                self._notify_trip_log_listeners()
                return

        # Location comes from the configured device_tracker (e.g. zone "home"); falls
        # back to "auto" so we can still tell auto-detected charges apart in the log.
        location = self._read_str(self._location) if self._location else None
        await self.async_log_charge_service(
            kwh=kwh,
            location=location or "auto",
            notes=f"auto-detected from {self._charge_sensor}",
            started_at=active.started_at,
            soc_start=active.soc_start,
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

    def _resolve_soc_start(
        self, now: datetime
    ) -> tuple[float | None, str]:
        """Return (soc_pct, source_tag) for a trip about to open.

        Designed to fight stale-SoC-at-vehicle-on on cloud-polled
        integrations (BYD, Tesla Fleet). Resolution order:

        (a) ``last_charge_end`` — when a charge finished within the last
            30 min, the plug just disconnected, and no charge is in
            progress, the charge's ending SoC is the most trustworthy
            anchor. Catches the exact bug the user reported: "charged
            overnight to 80 %, drove off, integration sees 79 %, records
            2 % consumption instead of 3 %".
        (b) ``pre_on_sample`` — freshest reading from the 5-min SoC ring
            buffer. Useful when (a) doesn't apply but the user's
            integration has emitted any SoC update very recently. On BYD
            the cadence is sparse (~8 min median) so this branch is rare
            but cheap.
        (c) ``post_on_sample`` — the current cached reading. Legacy
            behaviour. Subject to up to 1 % staleness on integer-SoC
            sensors like BYD's.
        """
        current = self._read_float(self._battery)

        # (a) Last charge end. Multi-gate to avoid overcounting when the
        # car has been sitting too long or has driven between charges:
        #  - charge ended ≤ 12 h ago
        #  - no charge currently in progress
        #  - plug disconnected (when a plug sensor is wired)
        #  - we haven't driven since the charge ended
        #  - current SoC hasn't drained more than _POST_CHARGE_DRAIN_BUDGET_PCT
        #    below soc_end (caps vampire-drain over-counting)
        #  - soc_end is not BELOW current (would indicate a top-up the
        #    integration missed; trust the live reading instead)
        lc = self.last_charge
        lt = self.last_trip
        no_drive_since_charge = (
            lc is not None
            and lc.ended_at is not None
            and (lt is None or lt.ended_at is None or lt.ended_at <= lc.ended_at)
        )
        if (
            lc is not None
            and lc.soc_end is not None
            and lc.ended_at is not None
            and (now - lc.ended_at) <= _POST_CHARGE_ANCHOR_WINDOW
            and self.current_charge is None
            and no_drive_since_charge
        ):
            plug_disconnected = (
                self._plug_sensor is None
                or self._read_bool(self._plug_sensor) is False
            )
            soc_end_f = float(lc.soc_end)
            drain_ok = (
                current is None
                or (soc_end_f - current) <= _POST_CHARGE_DRAIN_BUDGET_PCT
            )
            not_below_current = current is None or soc_end_f >= current - 0.5
            if plug_disconnected and drain_ok and not_below_current:
                return soc_end_f, "last_charge_end"

        # (b) Freshest sample within the pre-on lookback window.
        cutoff = now - _PRE_ON_LOOKBACK
        for ts, soc in reversed(self._soc_history):
            if ts < cutoff:
                break
            # SoC should be ≥ current — the car only drains after on.
            # If pre < current, the buffer entry is stale or a top-up
            # we missed; fall through.
            if current is None or soc >= current - 0.5:
                return float(soc), "pre_on_sample"
            break

        # (c) Fallback to whatever the integration currently reports.
        if current is not None:
            return float(current), "post_on_sample"
        return None, "unavailable"

    def _open_trip(self, now: datetime) -> None:
        # If a synth-trip finalize was pending, cancel it — the live trip
        # will own the distance from here on.
        if self._unsub_synth_finalize is not None:
            self._unsub_synth_finalize()
            self._unsub_synth_finalize = None
        self._synth_baseline = None
        odometer = self._read_float(self._odometer)
        soc, soc_source = self._resolve_soc_start(now)
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
            last_movement_ts=now,  # treat trip start as the first movement
            soc_start_source=soc_source,
        )
        # v0.5.25 — seed gps_samples with the most-recent buffered
        # sample (if any within the lookback window) so even very short
        # trips that close before _async_live_tick fires still have a
        # real start anchor. Live_tick continues appending while the
        # trip runs.
        cutoff = now - timedelta(seconds=_PRE_TRIP_GPS_LOOKBACK_S)
        for ts, lat, lon in reversed(self._gps_history):
            if ts < cutoff:
                break
            self.current.gps_samples.append((ts, lat, lon))
            break  # most recent only — preserves "start of trip" semantics
        _LOGGER.debug(
            "Trip opened at %s odo=%s soc=%s (source=%s)",
            now, odometer, soc, soc_source,
        )
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
        if self.current is None:
            return
        now = dt_util.now()

        # Idle watchdog — force-close ONLY when vehicle_on is also off,
        # which indicates we missed the off-edge entirely (cloud-poll
        # cycle skipped it). If vehicle_on is still on, the user is
        # just stopped briefly (running an errand, traffic, parked at
        # a destination) and the trip should remain open until the
        # off-edge actually arrives. v0.5.17 and earlier split a single
        # drive into two records when the user paused > 10 min mid-
        # cycle (audited: trips #123/#124 from cycle C 19:55-20:24).
        last_move = self.current.last_movement_ts
        if last_move is not None and (
            (now - last_move).total_seconds() > self._idle_trip_timeout_s
        ):
            vehicle_on = self._read_bool(self._vehicle_on)
            if vehicle_on is False:
                _LOGGER.info(
                    "Idle watchdog: no movement for %.0fs AND vehicle_on=off — "
                    "force-closing (missed off-edge)",
                    (now - last_move).total_seconds(),
                )
                self.hass.async_create_task(self._async_close_trip(last_move))
                return
            # vehicle_on still on (or unknown): log once and continue.
            # Don't keep spamming the log — only emit every ~5 min.
            if int((now - last_move).total_seconds()) % 300 < _LIVE_TICK.total_seconds():
                _LOGGER.debug(
                    "Idle watchdog: %.0fs without movement but vehicle_on=on — "
                    "leaving trip open",
                    (now - last_move).total_seconds(),
                )

        # GPS sampling — read configured location entity's lat/lon attributes
        # and store one sample per tick while the trip is open. The samples
        # are persisted on close via storage.async_insert_positions.
        if self._location:
            state = self.hass.states.get(self._location)
            if state is not None:
                lat = state.attributes.get("latitude")
                lon = state.attributes.get("longitude")
                if lat is not None and lon is not None:
                    try:
                        self.current.gps_samples.append(
                            (now, float(lat), float(lon))
                        )
                    except (TypeError, ValueError):
                        pass
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
            # v0.5.15 — log at INFO, not DEBUG. Silent discards make
            # missing-trip diagnoses impossible. Include all the values
            # that explain why the trip dropped so the user can see it
            # in HA logs without enabling debug.
            _LOGGER.info(
                "Discarding short trip: distance=%.2f km < min=%.2f km "
                "(odo=%s→%s, soc=%s→%s, duration=%.1f min). "
                "If you expected this drive to log, raise CONF_MIN_TRIP_DISTANCE "
                "or check that the odometer sensor is refreshing during trips.",
                distance, self._min_distance,
                active.odometer_start, odometer_end,
                active.soc_start, soc_end, duration_min,
            )
            self.current = None
            self._notify_listeners()
            return

        soc_used = (
            (active.soc_start - soc_end)
            if active.soc_start is not None and soc_end is not None
            else None
        )
        energy_soc = (
            (soc_used / 100.0) * self._battery_capacity
            if soc_used is not None and soc_used > 0
            else None
        )
        # v0.5.13 — power-integration backup. ∫|P|dt accumulated during
        # the trip is an independent estimator that doesn't depend on the
        # SoC sensor's cadence. We pick the larger of the two so a stale
        # SoC reading can never under-report consumption.
        energy_pwr = (
            active.energy_from_power_kwh
            if self._power and active.energy_from_power_kwh > 0
            else None
        )
        candidates = [e for e in (energy_soc, energy_pwr) if e is not None and e > 0]
        if candidates:
            energy = max(candidates)
            energy_source = (
                "power_integration"
                if energy_pwr is not None and energy_pwr >= (energy_soc or 0)
                else "soc"
            )
        else:
            energy = None
            energy_source = None
        # v0.5.15 — inline fallback when both SoC delta and power
        # integration come back empty. For BYD's integer-step SoC
        # (1 % resolution) any short trip that doesn't cross a 1 %
        # boundary has soc_used=0, and the power-integration trapezoid
        # is rejected when cloud-polling cadence is > 3 min — every
        # such trip would otherwise persist with NULL energy and only
        # heal on the next restart. Estimating from the user's own
        # distance-weighted average kWh/100km is more useful than blank.
        if energy is None and distance > 0:
            try:
                avg_per_100 = await self.storage.async_avg_consumption_kwh_per_100km()
            except Exception:  # pragma: no cover — defensive
                avg_per_100 = None
            if avg_per_100 and avg_per_100 > 0:
                energy = distance * avg_per_100 / 100.0
                energy_source = "estimated"
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
        price_per_kwh = self._trip_cost_price_per_kwh()
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

        # Journey membership — v0.5.14 clean invariant:
        #
        #   A journey is the sequence of trips between leaving home and
        #   returning home, no matter how many days elapse between
        #   intermediate trips. It opens iff a trip starts from home and
        #   ends away; closes iff a trip ends at home.
        #
        # Time gaps are irrelevant. Removed in this rewrite:
        #   - The "retroactively close journey when stage opens from
        #     home" band-aid, which conflated GPS-noise destinations
        #     with legitimate home arrivals.
        #   - Reliance on last_trip.destination at restart (handled in
        #     async_start via storage.async_resolve_open_journey_id).
        is_at_home_end = self._is_at_home(location_end)
        started_from_home = self._is_at_home(active.location_start)
        journey_id: int | None
        stitched_orphan_home = False
        if self.current_journey_id is not None:
            # Open journey absorbs this stage regardless of where it
            # started. The journey only closes on a home arrival.
            journey_id = self.current_journey_id
        elif started_from_home:
            # Mint a new journey id. If this trip also ends at home (a
            # short home→home round trip), it closes immediately as a
            # one-stage journey.
            journey_id = await self.storage.async_next_journey_id()
        elif is_at_home_end:
            # v0.5.16 — orphan home-arrival stitching. The audit showed
            # ~30 % of historical journeys had their closing home leg
            # logged with journey_id=NULL because device_tracker GPS
            # noise (or an old amend bug) had already closed the open
            # journey before this trip arrived home. Treat this trip as
            # the closing stage of its own one-stage journey so journey
            # aggregates always include the arrival.
            journey_id = await self.storage.async_next_journey_id()
            stitched_orphan_home = True
        else:
            # Orphan stage — not part of any journey. Happens when the
            # device_tracker missed the previous trip's home departure.
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
            # GPS endpoints picked from the live-tick sampler's buffer.
            # When no location entity is wired (or no samples accumulated)
            # they stay None and the dashboard falls back to text-based
            # Google-Maps links.
            start_lat=(active.gps_samples[0][1] if active.gps_samples else None),
            start_lon=(active.gps_samples[0][2] if active.gps_samples else None),
            end_lat=(active.gps_samples[-1][1] if active.gps_samples else None),
            end_lon=(active.gps_samples[-1][2] if active.gps_samples else None),
            soc_start_source=active.soc_start_source,
            energy_source=energy_source,
            energy_from_power=(
                round(active.energy_from_power_kwh, 4)
                if active.energy_from_power_kwh > 0 else None
            ),
            gps_distance_km=(
                round(_route_distance_km(active.gps_samples), 2)
                if active.gps_samples and len(active.gps_samples) >= 2 else None
            ),
            # v0.5.35 — live path always tags as 'live' (precise
            # times + full metrics).
            confidence="live",
        )

        # v0.5.27 — attribute pre/intra-trip charging energy. Lets the
        # dashboard show "+24 kWh antes de este trip" so a SoC bump
        # between consecutive trips is explained instead of looking
        # like a sensor glitch. The during-window should be ~0 thanks
        # to v0.5.18 mutex but we capture it for edge cases.
        prev_end = self.last_trip.ended_at if self.last_trip else None
        if prev_end is not None and prev_end < active.started_at:
            before = await self.storage.async_charges_in_window(
                prev_end, active.started_at,
            )
            record.kwh_charged_before = (
                round(before["kwh"], 2) if before["kwh"] > 0 else None
            )
        during = await self.storage.async_charges_in_window(
            active.started_at, now,
        )
        record.kwh_charged_during = (
            round(during["kwh"], 2) if during["kwh"] > 0 else None
        )

        trip_id = await self.storage.async_insert(record)
        record.trip_id = trip_id

        # v0.5.30 (issue #5) — when we just auto-stitched a new
        # one-stage journey for this home arrival, retro-absorb any
        # orphan trips (journey_id=NULL) since the last home arrival
        # so the journey actually represents the full casa→…→casa
        # chain instead of showing as a single-row 1-stage journey.
        if stitched_orphan_home and journey_id is not None:
            absorbed = await self.storage.async_absorb_orphans_into_journey(
                journey_id, self.home_zone,
            )
            if absorbed:
                _LOGGER.info(
                    "Auto-stitch: absorbed %d orphan trip(s) into journey #%s",
                    absorbed, journey_id,
                )

        # Persist GPS route samples accumulated during the trip.
        if active.gps_samples:
            await self.storage.async_insert_positions(trip_id, active.gps_samples)

        # Reverse-geocode start/end coords for any trip that doesn't end at
        # a named HA zone (zones are already informative on their own).
        # Best-effort — Nominatim failure leaves the field NULL.
        async def _geocode_async() -> None:
            start_addr = await self._async_reverse_geocode(record.start_lat, record.start_lon)
            end_addr = await self._async_reverse_geocode(record.end_lat, record.end_lon)
            if start_addr or end_addr:
                await self.storage.async_update_trip_addresses(
                    trip_id, start_address=start_addr, end_address=end_addr,
                )
                if self.last_trip and self.last_trip.trip_id == trip_id:
                    self.last_trip = replace(
                        self.last_trip,
                        start_address=start_addr or self.last_trip.start_address,
                        end_address=end_addr or self.last_trip.end_address,
                    )
                self._notify_trip_log_listeners()

        self.hass.async_create_task(_geocode_async())

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
        soc_start: float | None = None,
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
            # v0.5.16 — guard divide-by-near-zero. A sub-minute session
            # (charge-sensor flicker on/off in seconds) would compute an
            # astronomical avg_kw and misclassify a normal AC pulse as
            # DCFC. Require ≥3 min of duration AND a physically possible
            # avg_kw before classifying.
            if duration_h >= 0.05:
                avg_kw = kwh / duration_h
                if avg_kw <= 400:
                    is_dcfc = avg_kw > self._dcfc_threshold_kw

        record = ChargeRecord(
            started_at=started_at,
            ended_at=now,
            kwh=kwh,
            price_per_kwh=price_per_kwh,
            total_cost=total_cost,
            currency=currency or self._currency,
            soc_start=soc_start,
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
        charge_id: int | None = None,
    ) -> ChargeRecord | None:
        """Override price / location of a charge already in storage.

        Pass `charge_id` to target a specific row (useful for correcting an
        older external charge whose price you only just found out). Omit it
        to target the most-recent charge (the default — same as before).

        Use case: auto-detect logged a charge with the home default price, but
        you actually paid a public-charger rate. Pass price_per_kwh or
        total_cost (one of them) and the kWh + timestamp stay; price + cost
        are recomputed.
        """
        if charge_id is not None:
            updated = await self.storage.async_update_charge_by_id(
                charge_id,
                price_per_kwh=price_per_kwh,
                total_cost=total_cost,
                location=location,
                notes=notes,
            )
        else:
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
        # Re-cost every trip from the most-recent-before charge's price.
        # Correcting a charge's price retroactively fixes the trips that
        # used that energy. Trips with no prior charge fall back to the
        # configured home tariff.
        n = await self.storage.async_recompute_trip_costs_from_charges(
            default_price=self._energy_price
        )
        if n:
            _LOGGER.info("Recomputed cost on %d trip(s) after price correction", n)
            self.last_trip = await self.storage.async_get_last()
            self._notify_trip_log_listeners()
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

    async def async_set_trip_service(
        self, *, trip_id: int, fields: dict[str, Any]
    ) -> TripRecord | None:
        """User-driven patch of a historical trip. See storage._update_trip
        for the field whitelist. Refreshes `self.last_trip` if the updated
        row is currently exposed by sensors, and re-emits a recompute pass
        so cost/consumption recalculate when energy/distance/journey
        change.
        """
        updated = await self.storage.async_update_trip(trip_id, fields)
        if updated is None:
            _LOGGER.warning(
                "set_trip: trip_id=%s not found or no editable fields", trip_id,
            )
            return None
        # Refresh in-memory caches.
        if self.last_trip is not None and self.last_trip.trip_id == trip_id:
            self.last_trip = updated
        # If energy/distance/journey changed, re-run the recompute pass
        # so derived sums (today/week/month) reflect the correction.
        try:
            await self.storage.async_recompute_trip_costs_from_charges(
                default_price=self._energy_price
            )
        except Exception:  # pragma: no cover — defensive
            pass
        # Also re-resolve the open journey: if the user changed
        # journey_id or destination, the resume may now be different.
        self.current_journey_id = await self.storage.async_resolve_open_journey_id(
            self.home_zone
        )
        self.last_completed_journey_id = (
            await self.storage.async_last_completed_journey_id(self.current_journey_id)
        )
        self.last_trip = await self.storage.async_get_last()
        self._notify_listeners()
        self._notify_trip_log_listeners()
        _LOGGER.info(
            "set_trip: trip_id=%s patched fields=%s", trip_id, list(fields.keys()),
        )
        return updated

    async def async_set_charge_service(
        self, *, charge_id: int, fields: dict[str, Any]
    ) -> ChargeRecord | None:
        """User-driven patch of a historical charge."""
        updated = await self.storage.async_patch_charge(charge_id, fields)
        if updated is None:
            _LOGGER.warning(
                "set_charge: charge_id=%s not found or no editable fields",
                charge_id,
            )
            return None
        if self.last_charge is not None and self.last_charge.charge_id == charge_id:
            self.last_charge = updated
        else:
            self.last_charge = await self.storage.async_get_last_charge()
        self.hass.bus.async_fire(
            EVENT_CHARGE_LOGGED,
            {"entry_id": self.entry_id, **updated.to_dict()},
        )
        self._notify_listeners()
        self._notify_trip_log_listeners()
        _LOGGER.info(
            "set_charge: charge_id=%s patched fields=%s",
            charge_id, list(fields.keys()),
        )
        return updated

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
        price_per_kwh = self._trip_cost_price_per_kwh()
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
        price_per_kwh = self._trip_cost_price_per_kwh()
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
