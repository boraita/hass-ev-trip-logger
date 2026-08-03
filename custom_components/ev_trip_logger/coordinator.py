"""Trip detection state machine."""
from __future__ import annotations

import json
import logging
import math
import asyncio
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from collections.abc import Sequence
from typing import Any, Callable, Final

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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .abrp import AbrpClient, build_tlm
from .elevation import (
    compute_elevation_stats,
    downsample_route,
    fetch_elevations,
)
from .const import (
    ABRP_MIN_SEND_INTERVAL_S,
    DOMAIN,
    CONF_ABRP_API_KEY,
    CONF_ABRP_CAR_MODEL,
    CONF_ABRP_PUSH_INTERVAL_S,
    CONF_ABRP_TOKEN,
    DEFAULT_ABRP_PUSH_INTERVAL_S,
    CONF_BATTERY,
    CONF_BATTERY_CAPACITY,
    CONF_ELEVATION_PROVIDER,
    CONF_ELEVATION_PROVIDER_URL,
    CONF_IDLE_POWER_ESTIMATE_KW,
    CONF_VEHICLE_MODEL,
    CONF_CHARGE_SENSOR,
    CONF_CURRENCY,
    CONF_ENERGY_PRICE,
    CONF_ENERGY_PRICE_ENTITY,
    CONF_HOME_ZONE,
    CONF_SECONDARY_HOME_ZONES,
    CONF_SECONDARY_HOME_COORDS,
    DEFAULT_SECONDARY_HOME_RADIUS_M,
    CONF_IDLE_TIMEOUT,
    CONF_LOCATION,
    CONF_MIN_TRIP_DISTANCE,
    CONF_DRIVER_SENSOR,
    CONF_PLUG_SENSOR,
    CONF_POLLING_PAUSED_SENSOR,
    CONF_LAST_TRIP_ENERGY_SENSOR,
    CONF_LAST_TRIP_DISTANCE_SENSOR,
    CONF_POWER_SIGN_INVERTED,
    CONF_EVSE_POWER_SENSOR,
    CONF_TRACKED_SENSORS,
    CONF_ODOMETER,
    CONF_DCFC_THRESHOLD_KW,
    CONF_IDLE_TRIP_TIMEOUT_MIN,
    CONF_POWER,
    CONF_SPEED,
    CONF_RANGE_SENSOR,
    CONF_HEADING_SENSOR,
    CONF_CABIN_TEMP_SENSOR,
    CONF_HVAC_SETPOINT_SENSOR,
    CONF_TIRE_PRESSURE_FL_SENSOR,
    CONF_TIRE_PRESSURE_FR_SENSOR,
    CONF_TIRE_PRESSURE_RL_SENSOR,
    CONF_TIRE_PRESSURE_RR_SENSOR,
    CONF_BATTERY_CHEMISTRY,
    CONF_TEMP,
    CONF_VEHICLE_FIRST_REGISTERED,
    CONF_WEATHER_ENTITY,
    DEFAULT_BATTERY_CHEMISTRY,
    CONF_RECENT_LIMIT,
    CONF_VEHICLE_ON,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_ELEVATION_PROVIDER,
    DEFAULT_IDLE_POWER_ESTIMATE_KW,
    DEFAULT_DCFC_THRESHOLD_KW,
    DEFAULT_IDLE_TRIP_TIMEOUT_MIN,
    DEFAULT_CURRENCY,
    DEFAULT_ENERGY_PRICE,
    DEFAULT_HOME_ZONE,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MIN_TRIP_DISTANCE,
    DEFAULT_RECENT_LIMIT,
    DRIVER_NONE_STATES,
    EVENT_CHARGE_LOGGED,
    EVENT_TRIP_ENDED,
    EVENT_TRIP_STARTED,
)
from .storage import ChargeRecord, TripRecord, TripStorage, period_start

_LOGGER = logging.getLogger(__name__)


def _load_cohort_baselines() -> dict[str, dict[str, Any]]:
    """v0.6.3 — read the seeded `cohort_baselines.json` once at import.

    Returns the {model_key: {label, chemistry, nameplate_kwh,
    cohort_new_kwh, source}} mapping. The "_meta" key is filtered out
    so callers can iterate values as model rows. Any read/parse error
    degrades gracefully to an empty dict — the SoH model then keeps
    its v0.5.x nameplate behaviour, no warnings spamming the log.
    """
    path = Path(__file__).with_name("cohort_baselines.json")
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:  # pragma: no cover — defensive
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}


_COHORT_BASELINES: Final = _load_cohort_baselines()


def cohort_baseline_options() -> list[tuple[str, str]]:
    """[(model_key, human_label), …] — used by the config flow to
    populate the optional `CONF_VEHICLE_MODEL` dropdown."""
    rows = [
        (k, str(v.get("label") or k))
        for k, v in _COHORT_BASELINES.items()
    ]
    rows.sort(key=lambda kv: kv[1])
    return rows


_INVALID_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""}

# Tracker states that carry no zone information. When origin/destination
# resolves to one of these, the GPS-coords zone fallback kicks in
# (v0.5.44) so journey open/close logic isn't blinded by a stale tracker.
_NON_ZONE_STATES = frozenset({"not_home", "unknown", "unavailable", "none", ""})


def _is_zoneless(location: str | None) -> bool:
    """True when the tracker state names no zone (not_home/unknown/...)."""
    return not location or location.strip().casefold() in _NON_ZONE_STATES


# v0.5.47 — max age of a GPS sample for the zone-from-coords fallback.
# A stale last-route point can be a MID-ROUTE position captured before
# the car actually arrived; resolving a zone from it could close a
# journey on a false "home". Older than this → trust nothing (NULL is
# better than wrong).
_ZONE_FALLBACK_MAX_AGE = timedelta(minutes=10)
_LIVE_TICK = timedelta(seconds=30)
# v0.5.41 — force a fresh upstream poll on the location entity every
# N ticks while a trip is open. Cloud-polled integrations (BYD,
# Tesla Fleet) typically push location every ~8 min on their natural
# cadence; without a nudge we get 3–4 GPS points on a 30 min drive.
# update_entity asks the platform to refresh on demand; integrations
# that support it (BYD does) push fresh lat/lon shortly after.
# v0.5.42 — relaxed to 10 ticks (= 300 s / 5 min). Every-2-min was
# polling BYD ~4x harder than its natural cadence; 5 min still adds
# 1–2 extra samples per 30 min trip without risking rate-limit on the
# shared BYD account.
_LOCATION_REFRESH_EVERY_N_TICKS = 10
# ~1 m at the equator — drop duplicates from cloud cache without
# losing real movement. Shared between _capture_location_sample and
# _async_live_tick so both branches dedupe consistently.
_GPS_DUP_EPSILON = 1e-5
# Wait this long without further odo growth before committing a synthetic
# trip. Cloud-polling sources emit small odo deltas every ~1-2 min during a
# drive; the window must be longer than the polling interval so we don't
# fragment one drive into many micro-trips.
_SYNTH_COALESCE_WINDOW_S = 300
# v0.5.77 — delay after trip insert before we re-read the vehicle's
# native `last_trip_energy` sensor. Cloud-polled integrations (BYD,
# some Tesla setups) update this 1-3 min after the vehicle finishes
# the trip. 240 s sits comfortably past that lag without making the
# user wait noticeably for the corrected cost.
_VEHICLE_TRIP_HEAL_DELAY_S = 240.0
# v0.5.77 — distance tolerance when cross-checking that the vehicle
# sensor refers to OUR just-closed trip. ≥1 km absolute or 20 %
# relative — both must be exceeded to reject the override.
_VEHICLE_TRIP_DIST_TOL_KM = 1.0
_VEHICLE_TRIP_DIST_TOL_PCT = 0.20
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
# v0.5.40 — snap-on-short-park. When the previous trip ended very
# recently and no charge has happened since, anchor the new trip's
# soc_start to the previous trip's soc_end IF the apparent gap is
# within integer-quantization + BMS-settle noise. Eliminates the
# +1 % phantom drop observed on ~53 % of consecutive trips on BYD-
# class integer-SoC integrations (BMS rounds down 1–2 % after the
# pack relaxes; no real drain). Beyond 30 min parked, real vampire
# drain becomes plausible, so we don't snap.
_SHORT_PARK_SNAP_WINDOW = timedelta(minutes=30)
_SHORT_PARK_SNAP_GAP_PCT = 2.0
# v0.5.41 — orphan-trip detection between consecutive trips. When a
# new trip opens and the captured odometer_start is materially larger
# than the previous trip's odometer_end, a real drive was missed (a
# full on→off→on cycle slipped between two upstream cloud polls, or
# the previous close captured a stale odometer reading). We insert a
# synthetic TripRecord between them rather than absorb the km/SoC
# into the new trip (which would inflate its consumption) or leave
# the gap unexplained on the dashboard.
#
# Lower bound 0.3 km keeps quantization noise from triggering. Upper
# bound 200 km guards against odometer sensor glitches / unit-changes
# (anything bigger is implausibly large vs idle drain or a missed
# drive, and we'd rather log a warning than fabricate a trip record).
_ORPHAN_MIN_KM_GAP = 0.3
_ORPHAN_MAX_KM_GAP = 200.0
# How long after the previous trip closed we still attempt to classify
# an orphan. Beyond 12 h the SoC↔km correlation becomes noisy (vampire
# drain compounds, the user may have manually shuffled the car around
# in ways we can't model), so we leave the gap unexplained.
_ORPHAN_MAX_DURATION_S = 12 * 3600
# Default consumption when we don't have a recent 30 d average yet
# (used to compute the expected SoC drop for the missed km). 15 kWh/100km
# is the typical EV range; the ratio gate below accepts ±2x of this so
# the precise default barely matters in practice.
_ORPHAN_DEFAULT_KWH_100KM = 15.0
# Orphan-classification ratio (observed SoC drop / expected SoC drop
# from km × consumption). Anything outside this band is treated as
# inconsistent — we still record the orphan, but with confidence
# 'orphan_odo_only' (km only, energy fields left NULL) to flag that
# the SoC didn't track the km the way a real drive would.
_ORPHAN_RATIO_MIN = 0.5
_ORPHAN_RATIO_MAX = 2.0
# v0.8.1 — floor on the average speed an 'orphan' (SoC-consistent) window
# can imply before we treat it as padded by parked/offline time rather than
# driving. A short HA restart (or any gap the live path + recorder recovery
# both miss) still uses last_trip.ended_at -> now as the window, which bakes
# in however long HA was down as if it were part of the drive. 15 km/h is a
# conservative floor (well below sustained city-traffic speed); below it we
# cap the window to the longest duration still compatible with "drove at
# least this fast" instead of reporting hours of mostly-parked time as the
# trip's duration.
_ORPHAN_MIN_PLAUSIBLE_AVG_KMH = 15.0
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
# v0.5.53 — grace window where vehicle_on=off does NOT close the trip.
# Covers brief stops mid-trip (red lights, parking-lot wait, pickup):
# byd-trip-stats and BYDMate both treat the off-edge as a timer, not an
# event, so a quick off→on sequence within the grace just keeps the
# trip open. When the timer expires, the close happens with
# `ended_at = car_off_since` (NOT `now`), so the grace doesn't inflate
# duration. Live snapshot updates pause as soon as the off arrives —
# the sensors don't keep ticking during the grace window.
_VEHICLE_OFF_GRACE_S = 180.0
# v0.5.79 — stuck-trip watchdog. The idle watchdog (_async_live_tick)
# only force-closes when vehicle_on=off. When the upstream integration
# (BYD cloud poll, Tesla Fleet) goes offline for hours, vehicle_on may
# stay stuck at its last value, OR the off-edge gets lost entirely.
# Net effect: self.current never clears and the dashboard shows a stale
# "current_trip_distance=11 km" for hours after the car has been off.
#
# This watchdog is a defence-in-depth periodic check independent of the
# live-tick (which only registers when a trip opens). It fires every
# _STUCK_TRIP_TIMER_INTERVAL regardless and force-closes trips that:
#  - Have not seen movement for _STUCK_TRIP_NO_MOVEMENT_MIN minutes AND
#    vehicle_on is currently off (lost off-edge), or
#  - Are older than _STUCK_TRIP_MAX_AGE_H hours regardless of vehicle_on
#    (upstream wedged; we can't trust its state machine).
# Closed trips get a `confidence` tag so the user can spot reconstructed
# closes on the dashboard.
_STUCK_TRIP_NO_MOVEMENT_MIN = 60.0
_STUCK_TRIP_MAX_AGE_H = 4.0
_STUCK_TRIP_TIMER_INTERVAL = timedelta(minutes=5)
# v0.6.4 — how often the trailing-30d weighted-avg tariff cache is
# refreshed. Trips render `cost_at_avg_tariff` from this cache (sync
# attribute reader, can't await), so a 10-min cadence keeps it
# fresh-enough without hammering storage.
_AVG_TARIFF_REFRESH: Final = timedelta(minutes=10)
# v0.5.53 — telemetry silence watchdog. If no odometer/battery sample
# arrives for this long while a trip is "open", we assume the upstream
# polling has died and the car was actually parked. Close retroactively
# at `last_telemetry_ts`, not `now`, to avoid pinning a 2-hour
# duration to a 20-minute drive whose polling silently stalled.
_TELEMETRY_SILENCE_TIMEOUT_S = 480.0  # 8 min
# v0.5.80 — hard ceiling on how far back we'll reconstruct a missed
# drive when the cloud integration comes back from a long silence.
# 24 h covers a full day of polling outage (rare even for BYD) while
# keeping us from inserting stale trips after an install pause or HA
# downtime that spans multiple days.
_ORPHAN_DISCONNECT_MAX_AGE = timedelta(hours=24)
# v0.5.49 — live-path retry on the vehicle_on=on edge. Cloud-polled
# integrations (BYD, Tesla Fleet) often raise vehicle_on a poll-cycle
# before the odometer entity catches up to the fresh value. Previously
# the live opener bailed silently and every trip fell to the synthetic
# path — which loses regen, max_power, max_speed and temperature samples.
# Now we kick `homeassistant.update_entity` and re-check at these
# offsets while vehicle_on stays ON. The chain is cancelled on any
# off-edge or once another path opens the trip first.
_LIVE_OPEN_RETRY_DELAYS_S: tuple[float, ...] = (15.0, 30.0, 60.0, 120.0)
# v0.8.3 — how old an odometer reading may be at a live vehicle_on=on
# edge before we distrust it as the new trip's start anchor. `_read_float`
# happily returns a stale-but-valid `hass.states` value when the cloud
# source hasn't polled recently — indistinguishable from a fresh one by
# value alone. Left unchecked, a short vehicle_on blip that opens+closes
# faster than the cloud can deliver a sample gets discarded as noise
# (`_async_close_trip`'s min-distance branch), but the NEXT trip then
# opens with this same stale odometer as its anchor: its distance silently
# absorbs the discarded blip's km (plus any cloud-silence gap since), while
# its duration only spans its own short on/off window — producing
# physically impossible average speeds. Routing a stale reading into the
# same retry chain as a missing one gives the cloud a chance to catch up
# before we anchor on it; if it never does, the trip still falls through
# to the synthetic/orphan path, which has its own gap-aware baseline.
_ODOMETER_STALE_MAX_AGE_S = 90.0
# v0.5.50 — score baseline calibration.
# Default 14.5 kWh/100km is the reference anchor — the kWh/100km that
# maps to 10/10 before any calibration kicks in. Once 10+ eligible trips
# exist, the P5 of `consumption_kwh_100km` (distance ≥ 5 km) replaces
# 14.5 — BUT the result is clamped to [14.5, 20.0]. The 14.5 floor is
# deliberate: per the user's spec, the calibration may RAISE the bar
# (Tesla needing 18 kWh/100km for 10/10 is realistic) but never LOWER
# it (a freak downhill trip at 5 kWh/100km can't make every later trip
# look terrible by anchoring at 5).
_SCORE_BASELINE_DEFAULT = 14.5
_SCORE_BASELINE_MIN_TRIPS = 10
_SCORE_BASELINE_MIN_DISTANCE_KM = 5.0
_SCORE_BASELINE_BOUNDS: tuple[float, float] = (
    _SCORE_BASELINE_DEFAULT,  # 14.5 — calibration can only RAISE the bar
    20.0,
)
# v0.5.51 — auto-calibration of effective battery capacity from real
# charges (kwh / ΔSoC × 100). 30 % ΔSoC threshold filters top-ups
# whose SoC quantization noise (±1 %) dwarfs the signal; 5-charge floor
# avoids a single freak charge anchoring the value; bounds keep a
# corrupted charge from suggesting an impossible 200 kWh pack.
_CAPACITY_MIN_DELTA_PCT = 30.0
_CAPACITY_MIN_CHARGES = 5
_CAPACITY_BOUNDS_RATIO: tuple[float, float] = (0.5, 1.5)
_CAPACITY_CHARGE_WINDOW = 30  # last N eligible charges
# v0.5.54 — degradation tracking. Persist a new row in `capacity_history`
# whenever the calibrated value moves by more than this threshold. Smaller
# drifts just update n_charges on the latest row (more samples, same
# conclusion). Keeps the history table from churning on every charge while
# still capturing real degradation steps.
_CAPACITY_HISTORY_MIN_DELTA_KWH = 0.5
# v0.5.57 — expected SoH model. Constants derived from research:
# Geotab 22,700 EV study 2025, Tesla 2023 Impact Report, ADAC VW ID.3
# Dauertest, Recurrent climate study, BYD warranty extension Jan 2026,
# MDPI Batteries 2024 LFP/NMC review, NREL Smith 2021/2022.
# Each row: (year-1 knee, calendar/yr after knee, cycle pp/1000 km,
# extra hot-climate pp/yr, cold multiplier, DCFC threshold %, DCFC
# pp/yr per % above threshold, 100%-SoC daily pp/yr).
_DEGRADATION_PROFILES: dict[str, dict[str, float]] = {
    "lfp": {
        "knee_year1_pct": 3.5,
        "calendar_pct_per_year": 1.0,
        "cycle_pct_per_1000km": 0.040,
        "climate_hot_extra_per_year": 0.10,
        "climate_cold_mult": 0.7,
        "dcfc_threshold_pct": 12.0,
        "dcfc_penalty_per_pct_above": 0.04,
        "soc_100_extra_per_year": 0.05,
    },
    "nmc": {
        "knee_year1_pct": 4.0,
        "calendar_pct_per_year": 1.8,
        "cycle_pct_per_1000km": 0.100,
        "climate_hot_extra_per_year": 0.70,
        "climate_cold_mult": 0.5,
        "dcfc_threshold_pct": 12.0,
        "dcfc_penalty_per_pct_above": 0.12,
        "soc_100_extra_per_year": 0.55,
    },
    "nca": {
        "knee_year1_pct": 5.0,
        "calendar_pct_per_year": 2.2,
        "cycle_pct_per_1000km": 0.110,
        "climate_hot_extra_per_year": 0.85,
        "climate_cold_mult": 0.5,
        "dcfc_threshold_pct": 12.0,
        "dcfc_penalty_per_pct_above": 0.15,
        "soc_100_extra_per_year": 0.65,
    },
}
# SoH never drops below this floor in the model — BYD's 70% warranty
# is the industry floor anyway. Real packs do degrade past 70% but
# the model is no longer reliable, and reporting 50% would alarm
# without insight.
_EXPECTED_SOH_FLOOR_PCT = 70.0
# v0.5.61 — `charge_sensor` accepts both `binary_sensor.*` (Home
# Assistant's classic on/off) and `sensor.*` with a state enum.
# Different vehicle integrations use different vocabularies:
#   * BYD / generic: 'on' / 'off'
#   * Tesla:         'Charging' / 'Disconnected' / 'Complete' /
#                    'Stopped' / 'NoPower' / 'Starting' / 'Engaged'
#   * OVMS:          'charging' / 'idle' / 'done' / 'stopped'
#   * Some bridges:  'true' / 'false' / '1' / '0'
# We treat ANY of the values below as "currently delivering energy
# to the battery" — everything else means "not charging right now".
# Case-insensitive on the user's side; the helper normalises.
_CHARGING_STATES: frozenset[str] = frozenset({
    "on", "true", "1",
    "charging", "starting", "engaged",
    "ac_charging", "dc_charging", "slow_charging", "fast_charging",
})
# Buckets for the health-vs-expected sensor.
_HEALTH_AHEAD_THRESHOLD_PP = 2.0   # observed ≥ expected + 2pp → ahead
_HEALTH_BEHIND_THRESHOLD_PP = 2.0  # observed ≤ expected − 2pp → behind
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


def _parse_secondary_home_coords(
    raw: str | None,
) -> list[tuple[float, float, float, str]]:
    """Parse CONF_SECONDARY_HOME_COORDS free text into (lat, lon, radius_m, label).

    One entry per line (blank lines / '#' comments ignored):
    "lat,lon", "lat,lon,radius_m", or "lat,lon,radius_m,label". Radius
    defaults to DEFAULT_SECONDARY_HOME_RADIUS_M when omitted; label
    defaults to "secondary_home_<n>" (n = 1-based line position among
    valid entries) so a coordinate-matched trip still gets a real
    destination string instead of staying "not_home". Malformed lines
    are skipped rather than raising, since this is user-typed free text
    with no schema validation.
    """
    if not raw:
        return []
    out: list[tuple[float, float, float, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            lat = float(parts[0])
            lon = float(parts[1])
            radius = float(parts[2]) if len(parts) >= 3 and parts[2] else DEFAULT_SECONDARY_HOME_RADIUS_M
        except (TypeError, ValueError):
            continue
        label = parts[3] if len(parts) >= 4 and parts[3] else f"secondary_home_{len(out) + 1}"
        out.append((lat, lon, radius, label))
    return out


def _route_distance_km(
    samples: "Sequence[tuple[Any, float, float]]",
) -> float | None:
    """Sum haversine segments across a sequence of (ts, lat, lon).
    None if fewer than 2 points. Accepts any indexable sequence (list,
    deque) of (ts, lat, lon)-shaped tuples.
    """
    if not samples or len(samples) < 2:
        return None
    total = 0.0
    for i in range(1, len(samples)):
        _, lat1, lon1 = samples[i - 1]
        _, lat2, lon2 = samples[i]
        total += _haversine_km(lat1, lon1, lat2, lon2)
    return total
def _pick_driver_for_window(
    timeline: Sequence[tuple[datetime, str]],
    start: datetime,
    end: datetime,
) -> str | None:
    """Pick the driver active during [start, end] from sensor history.

    `timeline` is the driver sensor's (timestamp, state) changes sorted
    ascending; it may begin before `start` (the state already active at
    window open). Returns the valid driver name with the longest overlap
    with the window, or None when nobody valid was connected.

    v0.5.44 — needed because on cloud-polled cars (BYD) vehicle_on
    rarely flips, so most trips take the synthetic path which never ran
    the live driver capture.
    """
    overlap: dict[str, float] = {}
    for i, (ts, raw) in enumerate(timeline):
        seg_start = max(ts, start)
        seg_end = min(timeline[i + 1][0], end) if i + 1 < len(timeline) else end
        if seg_end <= seg_start:
            continue
        cleaned = (raw or "").strip()
        if (
            not cleaned
            or cleaned in _INVALID_STATES
            or cleaned.casefold() in DRIVER_NONE_STATES
        ):
            continue
        overlap[cleaned] = (
            overlap.get(cleaned, 0.0) + (seg_end - seg_start).total_seconds()
        )
    if not overlap:
        return None
    return max(overlap, key=lambda k: overlap[k])


def _speed_stats(
    samples: Sequence[float],
    *,
    highway_threshold_kmh: float,
) -> tuple[float | None, float | None]:
    """v0.7.3 — return `(v95_speed_kmh, highway_ratio_pct)` from the
    live-tick speed sample deque.

    * `v95_speed_kmh` — 95th-percentile of samples ≥ 0. Uses the
      classic linear-interpolation nearest-rank definition: for n
      samples, idx = ceil(0.95 × n) − 1, clamped to n − 1. Robust
      to a single sensor spike (max_speed_kmh is already elsewhere).
    * `highway_ratio_pct` — fraction of samples ≥ `highway_threshold_kmh`
      × 100. Useful for the dashboard's "urban vs autopista" split.

    Both return None when the deque is empty (no speed sensor wired,
    or the trip closed before the first live-tick fired).
    """
    if not samples:
        return (None, None)
    ordered = sorted(s for s in samples if s is not None and s >= 0)
    if not ordered:
        return (None, None)
    n = len(ordered)
    idx = max(0, min(n - 1, int(0.95 * n) - (1 if 0.95 * n == int(0.95 * n) else 0)))
    # Simpler & bias-free: nearest-rank on the ceil convention.
    idx = min(n - 1, max(0, math.ceil(0.95 * n) - 1))
    v95 = ordered[idx]
    highway = sum(1 for s in ordered if s >= highway_threshold_kmh)
    highway_pct = highway / n * 100.0
    return (round(v95, 1), round(highway_pct, 1))


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

# v0.5.97 — driver-sensor pre/post window used by `_async_driver_during`
# and the live-close fallback. Wide enough to catch BT/AA flickers that
# pair briefly *before* ignition (the trip-191 case: AA went on at
# 15:20:19 and off at 15:20:39, four minutes before vehicle_on=on at
# 15:25:09). 5 min covers the typical "driver opens car, AA pre-pairs,
# user fumbles with the screen, then turns the key" sequence; 2 min on
# the post side covers AA holding for a moment after the off-edge.
# Recorder queries widen by these values; the time-overlap picker
# weights segments against the same widened window so a single brief
# pre-trip AA toggle is still enough to identify the driver.
_DRIVER_PRE_WINDOW_MIN: Final = 5.0
_DRIVER_POST_WINDOW_MIN: Final = 2.0
# Bound the startup heal sweep so a freshly-installed integration on a
# car with many existing trips can't spawn hundreds of recorder queries
# at boot. Recorder retention is typically ~10 days anyway — older
# trips will return no samples regardless.
_DRIVER_HEAL_LOOKBACK_DAYS: Final = 10
_DRIVER_HEAL_MAX_TRIPS: Final = 50

# v0.5.43 — hard caps on the per-trip in-memory accumulators. A trip
# whose vehicle_on gets stuck "on" for days (the known BYD cloud failure
# mode) would otherwise grow gps_samples/temp_samples without bound:
# the idle watchdog only force-closes when vehicle_on is OFF. 2880
# samples = 24 h at the 30 s live tick; beyond that the trip is junk
# anyway and we just stop wasting memory (deque evicts oldest).
_TRIP_GPS_SAMPLES_MAX = 2880
_TRIP_TEMP_SAMPLES_MAX = 2880
# v0.7.3 — speed sample deque for V95 / highway-ratio metrics. Same
# 2880 (24 h at 30 s live-tick) hard cap; a percentile over more
# samples than that adds no information and just wastes memory.
_TRIP_SPEED_SAMPLES_MAX = 2880
# Highway-speed threshold in km/h. Trip time spent ≥ this counts as
# "highway" in `highway_ratio_pct`. 80 km/h is the classic urban →
# extra-urban boundary in EU spec sheets and mirrors what ABRP's
# reference speed (110 km/h) sits comfortably above.
_HIGHWAY_SPEED_KMH: Final = 80.0


@dataclass
class TripInProgress:
    """In-memory accumulator for an active trip."""

    started_at: datetime
    odometer_start: float | None
    soc_start: float | None
    location_start: str | None
    temp_samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=_TRIP_TEMP_SAMPLES_MAX)
    )
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
    # GPS samples accumulated during the trip — (ts, lat, lon) tuples.
    # Persisted to trip_positions on close so the dashboard can render the
    # route map. Sampled by the live-tick callback so cadence is bound to
    # _LIVE_TICK (30 s by default). Bounded deque: see _TRIP_GPS_SAMPLES_MAX.
    gps_samples: deque[tuple[datetime, float, float]] = field(
        default_factory=lambda: deque(maxlen=_TRIP_GPS_SAMPLES_MAX)
    )
    # v0.5.13: provenance of soc_start, set by resolve_soc_start.
    soc_start_source: str | None = None
    # v0.5.13: independent kWh estimator via ∫|power| dt. When a power
    # sensor is configured, every _async_power_changed tick adds a
    # trapezoid; on close we compare this against the SoC-derived energy
    # and pick the more pessimistic (= larger) value so consumption is
    # never under-reported due to stale SoC.
    energy_from_power_kwh: float = 0.0
    last_abs_power_kw: float | None = None
    # v0.5.41 — counts live_tick invocations to schedule periodic
    # update_entity force-refreshes (denser GPS during the drive
    # than the upstream's natural cadence delivers).
    live_tick_count: int = 0
    # v0.6.6 — seconds the vehicle was on but standing still during
    # the trip (idling at lights, waiting with the AC on, drive-
    # through queues, etc). Lets the close path expose a "moving-
    # only" consumption number that excludes parked-but-on overhead.
    # Sampled by `_async_live_tick`: speed sensor preferred when
    # wired, falls back to `last_movement_ts` staleness.
    idle_seconds: float = 0.0
    # v0.7.3 — deterministic per-tick speed samples for V95 percentile
    # + highway-ratio computation. Requires CONF_SPEED wired (falls
    # back gracefully to None-metrics when not available). The live-
    # tick appends the current speed value each cycle so cadence is
    # deterministic (unlike raw sensor updates which vary by vendor);
    # this makes the percentile stable across cloud-polled cars that
    # report speed at wildly different frequencies. Chalmers 2024
    # QRNN paper ranked V95 as the 4th-strongest feature for trip-
    # level consumption prediction (Spearman ρ ≈ 0.29 vs distance's
    # ρ ≈ 0.98) — worth capturing per trip.
    speed_samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=_TRIP_SPEED_SAMPLES_MAX)
    )
    # v0.5.43 — driver identity from the configured driver sensor
    # (e.g. the car's bluetooth-connected-device entity). Captured at
    # open; re-checked on every live tick until it resolves, since BT
    # pairing often completes a few seconds after ignition.
    driver: str | None = None
    # v0.5.82 — accumulate the time-on-each-driver-value during the
    # trip so the close path can pick the LONGEST DOMINANT driver
    # instead of the brittle "first non-empty wins" rule. Fixes the
    # BYD/Tesla BT-race-at-open case where someone else's phone gets
    # paired first and the actual driver's connection arrives 30 s
    # later. Keys are the cleaned driver-sensor states, values are
    # accumulated seconds.
    driver_samples: dict[str, float] = field(default_factory=dict)
    _last_driver_sample_ts: datetime | None = None
    _last_driver_sample_value: str | None = None
    # v0.5.54 — snapshot of the configured weather.* entity at trip
    # open (start_*) and close (end_*). The end_* fields are filled
    # by `_async_close_trip` just before persistence; the trip row
    # then stores the START–END average (when both exist) or whichever
    # half is non-null. None when CONF_WEATHER_ENTITY is unset.
    # v0.5.68 — weather snapshot fields removed from the dataclass; the
    # logger no longer reads a weather entity.


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
    # v0.5.89 — integrate the car-side power sensor during the charge to
    # measure the actual kWh that landed in the battery, independent of
    # the SoC delta (which suffers from 1% quantization on most BYD-
    # class cars). Charging convention after `power_sign_inverted` flip:
    # negative value = battery receiving energy. We sum the absolute
    # value of the integral so the result is positive kWh in.
    energy_added_kwh: float = 0.0
    _last_power_kw_signed: float | None = None
    _last_power_ts: datetime | None = None
    # v0.5.89 — same integral but from the EVSE / wallbox side
    # (`CONF_EVSE_POWER_SENSOR`). Charger output is typically 5-15 %
    # higher than what the battery receives — AC→DC conversion losses
    # + onboard charger efficiency. Exposing both lets the dashboard
    # show real charging efficiency.
    evse_energy_kwh: float = 0.0
    _last_evse_kw: float | None = None
    _last_evse_ts: datetime | None = None
    # v0.6.0 — peak instantaneous |power_kw| seen during this session,
    # used to flag high-stress (>=100 kW) DCFC events for the SoH
    # accumulator (Geotab fleet study). Tracks the vehicle's own power
    # sensor; falls back to the EVSE sensor's max when only the
    # wallbox is wired. Persisted to charges.peak_charge_power_kw at
    # close (and propagated through merge updates).
    peak_charge_power_kw: float = 0.0


class EvTripLoggerCoordinator:
    """Tracks vehicle_on transitions and produces trip records."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        storage: TripStorage,
        version: str = "unknown",
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.storage = storage
        # Integration version from manifest.json (passed by async_setup_entry)
        # — used for outbound User-Agent strings. Never hardcode it here:
        # the release workflow only bumps the manifest.
        self._version = version

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
        # v0.5.85 — power-sensor polarity. Some integrations (BYD cloud)
        # report discharge as NEGATIVE; default convention is positive.
        # Toggling this flag flips the value before regen / energy
        # integration so accounting is correct.
        self._power_sign_inverted: bool = bool(
            merged.get(CONF_POWER_SIGN_INVERTED, False)
        )
        # v0.5.89 — optional EVSE / wallbox power sensor. When wired,
        # the integration tracks AC-side energy delivered during each
        # charge session. Auto-detects W vs kW from the entity's
        # unit_of_measurement at sample time.
        self._evse_power_sensor = merged.get(CONF_EVSE_POWER_SENSOR)
        # v0.5.77 — vehicle-native per-trip energy + distance sensors.
        # Used as ground truth at trip close to override the logger's
        # SoC-delta / power-integration estimates. Auto-detected from
        # the odometer prefix in async_start when not configured.
        self._last_trip_energy_sensor = merged.get(CONF_LAST_TRIP_ENERGY_SENSOR)
        self._last_trip_distance_sensor = merged.get(CONF_LAST_TRIP_DISTANCE_SENSOR)
        # v0.5.38 — list of external numeric sensors to roll up via the
        # HA recorder. The platform creates two AVG sensors per entry
        # (7-day and 30-day). Stored verbatim so multi-entry option
        # flows can edit / extend the list.
        tracked = merged.get(CONF_TRACKED_SENSORS) or []
        if isinstance(tracked, str):
            tracked = [tracked]
        self._tracked_sensors: list[str] = [
            str(eid) for eid in tracked if eid
        ]
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
        # v0.5.69 — CONF_TEMP may be empty; `_auto_detect_temp_sensor`
        # in `async_start` will set it from the entity registry if a
        # `sensor.<prefix>_exterior_temperature` exists. Doing it here
        # (in __init__) would race with HA loading the BYD integration's
        # entities — they may not be in the state machine yet.
        self._temp = merged.get(CONF_TEMP)
        # v0.5.68 — weather_entity dropped. Kept the config-key read so
        # an old entry doesn't blow up; we just log once and ignore the
        # value. `CONF_TEMP` is the canonical exterior-temp source now.
        self._weather_entity = None
        if merged.get(CONF_WEATHER_ENTITY):
            _LOGGER.info(
                "weather_entity is deprecated and ignored from v0.5.68 — "
                "configure CONF_TEMP (the car's exterior temperature "
                "sensor) instead. Real-time updates, better granularity, "
                "no extra HTTP. The other weather fields were never "
                "consumed by the logger."
            )
        # v0.5.57 — battery chemistry + first-registered date drive the
        # expected SoH model (see _DEGRADATION_PROFILES). Chemistry
        # defaults to LFP when the user doesn't specify, since most
        # >75 kWh packs sold from 2022+ are LFP-based.
        self._battery_chemistry = str(
            merged.get(CONF_BATTERY_CHEMISTRY, DEFAULT_BATTERY_CHEMISTRY)
        ).lower()
        if self._battery_chemistry not in _DEGRADATION_PROFILES:
            self._battery_chemistry = DEFAULT_BATTERY_CHEMISTRY
        first_reg = merged.get(CONF_VEHICLE_FIRST_REGISTERED)
        self._vehicle_first_registered: datetime | None = None
        if first_reg:
            try:
                # Accept either ISO 8601 datetime or YYYY-MM-DD date.
                parsed = (
                    datetime.fromisoformat(str(first_reg))
                    if "T" in str(first_reg)
                    else datetime.fromisoformat(str(first_reg) + "T00:00:00")
                )
                # v0.5.59 — dt_util.now() is tz-aware; subtracting a
                # naive datetime raises TypeError. Promote to UTC when
                # we got a bare date from the DateSelector.
                if parsed.tzinfo is None:
                    parsed = dt_util.as_local(parsed).astimezone(dt_util.UTC)
                self._vehicle_first_registered = parsed
            except ValueError:
                _LOGGER.warning(
                    "Invalid vehicle_first_registered=%r — ignoring", first_reg,
                )
        self._speed = merged.get(CONF_SPEED)
        # v0.8.0 — optional sensors fed to ABRP telemetry only.
        self._range = merged.get(CONF_RANGE_SENSOR)
        self._heading = merged.get(CONF_HEADING_SENSOR)
        self._cabin_temp = merged.get(CONF_CABIN_TEMP_SENSOR)
        self._hvac_setpoint = merged.get(CONF_HVAC_SETPOINT_SENSOR)
        self._tire_fl = merged.get(CONF_TIRE_PRESSURE_FL_SENSOR)
        self._tire_fr = merged.get(CONF_TIRE_PRESSURE_FR_SENSOR)
        self._tire_rl = merged.get(CONF_TIRE_PRESSURE_RL_SENSOR)
        self._tire_rr = merged.get(CONF_TIRE_PRESSURE_RR_SENSOR)
        # v0.5.43 — optional driver-identity sensor (BT connected device,
        # input_select, template sensor...). State == driver name.
        self._driver_sensor = merged.get(CONF_DRIVER_SENSOR)

        self._battery_capacity_declared = float(
            merged.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
        )
        # v0.6.3 — optional cohort baseline (Tessie pattern). When the
        # user picks a model from `cohort_baselines.json`, the SoH
        # 100 % anchor uses that cohort's observed "new" capacity
        # instead of nameplate. SoC-math still uses
        # `_battery_capacity_declared` / `_battery_capacity_calibrated`,
        # because that's what reproduces the dashboard's "X kWh used"
        # reading; only the SoH percentage's denominator changes.
        self._vehicle_model_key: str | None = (
            merged.get(CONF_VEHICLE_MODEL) or None
        )
        self._cohort_baseline_kwh: float | None = None
        self._cohort_baseline_source: str | None = None
        if self._vehicle_model_key:
            cohort = _COHORT_BASELINES.get(self._vehicle_model_key)
            if cohort is not None:
                new_kwh = cohort.get("cohort_new_kwh")
                if isinstance(new_kwh, (int, float)) and new_kwh > 0:
                    self._cohort_baseline_kwh = float(new_kwh)
                    self._cohort_baseline_source = self._vehicle_model_key
        # v0.5.51 — capacity DERIVED from real charges (kwh / ΔSoC × 100).
        # The declared capacity in config can be optimistic for several
        # reasons: manufacturer-spec "useable kWh" lies on the high side
        # for some platforms, the pack degrades over time, and our
        # SoC→kWh conversion ends up overstating `energy_kwh` by 30–40%
        # on a Tesla until we calibrate. Stays None until enough
        # charges with ΔSoC ≥ _CAPACITY_MIN_DELTA_PCT exist; then the
        # property below prefers it.
        self._battery_capacity_calibrated: float | None = None
        self._battery_capacity_calibration_n: int = 0
        # v0.6.5 — per-gate reject counts from the last calibration run.
        # Surfaced in the BatterySohSensor attributes so a user can see
        # "9 charges considered, 4 used, 3 too-small, 2 cold".
        self._battery_capacity_calibration_rejects: dict[str, int] = {}
        # v0.6.4 — kWh-weighted avg €/kWh over the trailing 30d.
        # Cached so `_trip_to_attr` (a sync-context attribute builder)
        # can compute `cost_at_avg_tariff` without an async storage
        # call per render. Refreshed periodically by
        # `_async_refresh_avg_tariff_cache`.
        self._avg_tariff_cache_per_kwh: float | None = None
        self._dcfc_threshold_kw = float(
            merged.get(CONF_DCFC_THRESHOLD_KW, DEFAULT_DCFC_THRESHOLD_KW)
        )
        # v0.6.6 — estimated kW the car draws while parked with
        # ignition on (HVAC + electronics). Feeds the close-time
        # idle-energy estimate so dashboards can split "energy moving"
        # vs "energy waiting".
        self._idle_power_estimate_kw = float(
            merged.get(
                CONF_IDLE_POWER_ESTIMATE_KW, DEFAULT_IDLE_POWER_ESTIMATE_KW,
            )
        )
        # v0.7.5 — optional elevation provider ("none" default keeps
        # GPS points on-host until the user opts in). "custom" lets
        # a user hostname point at their own OpenTopoData instance
        # via CONF_ELEVATION_PROVIDER_URL for full privacy.
        self._elevation_provider = str(
            merged.get(CONF_ELEVATION_PROVIDER, DEFAULT_ELEVATION_PROVIDER)
        )
        self._elevation_provider_url = merged.get(
            CONF_ELEVATION_PROVIDER_URL,
        ) or None
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
        self._energy_price_entity = merged.get(CONF_ENERGY_PRICE_ENTITY) or None
        self._currency = merged.get(CONF_CURRENCY, DEFAULT_CURRENCY)
        self._home_zone = merged.get(CONF_HOME_ZONE, DEFAULT_HOME_ZONE)
        # v0.8.10 — secondary "home" locations (second house, holiday
        # home, …). Zone entities: raw entity_ids, slug resolved lazily
        # via secondary_home_zone_slugs (mirrors home_zone's own
        # slug-from-entity_id logic). Coordinates: parsed once here since
        # they're free text, not an entity reference.
        self._secondary_home_zones: list[str] = list(
            merged.get(CONF_SECONDARY_HOME_ZONES) or []
        )
        self._secondary_home_coords: list[tuple[float, float, float]] = (
            _parse_secondary_home_coords(merged.get(CONF_SECONDARY_HOME_COORDS))
        )

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
        # v0.5.79 — periodic stuck-trip watchdog (always-on; runs even
        # when no live tick is active).
        self._unsub_stuck_watchdog: CALLBACK_TYPE | None = None
        # v0.6.4 — periodic refresh of `_avg_tariff_cache_per_kwh`.
        self._unsub_avg_tariff: CALLBACK_TYPE | None = None
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
        # v0.5.49 — live-open retry chain. Set when vehicle_on=on arrives
        # but odometer is still stale; cleared as soon as a trip opens or
        # vehicle_on flips off. See _LIVE_OPEN_RETRY_DELAYS_S.
        self._pending_open_unsub: CALLBACK_TYPE | None = None
        self._pending_open_attempt: int = 0
        # v0.5.50 — score baseline calibration. Updated from history at
        # setup and after each new trip closes; the score column on every
        # exposed trip is recomputed against this anchor so changing cars
        # / driving styles doesn't permanently anchor the rating to one
        # vehicle's curve. Stays at the default until enough history
        # exists (see _async_refresh_score_baseline).
        self.score_baseline_kwh_100km: float = _SCORE_BASELINE_DEFAULT
        self.score_baseline_trip_count: int = 0

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
        # v0.5.47 — current_snapshot() memo, valid for one notify cycle.
        # Sensors only re-render on _notify_listeners (should_poll is
        # False everywhere), so caching between invalidations is safe.
        self._snapshot_cache: dict[str, Any] | None = None

    @property
    def entry_id(self) -> str:
        return self.entry.entry_id

    @property
    def battery_capacity(self) -> float:
        """Effective battery capacity in kWh.

        v0.5.51 — prefers the value calibrated from real charges
        (`_battery_capacity_calibrated`) when enough data exists; falls
        back to the declared CONF_BATTERY_CAPACITY otherwise. Every
        SoC→kWh conversion routes through this property, so a single
        fix here propagates to energy_kwh, consumption, cost and score.
        """
        return (
            self._battery_capacity_calibrated
            if self._battery_capacity_calibrated is not None
            else self._battery_capacity_declared
        )

    @property
    def battery_capacity_baseline(self) -> float:
        """v0.6.3 — the kWh value that maps to 100 % SoH.

        Precedence: `cohort_new_kwh` from the picked vehicle model (if
        set + present in the seeded JSON) > nameplate `CONF_BATTERY_CAPACITY`.
        Independent of `battery_capacity` (which prefers the live
        calibration); only the SoH denominator routes through here so
        the dashboard can show a "below cohort mean" reading even when
        the live calibration is still catching up.
        """
        if self._cohort_baseline_kwh is not None and self._cohort_baseline_kwh > 0:
            return self._cohort_baseline_kwh
        return self._battery_capacity_declared

    @property
    def vehicle_model_key(self) -> str | None:
        """Picked cohort key, or None when the user hasn't selected one."""
        return self._vehicle_model_key

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

    def _secondary_home_labels(self) -> set[str]:
        """v0.8.10 — every string a configured secondary home could show
        up as in a location field: a zone's slug (what device_tracker
        normally reports) AND its current friendly_name (what
        `_zone_from_coords`'s non-home branch returns — see that
        docstring), since either can reach the comparison depending on
        which path resolved the location; plus every free-typed
        coordinate entry's label. Resolved fresh each call (cheap: a
        handful of entries at most) so a renamed/added zone takes effect
        without a coordinator restart. Also the set storage-level journey
        queries (open-journey resolution, orphan absorption) treat as
        home-equivalent alongside `home_zone`.
        """
        labels: set[str] = set()
        for entity_id in self._secondary_home_zones:
            slug = entity_id[len("zone."):] if entity_id.startswith("zone.") else entity_id
            labels.add(slug.strip().casefold())
            state = self.hass.states.get(entity_id)
            if state is not None and state.name:
                labels.add(state.name.strip().casefold())
        for _lat, _lon, _radius, label in self._secondary_home_coords:
            labels.add(label.strip().casefold())
        return labels

    def _is_at_any_home(self, location: str | None) -> bool:
        """Like `_is_at_home`, but also true for any configured secondary
        home (second house, holiday home, …) — arriving there closes a
        journey, and starting from there opens one, exactly like the
        primary home_zone.
        """
        if self._is_at_home(location):
            return True
        if location is None:
            return False
        return location.strip().casefold() in self._secondary_home_labels()

    def _secondary_home_coord_label(
        self, lat: float | None, lon: float | None
    ) -> str | None:
        """v0.8.10 — resolve free-typed secondary-home coordinates
        (CONF_SECONDARY_HOME_COORDS) to their label, or None outside every
        configured radius. These aren't registered HA zones, so HA's own
        zone-matching (`_zone_from_coords`) never sees them; this is the
        dedicated check for that case. Also usable as a truthy "is near
        any secondary-home coordinate" test.
        """
        if lat is None or lon is None or not self._secondary_home_coords:
            return None
        for home_lat, home_lon, radius_m, label in self._secondary_home_coords:
            if _haversine_km(lat, lon, home_lat, home_lon) * 1000.0 <= radius_m:
                return label
        return None

    def _zone_from_coords(
        self, lat: float | None, lon: float | None
    ) -> str | None:
        """Resolve a HA zone label from GPS coordinates (best-effort).

        v0.5.44 — synthetic trips read the device_tracker STATE for
        origin/destination, but a cloud-paused tracker can be hours
        stale: 'not_home' while the car is physically parked at home.
        That leaves the journey open forever and tomorrow's commute gets
        absorbed into yesterday's journey. The route's GPS endpoints are
        fresher than the tracker state, so when the tracker gives us
        nothing usable we check the coords against HA's zones directly.

        Returns the same label a device_tracker would report (the home
        slug for the home zone, the friendly name for others), or None
        when the point is outside every zone.
        """
        if lat is None or lon is None:
            return None
        try:
            from homeassistant.components.zone import (  # noqa: PLC0415
                async_active_zone,
            )
            zone_state = async_active_zone(self.hass, lat, lon)
        except Exception:  # pragma: no cover — defensive
            return None
        if zone_state is None:
            return None
        slug = zone_state.entity_id.split(".", 1)[1]
        if self._is_at_home(slug):
            return slug
        return zone_state.name or slug

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
        """Current SoC % from the configured battery sensor, None if unreadable.

        v0.5.78 — when the upstream sensor goes `unknown` (Tesla
        integration asleep, BYD cloud poll paused), fall back to the
        last value we cached. The cached SoC is what the car had at
        the last successful poll, which is more useful for dashboards
        than `unknown` while the upstream wakes back up.
        """
        live = self._read_float(self._battery)
        if live is not None:
            self._battery_last_known = live
            return live
        return getattr(self, "_battery_last_known", None)

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

    async def _async_populate_elevation(
        self,
        trip_id: int,
        gps_samples: Sequence[tuple[datetime, float, float]],
    ) -> None:
        """v0.7.5 — fetch elevation profile for the trip's route and
        patch the row. Best-effort: any provider error (timeout,
        HTTP != 200, malformed) leaves the columns as NULL and logs
        a single info line; the rest of the trip data stays intact.
        """
        # Reduce (ts, lat, lon) tuples to (lat, lon) and downsample
        # to the elevation module's cap. The provider decides its
        # own point limit; downsample_route respects our default.
        points = [(lat, lon) for _, lat, lon in gps_samples]
        sampled = downsample_route(points)
        if len(sampled) < 2:
            return
        try:
            from homeassistant.helpers.aiohttp_client import (  # noqa: PLC0415
                async_get_clientsession,
            )
        except Exception:  # pragma: no cover — HA always ships this
            return
        session = async_get_clientsession(self.hass)
        elevations = await fetch_elevations(
            sampled,
            provider=self._elevation_provider,
            provider_url=self._elevation_provider_url,
            session=session,
        )
        if not elevations:
            return
        gain, loss, variance = compute_elevation_stats(elevations)
        if gain is None:
            return
        # Patch the row. `async_update_trip` writes only whitelisted
        # columns and returns the fresh record — no need to rewire
        # last_trip; the sensor's next refresh picks it up.
        try:
            await self.storage.async_update_trip(
                trip_id,
                {
                    "elevation_gain_m": gain,
                    "elevation_loss_m": loss,
                    "elevation_variance_m2": variance,
                },
            )
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.debug(
                "elevation patch on trip %s failed: %s", trip_id, exc,
            )
            return
        # If this row is the last-trip in memory, refresh so sensors
        # attached to `last_trip.elevation_*` see the value without
        # waiting for the next event.
        if self.last_trip and self.last_trip.trip_id == trip_id:
            self.last_trip.elevation_gain_m = gain
            self.last_trip.elevation_loss_m = loss
            self.last_trip.elevation_variance_m2 = variance
        _LOGGER.info(
            "Elevation for trip %s: gain=%.0fm loss=%.0fm var=%.0fm² "
            "(provider=%s, %d points)",
            trip_id, gain, loss, variance,
            self._elevation_provider, len(sampled),
        )
        self._notify_trip_log_listeners()

    async def _async_refresh_avg_tariff_cache(self, *_: Any) -> None:
        """v0.6.4 — refresh `_avg_tariff_cache_per_kwh` from the
        trailing-30d charge aggregates. Best-effort: any storage
        error keeps the previous cached value (falling back to
        `_energy_price` via the `recent_avg_tariff_per_kwh` property
        if cache is still None). Called periodically and after every
        trip-log notification (a new charge can move the avg).
        """
        try:
            since = period_start(dt_util.now(), "30d")
            agg = await self.storage.async_charges_aggregates_since(since)
        except Exception:  # pragma: no cover — defensive
            return
        avg = agg.get("avg_price_per_kwh")
        # Storage returns 0.0 when no charges in the window — keep
        # the cache at None in that case so the property falls back
        # to the home tariff instead of pretending charges were free.
        if isinstance(avg, (int, float)) and avg > 0:
            self._avg_tariff_cache_per_kwh = float(avg)
        else:
            self._avg_tariff_cache_per_kwh = None

    async def _async_heal_missing_drivers(self) -> None:
        """v0.5.97 — recompute `driver` for recent trips where the
        live-close logic persisted None.

        Recovery walks the configured driver sensor's recorder history
        with the same wider pre/post window as the live fallback. Trips
        whose recorder history is too old (default retention ~10 d) or
        whose sensor still has no usable value remain NULL — this is
        idempotent and safe to re-run.
        """
        if not self._driver_sensor:
            return
        try:
            todo = await self.storage.async_trips_missing_driver(
                days=_DRIVER_HEAL_LOOKBACK_DAYS,
                limit=_DRIVER_HEAL_MAX_TRIPS,
            )
        except Exception:  # pragma: no cover — storage call defensive
            return
        if not todo:
            return
        healed = 0
        for trip_id, started_at, ended_at in todo:
            try:
                driver = await self._async_driver_during(started_at, ended_at)
            except Exception:  # pragma: no cover — best-effort
                continue
            if driver is None:
                continue
            try:
                patched = await self.storage.async_update_trip(
                    trip_id, {"driver": driver},
                )
            except Exception:  # pragma: no cover — best-effort
                continue
            if patched is not None:
                healed += 1
        if healed:
            _LOGGER.info(
                "Driver heal: filled driver on %d trip(s) from recorder "
                "history (sensor=%s, window=%d d, cap=%d)",
                healed, self._driver_sensor,
                _DRIVER_HEAL_LOOKBACK_DAYS, _DRIVER_HEAL_MAX_TRIPS,
            )
            self._notify_listeners()
            self._notify_trip_log_listeners()

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
                    f"hass-ev-trip-logger/{self._version} "
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

    def _current_energy_price(self) -> float:
        """Live home tariff in €/kWh.

        If a price entity is configured (`energy_price_entity`) and its
        state is numeric, return that — so dynamic tariffs (Octopus,
        Nordpool, PVPC, …) drive trip/charge cost. The state is read at
        cost-computation time (trip/charge close), capturing the tariff
        period in effect then. Falls back to the fixed `energy_price_kwh`
        when no entity is set or its state is unavailable/non-numeric.
        """
        entity_id = self._energy_price_entity
        if entity_id:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in (
                "unknown",
                "unavailable",
                "",
            ):
                try:
                    return float(state.state)
                except (ValueError, TypeError):
                    pass
        return float(self._energy_price)

    def _trip_cost_price_per_kwh(self) -> float:
        """€/kWh fallback for trip cost when the battery pool is empty.

        v0.5.76, reworked v0.8.8 — Trip cost is computed by the
        weighted-average-cost (WAC) pool replay in
        `TripStorage._recompute_trip_costs_from_charges`: every charge
        blends its (kWh, price) into one running battery average, and
        each trip draws energy from that pool at whatever the blended
        average currently is — so the trip's actual cost tracks the
        real prices its energy was drawn from without discrete slices
        that have to fully drain before the price can change. The
        configured home tariff (returned here) is used:

          * as the seed `cost_basis_per_kwh` at insert time (before
            the post-insert recompute), so the live snapshot shows
            something sensible immediately;
          * as the price for any energy consumed before any charge
            was logged (typical at fresh install) or when the pool
            runs dry mid-trip.
        """
        return float(self._current_energy_price())

    @property
    def recent_avg_tariff_per_kwh(self) -> float:
        """v0.6.4 — kWh-weighted average price paid across recent
        charges, fallback to the home tariff (live entity if
        configured, else `_energy_price`) when nothing is cached or
        the average is zero.

        Powers the dashboard-friendly `cost_at_avg_tariff` attribute
        on trips. The default `cost` reflects the real blended battery
        price at the time of the trip (a free or DC-fast charge really
        did change what driving cost); this property gives a second,
        always-monotonic-with-kWh view for side-by-side comparisons.

        Refreshed by `_async_refresh_avg_tariff_cache`, which runs
        every _AVG_TARIFF_REFRESH and on every trip-log notification.
        """
        cached = self._avg_tariff_cache_per_kwh
        if cached is not None and cached > 0:
            return cached
        return float(self._current_energy_price())

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
        # v0.5.47 — invalidate the per-cycle snapshot memo BEFORE the
        # fan-out: ~13 CurrentTrip* sensors read current_snapshot() per
        # cycle and recomputed identical values each time.
        self._snapshot_cache = None
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
        # v0.5.50/51 — any change in the persisted log (trip or charge)
        # may shift the calibration we use for SoC→kWh conversion (via
        # `battery_capacity`) and/or the score curve baseline. Refresh
        # both asynchronously; both queries are cheap and idempotent.
        self.hass.async_create_task(self._async_refresh_battery_capacity())
        self.hass.async_create_task(self._async_refresh_score_baseline())

    async def async_compute_expected_soh(self) -> dict[str, Any]:
        """v0.5.57 — predict the SoH this car should have based on age,
        km, chemistry, climate and habits.

        Pure forward model — does NOT touch storage. The Sensor classes
        call this and surface the result. Returns:
            {
                "expected_soh_pct": 95.3,
                "factors": {                 # signed loss contributions
                    "year1_knee": 3.5,
                    "calendar": 0.0,
                    "cycle": 1.06,
                    "climate_hot": 0.1,
                    "dcfc": 0.0,
                    "soc_habit": 0.0,
                },
                "inputs": {                  # for transparency
                    "km": 26471,
                    "age_years": 1.0,
                    "chemistry": "lfp",
                    "avg_ambient_temp_c": 22.5,
                    "dcfc_ratio": 0.0,
                    "avg_soc_end_recent": 80.0,
                },
                "confidence": "low|medium|high",
            }
        """
        profile = _DEGRADATION_PROFILES[self._battery_chemistry]
        # 1. Inputs
        # v0.5.66 — `km` for the SoH model is the SUM of distance_km
        # across logged trips, NOT the car's lifetime odometer reading.
        # Rationale: the model penalties (climate_hot, DCFC, SoC habit)
        # only know about the period the logger has been watching.
        # Charging a car bought used at 50 000 km against the curve at
        # "50 000 + 6 000 km" would over-penalise it: the first 50 000
        # were under a different owner with unknown habits. The
        # vehicle's actual odometer is still exposed in the
        # `battery_soh` sensor attributes for transparency.
        logger_km = await self.storage.async_logger_total_km()
        km = logger_km
        if self._vehicle_first_registered is not None:
            age_years = (
                dt_util.now() - self._vehicle_first_registered
            ).total_seconds() / (365.25 * 86400)
            age_years = max(0.0, age_years)
        else:
            # Proxy: assume 15 000 km/yr (EU/US median). Lower-confidence.
            age_years = km / 15000.0
        dcfc_ratio, _, total_kwh = await self.storage.async_lifetime_dcfc_ratio()
        if dcfc_ratio is None:
            dcfc_ratio = 0.0
        avg_soc_end = await self.storage.async_avg_soc_end_recent(days=30)
        avg_ambient_temp = await self.storage.async_avg_ambient_temp_recent(days=90)

        # 2. Factors (each is a POSITIVE loss in pp, added to total)
        factors: dict[str, float] = {}
        factors["year1_knee"] = profile["knee_year1_pct"] * min(1.0, age_years)
        post_year1 = max(0.0, age_years - 1.0)
        factors["calendar"] = profile["calendar_pct_per_year"] * post_year1
        factors["cycle"] = profile["cycle_pct_per_1000km"] * (km / 1000.0)
        factors["climate_hot"] = 0.0
        if avg_ambient_temp is not None:
            if avg_ambient_temp > 25:
                factors["climate_hot"] = (
                    profile["climate_hot_extra_per_year"] * age_years
                )
            elif avg_ambient_temp < 10:
                # Cold: slow the calendar+cycle aging.
                mult = profile["climate_cold_mult"]
                factors["calendar"] *= mult
                factors["cycle"] *= mult
        factors["dcfc"] = 0.0
        dcfc_pct = dcfc_ratio * 100.0
        if dcfc_pct > profile["dcfc_threshold_pct"]:
            factors["dcfc"] = (
                profile["dcfc_penalty_per_pct_above"]
                * (dcfc_pct - profile["dcfc_threshold_pct"])
                * age_years
            )
        factors["soc_habit"] = 0.0
        if avg_soc_end is not None and avg_soc_end > 95:
            factors["soc_habit"] = profile["soc_100_extra_per_year"] * age_years

        total_loss = sum(factors.values())
        expected = max(_EXPECTED_SOH_FLOOR_PCT, 100.0 - total_loss)

        # 3. Confidence — high when first_registered is set AND we have
        # weather data; medium when only one is missing; low otherwise.
        confidence = "low"
        has_age = self._vehicle_first_registered is not None
        has_climate = avg_ambient_temp is not None
        if has_age and has_climate:
            confidence = "high"
        elif has_age or has_climate:
            confidence = "medium"

        return {
            "expected_soh_pct": round(expected, 2),
            "factors": {k: round(v, 3) for k, v in factors.items()},
            "inputs": {
                "km": round(km, 1),
                "age_years": round(age_years, 2),
                "chemistry": self._battery_chemistry,
                "avg_ambient_temp_c": (
                    round(avg_ambient_temp, 1) if avg_ambient_temp else None
                ),
                "dcfc_ratio": round(dcfc_ratio, 3),
                "avg_soc_end_recent": (
                    round(avg_soc_end, 1) if avg_soc_end else None
                ),
                "total_kwh_charged": round(total_kwh, 1),
            },
            "confidence": confidence,
        }

    async def _async_refresh_battery_capacity(self) -> None:
        """v0.5.51 — derive effective pack capacity from real charges.

        Adopts the median of `kwh / ΔSoC × 100` over the last
        `_CAPACITY_CHARGE_WINDOW` charges with ΔSoC ≥
        `_CAPACITY_MIN_DELTA_PCT`. Requires `_CAPACITY_MIN_CHARGES` to
        avoid anchoring on a single freak charge; clamps the result to
        50–150 % of the declared capacity so a corrupted charge can't
        suggest an impossibly small or large pack.
        """
        try:
            median, n, rejects = await self.storage.async_effective_capacity_kwh(
                min_delta_pct=_CAPACITY_MIN_DELTA_PCT,
                min_charges=_CAPACITY_MIN_CHARGES,
                window=_CAPACITY_CHARGE_WINDOW,
            )
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.debug("effective capacity query failed: %s", exc)
            return
        self._battery_capacity_calibration_n = n
        # v0.6.5 — cache the per-gate reject counts so the SoH sensor
        # can surface them as attributes (transparency: explains why a
        # given charge didn't contribute to the calibration).
        self._battery_capacity_calibration_rejects = rejects
        if median is None:
            new_value: float | None = None
        else:
            lo = self._battery_capacity_declared * _CAPACITY_BOUNDS_RATIO[0]
            hi = self._battery_capacity_declared * _CAPACITY_BOUNDS_RATIO[1]
            new_value = max(lo, min(hi, median))
        prev = self._battery_capacity_calibrated
        changed = (
            (prev is None) != (new_value is None)
            or (prev is not None and new_value is not None
                and abs(new_value - prev) > 0.2)
        )
        self._battery_capacity_calibrated = new_value
        if changed:
            shown = new_value if new_value is not None else self._battery_capacity_declared
            _LOGGER.info(
                "Effective battery capacity: %.2f kWh "
                "(n=%d charges, declared=%.2f)",
                shown, n, self._battery_capacity_declared,
            )
            # v0.5.51 — backfill historical trips so the new capacity
            # applies to old rows too, otherwise the dashboard would
            # show a discontinuity between trips logged before/after the
            # calibration kicked in. We only rewrite trips whose
            # energy_kwh was SoC-derived (energy_source NULL / 'soc' /
            # 'estimated'); power-integration-measured rows are left
            # untouched. Cost gets a separate heal pass.
            self.hass.async_create_task(self._async_heal_energy_after_calibration())
            # SoC→kWh conversion drives almost everything visible —
            # poke sensors so they re-render against the new value.
            self._notify_listeners()
        # v0.5.54 — capacity_history persistence runs even when `changed`
        # is False, because we still want to refresh the latest snapshot's
        # n_charges to reflect that more data agrees with the value.
        if new_value is not None:
            self.hass.async_create_task(
                self._async_snapshot_capacity_history(new_value, n)
            )

    async def _async_snapshot_capacity_history(
        self, calibrated_kwh: float, n_charges: int,
    ) -> None:
        """v0.5.54/65 — append a row to `capacity_history` when the
        calibrated value drifts ≥`_CAPACITY_HISTORY_MIN_DELTA_KWH` from
        the latest snapshot. v0.5.65 also records the odometer at the
        time of the snapshot so the dashboard can plot SoH vs km, not
        just vs time.
        """
        try:
            latest = await self.storage.async_latest_capacity_snapshot()
            if latest is not None:
                _, last_kwh, _, _, _ = latest
                if abs(calibrated_kwh - last_kwh) < _CAPACITY_HISTORY_MIN_DELTA_KWH:
                    return
            odo = self._read_float(self._odometer)
            logger_km = await self.storage.async_logger_total_km()
            await self.storage.async_insert_capacity_snapshot(
                calibrated_kwh=calibrated_kwh,
                declared_kwh=self._battery_capacity_declared,
                n_charges=n_charges,
                when=dt_util.now(),
                odometer_km=odo,
                logger_km=logger_km,
            )
            _LOGGER.info(
                "Capacity snapshot: %.2f kWh (n=%d charges, declared=%.2f, "
                "logger_km=%.0f, odo=%s) appended to history",
                calibrated_kwh, n_charges, self._battery_capacity_declared,
                logger_km,
                f"{odo:.0f} km" if odo is not None else "?",
            )
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.debug("capacity_history snapshot failed: %s", exc)

    async def _async_heal_energy_after_calibration(self) -> None:
        """v0.5.51 — backfill `energy_kwh` and `consumption_kwh_100km`
        for SoC-derived trips against the freshly-calibrated capacity.
        Re-costs the affected trips at the configured home tariff so the
        dashboard's € column stays consistent. No-op on the
        power-integration trips (those used direct measurement).
        """
        new_capacity = self.battery_capacity
        try:
            n = await self.storage.async_recompute_energy_from_capacity(new_capacity)
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.warning("Energy heal failed: %s", exc)
            return
        if n == 0:
            return
        _LOGGER.info(
            "Energy heal: rewrote %d trip(s) against %.2f kWh capacity", n, new_capacity,
        )
        # Re-cost the just-rewritten trips at the home tariff.
        try:
            await self.storage.async_recompute_trip_costs_from_charges(
                default_price=self._current_energy_price()
            )
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.debug("Cost re-heal post-energy-heal failed: %s", exc)
        # Refresh in-memory last_trip + listeners so the dashboard
        # picks the rewritten values up immediately.
        self.last_trip = await self.storage.async_get_last()
        self._notify_listeners()
        self._notify_trip_log_listeners()

    async def _async_refresh_score_baseline(self) -> None:
        """v0.5.50 — recompute `score_baseline_kwh_100km` from history.

        P5 of consumption_kwh_100km over trips with distance>=5 km maps
        to 10/10. Falls back to 14.5 until there are at least
        `_SCORE_BASELINE_MIN_TRIPS` such trips; clamps the result to
        `_SCORE_BASELINE_BOUNDS` so a single fluke trip (or a sensor
        glitch) can't pin the curve.
        """
        try:
            p5, n = await self.storage.async_score_baseline_p5(
                min_distance_km=_SCORE_BASELINE_MIN_DISTANCE_KM,
                min_trips=_SCORE_BASELINE_MIN_TRIPS,
            )
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.debug("score baseline query failed: %s", exc)
            return
        self.score_baseline_trip_count = n
        if p5 is None:
            new_baseline = _SCORE_BASELINE_DEFAULT
        else:
            lo, hi = _SCORE_BASELINE_BOUNDS
            new_baseline = max(lo, min(hi, p5))
        if abs(new_baseline - self.score_baseline_kwh_100km) > 0.05:
            _LOGGER.info(
                "Score baseline shift: %.2f → %.2f kWh/100km (n=%d eligible trips)",
                self.score_baseline_kwh_100km, new_baseline, n,
            )
            self.score_baseline_kwh_100km = new_baseline
            # Re-render dependent sensors so the rating curves update.
            self._notify_listeners()
        else:
            self.score_baseline_kwh_100km = new_baseline

    async def async_start(self) -> None:
        """Wire up state listeners and seed from existing storage."""
        # v0.5.69 — auto-detect exterior_temp_sensor here (not in
        # __init__) so the BYD/Tesla integration has had time to
        # publish its entities to the state machine.
        if not self._temp:
            self._temp = self._auto_detect_temp_sensor()
        # v0.5.77 — same deferred auto-detect for the vehicle-native
        # last-trip energy / distance sensors.
        if not self._last_trip_energy_sensor:
            self._last_trip_energy_sensor = self._auto_detect_vehicle_sensor(
                ("_last_trip_energy", "_last_trip_kwh", "_trip_energy"),
                "last-trip energy",
            )
        if not self._last_trip_distance_sensor:
            self._last_trip_distance_sensor = self._auto_detect_vehicle_sensor(
                ("_last_trip_distance", "_last_trip_km"),
                "last-trip distance",
            )
        self.last_trip = await self.storage.async_get_last()
        self.last_charge = await self.storage.async_get_last_charge()
        # Robust journey resume — derive from the actual trip log rather
        # than from `last_trip.destination` (which can be wrong if the
        # device_tracker lagged at close time or if an earlier amend
        # corrupted it). The storage query finds the first journey-
        # tagged trip after the most recent home-arrival; if any, that
        # journey is still open.
        self.current_journey_id = await self.storage.async_resolve_open_journey_id(
            self.home_zone, self._secondary_home_labels()
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

        # v0.5.51 — derive effective pack capacity from real charges
        # BEFORE the score baseline runs (the baseline relies on
        # consumption_kwh_100km, which derives from energy_kwh, which
        # derives from this capacity — so refreshing in the wrong
        # order leaves the first score render anchored to a stale curve).
        await self._async_refresh_battery_capacity()
        # v0.5.50 — seed the per-car score baseline from history at boot
        # so the very first sensor render uses the calibrated anchor
        # instead of the 14.5 default flicker.
        await self._async_refresh_score_baseline()

        # One-shot heal: re-cost every trip from its preceding charge's
        # price. Catches users whose CONF_ENERGY_PRICE was 0 at trip-close
        # time, or whose set_last_charge_price corrections never
        # propagated. Idempotent and cheap.
        try:
            healed = await self.storage.async_recompute_trip_costs_from_charges(
                default_price=self._current_energy_price()
            )
            if healed:
                _LOGGER.info("Startup heal: recomputed cost on %d trip(s)", healed)
                self.last_trip = await self.storage.async_get_last()
        except Exception as err:  # pragma: no cover — defensive
            _LOGGER.debug("Trip cost heal failed (non-fatal): %s", err)

        # v0.5.86 — startup vehicle-heal sweep. The v0.5.77 per-trip
        # heal is single-shot (`async_call_later` 240 s). If HA
        # restarts inside that window, the heal is lost forever and
        # the trip stays on the noisier SoC-derived energy. On
        # startup, re-scan the last 24 h of trips that aren't tagged
        # `energy_source="vehicle"` and re-run the heal — it'll pick
        # up any trips where the BYD-native sensor has since updated
        # and we missed the live heal.
        if self._last_trip_energy_sensor:
            self.hass.async_create_task(self._async_startup_vehicle_heal_sweep())

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

        # v0.5.97 — re-evaluate recent trips whose driver was never
        # captured (sensor unknown during the live window). Bounded by
        # _DRIVER_HEAL_MAX_TRIPS and recorder retention; idempotent.
        if self._driver_sensor:
            self.hass.async_create_task(self._async_heal_missing_drivers())

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
        if self._evse_power_sensor:
            self._unsub_evse = async_track_state_change_event(
                self.hass, [self._evse_power_sensor], self._async_evse_power_changed
            )
        if self._location:
            self._unsub_location = async_track_state_change_event(
                self.hass, [self._location], self._async_location_changed
            )

        # v0.5.79 — always-on periodic stuck-trip watchdog. Independent
        # of the live-tick (which only runs while a trip is open), so it
        # can rescue trips whose live-tick has been silenced by an
        # upstream integration outage.
        self._unsub_stuck_watchdog = async_track_time_interval(
            self.hass, self._async_check_stuck_trip, _STUCK_TRIP_TIMER_INTERVAL
        )

        # v0.6.4 — keep the trailing-30d avg-tariff cache fresh so the
        # `cost_at_avg_tariff` attribute on trips is current.
        self._unsub_avg_tariff = async_track_time_interval(
            self.hass,
            self._async_refresh_avg_tariff_cache,
            _AVG_TARIFF_REFRESH,
        )
        self.hass.async_create_task(self._async_refresh_avg_tariff_cache())

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
        # v0.5.61 — accept Tesla / OVMS / any enum-style 'charging' state.
        if st is None or self._is_charging_value(st.state) is not True:
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
            self._unsub_stuck_watchdog,
            getattr(self, "_unsub_avg_tariff", None),
        ):
            if unsub:
                unsub()
        self._unsub_state = self._unsub_metrics = None
        self._unsub_power = self._unsub_temp = self._unsub_idle = None
        self._unsub_speed = None
        self._unsub_charge = self._unsub_live_tick = None
        self._unsub_synth_finalize = None
        self._unsub_location = None
        self._unsub_stuck_watchdog = None
        self._unsub_avg_tariff = None
        self._synth_baseline = None
        # v0.5.49 — make sure a deferred live-open retry doesn't fire
        # after async_stop (would touch a stopped coordinator).
        self._cancel_pending_open()

    @callback
    def _async_vehicle_on_changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _INVALID_STATES:
            return
        is_on = new_state.state == STATE_ON
        now = dt_util.now()
        if is_on:
            self._cancel_idle()
            # v0.5.16/53 — if a deferred close is pending from a recent
            # off-edge, this on event means the off was just a pause
            # (red light, brief parking) within the grace window. Cancel
            # the pending close, leave the trip open, return.
            if self._pending_close_unsub is not None:
                self._pending_close_unsub()
                self._pending_close_unsub = None
                _LOGGER.info(
                    "vehicle_on=on cancelled a pending close — pause absorbed"
                )
                return
            # v0.5.53 — also cancel a pending SYNTHETIC finalize. The
            # finalize timer fires _SYNTH_COALESCE_WINDOW_S after the
            # last odometer growth; if vehicle_on goes off→on inside
            # that window, the live path should reclaim the trip, NOT
            # let the synthetic path commit a half-finished record.
            # This was the root cause of trip 162 closing prematurely:
            # vehicle_on flapped, synth_finalize was scheduled, on came
            # back but no one cancelled the finalize.
            if self._unsub_synth_finalize is not None:
                self._unsub_synth_finalize()
                self._unsub_synth_finalize = None
                self._synth_baseline = None
                _LOGGER.info(
                    "vehicle_on=on cancelled a pending synth finalize"
                )
            if self.current is None:
                # v0.5.49 — try to open immediately. If odometer isn't
                # ready yet (BYD cloud-poll lag), the helper schedules
                # retries instead of bailing silently. battery=None is
                # tolerated: _resolve_soc_start already handles it via
                # the SoC ring buffer / last_charge anchor.
                self._async_try_live_open(now, attempt=0)
            return
        # v0.5.49 — any off-edge cancels a pending live-open retry chain.
        # Without this, a brief on→off flap (BYD sometimes emits one as
        # the user merely unlocks the car) would still try to open a
        # trip 30-60 s later, after the car is already settled.
        self._cancel_pending_open()
        if self.current is not None:
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
                self.hass, _VEHICLE_OFF_GRACE_S, _debounced_close
            )

    @callback
    def _cancel_pending_open(self) -> None:
        if self._pending_open_unsub is not None:
            self._pending_open_unsub()
            self._pending_open_unsub = None
        self._pending_open_attempt = 0

    @callback
    def _async_try_live_open(self, now: datetime, *, attempt: int) -> None:
        """Open a live trip when odometer is fresh, else schedule a retry.

        v0.5.49 — cloud-polled integrations (BYD, Tesla Fleet) often raise
        `vehicle_on=on` a poll-cycle before the odometer entity catches
        up. Before this, the live opener bailed and every trip fell to
        the synthetic path — which loses regen / max_power / max_speed /
        avg_temp.

        Strategy:
          * If odometer is already readable → open immediately. battery
            being None is fine (`_resolve_soc_start` handles it).
          * Else: kick `homeassistant.update_entity` to nudge the cloud
            poll and re-check at the next entry in _LIVE_OPEN_RETRY_DELAYS_S.
          * Any off-edge cancels the chain (`_cancel_pending_open`).
          * If another path opens the trip first (metric arrival,
            charge-close chain), the next retry sees `self.current is
            not None` and exits silently.
        """
        # A previous attempt's timer may still be queued — replace it.
        self._cancel_pending_open()

        if self.current is not None:
            return
        # Re-check vehicle_on at every retry; the user may have turned
        # the car off mid-chain (handled by the off-edge cancel, but a
        # state read is a cheap second line of defence).
        if self._read_bool(self._vehicle_on) is not True:
            return

        if self.current_charge is not None:
            # v0.5.16 — mutual exclusion: close the charge first so its
            # final SoC anchors the new trip. Charge close races the
            # retry chain; cancel the chain because the chained helper
            # already opens the trip on its own once close persists.
            _LOGGER.info(
                "vehicle_on=on with charge in progress — "
                "closing charge before opening trip"
            )
            self.hass.async_create_task(
                self._async_close_charge_then_open_trip(now)
            )
            return

        if self._read_float_if_fresh(
            self._odometer, now, _ODOMETER_STALE_MAX_AGE_S
        ) is not None:
            self._open_trip(now)
            return

        # Odometer still stale (missing, or present but too old to trust
        # as this trip's start anchor). Nudge the upstream poll, then queue the
        # next retry. If we've exhausted the chain, log once and let the
        # synthetic path own this trip.
        if self._odometer or self._battery:
            self.hass.async_create_task(self._async_force_refresh_metrics())

        if attempt >= len(_LIVE_OPEN_RETRY_DELAYS_S):
            _LOGGER.info(
                "vehicle_on=on but odometer never settled after %d retries"
                " (%.0f s total); leaving trip to the synthetic path",
                attempt, sum(_LIVE_OPEN_RETRY_DELAYS_S),
            )
            self._pending_open_attempt = 0
            return

        delay = _LIVE_OPEN_RETRY_DELAYS_S[attempt]
        next_attempt = attempt + 1
        _LOGGER.debug(
            "Deferring live-open: odometer not ready"
            " (attempt %d/%d, retry in %.0fs)",
            next_attempt, len(_LIVE_OPEN_RETRY_DELAYS_S), delay,
        )

        @callback
        def _retry(_at: datetime) -> None:
            self._pending_open_unsub = None
            self._async_try_live_open(dt_util.now(), attempt=next_attempt)

        self._pending_open_unsub = async_call_later(self.hass, delay, _retry)
        self._pending_open_attempt = next_attempt

    async def _async_force_refresh_metrics(self) -> None:
        """v0.5.49 — kick `homeassistant.update_entity` on battery+odometer
        to shorten the BYD cloud-poll gap at vehicle_on=on. Best-effort;
        any failure is swallowed (logged at debug). Distinct from
        `_async_force_refresh_location` because that one targets the
        device_tracker; here we only care about the float metrics.
        """
        targets: list[str] = []
        if self._odometer:
            targets.append(self._odometer)
        if self._battery:
            targets.append(self._battery)
        if not targets:
            return
        try:
            await self.hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": targets},
                blocking=False,
            )
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.debug("update_entity refresh (metrics) failed: %s", exc)

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
        _metric_now = dt_util.now()
        if (
            self.current is None
            and self._read_bool(self._vehicle_on) is True
            and self._read_float_if_fresh(
                self._odometer, _metric_now, _ODOMETER_STALE_MAX_AGE_S
            ) is not None
        ):
            # v0.5.49 — odometer just landed; the deferred retry chain
            # would still re-check soon, but opening here makes the
            # response immediate and avoids a stale `_pending_open_unsub`
            # firing a no-op a few seconds later.
            self._cancel_pending_open()
            self._open_trip(_metric_now)
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
                amended_to_home = self._is_at_any_home(location)
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
            # v0.5.80 — before discarding the stale baseline: if the
            # odometer ALSO jumped above min_trip_distance while we
            # were silent, the user actually drove during the silence
            # window (cloud integration was offline). Capture it as a
            # disconnect-orphan trip instead of throwing the evidence
            # away. Cap at _ORPHAN_DISCONNECT_MAX_AGE to avoid resur-
            # recting trips from days ago after a long install pause.
            delta_stale = odo - prev_odo
            if (
                delta_stale >= self._min_distance
                and (now - prev_t) <= _ORPHAN_DISCONNECT_MAX_AGE
            ):
                _LOGGER.warning(
                    "Disconnect-orphan: +%.2f km after %.1f h of silence "
                    "(baseline at %s). Recording as 'orphan_disconnect'.",
                    delta_stale, (now - prev_t).total_seconds() / 3600.0,
                    prev_t.isoformat(),
                )
                await self._async_insert_disconnect_orphan(
                    prev_t, now, prev_odo, odo, prev_soc, soc,
                )
            else:
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
        # v0.5.53 — abort if vehicle_on is currently on. The synthetic
        # path is for "we missed the live trip entirely"; firing while
        # the ignition is on commits a half-trip before the real one
        # ends. This was the trip-162 bug: synth_finalize fired while
        # the car was still moving, closed at the last odo we'd seen
        # and lost the remaining 3 km / 1 % SoC.
        if self._read_bool(self._vehicle_on) is True:
            self._synth_baseline = None
            _LOGGER.debug(
                "synth finalize skipped — vehicle_on=on, live path "
                "owns this trip"
            )
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
            (soc_used / 100.0) * self.battery_capacity
            if soc_used is not None
            else None
        )
        energy_source: str | None = "soc" if energy is not None else None
        # v0.5.46 — same heal as the live close (v0.5.15). BYD's
        # integer-step SoC means a short trip often crosses no 1 %
        # boundary (soc 55→55) → soc_used None → the row persisted with
        # NULL energy/consumption/cost. On cloud-polled cars nearly
        # every trip is synthetic, so the live-path fallback never ran.
        # Estimate from the user's own distance-weighted average.
        if energy is None and distance > 0:
            try:
                avg_per_100 = await self.storage.async_avg_consumption_kwh_per_100km()
            except Exception:  # pragma: no cover — defensive
                avg_per_100 = None
            if avg_per_100 and avg_per_100 > 0:
                energy = distance * avg_per_100 / 100.0
                energy_source = "estimated"
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
        # v0.5.44 — when the tracker state names no zone (stale cloud
        # poll, paused polling), resolve it from the route's GPS
        # endpoints so the journey state machine sees the real home
        # arrival/departure instead of a frozen 'not_home'.
        # v0.5.47 — freshness guards: the endpoint sample must be close
        # in time to the trip boundary it represents, otherwise it can
        # be a mid-route point that resolves to the wrong zone.
        if (
            _is_zoneless(location_end)
            and route
            and (ended_at - route[-1][0]) <= _ZONE_FALLBACK_MAX_AGE
        ) or (_is_zoneless(location_end) and not route and end_lat is not None):
            # No-route case: end coords came from the tracker's CURRENT
            # position at finalize time, which is fresh by construction.
            zone_end = self._zone_from_coords(end_lat, end_lon)
            if zone_end is None:
                # v0.8.10 — not a registered HA zone; check free-typed
                # secondary-home coordinates instead.
                zone_end = self._secondary_home_coord_label(end_lat, end_lon)
            if zone_end is not None:
                location_end = zone_end
        if (
            _is_zoneless(location_start)
            and route
            and (route[0][0] - started_at) <= _ZONE_FALLBACK_MAX_AGE
            and route[0][0] >= started_at - _ZONE_FALLBACK_MAX_AGE
        ):
            zone_start = self._zone_from_coords(start_lat, start_lon)
            if zone_start is None:
                zone_start = self._secondary_home_coord_label(start_lat, start_lon)
            if zone_start is not None:
                location_start = zone_start
        started_from_home = self._is_at_any_home(location_start)
        is_at_home_end = self._is_at_any_home(location_end)
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
            # v0.5.73 — for synthetic trips we can't get per-tick temp
            # samples (no live tick), so we settle for the END
            # reading. Better than NULL — at least the trip is bucketed
            # into the right season / temperature range and feeds
            # consumption_by_temp_bucket. The user can override
            # post-hoc with set_trip if needed.
            avg_temp_c=self._read_float(self._temp) if self._temp else None,
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
            # v0.5.46 — provenance: 'soc' when SoC-derived, 'estimated'
            # when healed from the 30d average (dashboard badge).
            energy_source=energy_source,
            # v0.5.44 — resolve driver from recorder history: the live
            # capture never ran for reconstructed trips.
            driver=await self._async_driver_during(started_at, ended_at),
            # v0.5.76 — seed with the home tariff; post-insert WAC
            # recompute overwrites with the actual blended average.
            cost_basis_per_kwh=price_per_kwh if cost is not None else None,
            # v0.5.86 — confidence band on the synth-derived consumption.
            **dict(zip(
                (
                    "consumption_lower_kwh_100km",
                    "consumption_upper_kwh_100km",
                    "low_confidence",
                ),
                self._compute_consumption_band(
                    distance_km=distance,
                    energy_kwh=energy,
                    consumption=consumption,
                    energy_source=energy_source,
                    soc_used_pct=soc_used,
                ),
            )),
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
        # v0.5.77 — schedule the vehicle-native energy heal (no-op when
        # the vehicle sensor isn't configured / auto-detected).
        self._schedule_vehicle_heal(trip_id)

        # v0.5.76 — WAC pool replay so the new trip's cost reflects the
        # actual charge prices its energy was drawn from (and earlier
        # trips heal if their basis changed).
        if record.energy_kwh is not None and record.energy_kwh > 0:
            await self.storage.async_recompute_trip_costs_from_charges(
                self._current_energy_price(),
            )

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

        self._adopt_last_trip(record)
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
        # v0.5.85 — normalise the polarity: the rest of this function
        # assumes `value > 0 = motor drawing from battery (discharge)`.
        # When the configured sensor reports the opposite (BYD cloud),
        # flip it once at the entry point so every downstream branch
        # (regen accumulator, energy_from_power_kwh, max_power) gets
        # the right sign.
        if self._power_sign_inverted:
            value = -value
        # Charge tracking: capture live power so current_charge sensors can
        # display "charging at X kW right now". Runs even with no trip open.
        if self.current_charge is not None:
            abs_val = abs(value)
            self.current_charge.last_power_kw = abs_val
            # v0.6.0 — track per-session peak |P|. Used at close to
            # flag high-stress DCFC sessions (>=100 kW) for the SoH
            # accumulator; survives merges via storage.async_extend_
            # last_charge's peak-max logic.
            if abs_val > self.current_charge.peak_charge_power_kw:
                self.current_charge.peak_charge_power_kw = abs_val
            # v0.5.89 — integrate the car-side power into the charge
            # session so we have an independent measurement of kWh
            # delivered to the battery (cross-checks the SoC-derived
            # number, which has 1 % quantization noise). After the
            # `power_sign_inverted` flip the convention is "battery
            # receiving = negative"; we sum `-value` only when it's
            # negative (= actually charging) so AC inrush blips or
            # sensor noise crossing zero never inflates the total.
            now = dt_util.now()
            prev_kw = self.current_charge._last_power_kw_signed
            prev_ts = self.current_charge._last_power_ts
            if prev_kw is not None and prev_ts is not None:
                dt_h = (now - prev_ts).total_seconds() / 3600.0
                if 0 < dt_h <= _MAX_POWER_TRAPEZOID_DT_H:
                    # Charge contribution: trapezoidal area on the
                    # battery-receiving side only (both samples ≤ 0).
                    if prev_kw <= 0 and value <= 0:
                        self.current_charge.energy_added_kwh += (
                            (-prev_kw + -value) / 2.0 * dt_h
                        )
            self.current_charge._last_power_kw_signed = value
            self.current_charge._last_power_ts = now
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
                # v0.5.75 — exact area below zero, not the trapezoid of
                # negative-only endpoints. Convention: discharge>0, regen<0.
                # Cases:
                #   both ≤0 → trapezoid of |P| (whole segment is regen)
                #   both ≥0 → 0 (whole segment is discharge)
                #   cross   → triangle whose base is the fraction of dt
                #             below zero. Linear interp gives root at
                #             t* = |a|/(|a|+|b|); area = b²/(2·(|a|+|b|))·dt
                #             on the side where b<0, taking |b|.
                # Old formula (a+b)/2·dt with a=-min(prev,0), b=-min(val,0)
                # over-counted by ~3× when one endpoint was a deep
                # discharge and the next a deep regen (e.g. coasting →
                # foot off): trip 163 logged 1.97 kWh regen in 10 km of
                # city driving, physically impossible.
                if prev_kw <= 0 and value <= 0:
                    self.current.regen_kwh += (-prev_kw + -value) / 2.0 * dt_h
                elif prev_kw < 0 and value > 0:
                    span = -prev_kw + value
                    if span > 0:
                        self.current.regen_kwh += (prev_kw * prev_kw) / (2.0 * span) * dt_h
                elif prev_kw > 0 and value < 0:
                    span = prev_kw + -value
                    if span > 0:
                        self.current.regen_kwh += (value * value) / (2.0 * span) * dt_h
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
    def _async_evse_power_changed(self, event: Event[EventStateChangedData]) -> None:
        """v0.5.89 — integrate the EVSE / wallbox power into the active
        charge session. Handles W or kW units transparently. No-op
        when no charge is in progress (saves the integral state for
        when one opens).
        """
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _INVALID_STATES:
            return
        try:
            value = float(new_state.state)
        except (TypeError, ValueError):
            return
        # Normalise to kW. The EVSE often reports in W (e.g. Shelly
        # 3EM, Wallbox Pulsar, V2C Trydan ≈ 7400 W during AC charge).
        unit = (
            new_state.attributes.get("unit_of_measurement") or ""
        ).strip()
        if unit.lower() in ("w", "watt", "watts"):
            value = value / 1000.0
        if self.current_charge is None:
            # Save the latest reading anyway so the first sample after
            # the charge opens is a valid baseline (not from minutes
            # before the cable was plugged in).
            return
        now = dt_util.now()
        prev_kw = self.current_charge._last_evse_kw
        prev_ts = self.current_charge._last_evse_ts
        if prev_kw is not None and prev_ts is not None:
            dt_h = (now - prev_ts).total_seconds() / 3600.0
            if 0 < dt_h <= _MAX_POWER_TRAPEZOID_DT_H:
                # EVSE always reports positive while delivering power
                # (some report 0 in idle). Trapezoidal area between
                # consecutive readings.
                a = max(0.0, prev_kw)
                b = max(0.0, value)
                self.current_charge.evse_energy_kwh += (a + b) / 2.0 * dt_h
        self.current_charge._last_evse_kw = value
        self.current_charge._last_evse_ts = now
        # v0.6.0 — EVSE-derived peak power as a fallback when the
        # vehicle's own power sensor isn't wired (common AC-home
        # setups). The car-power tracker, when present, already
        # supplies a tighter `peak_charge_power_kw`; take the max so
        # whichever source saw the spike wins.
        if value > self.current_charge.peak_charge_power_kw:
            self.current_charge.peak_charge_power_kw = value
        self._notify_listeners()

    @callback
    def _async_charge_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _INVALID_STATES:
            return
        # v0.5.61 — `state == STATE_ON` only matched binary_sensor 'on'.
        # Now accepts Tesla's `Charging`, OVMS's `charging`, etc.
        is_charging = self._is_charging_value(new_state.state) is True
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

    def kick_abrp_push(self) -> None:
        """Force the next ABRP push to fire ASAP, bypassing the throttle.

        Called when a trip opens or the user flips the ABRP switch ON
        so we don't wait for the next upstream metric tick to surface
        a fresh datapoint. The push itself is scheduled as a task so
        the caller stays sync-safe.
        """
        if self._abrp is None or not self.abrp_push_enabled:
            return
        self._abrp_last_send = 0.0
        self.hass.async_create_task(self._async_maybe_send_abrp())

    async def _async_maybe_send_abrp(self) -> None:
        """Build a TLM payload from current sensor readings and push to ABRP.

        Throttled by ABRP_MIN_SEND_INTERVAL_S so a metric burst (BYD's
        cloud-poll can emit several state changes within a second)
        doesn't flood the endpoint. Skipped entirely if the client
        isn't configured.

        Sign note: ABRP wants kW in the convention **+discharge /
        -charge**. `build_tlm` negates whatever watts it receives, so
        it expects the opposite convention as input (-discharge /
        +charge) and flips it back. `CONF_POWER_SIGN_INVERTED` is the
        user-facing knob for sources that report discharge as negative
        (e.g. some BYD cloud entities) — applied here (not just in
        `_async_power_changed`) because this reads the raw sensor
        state fresh rather than reusing that method's already-flipped
        local value. v0.8.1 fix: this used to skip the flag entirely
        and pre-negate the raw reading unconditionally, which cancelled
        `build_tlm`'s negation and sent the raw sensor sign straight
        through — backwards for any source where discharge isn't
        already negative.
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
        # Normalise to +discharge/-charge exactly like _async_power_changed
        # does (coordinator.py ~2913), since we're reading the raw sensor
        # state fresh here rather than reusing that method's local value.
        # build_tlm then negates once more to hand ABRP its own
        # +discharge/-charge convention back.
        power_w_for_tlm: float | None = None
        if power_kw is not None:
            norm_kw = -power_kw if self._power_sign_inverted else power_kw
            power_w_for_tlm = -float(norm_kw) * 1000.0
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
            # v0.5.61 — multi-vocab. Tesla's `charging_state` etc.
            is_charging = self._read_is_charging(self._charge_sensor)
        is_parked: bool | None = None
        veh_on = self._read_bool(self._vehicle_on)
        if veh_on is not None:
            is_parked = not veh_on
        # v0.8.0 — extra fields when we have the data.
        est_range = self._read_float(self._range) if self._range else None
        heading = self._read_float(self._heading) if self._heading else None
        # v0.8.7 — cabin temp, HVAC setpoint, tire pressures (bar/psi -> kPa).
        cabin_temp = self._read_float(self._cabin_temp) if self._cabin_temp else None
        hvac_setpoint = (
            self._read_float(self._hvac_setpoint) if self._hvac_setpoint else None
        )
        tire_fl = self._read_pressure_kpa(self._tire_fl)
        tire_fr = self._read_pressure_kpa(self._tire_fr)
        tire_rl = self._read_pressure_kpa(self._tire_rl)
        tire_rr = self._read_pressure_kpa(self._tire_rr)
        capacity = self.battery_capacity  # calibrated kWh (>0 → sent)
        # v0.8.5 — send the REAL calibrated SoH (same figure as
        # sensor.<D>_battery_soh: calibrated / baseline capacity), not
        # self._abrp_soh_cache (the age/mileage/climate *model* used only
        # as a diagnostic "expected vs actual" comparison). Sending the
        # generic model to ABRP as if it were this car's actual health
        # would be misleading; the calibrated figure is grounded in this
        # vehicle's own observed charge behaviour and gracefully reads
        # 100 % before enough charges have accumulated to calibrate.
        baseline = self.battery_capacity_baseline
        soh = (
            round(self.battery_capacity / baseline * 100.0, 2)
            if baseline > 0
            else None
        )
        kwh_charged = (
            self.current_charge.energy_added_kwh
            if self.current_charge is not None
            else None
        )
        tlm = build_tlm(
            soc=soc,
            power_w=power_w_for_tlm,
            speed=speed,
            lat=lat, lon=lon,
            is_charging=is_charging,
            is_parked=is_parked,
            ext_temp=ext_temp,
            est_range=est_range,
            odometer=odo,
            car_model=self._abrp_car_model,
            heading=heading,
            soh=soh,
            capacity=capacity,
            kwh_charged=kwh_charged,
            cabin_temp=cabin_temp,
            hvac_setpoint=hvac_setpoint,
            tire_pressure_fl=tire_fl,
            tire_pressure_fr=tire_fr,
            tire_pressure_rl=tire_rl,
            tire_pressure_rr=tire_rr,
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
        # None check, not `or` — 0 % is a valid (if grim) reading.
        soc_read = self._read_float(self._battery)
        soc_end = soc_read if soc_read is not None else active.last_seen_soc
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

        # Merge eligibility — decided BEFORE the time dedup (v0.5.47).
        # Multiple `charging` on/off pulses (battery balancing, scheduled
        # charging windows, sentry top-ups) inside one plugged interval
        # are the same session — we shouldn't fragment OR drop them.
        #
        # v0.5.45 — "still connected" must mean CONTINUOUSLY connected.
        # Checking only the current plug state merged sessions that were
        # days apart (unplug → drive 70 km → replug overnight) into one
        # multi-day 60+ kWh row. Two extra gates:
        #   1. SoC didn't drop since the previous charge ended (a drop
        #      means the car was driven — different session).
        #   2. Recorder history shows no plug 'off' since the previous
        #      charge. On any doubt we insert a new row: fragmentation
        #      is recoverable, a corrupted merge is not.
        soc_dropped_since_last = (
            self.last_charge is not None
            and self.last_charge.soc_end is not None
            and active.soc_start is not None
            and float(active.soc_start) < float(self.last_charge.soc_end) - 1.0
        )
        can_merge = (
            self._plug_sensor is not None
            and self._read_bool(self._plug_sensor) is True
            and self.last_charge is not None
            and not soc_dropped_since_last
            and await self._async_plug_stayed_connected_since(
                self.last_charge.ended_at
            )
        )

        if self.last_charge is not None and not can_merge:
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
            #
            # v0.5.47 — the dedup used to run BEFORE the merge decision,
            # so a continuity-proven pulse within 2 h of the session
            # start was silently DROPPED (its kWh lost) instead of
            # merged. Continuity-proven pulses now bypass this gate.
            if elapsed < 7200:  # 2 h
                _LOGGER.debug(
                    "Skipping auto-charge: previous charge %.0fs ago "
                    "(price_locked=%s)",
                    elapsed, self.last_charge.price_locked,
                )
                self._notify_listeners()
                return

        kwh_soc = (soc_end - active.soc_start) / 100.0 * self.battery_capacity
        # v0.5.89 — prefer the power-integration measurement when it
        # exists and is within reasonable bounds of the SoC delta.
        # The integral covers the FULL charge curve (incl. taper at
        # high SoC where 1 % covers more time than 1 % at low SoC),
        # so it's a more accurate estimate than `(Δ% × nominal_cap)`
        # which assumes uniform energy-per-percent.
        kwh = kwh_soc
        if (
            active.energy_added_kwh > 0
            and 0.7 * kwh_soc <= active.energy_added_kwh <= 1.3 * kwh_soc
        ):
            kwh = round(active.energy_added_kwh, 3)
            _LOGGER.info(
                "Charge kWh: using power-integration %.2f (SoC said %.2f, "
                "delta %.0f %%)",
                kwh, kwh_soc, (kwh - kwh_soc) / kwh_soc * 100.0,
            )
        elif active.energy_added_kwh > 0:
            _LOGGER.info(
                "Charge kWh: power-integration %.2f outside ±30 %% of SoC "
                "(%.2f kWh) — falling back to SoC math.",
                active.energy_added_kwh, kwh_soc,
            )
        # v0.5.89 — log the EVSE/wallbox side delivery + implied
        # AC→DC efficiency. Stored in attributes later; for now the
        # info log gives the user a real-time number to check against
        # their utility / EVSE app.
        if active.evse_energy_kwh > 0:
            eff = kwh / active.evse_energy_kwh if active.evse_energy_kwh > 0 else None
            _LOGGER.info(
                "Charge EVSE side: %.2f kWh delivered → %.2f kWh battery, "
                "efficiency = %.1f %%",
                active.evse_energy_kwh, kwh,
                eff * 100.0 if eff else 0.0,
            )

        if can_merge:
            # v0.5.94 — propagate the new pulse's EVSE-side energy so
            # merged multi-pulse sessions accumulate the AC reading
            # instead of dropping it on the floor.
            extra_evse = (
                active.evse_energy_kwh
                if active.evse_energy_kwh > 0 else None
            )
            # v0.6.0 — also propagate the new pulse's peak |P| so the
            # merged row keeps the session's lifetime peak (storage
            # takes the max of the existing row and this value).
            new_peak = (
                active.peak_charge_power_kw
                if active.peak_charge_power_kw > 0 else None
            )
            merged = await self.storage.async_extend_last_charge(
                extra_kwh=kwh, ended_at=now, soc_end=soc_end,
                extra_evse_kwh=extra_evse,
                new_peak_power_kw=new_peak,
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
            evse_energy_kwh=(
                active.evse_energy_kwh if active.evse_energy_kwh > 0 else None
            ),
            peak_charge_power_kw=(
                active.peak_charge_power_kw
                if active.peak_charge_power_kw > 0 else None
            ),
            # v0.6.5 — auto-detect path doesn't carry a per-session
            # temperature sample yet, so leave it to the service to
            # read the current exterior-temp sensor at close.
            temperature_c=None,
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
        self, now: datetime, *, suppress_snap: bool = False
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

        # (a.5) Snap to previous trip's soc_end when parking was short
        # and the apparent gap is within integer-quantization / BMS-
        # settle noise. Most consecutive-trip pairs on integer-SoC
        # integrations (BYD, …) show a phantom 1 % drop after a few
        # minutes parked because the BMS rounds down after the pack
        # relaxes; this branch erases the visible inconsistency.
        # Conditions:
        #   - last trip ended ≤ _SHORT_PARK_SNAP_WINDOW ago
        #   - no charge ended since the last trip closed
        #   - current SoC is in [last.soc_end - 2, last.soc_end]
        #     (positive-only gap; real charges are handled by branch (a))
        #
        # v0.5.41 — suppress_snap is True when _open_trip already
        # detected a km gap and will insert an orphan trip to absorb
        # the missing distance + SoC. Snapping in that case would
        # steal the SoC the orphan needs.
        if (
            not suppress_snap
            and lt is not None
            and lt.ended_at is not None
            and lt.soc_end is not None
            and (now - lt.ended_at) <= _SHORT_PARK_SNAP_WINDOW
            and (lc is None or lc.ended_at is None or lc.ended_at <= lt.ended_at)
            and current is not None
        ):
            soc_end_f = float(lt.soc_end)
            gap = soc_end_f - current
            if 0.0 <= gap <= _SHORT_PARK_SNAP_GAP_PCT:
                return soc_end_f, "snap_short_park"

        # (b) Freshest sample within the pre-on lookback window.
        # v0.5.81 — defensive silence gate: if the SoC sample is older
        # than `_TELEMETRY_SILENCE_TIMEOUT_S` it predates a cloud
        # disconnect or polling pause. The pre-silence reading does
        # NOT reflect the SoC right before this trip — vampire drain
        # during the disconnect would otherwise be billed as trip
        # consumption. Skip it and let branch (c) use the freshest
        # current value instead.
        cutoff = now - _PRE_ON_LOOKBACK
        silence_floor = now - timedelta(seconds=_TELEMETRY_SILENCE_TIMEOUT_S)
        for ts, soc in reversed(self._soc_history):
            if ts < cutoff:
                break
            if ts < silence_floor:
                # Sample lived through telemetry silence — distrust it.
                continue
            # SoC should be ≥ current — the car only drains after on.
            # If pre < current, the buffer entry is stale or a noisy dip
            # (78→77→78 across polls); keep scanning back to the cutoff
            # for an older-but-valid sample instead of giving up on the
            # first miss.
            if current is None or soc >= current - 0.5:
                return float(soc), "pre_on_sample"

        # (c) Fallback to whatever the integration currently reports.
        if current is not None:
            return float(current), "post_on_sample"
        return None, "unavailable"

    def _detect_orphan_gap(
        self, now: datetime, odometer: float | None
    ) -> tuple[TripRecord, float] | None:
        """Decide whether a km gap with the previous trip warrants an
        orphan-trip record. Returns (last_trip, km_gap) if yes, else None.

        Conditions:
          - last_trip exists with ended_at + odometer_end
          - elapsed since prev close ≤ _ORPHAN_MAX_DURATION_S
          - km_gap ∈ (threshold, _ORPHAN_MAX_KM_GAP] where
            threshold = max(_ORPHAN_MIN_KM_GAP, self._min_distance)

        Anything outside these guards is either pure noise (snap_short_park
        territory) or implausible (odometer glitch) and should not produce
        a synthetic record.

        v0.5.100 — `_ORPHAN_MIN_KM_GAP` (0.3 km, the quantization noise
        floor) used to be the only lower bound. The user-configured
        `min_trip_distance_km` was ignored on the orphan path, so
        re-park maneuvers (1 km on a setting of 2 km) still produced
        rows. Now the orphan path also respects the user's threshold;
        the 0.3 floor stays as a noise gate so a setting < 0.3 doesn't
        re-open the quantization door.
        """
        lt = self.last_trip
        if lt is None or lt.ended_at is None or lt.odometer_end is None:
            return None
        if odometer is None:
            return None
        elapsed_s = (now - lt.ended_at).total_seconds()
        if elapsed_s <= 0 or elapsed_s > _ORPHAN_MAX_DURATION_S:
            return None
        km_gap = float(odometer) - float(lt.odometer_end)
        threshold = max(_ORPHAN_MIN_KM_GAP, float(self._min_distance))
        if not (threshold < km_gap <= _ORPHAN_MAX_KM_GAP):
            return None
        return lt, km_gap

    async def _async_insert_orphan_with_recovery(
        self,
        last_trip: TripRecord,
        now: datetime,
        new_odo: float | None,
        new_soc: float | None,
        km_gap: float,
    ) -> None:
        """v0.5.98 — try recorder-based recovery before inserting an
        orphan_odo_only row.

        The orphan path was created for two distinct failure modes:
          1. Cloud reported odometer growth with no vehicle_on edges
             we could observe (true silence, recorder is just as
             blind as the live capture).
          2. A live trip happened but the live capture missed the
             vehicle_on transitions (BT race / brief polling pause /
             integration startup window). Recorder DOES have the
             evidence — odometer growth + vehicle_on toggles — but
             the orphan path doesn't look at them and produces a
             single bogus 30-min window with `orphan_odo_only`
             confidence.

        Run `async_recover_missing_trips_service` over the silence
        window first. If it produces ≥1 trip the gap is covered with
        proper timestamps + SoC + GPS — no orphan needed. Only when
        recovery finds nothing do we fall back to the orphan row, as
        a residual catch for true catch-up snapshots.
        """
        if last_trip.ended_at is None:
            return
        try:
            recovered = await self.async_recover_missing_trips_service(
                since=last_trip.ended_at, until=now,
            )
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.debug(
                "Orphan recovery attempt raised: %s — falling back "
                "to orphan_odo_only insert", exc,
            )
            recovered = 0
        if recovered > 0:
            _LOGGER.info(
                "Orphan replaced by %d recovered live trip(s) — recorder "
                "had the precise vehicle_on edges, no synthetic window "
                "needed (km_gap=%.2f)",
                recovered, km_gap,
            )
            return
        await self._async_insert_orphan_trip(
            last_trip, now, new_odo, new_soc, km_gap,
        )

    async def _async_insert_orphan_trip(
        self,
        last_trip: TripRecord,
        now: datetime,
        new_odo: float | None,
        new_soc: float | None,
        km_gap: float,
    ) -> None:
        """Persist a synthetic trip covering the gap between two real
        trips. Classifies via the observed SoC drop against the expected
        drop from km × default consumption.

        Confidence tags:
          'orphan'         → km and SoC both consistent → real missed drive.
          'orphan_odo_only'→ km present but SoC drop ≈ 0 → previous trip's
                             odometer_end was stale (the km are really the
                             tail of the previous drive). Energy fields
                             stay NULL to avoid double-counting.

        Bails (without inserting) when the SoC moves UP between trips
        (a charge happened — handled by _resolve_soc_start's branch a)
        or when the SoC↔km ratio is so far off that we can't tell what
        kind of event we're looking at.
        """
        if last_trip.ended_at is None or last_trip.odometer_end is None:
            return
        if new_odo is None:
            return
        # SoC going UP between trips means a charge slipped through.
        # Branch (a) of _resolve_soc_start handles that anchor; we do
        # not want to invent an orphan over a charge.
        soc_gap: float | None = None
        if last_trip.soc_end is not None and new_soc is not None:
            soc_gap = float(last_trip.soc_end) - float(new_soc)
            if soc_gap < -0.5:
                _LOGGER.info(
                    "Orphan skipped: SoC rose %.1f%% between trips "
                    "(charge likely happened — handled by anchor branch)",
                    -soc_gap,
                )
                return

        duration_s = max(0.0, (now - last_trip.ended_at).total_seconds())
        duration_min = duration_s / 60.0
        # v0.5.83 — orphan windows default to the full prev-end → now
        # span, which for `orphan_odo_only` catch-up snapshots (single
        # odo delta with no SoC drop) produces multi-hour "trips" that
        # mislead the dashboard. We can't know the exact moment the
        # catch-up landed, but we DO know it's bounded by the silence
        # window — 30 min is a reasonable best-guess upper bound.
        # Concrete fix is applied below after we classify.
        orphan_started_at = last_trip.ended_at

        # Classify by SoC↔km consistency.
        confidence: str
        soc_used_pct: float | None
        energy_kwh: float | None
        consumption: float | None
        if soc_gap is None or soc_gap < 0.5:
            confidence = "orphan_odo_only"
            soc_used_pct = None
            energy_kwh = None
            consumption = None
        else:
            expected_soc = km_gap * _ORPHAN_DEFAULT_KWH_100KM / self.battery_capacity
            ratio = (
                soc_gap / expected_soc if expected_soc > 0 else float("inf")
            )
            if _ORPHAN_RATIO_MIN <= ratio <= _ORPHAN_RATIO_MAX:
                confidence = "orphan"
                soc_used_pct = soc_gap
                energy_kwh = (soc_gap / 100.0) * self.battery_capacity
                consumption = (
                    (energy_kwh / km_gap * 100.0) if km_gap > 0 else None
                )
            else:
                # Inconsistent — log + skip rather than fabricate numbers
                # that the dashboard can't reason about.
                _LOGGER.info(
                    "Orphan candidate rejected: km_gap=%.2f soc_gap=%.2f "
                    "expected=%.2f ratio=%.2f",
                    km_gap, soc_gap, expected_soc, ratio,
                )
                return

        # v0.5.83 — bound the orphan window for catch-up snapshots.
        # When confidence is `orphan_odo_only` (km appeared but SoC
        # didn't drop) AND the prev→now gap is > 30 min, the row is
        # almost certainly a single catch-up odo delta that landed
        # during silence — NOT a 6 h missed drive. Cap the visible
        # window at 30 min ending at `now` so the dashboard reads
        # "short missed drive" instead of "6 h trip with 2 km".
        if confidence == "orphan_odo_only" and duration_s > 1800:
            bounded = now - timedelta(seconds=1800)
            if bounded > last_trip.ended_at:
                orphan_started_at = bounded
                duration_s = 1800.0
                duration_min = 30.0
                _LOGGER.info(
                    "Orphan window bounded: catch-up snapshot, %.1f h → 30 min "
                    "(prev_end=%s, now=%s)",
                    (now - last_trip.ended_at).total_seconds() / 3600.0,
                    last_trip.ended_at.isoformat(), now.isoformat(),
                )
        # v0.8.1 — same padding problem for real 'orphan' drives (SoC
        # consistent with the km, so distance/energy stay trustworthy),
        # but a short HA restart (or any gap the live path + recorder
        # recovery both miss) still stretches the window from the
        # previous trip's end to `now`, baking the downtime in as if it
        # were driving. Cap it to the longest duration still compatible
        # with having driven at _ORPHAN_MIN_PLAUSIBLE_AVG_KMH — this
        # only ever shortens (never invents a faster-than-observed
        # trip), so a 10 km gap can't read as a multi-hour drive.
        elif confidence == "orphan" and duration_min > 0 and km_gap > 0:
            implied_avg = km_gap / (duration_min / 60.0)
            if implied_avg < _ORPHAN_MIN_PLAUSIBLE_AVG_KMH:
                capped_min = (km_gap / _ORPHAN_MIN_PLAUSIBLE_AVG_KMH) * 60.0
                bounded = now - timedelta(minutes=capped_min)
                if bounded > last_trip.ended_at:
                    _LOGGER.info(
                        "Orphan duration capped: implied avg %.1f km/h < "
                        "%.0f km/h floor — window likely padded by parked/"
                        "offline time (%.1f min -> %.1f min)",
                        implied_avg, _ORPHAN_MIN_PLAUSIBLE_AVG_KMH,
                        duration_min, capped_min,
                    )
                    orphan_started_at = bounded
                    duration_s = capped_min * 60.0
                    duration_min = capped_min

        avg_speed = (
            (km_gap / (duration_min / 60.0))
            if duration_min > 0 and km_gap > 0
            else None
        )
        # Reject implausible average speeds (sensor glitch / overlap).
        if avg_speed is not None and avg_speed > 300:
            avg_speed = None

        # v0.8.5 — same destination/journey resolution as the disconnect
        # path below, and for the same reason: hardcoding destination=None
        # meant an orphan window that actually landed back home never
        # closed the journey, and this trip was never adopted as
        # last_trip, so dashboards kept showing whatever trip predated it.
        location_end = self._read_str(self._location) if self._location else None
        # v0.8.10 — no GPS route in this reconstruction path, but the
        # tracker's CURRENT coords are still worth a free-typed
        # secondary-home check when the state itself names no zone.
        if _is_zoneless(location_end) and self._location:
            loc_state = self.hass.states.get(self._location)
            if loc_state is not None:
                try:
                    cur_lat = float(loc_state.attributes.get("latitude"))
                    cur_lon = float(loc_state.attributes.get("longitude"))
                except (TypeError, ValueError):
                    cur_lat = cur_lon = None
                coord_label = self._secondary_home_coord_label(cur_lat, cur_lon)
                if coord_label is not None:
                    location_end = coord_label
        origin_location = last_trip.destination
        is_at_home_end = self._is_at_any_home(location_end)
        started_from_home = self._is_at_any_home(origin_location)
        journey_id: int | None
        if self.current_journey_id is not None:
            journey_id = self.current_journey_id
        elif started_from_home:
            journey_id = await self.storage.async_next_journey_id()
        elif is_at_home_end:
            journey_id = await self.storage.async_next_journey_id()
        else:
            journey_id = None

        record = TripRecord(
            started_at=orphan_started_at,
            ended_at=now,
            duration_min=duration_min,
            distance_km=km_gap,
            odometer_start=last_trip.odometer_end,
            odometer_end=new_odo,
            soc_start=last_trip.soc_end,
            soc_end=new_soc,
            soc_used_pct=soc_used_pct,
            energy_kwh=energy_kwh,
            consumption_kwh_100km=consumption,
            avg_speed_kmh=avg_speed,
            origin=origin_location,
            destination=location_end,
            confidence=confidence,
            journey_id=journey_id,
            # v0.5.44 — orphan windows are reconstructed too; pull the
            # driver from recorder history.
            driver=await self._async_driver_during(last_trip.ended_at, now),
        )
        try:
            trip_id = await self.storage.async_insert(record)
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.warning("Orphan trip insert failed: %s", exc)
            return
        record.trip_id = trip_id
        # v0.5.77 — schedule the vehicle-native energy heal.
        self._schedule_vehicle_heal(trip_id)
        # v0.5.76 — keep the WAC pool accounting consistent.
        if record.energy_kwh is not None and record.energy_kwh > 0:
            await self.storage.async_recompute_trip_costs_from_charges(
                self._current_energy_price(),
            )
        if is_at_home_end and journey_id is not None:
            self.last_completed_journey_id = journey_id
            self.current_journey_id = None
        else:
            self.current_journey_id = journey_id
        self._adopt_last_trip(record)
        _LOGGER.info(
            "Orphan trip inserted (%s): %.2f km, soc %s→%s, duration %.1f min",
            confidence, km_gap, last_trip.soc_end, new_soc, duration_min,
        )
        self._notify_listeners()
        self._notify_trip_log_listeners()

    async def _async_insert_disconnect_orphan(
        self,
        prev_t: datetime,
        now: datetime,
        prev_odo: float,
        odo: float,
        prev_soc: float | None,
        soc: float | None,
    ) -> None:
        """v0.5.80 — record a drive that happened while the cloud
        integration was offline. The integration came back from
        silence with a clear odometer jump; we know it happened SOME
        time inside [prev_t, now] but not exactly when, so we use the
        widest bounds and tag the row as 'orphan_disconnect' so
        dashboards / aggregates can de-emphasise it.
        """
        # SoC delta: only trust if it went DOWN (driving consumes).
        soc_used: float | None = None
        energy_kwh: float | None = None
        if prev_soc is not None and soc is not None and prev_soc > soc:
            soc_used = float(prev_soc) - float(soc)
            energy_kwh = soc_used / 100.0 * self.battery_capacity
        distance = float(odo) - float(prev_odo)
        consumption = (
            (energy_kwh / distance * 100.0)
            if energy_kwh is not None and distance > 0
            else None
        )
        duration_min = max(0.1, (now - prev_t).total_seconds() / 60.0)

        # v0.8.5 — resolve the real end location instead of hardcoding
        # None. The disconnect could easily have ended with the vehicle
        # back home (the whole point of a "disconnect" gap is that we
        # were blind while it happened), and getting this wrong meant
        # dashboards read "Outside known zones" for a trip that actually
        # ended at home, plus the home arrival never closed the journey.
        location_end = self._read_str(self._location) if self._location else None
        # v0.8.10 — no GPS route in this reconstruction path, but the
        # tracker's CURRENT coords are still worth a free-typed
        # secondary-home check when the state itself names no zone.
        if _is_zoneless(location_end) and self._location:
            loc_state = self.hass.states.get(self._location)
            if loc_state is not None:
                try:
                    cur_lat = float(loc_state.attributes.get("latitude"))
                    cur_lon = float(loc_state.attributes.get("longitude"))
                except (TypeError, ValueError):
                    cur_lat = cur_lon = None
                coord_label = self._secondary_home_coord_label(cur_lat, cur_lon)
                if coord_label is not None:
                    location_end = coord_label
        origin_location = self.last_trip.destination if self.last_trip else None
        is_at_home_end = self._is_at_any_home(location_end)
        started_from_home = self._is_at_any_home(origin_location)

        # Journey membership — same rule as the live close path (v0.5.14):
        # continue an already-open journey regardless of where this gap
        # started; otherwise only open one if the previous trip's
        # destination was home (this gap's implied origin), or stitch a
        # one-stage journey if it lands back home now.
        journey_id: int | None
        if self.current_journey_id is not None:
            journey_id = self.current_journey_id
        elif started_from_home:
            journey_id = await self.storage.async_next_journey_id()
        elif is_at_home_end:
            journey_id = await self.storage.async_next_journey_id()
        else:
            journey_id = None

        record = TripRecord(
            started_at=prev_t,
            ended_at=now,
            duration_min=duration_min,
            distance_km=distance,
            odometer_start=prev_odo,
            odometer_end=odo,
            soc_start=prev_soc,
            soc_end=soc,
            soc_used_pct=soc_used,
            energy_kwh=energy_kwh,
            consumption_kwh_100km=consumption,
            confidence="orphan_disconnect",
            energy_source="soc" if energy_kwh is not None else None,
            origin=origin_location,
            destination=location_end,
            journey_id=journey_id,
        )
        try:
            trip_id = await self.storage.async_insert(record)
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.warning("Disconnect orphan insert failed: %s", exc)
            return
        record.trip_id = trip_id
        self._schedule_vehicle_heal(trip_id)
        if record.energy_kwh is not None and record.energy_kwh > 0:
            await self.storage.async_recompute_trip_costs_from_charges(
                self._current_energy_price(),
            )
        if is_at_home_end and journey_id is not None:
            self.last_completed_journey_id = journey_id
            self.current_journey_id = None
        else:
            self.current_journey_id = journey_id
        # v0.8.5 — this trip can be newer than whatever last_trip currently
        # points to (it was inserted the moment connectivity returned, out
        # of band from the normal live-close flow) — without this, every
        # last_trip_* sensor stayed stuck on the trip BEFORE the gap until
        # the next live trip closed.
        self._adopt_last_trip(record)
        _LOGGER.info(
            "Disconnect-orphan inserted #%s: %.2f km / %.1f min (soc %s→%s)",
            trip_id, distance, duration_min, prev_soc, soc,
        )
        self._notify_listeners()
        self._notify_trip_log_listeners()

    def _open_trip(self, now: datetime) -> None:
        # If a synth-trip finalize was pending, cancel it — the live trip
        # will own the distance from here on.
        if self._unsub_synth_finalize is not None:
            self._unsub_synth_finalize()
            self._unsub_synth_finalize = None
        self._synth_baseline = None
        # v0.5.49 — any path that reaches _open_trip means a trip is
        # being opened NOW; drop any deferred retry so it can't fire
        # later and try to open a second one.
        self._cancel_pending_open()
        odometer = self._read_float(self._odometer)
        # v0.5.41 — detect a km gap with the previous trip BEFORE
        # resolving SoC. If a gap is found, suppress the snap-on-
        # short-park branch (otherwise it would consume the SoC the
        # orphan needs to absorb) and schedule the orphan insert.
        orphan_payload = self._detect_orphan_gap(now, odometer)
        soc, soc_source = self._resolve_soc_start(
            now, suppress_snap=orphan_payload is not None
        )
        if orphan_payload is not None:
            last_trip, km_gap = orphan_payload
            # v0.5.98 — prefer recorder-based reconstruction over the
            # orphan_odo_only synthetic window. If the recorder still
            # has the precise vehicle_on edges + odometer growth from
            # the missed drive, recover_missing_trips will insert
            # proper rows with real timestamps; only fall back to the
            # orphan row when recovery comes up empty (true odo drift
            # or cloud catch-up without any underlying drive).
            self.hass.async_create_task(
                self._async_insert_orphan_with_recovery(
                    last_trip, now, odometer, soc, km_gap,
                )
            )
        location = self._read_str(self._location) if self._location else None
        temp = self._read_float(self._temp) if self._temp else None

        self.current = TripInProgress(
            started_at=now,
            odometer_start=odometer,
            soc_start=soc,
            location_start=location,
            temp_samples=deque(
                [temp] if temp is not None else [],
                maxlen=_TRIP_TEMP_SAMPLES_MAX,
            ),
            last_seen_odometer=odometer,
            last_seen_soc=soc,
            last_movement_ts=now,  # treat trip start as the first movement
            soc_start_source=soc_source,
            # v0.5.43 — may still be None here (BT pairs a few seconds
            # after ignition); the live tick keeps retrying.
            driver=self._read_driver(),
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
        # v0.5.40 — kick ABRP immediately so the first telemetry point
        # lands within seconds of ignition instead of waiting for the
        # next upstream metric tick (BYD's median cadence is ~8 min;
        # at the start of a drive that's a visible gap in ABRP).
        self.kick_abrp_push()
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

        # v0.6.6 — idle accumulator. Count this tick (_LIVE_TICK
        # seconds, default 30) as "stationary with ignition on" when
        # either the explicit speed sensor reads < 1 km/h, OR — when
        # no speed sensor is wired — there's been no movement signal
        # for longer than 2× the live-tick interval. The threshold of
        # 2× protects against single-sample blips: a brief 0 km/h on a
        # cloud-polled source between two motion samples doesn't
        # falsely accumulate idle time.
        is_idle: bool | None = None
        if self._speed:
            s = self._read_float(self._speed)
            if s is not None:
                is_idle = s < 1.0
                # v0.7.3 — append this tick's speed to the deque used
                # by close-time V95 / highway-ratio math. Cadence is
                # bound to _LIVE_TICK (30 s) so the percentile is
                # deterministic across cloud-polled sources with
                # wildly different sensor update rates.
                self.current.speed_samples.append(float(s))
        if is_idle is None:
            # Fallback path — relies on the last_movement_ts updated
            # by _async_metric_changed (odometer growth) and
            # _async_speed_changed (speed > 0).
            last_move = self.current.last_movement_ts
            if last_move is not None:
                idle_threshold_s = _LIVE_TICK.total_seconds() * 2
                is_idle = (now - last_move).total_seconds() > idle_threshold_s
        if is_idle:
            self.current.idle_seconds += _LIVE_TICK.total_seconds()

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
                # v0.5.53 — if a vehicle_on=off grace close is already
                # scheduled (_pending_close_unsub), defer to it. The
                # grace's backdated `ended_at = car_off_since` is more
                # precise than `last_movement_ts` here (the off-edge
                # itself is captured exactly at off_ts, while
                # last_movement_ts is the last odo growth which may
                # have been minutes earlier in heavy traffic).
                if self._pending_close_unsub is not None:
                    return
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
        #
        # v0.5.41 — dedupe against the most recent sample to avoid storing
        # 60 identical points per 30 min trip (the upstream cadence is ~8
        # min on BYD, so 90 % of 30 s ticks landed on stale state). Also
        # force-refresh the upstream every _LOCATION_REFRESH_EVERY_N_TICKS
        # so the dashboard route is denser than the natural cadence.
        if self._location:
            state = self.hass.states.get(self._location)
            if state is not None:
                lat_raw = state.attributes.get("latitude")
                lon_raw = state.attributes.get("longitude")
                if lat_raw is not None and lon_raw is not None:
                    try:
                        lat = float(lat_raw)
                        lon = float(lon_raw)
                    except (TypeError, ValueError):
                        lat = lon = None  # type: ignore[assignment]
                    if lat is not None and lon is not None:
                        prev = (
                            self.current.gps_samples[-1]
                            if self.current.gps_samples else None
                        )
                        if prev is None or (
                            abs(lat - prev[1]) >= _GPS_DUP_EPSILON
                            or abs(lon - prev[2]) >= _GPS_DUP_EPSILON
                        ):
                            self.current.gps_samples.append((now, lat, lon))
        # v0.5.82 — accumulate driver-sensor samples weighted by time.
        # The close path picks the longest-dominant non-none value
        # instead of the brittle "first non-empty wins" of v0.5.43.
        # Same retry purpose (BT pairing completes seconds after
        # ignition) but the resolution is now evidence-based: a
        # passenger's phone briefly connecting then dropping does NOT
        # overwrite the actual driver's longer-running connection.
        if self._driver_sensor:
            fresh = self._read_driver()
            now = dt_util.now()
            # Accumulate time on the PREVIOUS sample before moving on.
            prev_ts = self.current._last_driver_sample_ts
            prev_val = self.current._last_driver_sample_value
            if prev_ts is not None and prev_val is not None:
                dt_s = (now - prev_ts).total_seconds()
                if dt_s > 0:
                    self.current.driver_samples[prev_val] = (
                        self.current.driver_samples.get(prev_val, 0.0) + dt_s
                    )
            self.current._last_driver_sample_ts = now
            self.current._last_driver_sample_value = fresh
            # Backwards-compat: keep `current.driver` set to the first
            # non-none value so any code that reads it mid-trip (e.g.
            # current_driver sensor) still has something to display.
            # Close-path replaces it with the dominant value.
            if self.current.driver is None and fresh is not None:
                self.current.driver = fresh
        # Periodic upstream poll-nudge so denser GPS data lands while
        # the trip is in progress (above & beyond what the integration
        # natively reports).
        self.current.live_tick_count += 1
        if (
            self.current.live_tick_count % _LOCATION_REFRESH_EVERY_N_TICKS == 0
            and self._location
        ):
            self.hass.async_create_task(self._async_force_refresh_location())
        self._notify_listeners()

    async def _async_force_refresh_location(self) -> None:
        """Ask HA to refresh the location entity (and battery/odometer
        when available) so denser samples land mid-trip. Cloud-polled
        integrations (BYD, Tesla Fleet) honour homeassistant.update_entity
        by triggering a fresh upstream fetch.

        Best-effort — any error is swallowed; if the platform doesn't
        support update_entity we still have the natural cadence.
        """
        targets: list[str] = []
        if self._location:
            targets.append(self._location)
        # Including battery + odometer here means SoC samples are also
        # denser during the drive — feeds _resolve_soc_start's ring
        # buffer for the next trip too.
        if self._battery:
            targets.append(self._battery)
        if self._odometer:
            targets.append(self._odometer)
        if not targets:
            return
        try:
            await self.hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": targets},
                blocking=False,
            )
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.debug("update_entity refresh failed: %s", exc)

    def _cancel_live_tick(self) -> None:
        if self._unsub_live_tick is not None:
            self._unsub_live_tick()
            self._unsub_live_tick = None

    @callback
    def _async_check_stuck_trip(self, _now: datetime) -> None:
        """v0.5.79 — periodic safety net for trips that get stuck open
        when the upstream integration goes offline or drops the off-edge.

        Two independent triggers:
          1. No movement for `_STUCK_TRIP_NO_MOVEMENT_MIN` minutes AND
             vehicle_on is currently off. The off-edge handler must have
             missed (or the on→off→on debounce hid it); close at the
             last movement time and tag confidence.
          2. Trip age exceeds `_STUCK_TRIP_MAX_AGE_H` hours regardless of
             vehicle_on state. At this point the upstream's state machine
             can't be trusted — even if vehicle_on is still 'on', the
             integration may simply not be polling. We close at the last
             known movement (a real timestamp) rather than `now` to avoid
             pinning a 6-hour duration on a 30-minute drive.

        Both close paths use `_async_close_trip` with a confidence
        override so the dashboard can show the user that the close was
        reconstructed rather than observed.

        TODO (integration-disconnect detection): when vehicle_on falls
        normally but the recorder shows the vehicle_on entity itself
        was unavailable for > 10 min before the off-edge, the off
        transition is suspect — the integration was offline and the
        edge may not coincide with the real parking moment. Tag as
        `reconstructed_disconnect` and use last_movement_ts as
        ended_at. Implementation requires reading recorder history for
        the vehicle_on entity in the off-edge path, ordering it
        relative to the debounced close, and threading the tag through.
        Deferred until we have a confirmed disconnect case to test
        against.
        """
        if self.current is None:
            return
        # If a debounced close is already pending from a real off-edge,
        # let it run instead of racing it.
        if self._pending_close_unsub is not None:
            return
        now = dt_util.now()
        last_move = self.current.last_movement_ts or self.current.started_at
        age_h = (now - self.current.started_at).total_seconds() / 3600.0
        no_move_min = (now - last_move).total_seconds() / 60.0

        # Trigger 2: trip is too old to be plausible. Close regardless of
        # vehicle_on (upstream may be wedged).
        if age_h > _STUCK_TRIP_MAX_AGE_H:
            close_ts = last_move
            _LOGGER.warning(
                "Stuck-trip watchdog: trip open for %.1f h (> %.1f h cap) — "
                "force-closing at last movement %s",
                age_h, _STUCK_TRIP_MAX_AGE_H, close_ts.isoformat(),
            )
            self.hass.async_create_task(
                self._async_close_trip(
                    close_ts, confidence_override="force_closed_max_age",
                )
            )
            return

        # Trigger 1: no movement AND vehicle_on=off → lost off-edge.
        if no_move_min > _STUCK_TRIP_NO_MOVEMENT_MIN:
            vehicle_on = self._read_bool(self._vehicle_on)
            if vehicle_on is False:
                close_ts = last_move
                _LOGGER.warning(
                    "Stuck-trip watchdog: no movement for %.0f min AND "
                    "vehicle_on=off — force-closing at %s "
                    "(lost off-edge)",
                    no_move_min, close_ts.isoformat(),
                )
                self.hass.async_create_task(
                    self._async_close_trip(
                        close_ts,
                        confidence_override="force_closed_no_movement",
                    )
                )

    async def _async_close_trip(
        self,
        now: datetime,
        *,
        confidence_override: str | None = None,
    ) -> None:
        """Close the in-memory trip and persist a TripRecord.

        v0.5.79 — `confidence_override` lets callers (e.g. the stuck-
        trip watchdog) tag a non-live close so the dashboard can warn
        the user. None preserves the existing 'live' tag.
        """
        active = self.current
        if active is None:
            return
        self._cancel_live_tick()

        # Explicit None checks — `or` would treat a legitimate 0 reading
        # (0 % SoC, reset odometer) as "unreadable" and silently replace
        # it with the stale last_seen value.
        odo_read = self._read_float(self._odometer)
        odometer_end = odo_read if odo_read is not None else active.last_seen_odometer
        soc_read = self._read_float(self._battery)
        soc_end = soc_read if soc_read is not None else active.last_seen_soc
        location_end = self._read_str(self._location) if self._location else None
        # v0.5.44 — tracker-lag fallback: when the tracker still says
        # not_home/unknown at close, resolve the zone from the last GPS
        # sample so the journey closes on a real home arrival.
        # v0.5.47 — only when the sample is FRESH: a stale point can be
        # mid-route and resolve to the wrong zone (worse than NULL).
        if (
            _is_zoneless(location_end)
            and active.gps_samples
            and (now - active.gps_samples[-1][0]) <= _ZONE_FALLBACK_MAX_AGE
        ):
            zone_end = self._zone_from_coords(
                active.gps_samples[-1][1], active.gps_samples[-1][2]
            )
            if zone_end is None:
                # v0.8.10 — not a registered HA zone; check free-typed
                # secondary-home coordinates instead.
                zone_end = self._secondary_home_coord_label(
                    active.gps_samples[-1][1], active.gps_samples[-1][2]
                )
            if zone_end is not None:
                location_end = zone_end

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
            (soc_used / 100.0) * self.battery_capacity
            if soc_used is not None and soc_used > 0
            else None
        )
        # v0.5.13 — power-integration backup. ∫|P|dt accumulated during
        # the trip is an independent estimator that doesn't depend on the
        # SoC sensor's cadence. We pick the larger of the two so a stale
        # SoC reading can never under-report consumption.
        # v0.5.75 — energy_from_power_kwh is GROSS throughput
        # (discharge + regen as magnitudes). To compare apples-to-apples
        # with the SoC delta, subtract regen twice: gross = discharge +
        # regen, net = discharge − regen = gross − 2·regen. Without this
        # the max() rule systematically inflated live trips: trip 163
        # reported 2.6 kWh / 26 kWh·100km when the SoC said 1.65 / 16.5,
        # because regen energy was double-counted (once as discharge in
        # |P|, once subtracted as regen).
        net_pwr = active.energy_from_power_kwh - 2.0 * (active.regen_kwh or 0.0)
        energy_pwr = (
            net_pwr
            if self._power and net_pwr > 0
            else None
        )
        # v0.5.88 — priority-based selection replaces the v0.5.13 `max()`.
        # The old "pessimistic" rule biased the row upward whenever
        # power_integration over-counted (trapezoidal zero-crossings
        # double-account when adjacent BYD cloud samples straddle the
        # zero line, e.g. +50→-50 reports area=50·dt instead of 0).
        # With v0.5.81 silence-gate making SoC anchoring robust, the
        # `max()` defence is no longer justified — SoC is now ground
        # truth above the 1 % quantization noise floor.
        #
        # Decision ladder:
        #   a) SoC delta ≥ 3 %  → use SoC unconditionally (above
        #      quantization, trustworthy). Power is logged but ignored.
        #   b) SoC delta 1–2 %  → use min(soc, power) when power passes
        #      sanity guards. The lower of the two is the more honest
        #      estimate in this regime.
        #   c) SoC delta 0      → use power if it passes sanity, else
        #      fall through to the historical-average fallback below.
        # Sanity guards reject power_integration when:
        #   - regen_kwh > 0.5 × energy_from_power_kwh (polarity flip
        #     suspected mid-trip — see v0.5.85)
        #   - net_pwr > 2 × energy_soc when both exist (bias evidence)
        regen = active.regen_kwh or 0.0
        gross = active.energy_from_power_kwh or 0.0
        power_polarity_ok = gross <= 0 or regen <= 0.5 * gross
        if energy_pwr is not None and energy_soc is not None:
            power_ratio_ok = energy_pwr <= 2.0 * energy_soc
        else:
            power_ratio_ok = True
        power_trusted = (
            energy_pwr is not None
            and power_polarity_ok
            and power_ratio_ok
        )
        soc_pct = soc_used or 0.0
        if energy_soc is not None and soc_pct >= 3.0:
            energy = energy_soc
            energy_source = "soc"
        elif energy_soc is not None and soc_pct >= 1.0:
            # Marginal SoC delta: defend against quantization noise on
            # short trips by anchoring to the LOWER of soc / power when
            # both signals exist. Picks the more honest of the two
            # instead of letting either side inflate freely.
            if power_trusted:
                energy = min(energy_soc, energy_pwr)
                energy_source = "soc" if energy_soc <= energy_pwr else "power_integration"
            else:
                energy = energy_soc
                energy_source = "soc"
        elif power_trusted:
            energy = energy_pwr
            energy_source = "power_integration"
        elif energy_soc is not None:
            energy = energy_soc
            energy_source = "soc"
        else:
            energy = None
            energy_source = None
        # When SoC won but power existed and disagrees by >50%, log the
        # divergence so future tuning has data without enabling DEBUG.
        if (
            energy_source == "soc"
            and energy is not None
            and energy_pwr is not None
            and energy_pwr > 0
        ):
            divergence = abs(energy_pwr - energy) / energy
            if divergence > 0.5:
                _LOGGER.info(
                    "Energy source: SoC won (%.2f kWh) over power-int "
                    "(%.2f kWh, %.0f%% divergence). polarity_ok=%s "
                    "ratio_ok=%s regen=%.2f gross=%.2f",
                    energy, energy_pwr, divergence * 100,
                    power_polarity_ok, power_ratio_ok, regen, gross,
                )
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
        # v0.8.3 — avg_speed > max_speed is physically impossible (the max
        # is a running ceiling sampled over the same window) and a strong
        # signal that distance/duration are corrupted even when neither
        # alone crosses the blunt >300 km/h cap above — e.g. a stale
        # odometer anchor (_ODOMETER_STALE_MAX_AGE_S) inflating distance
        # with km driven before `started_at` while duration only covers
        # the real short window. max_speed_kmh is tracked independently
        # from live speed samples (_async_speed_changed) and stays
        # trustworthy in that case, so don't let a broken avg outrank it
        # on the dashboard. 5% slack absorbs rounding / sampling-cadence
        # gaps, not corruption.
        if (
            avg_speed is not None
            and active.max_speed_kmh
            and avg_speed > active.max_speed_kmh * 1.05
        ):
            _LOGGER.warning(
                "Trip avg_speed %.1f km/h exceeds max_speed %.1f km/h — "
                "distance/duration input is inconsistent, dropping avg_speed",
                avg_speed, active.max_speed_kmh,
            )
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
        is_at_home_end = self._is_at_any_home(location_end)
        started_from_home = self._is_at_any_home(active.location_start)
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
            # v0.6.0 — gross discharge = ∫P·dt over P>0 segments.
            # Derived as energy_from_power_kwh (gross throughput) minus
            # regen_kwh; floor at 0 to guard against sampling artefacts
            # where regen briefly out-counts discharge in a single tick.
            # None when no power samples were captured.
            discharge_kwh=(
                round(max(
                    0.0,
                    active.energy_from_power_kwh - (active.regen_kwh or 0.0),
                ), 4)
                if active.energy_from_power_kwh > 0 else None
            ),
            # v0.6.6 — minutes vehicle was on but stationary. Capped at
            # the trip duration as a sanity guard (the idle counter
            # advances on every live tick, so a clock glitch can't
            # push it above duration). NULL when no live-tick ran
            # (synth / recovered paths).
            idle_minutes=(
                round(min(active.idle_seconds / 60.0, duration_min), 2)
                if active.idle_seconds > 0 else None
            ),
            # v0.7.3 — speed distribution metrics from per-tick samples.
            # Both None when no speed sensor is wired.
            v95_speed_kmh=_speed_stats(
                active.speed_samples,
                highway_threshold_kmh=_HIGHWAY_SPEED_KMH,
            )[0],
            highway_ratio_pct=_speed_stats(
                active.speed_samples,
                highway_threshold_kmh=_HIGHWAY_SPEED_KMH,
            )[1],
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
            # times + full metrics). v0.5.79 — the stuck-trip watchdog
            # (and similar reconstructed-close callers) override this.
            confidence=confidence_override or "live",
            # v0.5.97 — dominant in-memory driver, falling back to a
            # recorder lookup with a small pre/post window when the
            # in-memory capture missed (e.g. AA paired briefly BEFORE
            # ignition and dropped before the trip opened).
            driver=await self._async_resolve_trip_driver(
                active, active.started_at, now,
            ),
            # v0.5.68 — weather fields stay as their dataclass defaults
            # (None). They survive in the schema for backwards compat;
            # nothing new fills them.
            # v0.5.76 — initial cost-basis seed = home tariff. The
            # post-insert recompute below replays the WAC battery pool
            # and overwrites this with the blended price the trip
            # actually experienced.
            cost_basis_per_kwh=price_per_kwh if cost is not None else None,
            # v0.5.84 — battery-capacity calibration factor. K compares
            # the power-integration NET energy (real motor draw) to
            # the SoC-derived nominal energy. K < 1 over many trips
            # signals real degradation; spikes per trip are sampling
            # noise. Only computed when both signals are confident:
            # SoC delta ≥ 2 % (above 1 % quantization) and the trip
            # has accumulated power samples (energy_from_power > 0).
            calibration_factor_k=self._compute_calibration_k(
                active.energy_from_power_kwh,
                active.regen_kwh,
                soc_used,
            ),
            # v0.5.86 — confidence band + low-confidence flag.
            **dict(zip(
                (
                    "consumption_lower_kwh_100km",
                    "consumption_upper_kwh_100km",
                    "low_confidence",
                ),
                self._compute_consumption_band(
                    distance_km=distance,
                    energy_kwh=energy,
                    consumption=consumption,
                    energy_source=energy_source,
                    soc_used_pct=soc_used,
                ),
            )),
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
        # v0.5.77 — vehicle-native energy heal scheduled here too.
        self._schedule_vehicle_heal(trip_id)

        # v0.5.76 — WAC pool replay: trip cost reflects what the
        # energy actually cost (the battery's blended average at that
        # moment). Earlier trips may also heal if the pool shape
        # changed.
        if record.energy_kwh is not None and record.energy_kwh > 0:
            await self.storage.async_recompute_trip_costs_from_charges(
                self._current_energy_price(),
            )

        # v0.5.30 (issue #5) — when we just auto-stitched a new
        # one-stage journey for this home arrival, retro-absorb any
        # orphan trips (journey_id=NULL) since the last home arrival
        # so the journey actually represents the full casa→…→casa
        # chain instead of showing as a single-row 1-stage journey.
        if stitched_orphan_home and journey_id is not None:
            absorbed = await self.storage.async_absorb_orphans_into_journey(
                journey_id, self.home_zone, self._secondary_home_labels(),
            )
            if absorbed:
                _LOGGER.info(
                    "Auto-stitch: absorbed %d orphan trip(s) into journey #%s",
                    absorbed, journey_id,
                )

        # Persist GPS route samples accumulated during the trip.
        if active.gps_samples:
            await self.storage.async_insert_positions(trip_id, active.gps_samples)

        # v0.7.5 — schedule the optional elevation join. Fire-and-
        # forget so a slow / down provider never blocks the caller.
        # No-op when provider="none" or no route was captured.
        if (
            self._elevation_provider != "none"
            and active.gps_samples
            and len(active.gps_samples) >= 5
        ):
            self.hass.async_create_task(
                self._async_populate_elevation(trip_id, active.gps_samples),
            )

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
        evse_energy_kwh: float | None = None,
        peak_charge_power_kw: float | None = None,
        temperature_c: float | None = None,
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
                price_per_kwh = self._current_energy_price()
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

        # v0.5.90 — AC→DC efficiency from the EVSE-side integral.
        charging_eff_pct: float | None = None
        if evse_energy_kwh is not None and evse_energy_kwh > 0:
            charging_eff_pct = round(kwh / evse_energy_kwh * 100.0, 1)
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
            evse_energy_kwh=(
                round(evse_energy_kwh, 3)
                if evse_energy_kwh is not None and evse_energy_kwh > 0
                else None
            ),
            charging_efficiency_pct=charging_eff_pct,
            peak_charge_power_kw=(
                round(peak_charge_power_kw, 2)
                if peak_charge_power_kw is not None and peak_charge_power_kw > 0
                else None
            ),
            # v0.6.5 — capture exterior temperature at close from the
            # configured sensor; falls through to the caller-supplied
            # value when wired (manual log_charge service can pass it
            # in). Drives the SoH sample-gate.
            temperature_c=(
                round(temperature_c, 1)
                if temperature_c is not None
                else (
                    round(self._read_float(self._temp), 1)
                    if self._temp is not None
                    and self._read_float(self._temp) is not None
                    else None
                )
            ),
        )
        charge_id = await self.storage.async_insert_charge(record)
        record.charge_id = charge_id
        self.last_charge = record

        # v0.5.76 — a new charge changes the WAC pool's blended price;
        # rebuild trip costs so everything stays consistent.
        await self.storage.async_recompute_trip_costs_from_charges(
            self._current_energy_price(),
        )

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
            default_price=self._current_energy_price()
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
                default_price=self._current_energy_price()
            )
        except Exception:  # pragma: no cover — defensive
            pass
        # Also re-resolve the open journey: if the user changed
        # journey_id or destination, the resume may now be different.
        self.current_journey_id = await self.storage.async_resolve_open_journey_id(
            self.home_zone, self._secondary_home_labels()
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

    async def async_backfill_charge_evse_service(
        self,
        *,
        charge_id: int,
        evse_power_sensor: str | None = None,
        mask_by_charge_sensor: bool = True,
    ) -> ChargeRecord | None:
        """Backfill `evse_energy_kwh` on a historical charge from recorder
        history of the EVSE power sensor.

        Why: when the user adds `CONF_EVSE_POWER_SENSOR` to options
        AFTER charges have started (or upgrades to a version that
        introduced EVSE integration), the in-memory integral was zero
        when those charges closed, so the row's `evse_energy_kwh` ended
        up NULL even though the wallbox sensor was reporting fine.

        Walks the recorder for the EVSE power sensor between the
        charge's `started_at` and `ended_at`, integrates trapezoidally,
        and (when `mask_by_charge_sensor` is True) zeros out windows
        where `CONF_CHARGE_SENSOR` was not in a charging state — so the
        idle gaps between pulses don't accumulate the EVSE's tiny
        standby draw into the total.

        Auto-detects unit (W → /1000) per state-attribute. Patches the
        row via `async_patch_charge` which recomputes
        `charging_efficiency_pct = kwh / evse_energy_kwh × 100`.
        """
        existing = await self.storage.async_get_charge_by_id(charge_id)
        if existing is None:
            _LOGGER.warning(
                "backfill_charge_evse: charge_id=%s not found", charge_id,
            )
            return None
        if existing.started_at is None or existing.ended_at is None:
            _LOGGER.warning(
                "backfill_charge_evse: charge %s missing started_at/ended_at",
                charge_id,
            )
            return None
        sensor = evse_power_sensor or self._evse_power_sensor
        if not sensor:
            _LOGGER.warning(
                "backfill_charge_evse: no EVSE power sensor configured "
                "for entry %s and none provided in the service call",
                self.entry_id,
            )
            return None
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.history import (  # noqa: PLC0415
                state_changes_during_period,
            )
        except Exception:  # pragma: no cover — recorder always present
            _LOGGER.warning(
                "backfill_charge_evse: recorder integration unavailable",
            )
            return None
        recorder = get_instance(self.hass)
        ents: list[str] = [sensor]
        if mask_by_charge_sensor and self._charge_sensor:
            ents.append(self._charge_sensor)
        # state_changes_during_period requires a non-None entity_id, so
        # query each entity separately and merge.
        result: dict = {}
        try:
            for e in ents:
                sub = await recorder.async_add_executor_job(
                    state_changes_during_period,
                    self.hass,
                    existing.started_at,
                    existing.ended_at,
                    e,
                )
                if isinstance(sub, dict):
                    result.update(sub)
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.warning(
                "backfill_charge_evse: recorder query failed for "
                "charge %s: %s", charge_id, exc,
            )
            return None
        evse_states = result.get(sensor, []) if isinstance(result, dict) else []
        if not evse_states:
            _LOGGER.warning(
                "backfill_charge_evse: no recorder samples for %s in "
                "[%s, %s] — recorder retention may have purged them",
                sensor,
                existing.started_at.isoformat(),
                existing.ended_at.isoformat(),
            )
            return None
        charge_states = (
            result.get(self._charge_sensor, [])
            if (mask_by_charge_sensor and self._charge_sensor)
            else []
        )
        evse_kwh = self._integrate_evse_from_recorder(
            evse_states=evse_states,
            charge_states=charge_states,
            window_start=existing.started_at,
            window_end=existing.ended_at,
        )
        if evse_kwh is None or evse_kwh <= 0:
            _LOGGER.warning(
                "backfill_charge_evse: integrated energy was zero for "
                "charge %s (no power samples > 0 inside charging windows)",
                charge_id,
            )
            return None
        patched = await self.storage.async_patch_charge(
            charge_id, {"evse_energy_kwh": round(evse_kwh, 3)},
        )
        if patched is None:
            return None
        if self.last_charge is not None and self.last_charge.charge_id == charge_id:
            self.last_charge = patched
        self.hass.bus.async_fire(
            EVENT_CHARGE_LOGGED,
            {"entry_id": self.entry_id, **patched.to_dict()},
        )
        self._notify_listeners()
        self._notify_trip_log_listeners()
        _LOGGER.info(
            "backfill_charge_evse: charge %s patched — evse=%.3f kWh, "
            "eff=%s %%",
            charge_id, evse_kwh, patched.charging_efficiency_pct,
        )
        return patched

    @staticmethod
    def _integrate_evse_from_recorder(
        *,
        evse_states: list,
        charge_states: list,
        window_start: datetime,
        window_end: datetime,
    ) -> float | None:
        """Trapezoidal integral of EVSE power across a charge window,
        masked by charge_sensor on/off intervals when supplied.

        Returns the integrated energy in kWh, or None when no usable
        samples were found.
        """
        def _to_kw(state_obj) -> float | None:
            try:
                v = float(state_obj.state)
            except (TypeError, ValueError):
                return None
            unit = (state_obj.attributes or {}).get("unit_of_measurement") or ""
            if str(unit).strip().lower() in ("w", "watt", "watts"):
                v = v / 1000.0
            return max(0.0, v)

        # Build sorted (ts, kw) samples within the window.
        samples: list[tuple[datetime, float]] = []
        for s in evse_states:
            kw = _to_kw(s)
            if kw is None:
                continue
            ts = getattr(s, "last_updated", None) or getattr(
                s, "last_changed", None,
            )
            if ts is None:
                continue
            if ts < window_start or ts > window_end:
                continue
            samples.append((ts, kw))
        if len(samples) < 2:
            return None
        samples.sort(key=lambda x: x[0])

        # Build charging=on intervals (or [window_start, window_end] when
        # masking is disabled / no charge sensor states available).
        def _is_on(s) -> bool:
            return str(getattr(s, "state", "")).lower() in (
                "on", "true", "charging",
            )
        intervals: list[tuple[datetime, datetime]] = []
        if charge_states:
            cs_sorted = sorted(
                charge_states,
                key=lambda x: (
                    getattr(x, "last_updated", None)
                    or getattr(x, "last_changed", None)
                ),
            )
            # Determine state at window_start by looking at the first
            # sample: if it's already "on" at >= window_start, treat the
            # window_start as the opening edge.
            cur_open: datetime | None = (
                window_start if _is_on(cs_sorted[0]) else None
            )
            for s in cs_sorted:
                ts = getattr(s, "last_updated", None) or getattr(
                    s, "last_changed", None,
                )
                if ts is None:
                    continue
                if _is_on(s) and cur_open is None:
                    cur_open = ts
                elif (not _is_on(s)) and cur_open is not None:
                    intervals.append((cur_open, ts))
                    cur_open = None
            if cur_open is not None:
                intervals.append((cur_open, window_end))
        else:
            intervals.append((window_start, window_end))

        if not intervals:
            return None

        # Trapezoidal integration over the on-intervals only. For each
        # consecutive sample pair, only count the portion that intersects
        # an on-interval.
        max_dt_h = _MAX_POWER_TRAPEZOID_DT_H
        total_kwh = 0.0
        for (t0, kw0), (t1, kw1) in zip(samples, samples[1:]):
            if t1 <= t0:
                continue
            dt_full_s = (t1 - t0).total_seconds()
            if dt_full_s <= 0 or dt_full_s / 3600.0 > max_dt_h:
                continue
            # Intersect [t0,t1] with each on-interval and accumulate
            # area proportional to the intersected slice.
            for iv_a, iv_b in intervals:
                a = t0 if t0 > iv_a else iv_a
                b = t1 if t1 < iv_b else iv_b
                if b <= a:
                    continue
                slice_h = (b - a).total_seconds() / 3600.0
                # Trapezoid over the slice — use linear interpolation
                # for the kw at slice endpoints, then average.
                frac_a = (a - t0).total_seconds() / dt_full_s
                frac_b = (b - t0).total_seconds() / dt_full_s
                kwa = kw0 + (kw1 - kw0) * frac_a
                kwb = kw0 + (kw1 - kw0) * frac_b
                total_kwh += (kwa + kwb) / 2.0 * slice_h
        return total_kwh if total_kwh > 0 else None

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

    async def async_fix_speed_stats_service(self) -> int:
        """Maintenance backfill: null out avg_speed_kmh on any persisted
        trip where it exceeds max_speed_kmh — the same sanity check
        `_async_close_trip` has applied at close time since v0.8.3,
        applied retroactively to rows written before that fix.
        """
        n = await self.storage.async_fix_avg_speed_outliers()
        if n:
            self.last_trip = await self.storage.async_get_last()
            self._notify_listeners()
            self._notify_trip_log_listeners()
        return n

    async def async_recover_missing_trips_service(
        self, *, since: datetime, until: datetime | None = None,
    ) -> int:
        """Scan recorder history for trips not covered by any existing
        row, and INSERT synth records with confidence
        'reconstructed_recovery'. Existing rows are never modified.

        Algorithm (v0.5.99):
          1. If `vehicle_on` is configured, walk its history first.
             Each on→off pair is a candidate segment with precise
             timestamps — the recorder kept the edges even when the
             live capture missed them. This is the primary path: it
             handles sparse cloud-polled odometers (BYD ~8 min
             cadence) that the odometer-walker can't segment
             correctly.
          2. Fall back to the legacy odometer-growth walker for the
             entries where vehicle_on isn't wired, or when the
             vehicle_on history has no on→off pair in the window.
          3. For each segment, query storage: skip if any trip already
             overlaps [seg_start, seg_end].
          4. For surviving segments, pull battery + location at the
             boundaries and persist a TripRecord.

        Returns the number of trips inserted.
        """
        if self._odometer is None:
            _LOGGER.warning("recover_missing_trips: no odometer configured")
            return 0
        if until is None:
            until = dt_util.now()
        if since >= until:
            return 0
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.history import (  # noqa: PLC0415
                state_changes_during_period,
            )
        except Exception:
            return 0
        recorder = get_instance(self.hass)
        # v0.5.99 — vehicle_on-driven segmentation is more reliable
        # than the odometer-plateau walker on sparse cloud-polled
        # sources. Try it first.
        segments: list[tuple[datetime, datetime, float, float]] = []
        if self._vehicle_on is not None:
            segments = await self._recover_segments_via_vehicle_on(
                since=since, until=until, recorder=recorder,
            )
            if segments:
                _LOGGER.info(
                    "recover_missing_trips: vehicle_on path produced %d "
                    "segment(s)",
                    len(segments),
                )
                return await self._async_insert_recovered_segments(
                    segments, recorder=recorder,
                )
        try:
            result = await recorder.async_add_executor_job(
                state_changes_during_period,
                self.hass, since, until, self._odometer,
            )
        except Exception as exc:
            _LOGGER.warning("recover_missing_trips: recorder query failed: %s", exc)
            return 0
        states = result.get(self._odometer, []) if isinstance(result, dict) else []
        # Coerce to (ts, odo) pairs, drop unparseable.
        pairs: list[tuple[datetime, float]] = []
        for s in states:
            try:
                pairs.append((s.last_updated, float(s.state)))
            except (TypeError, ValueError):
                continue
        if len(pairs) < 2:
            return 0
        pairs.sort(key=lambda x: x[0])

        # Walk segments.
        segments: list[tuple[datetime, datetime, float, float]] = []
        seg_start_ts: datetime | None = None
        seg_start_odo: float | None = None
        seg_last_growth_ts: datetime | None = None
        seg_last_odo: float | None = None
        for ts, odo in pairs:
            if seg_start_ts is None:
                # First sample; arm baseline.
                seg_start_ts = ts
                seg_start_odo = odo
                seg_last_growth_ts = ts
                seg_last_odo = odo
                continue
            if seg_last_odo is None:
                continue
            if odo > seg_last_odo + 0.05:  # growth (more than rounding)
                seg_last_growth_ts = ts
                seg_last_odo = odo
            elif (
                seg_last_growth_ts is not None
                and (ts - seg_last_growth_ts).total_seconds()
                    > _SYNTH_COALESCE_WINDOW_S
            ):
                # Long plateau — finalise segment if it actually moved.
                if seg_last_odo > seg_start_odo + self._min_distance:
                    segments.append(
                        (seg_start_ts, seg_last_growth_ts,
                         seg_start_odo, seg_last_odo)
                    )
                seg_start_ts = ts
                seg_start_odo = odo
                seg_last_growth_ts = ts
                seg_last_odo = odo
        # Tail segment.
        if (
            seg_start_ts is not None and seg_last_growth_ts is not None
            and seg_last_odo is not None and seg_start_odo is not None
            and seg_last_odo > seg_start_odo + self._min_distance
        ):
            segments.append(
                (seg_start_ts, seg_last_growth_ts,
                 seg_start_odo, seg_last_odo)
            )

        _LOGGER.info(
            "recover_missing_trips: %d odometer-walker segment(s) in [%s, %s]",
            len(segments), since.isoformat(), until.isoformat(),
        )
        return await self._async_insert_recovered_segments(
            segments, recorder=recorder,
        )

    async def _recover_segments_via_vehicle_on(
        self,
        *,
        since: datetime,
        until: datetime,
        recorder,
    ) -> list[tuple[datetime, datetime, float, float]]:
        """v0.5.99 — derive recovery segments from vehicle_on edges.

        Each on→off pair becomes one segment. Odometer at the
        endpoints is pulled from the recorder (last reading ≤ ts) so
        sparse cloud-polled odometers (BYD ~8 min cadence) don't lose
        the trip even when no odometer sample falls inside the
        on-window. A segment is kept only when end_odo − start_odo
        ≥ `_min_distance` (= the discard floor used everywhere else).

        Returns [] when vehicle_on history is empty or no usable
        segment passes the distance gate — the caller then falls
        back to the legacy odometer-walker.
        """
        try:
            from homeassistant.components.recorder.history import (  # noqa: PLC0415
                state_changes_during_period,
            )
        except Exception:  # pragma: no cover — recorder optional
            return []
        if not self._vehicle_on or not self._odometer:
            return []
        try:
            r = await recorder.async_add_executor_job(
                state_changes_during_period,
                self.hass, since, until, self._vehicle_on,
            )
        except Exception as exc:  # pragma: no cover — defensive
            _LOGGER.debug(
                "recover_missing_trips: vehicle_on query failed: %s", exc,
            )
            return []
        sts = r.get(self._vehicle_on, []) if isinstance(r, dict) else []
        if not sts:
            return []
        # Build the on/off timeline. The recorder returns transitions
        # only; the state already-active at window-open is captured by
        # the first sample (HA includes a synthetic initial change).
        toggles = sorted(
            ((x.last_updated, str(getattr(x, "state", "")).strip().lower())
             for x in sts),
            key=lambda y: y[0],
        )
        pairs: list[tuple[datetime, datetime]] = []
        on_ts: datetime | None = None
        for ts, st in toggles:
            if st == "on":
                if on_ts is None:
                    on_ts = ts
            else:
                if on_ts is not None:
                    pairs.append((on_ts, ts))
                    on_ts = None
        # Trailing on with no off in window — clamp to `until` so the
        # segment isn't lost. The integration's idle/stuck watchdog
        # would have closed it eventually; for recovery, until is fine.
        if on_ts is not None:
            pairs.append((on_ts, until))
        if not pairs:
            return []

        # Pre-fetch the entire odometer timeline for [since-30min,
        # until+5min] in ONE query. Per-segment lookups then bisect
        # the cached list — this is what made the per-segment 30-min
        # window approach miss samples on sparse cloud-polled data,
        # where the start anchor for a 19:06 trip lives at 18:20 and
        # the 30-min lookback didn't reach it.
        try:
            rr = await recorder.async_add_executor_job(
                state_changes_during_period,
                self.hass,
                since - timedelta(minutes=30),
                until + timedelta(minutes=5),
                self._odometer,
            )
        except Exception:  # pragma: no cover — defensive
            return []
        odo_states = rr.get(self._odometer, []) if isinstance(rr, dict) else []
        odo_pairs: list[tuple[datetime, float]] = []
        for x in odo_states:
            try:
                odo_pairs.append((x.last_updated, float(x.state)))
            except (TypeError, ValueError):
                continue
        odo_pairs.sort(key=lambda y: y[0])

        def _odo_le(when: datetime) -> float | None:
            cand = [v for t, v in odo_pairs if t <= when]
            return cand[-1] if cand else None

        def _odo_ge(when: datetime) -> float | None:
            cand = [v for t, v in odo_pairs if t >= when]
            return cand[0] if cand else None

        segments: list[tuple[datetime, datetime, float, float]] = []
        for s_ts, e_ts in pairs:
            # Start anchor = last sample ≤ s_ts (state just before
            # ignition). End anchor = last sample ≤ e_ts; fall back
            # to first sample ≥ e_ts if no sample landed before the
            # off-edge (common when the BYD odo cadence is ~8 min and
            # the trip was very short).
            s_odo = _odo_le(s_ts)
            e_odo = _odo_le(e_ts) or _odo_ge(e_ts)
            if s_odo is None or e_odo is None:
                continue
            if e_odo - s_odo < self._min_distance:
                # Sub-threshold drive — same gate the live close
                # path uses. Skip rather than fabricate a phantom row.
                continue
            segments.append((s_ts, e_ts, s_odo, e_odo))
        return segments

    async def _async_insert_recovered_segments(
        self,
        segments: list[tuple[datetime, datetime, float, float]],
        *,
        recorder,
    ) -> int:
        """Per-segment persistence — shared by the v0.5.99 vehicle_on
        path and the legacy odometer-walker. Skips segments overlapping
        an existing trip; pulls SoC + location at the boundaries from
        the recorder; inserts a TripRecord with confidence
        'reconstructed_recovery'. Returns the count inserted.
        """
        try:
            from homeassistant.components.recorder.history import (  # noqa: PLC0415
                state_changes_during_period,
            )
        except Exception:  # pragma: no cover
            return 0
        inserted = 0
        for s_ts, e_ts, s_odo, e_odo in segments:
            if await self.storage.async_trip_overlaps(s_ts, e_ts):
                continue
            soc_start = None
            soc_end = None

            async def _scalar(eid: str, when: datetime) -> float | None:
                try:
                    r = await recorder.async_add_executor_job(
                        state_changes_during_period,
                        self.hass, when - timedelta(minutes=30),
                        when + timedelta(minutes=5), eid,
                    )
                    sts = r.get(eid, []) if isinstance(r, dict) else []
                    seen = sorted(
                        ((x.last_updated, x.state) for x in sts),
                        key=lambda y: y[0],
                    )
                    cand = [v for t, v in seen if t <= when] or [v for _, v in seen]
                    if not cand:
                        return None
                    return float(cand[-1])
                except Exception:
                    return None

            if self._battery:
                soc_start = await _scalar(self._battery, s_ts)
                soc_end = await _scalar(self._battery, e_ts)
            start_lat = start_lon = end_lat = end_lon = None
            origin = destination = None
            if self._location:
                try:
                    r = await recorder.async_add_executor_job(
                        state_changes_during_period,
                        self.hass, s_ts - timedelta(minutes=30),
                        e_ts + timedelta(minutes=5), self._location,
                    )
                    sts = r.get(self._location, []) if isinstance(r, dict) else []
                    sorted_states = sorted(sts, key=lambda x: x.last_updated)
                    pre = [x for x in sorted_states if x.last_updated <= s_ts]
                    post = [x for x in sorted_states if x.last_updated <= e_ts]
                    if pre:
                        origin = pre[-1].state
                        try:
                            start_lat = float(pre[-1].attributes.get("latitude"))
                            start_lon = float(pre[-1].attributes.get("longitude"))
                        except (TypeError, ValueError):
                            start_lat = start_lon = None
                    if post:
                        destination = post[-1].state
                        try:
                            end_lat = float(post[-1].attributes.get("latitude"))
                            end_lon = float(post[-1].attributes.get("longitude"))
                        except (TypeError, ValueError):
                            end_lat = end_lon = None
                except Exception:
                    pass
            distance = round(e_odo - s_odo, 1)
            duration_min = max(0.0, (e_ts - s_ts).total_seconds() / 60.0)
            soc_used = (
                round(soc_start - soc_end, 1)
                if soc_start is not None and soc_end is not None
                and soc_start > soc_end else None
            )
            energy = (
                round((soc_used / 100.0) * self.battery_capacity, 2)
                if soc_used is not None else None
            )
            cost = (
                round(energy * self._trip_cost_price_per_kwh(), 2)
                if energy is not None and energy > 0 else None
            )
            record = TripRecord(
                started_at=s_ts, ended_at=e_ts,
                duration_min=duration_min, distance_km=distance,
                odometer_start=s_odo, odometer_end=e_odo,
                soc_start=soc_start, soc_end=soc_end, soc_used_pct=soc_used,
                energy_kwh=energy,
                consumption_kwh_100km=(
                    round(energy / distance * 100, 1)
                    if energy and distance > 0 else None
                ),
                origin=origin, destination=destination,
                start_lat=start_lat, start_lon=start_lon,
                end_lat=end_lat, end_lon=end_lon,
                cost=cost,
                currency=self._currency if cost else None,
                confidence="reconstructed_recovery",
                driver=await self._async_driver_during(s_ts, e_ts),
            )
            trip_id = await self.storage.async_insert(record)
            inserted += 1
            _LOGGER.info(
                "recover_missing_trips: inserted #%s %s→%s (%.1fkm)",
                trip_id, s_ts.isoformat(), e_ts.isoformat(), distance,
            )
        if inserted:
            await self.storage.async_recompute_trip_costs_from_charges(
                self._current_energy_price(),
            )
            self.last_trip = await self.storage.async_get_last()
            self._notify_listeners()
            self._notify_trip_log_listeners()
        return inserted

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
        driver: str | None = None,
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
            (soc_used / 100.0) * self.battery_capacity
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

        # v0.5.36 — apply the same journey state machine as the live
        # close path. Without this, manually-logged trips ended at
        # home but with current_journey_id=None left the open journey
        # ungrouped and let the NEXT trip's auto-stitch include the
        # whole orphan range — corrupting today's journey with
        # yesterday's leg.
        is_at_home_end = self._is_at_any_home(destination)
        started_from_home = self._is_at_any_home(origin)
        journey_id: int | None
        stitched_orphan_home = False
        if self.current_journey_id is not None:
            journey_id = self.current_journey_id
        elif started_from_home:
            journey_id = await self.storage.async_next_journey_id()
        elif is_at_home_end:
            journey_id = await self.storage.async_next_journey_id()
            stitched_orphan_home = True
        else:
            journey_id = None

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
            journey_id=journey_id,
            confidence="live",  # manual entries are intentional, treat as live
            driver=driver,
        )

        trip_id = await self.storage.async_insert(record)
        record.trip_id = trip_id
        # v0.5.77 — vehicle-native energy heal scheduled for the
        # manually-logged trip too (user might invoke log_manual_trip
        # immediately after the drive while the sensor is fresh).
        self._schedule_vehicle_heal(trip_id)

        # v0.5.76 — WAC pool replay so manual trips share the same
        # accounting as live / synth ones.
        if record.energy_kwh is not None and record.energy_kwh > 0:
            await self.storage.async_recompute_trip_costs_from_charges(
                self._current_energy_price(),
            )

        if stitched_orphan_home and journey_id is not None:
            await self.storage.async_absorb_orphans_into_journey(
                journey_id, self.home_zone, self._secondary_home_labels(),
            )
        # Update journey state mirror.
        if is_at_home_end and journey_id is not None:
            self.last_completed_journey_id = journey_id
            self.current_journey_id = None
        else:
            self.current_journey_id = journey_id

        self._adopt_last_trip(record)
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

    def _adopt_last_trip(self, record: TripRecord) -> None:
        """Set last_trip only when `record` is chronologically newest.

        Manual backfills and synthetic inserts can be OLDER than the
        genuine most-recent trip; blindly adopting them corrupts the
        journey grouping, the snap-on-short-park SoC anchor and orphan
        detection, which all key off last_trip.ended_at/odometer_end.
        """
        prev = self.last_trip
        if prev is None:
            self.last_trip = record
            return
        try:
            is_newer = record.ended_at >= prev.ended_at
        except TypeError:
            # naive vs aware mix (manual service input) — compare wall
            # clocks; good enough for a guard.
            is_newer = (
                record.ended_at.replace(tzinfo=None)
                >= prev.ended_at.replace(tzinfo=None)
            )
        if is_newer:
            self.last_trip = record
        else:
            _LOGGER.debug(
                "Not adopting trip #%s as last_trip: ended_at %s is older "
                "than current last_trip %s",
                record.trip_id, record.ended_at, prev.ended_at,
            )

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

    #: Multiplier to kPa from each `unit_of_measurement` HA's pressure
    #: device_class may report (tire pressure sensors are commonly bar
    #: or psi; ABRP's tire_pressure_* fields want kPa).
    _PRESSURE_TO_KPA: dict[str, float] = {
        "bar": 100.0,
        "cbar": 1.0,
        "mbar": 0.1,
        "hpa": 0.1,
        "kpa": 1.0,
        "pa": 0.001,
        "psi": 6.894757,
    }

    def _read_pressure_kpa(self, entity_id: str | None) -> float | None:
        """Read a pressure sensor and convert its value to kPa.

        Unit taken from the entity's own `unit_of_measurement` attribute
        so it works regardless of which unit the source integration (or
        HA's unit-system conversion) reports in. Unrecognised/missing
        units are assumed to already be kPa rather than dropped, since a
        wrong-but-present tire pressure is more useful to ABRP than none.
        """
        if not entity_id:
            return None
        value = self._read_float(entity_id)
        if value is None:
            return None
        state = self.hass.states.get(entity_id)
        unit = (
            state.attributes.get("unit_of_measurement") if state else None
        )
        factor = self._PRESSURE_TO_KPA.get(str(unit).strip().lower(), 1.0)
        return value * factor

    def _read_float_if_fresh(
        self, entity_id: str | None, now: datetime, max_age_s: float,
    ) -> float | None:
        """Like `_read_float`, but None if the state is older than
        `max_age_s` relative to `now`.

        `hass.states.get()` keeps returning the last known value
        indefinitely on a cloud-polled entity that has gone quiet —
        `_read_float` can't tell "fresh" from "leftover from before the
        car went quiet". Callers that use this value to anchor a new
        trip's odometer_start need to know the difference (see
        `_ODOMETER_STALE_MAX_AGE_S`).
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _INVALID_STATES:
            return None
        if (now - state.last_updated).total_seconds() > max_age_s:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _read_str(self, entity_id: str | None) -> str | None:
        return self._read_state(entity_id)

    async def _async_startup_vehicle_heal_sweep(self) -> None:
        """v0.5.86 — re-run the vehicle-native energy heal on recent
        trips that escaped the live `_async_heal_from_vehicle` pass.

        The live heal is `async_call_later`-scheduled 240 s after
        insert. If HA restarts inside that window, the task is gone
        and the trip keeps whatever SoC-derived energy it landed
        with at close. On startup, sweep the last 24 h of trips
        where `energy_source != "vehicle"` and re-attempt heal — the
        BYD `last_trip_energy` sensor may have refreshed by now and
        the existing guards (`last_changed >= trip.ended_at` +
        distance cross-check) ensure we don't overwrite with a
        stale or mismatched value.
        """
        try:
            recent = await self.storage.async_trips_needing_vehicle_heal(
                hours=24,
            )
        except Exception as err:  # pragma: no cover — defensive
            _LOGGER.debug("Vehicle-heal sweep query failed: %s", err)
            return
        healed = 0
        for trip_id in recent:
            try:
                before = await self.storage.async_get_trip_by_id(trip_id)
                await self._async_heal_from_vehicle(trip_id)
                after = await self.storage.async_get_trip_by_id(trip_id)
                if (
                    after is not None
                    and after.energy_source == "vehicle"
                    and (
                        before is None
                        or before.energy_source != "vehicle"
                    )
                ):
                    healed += 1
            except Exception as err:  # pragma: no cover — defensive
                _LOGGER.debug(
                    "Vehicle-heal sweep skipped trip %s: %s", trip_id, err,
                )
        if healed:
            _LOGGER.info(
                "Startup vehicle-heal sweep: %d/%d trip(s) recovered with "
                "vehicle-native energy.", healed, len(recent),
            )

    def _schedule_vehicle_heal(self, trip_id: int) -> None:
        """v0.5.77 — after `_VEHICLE_TRIP_HEAL_DELAY_S` re-read the vehicle's
        last_trip_* sensors. If they refer to this trip (timestamp + distance
        cross-check) and disagree with the logger's energy estimate, override.

        Cloud integrations update `last_trip_energy` 1-3 min after the
        physical trip ends. We schedule the heal once per insert; if the
        sensor still hasn't refreshed when the callback fires, we leave
        the row alone (next trip's heal naturally re-checks the
        previous row by reading the row before triggering the WAC replay).
        """
        if not self._last_trip_energy_sensor:
            return

        @callback
        def _fire(_at: datetime) -> None:
            self.hass.async_create_task(self._async_heal_from_vehicle(trip_id))

        async_call_later(self.hass, _VEHICLE_TRIP_HEAL_DELAY_S, _fire)

    async def _async_heal_from_vehicle(self, trip_id: int) -> None:
        """v0.5.77 — override `energy_kwh` from the vehicle-native sensor.

        Guards:
          - sensor's `last_changed` must be ≥ trip.ended_at (the sensor
            refers to a trip that closed after ours, not a stale value
            from the PREVIOUS trip)
          - if `_last_trip_distance_sensor` is configured, its value must
            match the logger's `distance_km` within tolerance (avoids
            healing from a sensor that refers to a different trip the
            logger missed)
          - sensor value must be a positive float
        """
        trip = await self.storage.async_get_trip_by_id(trip_id)
        if trip is None or trip.distance_km is None or trip.distance_km <= 0:
            return
        if trip.ended_at is None:
            return
        sensor_state = self.hass.states.get(self._last_trip_energy_sensor)
        if sensor_state is None or sensor_state.state in _INVALID_STATES:
            return
        if sensor_state.last_changed < trip.ended_at:
            _LOGGER.debug(
                "Vehicle heal skipped for trip %s: sensor stale "
                "(last_changed=%s, trip ended_at=%s)",
                trip_id, sensor_state.last_changed, trip.ended_at,
            )
            return
        try:
            vehicle_kwh = float(sensor_state.state)
        except (TypeError, ValueError):
            return
        if vehicle_kwh <= 0:
            return
        # Distance cross-check (optional).
        if self._last_trip_distance_sensor:
            dist_state = self.hass.states.get(self._last_trip_distance_sensor)
            if dist_state and dist_state.state not in _INVALID_STATES:
                try:
                    vehicle_km = float(dist_state.state)
                except (TypeError, ValueError):
                    vehicle_km = None
                if vehicle_km is not None and vehicle_km > 0:
                    diff_abs = abs(vehicle_km - trip.distance_km)
                    diff_rel = diff_abs / trip.distance_km
                    if (
                        diff_abs > _VEHICLE_TRIP_DIST_TOL_KM
                        and diff_rel > _VEHICLE_TRIP_DIST_TOL_PCT
                    ):
                        _LOGGER.info(
                            "Vehicle heal skipped for trip %s: distance "
                            "mismatch (vehicle=%.1f km, logger=%.1f km)",
                            trip_id, vehicle_km, trip.distance_km,
                        )
                        return
        # All guards passed — override.
        old_energy = trip.energy_kwh
        new_consumption = vehicle_kwh / trip.distance_km * 100.0
        # v0.5.93 — recompute the confidence band with the new source.
        # The old band was sized for SoC quantization noise; for a
        # vehicle-native value the relative uncertainty is ~3 % so the
        # band collapses dramatically. Without this the dashboard
        # shows "27.6 kWh/100km, band [12-21]" — the band excludes the
        # value because it was calculated under different assumptions.
        lower, upper, low_conf = self._compute_consumption_band(
            distance_km=trip.distance_km,
            energy_kwh=vehicle_kwh,
            consumption=new_consumption,
            energy_source="vehicle",
            soc_used_pct=trip.soc_used_pct,
        )
        await self.storage.async_update_trip(
            trip_id,
            {
                "energy_kwh": round(vehicle_kwh, 3),
                "consumption_kwh_100km": round(new_consumption, 2),
                "energy_source": "vehicle",
                "consumption_lower_kwh_100km": lower,
                "consumption_upper_kwh_100km": upper,
                "low_confidence": low_conf,
            },
        )
        _LOGGER.warning(
            "Vehicle heal for trip %s: energy %.2f → %.2f kWh (source: "
            "%s). Consumption now %.1f kWh/100km.",
            trip_id, old_energy or 0.0, vehicle_kwh,
            self._last_trip_energy_sensor, new_consumption,
        )
        # Re-cost via the WAC pool so the trip's cost + basis reflect
        # the new energy. Idempotent.
        await self.storage.async_recompute_trip_costs_from_charges(
            self._current_energy_price(),
        )
        self._notify_trip_log_listeners()

    def _is_own_entity(self, entity_id: str) -> bool:
        """v0.8.11 — True if `entity_id` was registered by this
        integration itself (`platform == DOMAIN`). When the vehicle
        integration's slug collides with the logger's own device slug
        (e.g. both named "relampago"), the prefix-walk in
        `_auto_detect_vehicle_sensor`/`_auto_detect_temp_sensor` can
        otherwise land on `sensor.<prefix>_last_trip_energy` — the
        logger's own output sensor — and adopt it as its own vehicle
        source, healing trips from data it just wrote itself.
        """
        reg = er.async_get(self.hass)
        entry = reg.async_get(entity_id)
        return entry is not None and entry.platform == DOMAIN

    def _auto_detect_vehicle_sensor(
        self, suffixes: tuple[str, ...], label: str
    ) -> str | None:
        """v0.5.77 — share the prefix-walk used for the temp auto-detect.
        Returns the first `sensor.<prefix><suffix>` that exists in the
        state machine, or None.
        """
        if not self._odometer or not self._odometer.startswith("sensor."):
            return None
        prefix = self._odometer[len("sensor."):].rsplit("_", 1)[0]
        for suffix in suffixes:
            candidate = f"sensor.{prefix}{suffix}"
            if self.hass.states.get(candidate) is not None:
                if self._is_own_entity(candidate):
                    continue
                _LOGGER.warning(
                    "Auto-detected %s sensor: %s.",
                    label, candidate,
                )
                return candidate
        return None

    def _auto_detect_temp_sensor(self) -> str | None:
        """v0.5.69 — look for `sensor.<prefix>_exterior_temperature`
        (or common synonyms) when the user hasn't configured CONF_TEMP.

        Prefix is derived from `CONF_ODOMETER`. Examples:
          `sensor.my_car_odometer`     → prefix `my_car`
          `sensor.byd_sealion_7_odometer` → prefix `byd_sealion_7`
          `sensor.tesla_model3_odometer`  → prefix `tesla_model3`
        Tries `_exterior_temperature`, `_outside_temperature`,
        `_ambient_temperature`. Returns the first entity that exists
        in the HA state machine. The runtime override is in-memory only
        — the config entry isn't mutated, so the field stays empty in
        the UI and the user can still override it later via Options.
        """
        if not self._odometer or not self._odometer.startswith("sensor."):
            return None
        # Strip the platform prefix + the `_odometer` suffix to get the
        # vehicle-specific stem (works for any integration that follows
        # the `sensor.<vehicle>_<metric>` HA convention).
        prefix = self._odometer[len("sensor."):].rsplit("_", 1)[0]
        for suffix in (
            "_exterior_temperature",
            "_outside_temperature",
            "_ambient_temperature",
        ):
            candidate = f"sensor.{prefix}{suffix}"
            if self.hass.states.get(candidate) is not None:
                if self._is_own_entity(candidate):
                    continue
                # WARNING level so it surfaces in `system_log/list` and
                # the HA UI's Logs panel without needing custom logger
                # config. Cosmetic but the user wants to KNOW we wired
                # this for them.
                _LOGGER.warning(
                    "Auto-detected exterior temp sensor: %s. "
                    "(CONF_TEMP was empty — set it in Configure to override.)",
                    candidate,
                )
                return candidate
        return None

    def _read_driver(self) -> str | None:
        """Current driver name from the configured driver sensor.

        Returns None when no sensor is wired, the state is unavailable,
        or the state is one of the 'nobody connected' markers.
        """
        raw = self._read_state(self._driver_sensor)
        if raw is None:
            return None
        cleaned = raw.strip()
        if not cleaned or cleaned.casefold() in DRIVER_NONE_STATES:
            return None
        return cleaned

    def _compute_consumption_band(
        self,
        *,
        distance_km: float | None,
        energy_kwh: float | None,
        consumption: float | None,
        energy_source: str | None,
        soc_used_pct: float | None,
    ) -> tuple[float | None, float | None, bool]:
        """v0.5.86 — 95% confidence band on `consumption_kwh_100km`.

        The headline `consumption` is a point estimate. This function
        derives lower/upper bounds and a `low_confidence` flag that
        capture the noise of the source actually used:

        - `soc` source: SoC is reported to 1% by most car APIs, so the
          true delta is `recorded ± 0.5%`. The band width is
          `0.5 × kwh_per_step / distance × 100`.
        - `power_integration`: 15% relative error band (sampling gaps).
        - `vehicle`: 3% relative error (vendor rounding).
        - `estimated` / `vehicle_eff`: same as power.

        Returns (lower, upper, low_confidence). Lower/upper are None
        when consumption is None. `low_confidence` is True when:
          - distance < 2 km on SoC-derived energy, OR
          - relative band exceeds 40 % of consumption, OR
          - energy_source == 'estimated'

        The dashboard reads these to decide whether to show the trip
        with full opacity or grey it out; aggregates can filter on
        the flag to keep rolling baselines clean.
        """
        if consumption is None or distance_km is None or distance_km <= 0:
            return None, None, True
        # Per-step quantization. Use the calibrated effective capacity
        # when available — the K-rolling-median refines `nominal` from
        # real charges, and that's the right unit for "how many kWh
        # does one SoC step actually carry".
        kwh_per_step = float(self.battery_capacity) / 100.0  # 1 % = nominal/100
        rel_sigma: float
        if energy_source == "vehicle":
            rel_sigma = 0.03  # 3 % vendor rounding
        elif energy_source == "soc":
            # σ_consumption = 0.5 × kwh_per_step / distance × 100
            sigma_abs = 0.5 * kwh_per_step / distance_km * 100.0
            rel_sigma = sigma_abs / consumption if consumption > 0 else 1.0
        elif energy_source == "power_integration":
            rel_sigma = 0.15
        else:
            # estimated, vehicle_eff, None
            rel_sigma = 0.25
        half_band = 1.96 * rel_sigma * consumption
        lower = max(0.0, consumption - half_band)
        upper = consumption + half_band
        band_ratio = (upper - lower) / consumption if consumption > 0 else 1.0
        low_conf = (
            (distance_km < 2.0 and energy_source in ("soc", None))
            or band_ratio > 0.40
            or energy_source == "estimated"
        )
        return round(lower, 2), round(upper, 2), bool(low_conf)

    def _compute_calibration_k(
        self,
        energy_from_power_kwh: float | None,
        regen_kwh: float | None,
        soc_used_pct: float | None,
    ) -> float | None:
        """v0.5.84 — battery-capacity calibration factor per trip.

        K = net_power_kwh / soc_delta_kwh_nominal

        Where:
          - net_power_kwh  = ∫|P|·dt − 2·regen (real energy drawn from
            battery, measured at the motor side via power integration)
          - soc_delta_kwh_nominal = soc_used_pct / 100 × nominal_capacity
            (theoretical energy assuming nominal pack capacity)

        K ≈ 1.0 → capacity matches nominal. K consistently < 1.0
        across many trips → real degradation OR systematic power-
        integration undercount. Median across a rolling window
        smooths individual-trip noise; that aggregate is the actual
        SoH proxy.

        Returns None when either side is unreliable:
          - SoC delta < 2 % (1 % quantization dominates)
          - energy_from_power ≤ 0 (no power samples accumulated)
          - nominal capacity not set
        """
        if soc_used_pct is None or soc_used_pct < 2.0:
            return None
        if energy_from_power_kwh is None or energy_from_power_kwh <= 0:
            return None
        nominal = self.battery_capacity
        if not nominal or nominal <= 0:
            return None
        net_power = energy_from_power_kwh - 2.0 * (regen_kwh or 0.0)
        if net_power <= 0:
            # Regen overcounted vs discharge — sampling artefact, drop
            # this trip from the calibration pool rather than letting a
            # negative K poison the rolling median.
            return None
        soc_delta_kwh = soc_used_pct / 100.0 * float(nominal)
        if soc_delta_kwh <= 0:
            return None
        return round(net_power / soc_delta_kwh, 3)

    def _resolve_dominant_driver(self, active: "TripInProgress") -> str | None:
        """v0.5.82 — pick the driver who held the sensor the LONGEST
        during the trip, not the brittle 'first non-empty wins' of
        v0.5.43. Fixes the BT-race-at-open case: when a passenger's
        phone connects first and the actual driver's connection
        arrives ~30 s later, the dominant value over the full trip
        window is the driver — not the brief 30 s passenger sample.

        Falls back to the live-tick "first valid" (`active.driver`)
        when no samples were accumulated, then to a final-read of the
        sensor. None when no sensor is wired.
        """
        # Close out the in-flight sample window: add the last
        # observation's elapsed time to its bucket so the final tally
        # covers the full trip duration.
        if (
            active._last_driver_sample_ts is not None
            and active._last_driver_sample_value is not None
        ):
            now = dt_util.now()
            dt_s = (now - active._last_driver_sample_ts).total_seconds()
            if dt_s > 0:
                active.driver_samples[active._last_driver_sample_value] = (
                    active.driver_samples.get(
                        active._last_driver_sample_value, 0.0,
                    ) + dt_s
                )
        # Pick the value with the most accumulated time. `None` is
        # already filtered by `_read_driver` so the dict only carries
        # real driver names.
        if active.driver_samples:
            dominant = max(
                active.driver_samples,
                key=lambda k: active.driver_samples[k],
            )
            return dominant
        # No samples: fall through to legacy behaviour.
        if active.driver is not None:
            return active.driver
        return self._read_driver()

    async def _async_plug_stayed_connected_since(self, since: datetime) -> bool:
        """True when the plug sensor never reported 'off' since `since`.

        v0.5.45 — proves session continuity for the cable-still-plugged
        charge merge. Conservative: when the recorder can't answer (not
        loaded, query error) we return False so the charge is inserted
        as its own row instead of merged into a potentially unrelated
        one. Brief unavailable/unknown blips (cloud reloads) don't count
        as disconnects — only an explicit 'off' does.
        """
        if not self._plug_sensor:
            return False
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.history import (  # noqa: PLC0415
                state_changes_during_period,
            )
            r = await get_instance(self.hass).async_add_executor_job(
                state_changes_during_period,
                self.hass,
                since,
                dt_util.now(),
                self._plug_sensor,
            )
            sts = r.get(self._plug_sensor, []) if isinstance(r, dict) else []
        except Exception:  # pragma: no cover — recorder optional/best-effort
            return False
        return not any(x.state == STATE_OFF for x in sts)

    async def _async_driver_during(
        self,
        start: datetime,
        end: datetime,
        *,
        pre_window_min: float = _DRIVER_PRE_WINDOW_MIN,
        post_window_min: float = _DRIVER_POST_WINDOW_MIN,
    ) -> str | None:
        """Resolve who drove during [start, end] from recorder history.

        v0.5.44 — used by the synthetic / orphan / recovery paths, which
        reconstruct trips after the fact: the live capture in _open_trip
        and the live tick never ran for them. Best-effort: any recorder
        hiccup returns None (same posture as the other recovery lookups).

        v0.5.97 — pre/post window made configurable + the post-window
        defaults to a few minutes after the trip end. Real-world driver
        sensors (Android Auto / BT connections) can drop a moment before
        ignition or fire a moment after the off-edge; the picker
        weights overlap with [start, end] so extending the recorder
        query window doesn't bias the result, it just gives the picker
        more samples to weigh.
        """
        if not self._driver_sensor:
            return None
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.history import (  # noqa: PLC0415
                state_changes_during_period,
            )
            r = await get_instance(self.hass).async_add_executor_job(
                state_changes_during_period,
                self.hass,
                start - timedelta(minutes=pre_window_min),
                end + timedelta(minutes=post_window_min),
                self._driver_sensor,
            )
            sts = r.get(self._driver_sensor, []) if isinstance(r, dict) else []
        except Exception:  # pragma: no cover — recorder optional/best-effort
            return None
        timeline = sorted(
            ((x.last_updated, x.state) for x in sts), key=lambda y: y[0]
        )
        # v0.5.97 — also widen the picker window so a sensor that only
        # toggled BEFORE the trip (BT pre-pair flicker that drops before
        # ignition) still counts. Without this, the in-window overlap
        # would be 0 and the picker returns None even though we know
        # who was about to drive.
        pick_start = start - timedelta(minutes=pre_window_min)
        pick_end = end + timedelta(minutes=post_window_min)
        return _pick_driver_for_window(timeline, pick_start, pick_end)

    async def _async_resolve_trip_driver(
        self,
        active: "TripInProgress",
        start: datetime,
        end: datetime,
    ) -> str | None:
        """v0.5.97 — pick the driver for an in-memory trip with a
        recorder fallback.

        Order:
          1. In-memory dominant from live samples (the v0.5.82 logic).
          2. Recorder query over [start − pre, end + post] using the
             same time-overlap picker — covers the trip-191 pattern
             where AA paired briefly before ignition and dropped
             before the trip opened, so the live capture saw nothing.
          3. None — the integration MUST persist None rather than
             fabricate a driver (per-driver stats correctness).
        """
        dominant = self._resolve_dominant_driver(active)
        if dominant is not None:
            return dominant
        if not self._driver_sensor:
            return None
        return await self._async_driver_during(start, end)

    def _read_bool(self, entity_id: str | None) -> bool | None:
        raw = self._read_state(entity_id)
        if raw is None:
            return None
        return raw == STATE_ON

    @staticmethod
    def _is_charging_value(raw: str | None) -> bool | None:
        """v0.5.61 — multi-vocab "is the car charging right now?".

        Accepts:
          * the classic binary_sensor 'on' / 'off'
          * Tesla's `sensor.<v>_charging_state` enum
          * OVMS / generic textual states
        Returns None when the state itself is unknown/unavailable so
        the caller can short-circuit just like before.
        """
        if raw is None:
            return None
        return raw.strip().lower() in _CHARGING_STATES

    def _read_is_charging(self, entity_id: str | None) -> bool | None:
        return self._is_charging_value(self._read_state(entity_id))

    def current_snapshot(self) -> dict[str, Any] | None:
        """Return live trip metrics for the sensor platform."""
        active = self.current
        if active is None:
            return None
        # v0.5.47 — memo per notify cycle (see _notify_listeners).
        if self._snapshot_cache is not None:
            return self._snapshot_cache

        # None checks, not `or` — 0 is a valid reading for both.
        odo_read = self._read_float(self._odometer)
        odometer_now = odo_read if odo_read is not None else active.last_seen_odometer
        soc_read = self._read_float(self._battery)
        soc_now = soc_read if soc_read is not None else active.last_seen_soc
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
            (soc_used / 100.0) * self.battery_capacity
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
        # Live score: same curve as TripRecord.score_with_baseline,
        # anchored to the per-car baseline (v0.5.50). Inline rather
        # than instantiating a TripRecord to avoid the round-trip.
        score = None
        if consumption is not None and consumption > 0:
            baseline = self.score_baseline_kwh_100km
            score = max(
                0.0, min(10.0, 10.0 - max(0.0, consumption - baseline) * 0.6)
            )

        self._snapshot_cache = {
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
            "driver": active.driver,
            # v0.6.6 — surface live idle accounting so the dashboard
            # can render moving-only consumption / idle-ratio tiles
            # while the trip is in progress.
            "idle_minutes": (
                round(active.idle_seconds / 60.0, 1)
                if active.idle_seconds > 0 else None
            ),
            # v0.7.3 — live V95 + highway-ratio from the running
            # sample deque so the tiles reflect current driving
            # pattern mid-trip.
            "v95_speed_kmh": _speed_stats(
                active.speed_samples,
                highway_threshold_kmh=_HIGHWAY_SPEED_KMH,
            )[0],
            "highway_ratio_pct": _speed_stats(
                active.speed_samples,
                highway_threshold_kmh=_HIGHWAY_SPEED_KMH,
            )[1],
        }
        return self._snapshot_cache

    def current_charge_snapshot(self) -> dict[str, Any] | None:
        """Live charging metrics — mirror of LastChargeSensor while charging."""
        active = self.current_charge
        if active is None:
            return None
        soc_now = active.last_seen_soc if active.last_seen_soc is not None else active.soc_start
        if soc_now is None or active.soc_start is None or soc_now <= active.soc_start:
            kwh_so_far: float | None = 0.0
        else:
            kwh_so_far = (soc_now - active.soc_start) / 100.0 * self.battery_capacity
        # Live price: the user can correct it post-hoc on the last completed
        # charge; while in progress we project the configured home tariff.
        price_per_kwh = self._current_energy_price()
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
        # v0.5.92 — live EVSE-side energy and AC→DC efficiency. The
        # snapshot exposes both so current_charge_* sensors can render
        # them while the session is in progress.
        evse_kwh = (
            round(active.evse_energy_kwh, 3)
            if active.evse_energy_kwh > 0 else None
        )
        eff_pct: float | None = None
        if evse_kwh and evse_kwh > 0:
            # Prefer the integrated battery-side value when we have it,
            # otherwise fall back to the SoC-derived kwh_so_far.
            battery_kwh = (
                active.energy_added_kwh if active.energy_added_kwh > 0
                else (kwh_so_far or 0)
            )
            if battery_kwh > 0:
                eff_pct = round(battery_kwh / evse_kwh * 100.0, 1)
        return {
            "kwh": round(kwh_so_far, 2) if kwh_so_far else 0.0,
            "total_cost": round(total_cost, 2),
            "price_per_kwh": price_per_kwh,
            "power_kw": active.last_power_kw,
            "duration_min": duration_min,
            "is_dcfc": is_dcfc,
            "soc_start": active.soc_start,
            "soc_now": soc_now,
            "evse_energy_kwh": evse_kwh,
            "charging_efficiency_pct": eff_pct,
        }
