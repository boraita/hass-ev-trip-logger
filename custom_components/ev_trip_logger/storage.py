"""SQLite-backed storage for trip records."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, UTC
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import STORAGE_FILENAME_TEMPLATE


# v0.8.17 — distance-weighted consumption, over rows that carry BOTH
# columns. `AVG(consumption_kwh_100km)` is a mean of ratios: it gave a
# 2 km cold-start the same weight as a 200 km motorway run (2 km at 40
# and 200 km at 15 averaged to 27.5 instead of 15.25). A plain
# SUM(e)/SUM(d) is not enough either — SUM skips NULL energy while the
# same row's kilometres still land in the denominator, understating the
# result. Both sides are restricted to the same subset.
_WEIGHTED = (
    "CASE WHEN energy_kwh IS NOT NULL AND energy_kwh > 0 "
    "AND distance_km IS NOT NULL AND distance_km > 0 THEN {col} END"
)
_WEIGHTED_CONSUMPTION_SQL = (
    "CASE WHEN SUM(" + _WEIGHTED.format(col="distance_km") + ") > 0 "
    "THEN SUM(" + _WEIGHTED.format(col="energy_kwh") + ") / "
    "SUM(" + _WEIGHTED.format(col="distance_km") + ") * 100.0 "
    "ELSE NULL END"
)
# v0.8.17 — real average speed = total distance / total time. `AVG(
# avg_speed_kmh)` is a mean of means: 29 city trips of 5 km/20 min plus
# one 400 km/4 h motorway run averaged to 18.2 km/h when the true figure
# is 39.9. The three 30-day sensors used to contradict each other —
# distance / duration did not equal the published speed.
_SPEED_SUBSET = (
    "CASE WHEN distance_km IS NOT NULL AND distance_km > 0 "
    "AND duration_min IS NOT NULL AND duration_min > 0 THEN {col} END"
)
_WEIGHTED_SPEED_SQL = (
    "CASE WHEN SUM(" + _SPEED_SUBSET.format(col="duration_min") + ") > 0 "
    "THEN SUM(" + _SPEED_SUBSET.format(col="distance_km") + ") / "
    "SUM(" + _SPEED_SUBSET.format(col="duration_min") + ") * 60.0 "
    "ELSE NULL END"
)


def _parse_stored(value: str | None) -> datetime | None:
    """Parse a stored ISO timestamp; naive rows are local (see _iso_local)."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return ts


#: Tolerance on the upper bound of the "charged before this trip" window.
#:
#: The v0.8.17 mutex has the charge-off handler open the trip, so both
#: stamps come out of the same event cascade and land microseconds apart —
#: one instant recorded twice. Compared exactly, `ended_at <= trip_start`
#: fails, and the session is not reported as having charged before the
#: trip. That NULL is not cosmetic: `heal_history`'s "provably impossible
#: SoC rise while parked" rule reads it as "nothing charged in the gap" and
#: re-anchors a real SoC start downwards, destroying the charge's points.
#:
#: 60 s absorbs the shared-instant case plus a full cloud-poll interval.
#:
#: Deliberately NOT applied to the "during" bucket. That question — how
#: much of a session was still to come when the window opened — is already
#: answered by SoC in `_apportion_straddling_charge`, and a time-based
#: exclusion there wrongly drops a genuine mid-trip charge whenever the
#: two events happen to sit close together. The buckets may therefore both
#: count a charge that ends right at the boundary; `before` is
#: informational, only `during` feeds the trip's energy, so the overlap
#: costs nothing while the missing `before` cost a SoC anchor.
_CHARGE_TRIP_BOUNDARY_TOLERANCE_S: int = 60

# v0.8.29 — plausibility band for `charging_efficiency_pct`
# (= battery kWh / charger kWh × 100). Energy reaching the battery can
# never exceed what the meter delivered, so anything above 100 % is
# physically impossible; the headroom to _EFFICIENCY_MAX_PCT covers
# meter resolution and the ±1 % SoC quantization that can push a
# genuinely-99 % session slightly over. Below _EFFICIENCY_MIN_PCT means
# over half the delivered energy went somewhere other than the pack,
# which no charge session does — it means one of the two figures is
# wrong. Rows outside the band are excluded from the rolling median so
# a known-bad measurement cannot move a number the user reads as truth.
_EFFICIENCY_MIN_PCT: float = 50.0
_EFFICIENCY_MAX_PCT: float = 105.0

#: SQL predicate for "this charge has a usable EVSE meter reading" —
#: shared by every paired kwh/evse sum so the ratio between them cannot
#: fall outside the plausibility band above.
_EVSE_USABLE_SQL = (
    "evse_energy_kwh IS NOT NULL AND evse_energy_kwh > 0 "
    "AND kwh IS NOT NULL "
    f"AND kwh / evse_energy_kwh >= {_EFFICIENCY_MIN_PCT / 100.0} "
    f"AND kwh / evse_energy_kwh <= {_EFFICIENCY_MAX_PCT / 100.0}"
)


def _anchor_lots(lots: list[list[float]], target_kwh: float, pad_price: float) -> None:
    """Re-anchor a LIFO lot stack to the pack's known physical content.

    The pool re-anchors its QUANTITY to `soc_start` / `soc_end` because
    energy leaves the battery without being a trip — standby drain,
    preconditioning, driving the logger never saw. The lot stack has to
    track the same quantity or the two figures drift apart, and the
    per-lot number stops meaning anything.

    Which lots absorb the discrepancy matters:

    * Surplus (we hold more than the pack does) is trimmed from the
      FRONT — the oldest energy. Unexplained loss is far likelier to be
      the energy that has been sitting there for days than the charge
      that just went in, and trimming the newest would delete the very
      lot the caller is about to be priced against.
    * Shortfall (the pack holds more than we know about — opening
      inventory on a fresh install) is padded at the front at
      `pad_price`, mirroring the pool's own "price the unknown opening
      inventory at the incoming charge's price" rule. Pricing it at 0
      would report that energy as free forever.

    Mutates `lots` in place; each lot is `[kwh, price]`, oldest first.
    """
    total = sum(lot[0] for lot in lots)
    if target_kwh < total:
        excess = total - target_kwh
        while excess > 1e-9 and lots:
            if lots[0][0] <= excess + 1e-9:
                excess -= lots[0][0]
                lots.pop(0)
            else:
                lots[0][0] -= excess
                excess = 0.0
    elif target_kwh > total and pad_price > 0:
        lots.insert(0, [target_kwh - total, pad_price])


def _apportion_straddling_charge(
    kwh: float,
    *,
    soc_start: float | None,
    soc_end: float | None,
    trip_soc_start: float | None,
) -> float:
    """kWh of a charge that was already running when a trip window opened.

    v0.8.18 — SoC is the meter: the fraction of the session delivered
    after `trip_soc_start` is (soc_end - trip_soc_start) / (soc_end -
    soc_start). Returns 0.0 when the readings cannot support the split,
    because a guess here lands in the trip's consumption and its cost.
    """
    if soc_start is None or soc_end is None or trip_soc_start is None:
        return 0.0
    span = float(soc_end) - float(soc_start)
    if span <= 0:
        return 0.0
    cut = float(trip_soc_start)
    if cut <= float(soc_start):
        return kwh
    if cut >= float(soc_end):
        return 0.0
    return kwh * (float(soc_end) - cut) / span


def _iso_local(value: datetime) -> str:
    """Serialise a datetime as local-time ISO.

    v0.8.17 — rows are stored as ISO TEXT and every "most recent" query
    orders by `ended_at` as a STRING, so two rows written with different
    UTC offsets do not compare correctly: a row stamped '+00:00' sorts as
    if it happened `utcoffset` earlier than it really did. The live paths
    all use `dt_util.now()` (local), but the recovery sweep takes stamps
    straight from the recorder (UTC) and the correction services take
    whatever the caller passed — a naive datetime serialises with no
    offset at all and sorts below everything. Normalising on the way in
    keeps the text ordering equal to the chronological ordering.
    """
    if value.tzinfo is None:
        return dt_util.as_local(value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)).isoformat()
    return dt_util.as_local(value).isoformat()

_LOGGER = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_min REAL NOT NULL,
    distance_km REAL NOT NULL,
    odometer_start REAL,
    odometer_end REAL,
    soc_start REAL,
    soc_end REAL,
    soc_used_pct REAL,
    energy_kwh REAL,
    consumption_kwh_100km REAL,
    avg_speed_kmh REAL,
    max_power_kw REAL,
    max_speed_kmh REAL,
    regen_kwh REAL,
    avg_temp_c REAL,
    origin TEXT,
    destination TEXT,
    cost REAL,
    currency TEXT,
    journey_id INTEGER,
    start_lat REAL,
    start_lon REAL,
    end_lat REAL,
    end_lon REAL,
    -- v0.5.13: stale-SoC-at-trip-start fix.
    -- soc_start_source: which heuristic produced soc_start
    --   'last_charge_end'  → anchored to the prior charge's end SoC (best)
    --   'snap_short_park'  → v0.5.40: snapped to prev trip's soc_end
    --                        when parked < 30 min and apparent gap ≤ 2 %
    --                        (kills integer-SoC quantization phantom drop)
    --   'pre_on_sample'    → buffer sample taken < 5 min before vehicle_on
    --   'post_on_sample'   → current/cached reading (legacy fallback)
    --   'unavailable'      → battery sensor unreadable at open
    -- energy_source: how energy_kwh was derived
    --   'soc'              → from soc_used * capacity
    --   'power_integration'→ ∫|power| dt while trip open (more pessimistic)
    --   'estimated'        → distance × avg kWh/100km heal
    -- energy_from_power: raw integration result, kept for audit/dashboards.
    soc_start_source TEXT,
    energy_source TEXT,
    energy_from_power REAL,
    -- v0.5.26: distance recomputed from the route polyline via
    -- haversine. Compared with the odometer-derived distance_km it
    -- reveals odo cadence issues (gps > odo means we have route
    -- points the odometer missed) or GPS noise (gps >> odo).
    gps_distance_km REAL,
    -- v0.5.27: kWh added by charges that ended between the previous
    -- trip's ended_at and THIS trip's started_at. Lets the dashboard
    -- show "antes de este trip cargaste +24 kWh" so the user knows
    -- a SoC bump between trips wasn't a measurement glitch.
    kwh_charged_before REAL,
    -- kWh added by charges that ended INSIDE this trip's window
    -- (started_at ≤ charge.ended_at ≤ ended_at). Should be ~0 thanks
    -- to v0.5.18 mutex (trip force-closes on charging=on), but
    -- non-zero in edge cases — manual logs, force-close races, etc.
    kwh_charged_during REAL,
    -- v0.5.35: detection-quality flag.
    --   'live'                          → captured via vehicle_on
    --                                     transitions (precise times,
    --                                     full metrics).
    --   'reconstructed'                 → synth path. Odometer
    --                                     monotonic growth detected,
    --                                     vehicle_on never flipped.
    --                                     started_at is the last
    --                                     idle reading; metrics are
    --                                     partial.
    --   'reconstructed_polling_paused'  → reconstructed AND the
    --                                     configured polling-pause
    --                                     sensor was ON during the
    --                                     window, so even the route
    --                                     points are sparse.
    --   'orphan'                        → v0.5.41: synthetic record
    --                                     between two live trips when
    --                                     the odometer showed a real
    --                                     km gap whose SoC drop
    --                                     matched expected consumption
    --                                     (missed on→off→on cycle).
    --   'orphan_odo_only'               → v0.5.41: km gap detected
    --                                     but SoC didn't track —
    --                                     previous odo_end was stale,
    --                                     km belong to the prior
    --                                     drive. Energy fields NULL.
    confidence TEXT,
    -- v0.5.43: who drove. State of the configured driver sensor
    -- (e.g. the car's "connected bluetooth device" entity) captured
    -- while the trip was open. NULL when no sensor is configured or
    -- nobody was identified. Powers the per-driver km/hours stats.
    driver TEXT,
    -- v0.5.76: weighted-average €/kWh experienced by the trip after
    -- the WAC pool replay. NULL when energy_kwh is missing.
    -- Equals cost / energy_kwh; useful for dashboards to surface the
    -- "what did this trip actually cost per kWh" answer (mixes home
    -- tariff and external charges).
    cost_basis_per_kwh REAL,
    cost_lifo REAL,
    -- v0.5.84: per-trip battery-capacity calibration factor. Ratio of
    -- the power-integration net energy (∫|P|·dt − 2·regen, the real
    -- kWh drawn from the battery measured at the motor) to the SoC-
    -- delta energy assuming nominal capacity. K ≈ 1.0 means battery
    -- capacity matches nominal. Persistent drift toward K < 1.0 across
    -- many trips signals real degradation (or power-integration gap).
    -- NULL when soc_delta is too small (<2%, quantization-dominated)
    -- or when energy_from_power is missing.
    calibration_factor_k REAL,
    -- v0.5.86: 95% confidence band on consumption_kwh_100km. Lower
    -- and upper bounds capture quantization noise of the energy
    -- source used. Useful when a 5km trip shows 16.5 with band
    -- [11-22] vs a 30km trip showing 16.5 with band [15-18]; same
    -- headline number, very different signal quality.
    consumption_lower_kwh_100km REAL,
    consumption_upper_kwh_100km REAL,
    -- v0.5.86: heuristic flag. True when the trip's data has noise
    -- dominating the signal (distance < 2 km on SoC-only, or
    -- relative band > 40 %). Dashboards hide / grey these out of
    -- aggregates so the rolling baselines aren't polluted by
    -- quantization artifacts.
    low_confidence INTEGER,
    -- v0.6.0: gross discharge energy = ∫P·dt over P>0 segments.
    -- Pairs with regen_kwh to separate "what came out of the battery"
    -- from "what went back in" — OVMS-style split (ms_v_bat_energy_used
    -- vs _recd). The existing energy_kwh stays NET (what the user
    -- effectively burned); discharge_kwh is the gross half useful for
    -- per-driver regen ratio, per-direction K calibration, and SoH
    -- inputs that should exclude regen overhead.
    discharge_kwh REAL,
    -- v0.6.6: minutes the vehicle was on but stationary (waiting with
    -- AC on, drive-through queues, traffic lights on a hot day).
    -- Combined with the configured `idle_power_estimate_kw` lets
    -- dashboards split "energy used moving" vs "energy burned
    -- waiting" — explains high kWh/100km headlines on trips
    -- dominated by idle time.
    idle_minutes REAL,
    -- v0.7.3: 95th-percentile of the trip's per-tick speed samples.
    -- Ranked 4th-strongest feature for trip-level consumption
    -- prediction in the Chalmers 2024 QRNN study (ρ ≈ 0.29). Robust
    -- to a single spike (unlike max_speed_kmh). NULL when no speed
    -- sensor was wired.
    v95_speed_kmh REAL,
    -- v0.7.3: percentage of live-tick samples where speed ≥ 80 km/h.
    -- Lets dashboards render an "urban vs autopista" split without
    -- re-deriving it from raw samples. NULL when no speed sensor.
    highway_ratio_pct REAL,
    -- v0.7.5: elevation profile stats from an external elevation
    -- provider (open-elevation.com / opentopodata / custom URL).
    -- Ranked 3rd-strongest feature for consumption prediction in
    -- Chalmers 2024 QRNN study (ρ ≈ 0.39). All NULL when the
    -- elevation feature is off (default) or the API call failed.
    elevation_gain_m REAL,
    elevation_loss_m REAL,
    elevation_variance_m2 REAL
);
CREATE INDEX IF NOT EXISTS idx_trips_started_at ON trips(started_at);

CREATE TABLE IF NOT EXISTS charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    ended_at TEXT NOT NULL,
    kwh REAL NOT NULL,
    price_per_kwh REAL NOT NULL,
    total_cost REAL NOT NULL,
    currency TEXT,
    soc_start REAL,
    soc_end REAL,
    location TEXT,
    notes TEXT,
    is_dcfc INTEGER,
    -- price_locked = 1 when the user has explicitly corrected the price via
    -- set_last_charge_price. Auto-detect must NOT stomp it with a phantom
    -- second insert. NULL/0 = price is just the default fallback.
    price_locked INTEGER,
    -- v0.5.90: AC-side energy delivered by the EVSE/wallbox (W·h
    -- converted to kWh by the integration). Comparing kwh (battery
    -- input) vs evse_energy_kwh (charger output) gives real AC→DC
    -- efficiency. NULL when no `evse_power_sensor` was configured.
    evse_energy_kwh REAL,
    -- v0.5.90: kwh / evse_energy_kwh × 100. Typical AC home charger:
    -- 88-94 %. DC fast: 92-97 %. Below 85 % signals lossy cable /
    -- wallbox / onboard charger derating.
    charging_efficiency_pct REAL,
    -- v0.6.0: peak instantaneous charge power during the session
    -- (max |power| sample from the vehicle's power sensor, or the
    -- EVSE sensor when only that is wired). Drives DCFC-stress
    -- accounting in the SoH model — Geotab's 22 700-EV study
    -- (https://www.geotab.com/blog/ev-battery-health/) reports
    -- ~3.0 %/yr degradation for heavy DCFC users (>100 kW peaks)
    -- vs ~1.5 %/yr for AC-only, the largest operational stressor.
    peak_charge_power_kw REAL,
    -- v0.6.5: ambient temperature at charge close (read from
    -- CONF_TEMP if wired). Feeds the SoH sample-gate: charges at
    -- extreme temperatures (<5 °C or >35 °C) produce noisier
    -- ΔSoC↔ΔkWh samples (BMS reserves a bigger buffer when cold,
    -- fast-charging derates when hot) so they're excluded from the
    -- capacity median. NULL when no temperature sensor is wired.
    temperature_c REAL,
    -- v0.8.14: how `kwh` was derived, mirroring trips.energy_source.
    --   'power_integration' → ∫|P| dt during the session, only used
    --                         once it covered most of the session
    --                         (see coordinator._CHARGE_ENERGY_MIN_*)
    --   'soc_delta'         → (soc_end - soc_start) × capacity
    --   NULL                → manually logged (log_charge service /
    --                         historical rows from before this field)
    -- Lets the capacity self-calibration prefer grounded, directly-
    -- measured samples over the SoC-delta guess it's meant to correct
    -- (previously circular: most calibration inputs WERE the SoC-delta
    -- estimate). See _effective_capacity_kwh.
    energy_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_charges_ended_at ON charges(ended_at);

CREATE TABLE IF NOT EXISTS trip_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_trip_positions_trip_id ON trip_positions(trip_id);
"""


@dataclass(slots=True)
class TripRecord:
    """A completed trip."""

    started_at: datetime
    ended_at: datetime
    duration_min: float
    distance_km: float
    odometer_start: float | None = None
    odometer_end: float | None = None
    soc_start: float | None = None
    soc_end: float | None = None
    soc_used_pct: float | None = None
    energy_kwh: float | None = None
    consumption_kwh_100km: float | None = None
    avg_speed_kmh: float | None = None
    max_power_kw: float | None = None
    max_speed_kmh: float | None = None
    regen_kwh: float | None = None
    avg_temp_c: float | None = None
    origin: str | None = None
    destination: str | None = None
    cost: float | None = None
    currency: str | None = None
    journey_id: int | None = None
    # GPS endpoints — first and last position sampled during the trip.
    # Populated by the live tick's sampler when a location entity is wired.
    # Lets the dashboard build a Google-Maps route link.
    start_lat: float | None = None
    start_lon: float | None = None
    end_lat: float | None = None
    end_lon: float | None = None
    # Reverse-geocoded human-readable addresses (Nominatim, optional).
    # Populated at trip close when the GPS endpoint is outside any HA zone.
    start_address: str | None = None
    end_address: str | None = None
    # v0.5.13: provenance of soc_start / energy_kwh — see _SCHEMA header.
    soc_start_source: str | None = None
    energy_source: str | None = None
    energy_from_power: float | None = None
    gps_distance_km: float | None = None
    kwh_charged_before: float | None = None
    kwh_charged_during: float | None = None
    confidence: str | None = None
    # v0.5.43: driver identity captured from the configured driver sensor.
    driver: str | None = None
    # v0.5.54: weather snapshot averages from the configured weather entity.
    ambient_temp_c: float | None = None
    weather_condition: str | None = None
    humidity_pct: float | None = None
    wind_kmh: float | None = None
    precipitation_mm: float | None = None
    # v0.5.76: weighted-average €/kWh the WAC pool replay
    # produced for this trip. Equals cost / energy_kwh once the
    # post-insert recompute has run; None until then.
    cost_basis_per_kwh: float | None = None
    #: v0.8.22 — what this trip's energy cost at the prices of the charges
    #: that actually filled the battery, newest lot first. Companion to
    #: `cost` (the blended pool), never a replacement: the pool is the
    #: honest average, this is the answer to "what did the energy I just
    #: bought cost me". None when the replay could not compute it.
    cost_lifo: float | None = None
    # v0.5.84: ratio of power-integrated net energy to SoC-derived
    # nominal energy. ~1.0 when battery capacity matches nominal;
    # rolling median across many trips estimates real degradation.
    calibration_factor_k: float | None = None
    # v0.5.86: 95% confidence band on `consumption_kwh_100km`. Lower
    # and upper bounds derived from quantization noise of the source
    # used. None when not computed.
    consumption_lower_kwh_100km: float | None = None
    consumption_upper_kwh_100km: float | None = None
    # v0.5.86: heuristic — trips where noise dominates signal. True
    # for sub-2km trips on SoC-only, or trips where the band is more
    # than 40% of the headline value.
    low_confidence: bool | None = None
    # v0.6.0: gross discharge energy. Paired with regen_kwh, lets the
    # caller see "energy out vs energy back" without losing the net
    # number in `energy_kwh`. Always positive; None when no power
    # samples were captured.
    discharge_kwh: float | None = None
    # v0.6.6: minutes the vehicle was on but stationary during the trip.
    # Lets dashboards expose a "moving consumption" metric that excludes
    # waiting-with-AC overhead. None when no live-tick data was captured
    # (synth / recovered trips).
    idle_minutes: float | None = None
    # v0.7.3: 95th-percentile speed and highway-ratio derived from the
    # trip's per-tick speed samples. Both None when no speed sensor
    # was wired.
    v95_speed_kmh: float | None = None
    highway_ratio_pct: float | None = None
    # v0.7.5: elevation profile stats from the configured provider.
    # All None when the feature is disabled (default) or the API
    # call failed (network / non-200 / malformed).
    elevation_gain_m: float | None = None
    elevation_loss_m: float | None = None
    elevation_variance_m2: float | None = None
    trip_id: int | None = field(default=None, compare=False)

    @property
    def score(self) -> float | None:
        """Default score using the historical 14.5 kWh/100km anchor.

        Kept for backwards compatibility. Production callers should prefer
        `score_with_baseline(coordinator.score_baseline_kwh_100km)` which
        adapts the 10/10 anchor to THIS car's own best-observed efficiency.
        """
        return self.score_with_baseline(14.5)

    def score_with_baseline(self, baseline: float) -> float | None:
        """Efficiency rating 0–10 derived from kWh/100km.

        v0.5.50 — `baseline` is the kWh/100km value that maps to 10/10.
        Slope is 0.6 points per excess kWh/100km. Originally the baseline
        was hard-coded at 14.5 (matching the BYD app's curve), but it's
        unfair on Teslas in the Alps. The coordinator now derives the
        baseline from the car's own historical best (P5 of distance>=5km
        trips, clamped to [8, 20]); the default of 14.5 is the fallback
        when there's not enough history yet.
        """
        e = self.consumption_kwh_100km
        if e is None or e <= 0:
            return None
        return max(0.0, min(10.0, 10.0 - max(0.0, e - baseline) * 0.6))

    def to_dict(self) -> dict[str, Any]:
        """Serialise for events / export."""
        return {
            "trip_id": self.trip_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "duration_min": self.duration_min,
            "distance_km": self.distance_km,
            "odometer_start": self.odometer_start,
            "odometer_end": self.odometer_end,
            "soc_start": self.soc_start,
            "soc_end": self.soc_end,
            "soc_used_pct": self.soc_used_pct,
            "energy_kwh": self.energy_kwh,
            "consumption_kwh_100km": self.consumption_kwh_100km,
            "avg_speed_kmh": self.avg_speed_kmh,
            "max_power_kw": self.max_power_kw,
            "max_speed_kmh": self.max_speed_kmh,
            "regen_kwh": self.regen_kwh,
            "avg_temp_c": self.avg_temp_c,
            "origin": self.origin,
            "destination": self.destination,
            "cost": self.cost,
            "currency": self.currency,
            "soc_start_source": self.soc_start_source,
            "energy_source": self.energy_source,
            "energy_from_power": self.energy_from_power,
            "driver": self.driver,
            "discharge_kwh": self.discharge_kwh,
            "idle_minutes": self.idle_minutes,
            "v95_speed_kmh": self.v95_speed_kmh,
            "highway_ratio_pct": self.highway_ratio_pct,
            "elevation_gain_m": self.elevation_gain_m,
            "elevation_loss_m": self.elevation_loss_m,
            "elevation_variance_m2": self.elevation_variance_m2,
        }


@dataclass(slots=True)
class ChargeRecord:
    """A charging session."""

    ended_at: datetime
    kwh: float
    price_per_kwh: float
    total_cost: float
    started_at: datetime | None = None
    currency: str | None = None
    soc_start: float | None = None
    soc_end: float | None = None
    location: str | None = None
    notes: str | None = None
    is_dcfc: bool | None = None
    # True if the user has explicitly set the price (incl. €0 for "free").
    # Auto-detect must not stomp it.
    price_locked: bool = False
    # v0.5.90: AC-side energy from the configured EVSE / wallbox sensor.
    # None when no `evse_power_sensor` was wired.
    evse_energy_kwh: float | None = None
    # v0.5.90: kwh / evse_energy_kwh × 100 — real AC→DC charging
    # efficiency. None when evse_energy_kwh is missing or zero.
    charging_efficiency_pct: float | None = None
    # v0.6.0: peak instantaneous charging power observed during the
    # session (max |power_kw| sample). Drives the DCFC-stress
    # accumulator in the SoH model; sessions ≥ 100 kW are flagged as
    # high-stress (Geotab fleet study). None when no power signal
    # was available during the session.
    peak_charge_power_kw: float | None = None
    # v0.6.5: ambient temperature at close, drives the SoH sample-gate.
    # None when no exterior-temp sensor is wired.
    temperature_c: float | None = None
    # v0.8.14: 'power_integration' | 'soc_delta' | None (manual entry /
    # pre-v0.8.14 row). See _SCHEMA header comment for the full story.
    energy_source: str | None = None
    charge_id: int | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "charge_id": self.charge_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat(),
            "kwh": self.kwh,
            "price_per_kwh": self.price_per_kwh,
            "total_cost": self.total_cost,
            "currency": self.currency,
            "soc_start": self.soc_start,
            "soc_end": self.soc_end,
            "location": self.location,
            "notes": self.notes,
            "is_dcfc": self.is_dcfc,
        }


class TripStorage:
    """Lightweight SQLite wrapper for the trip log."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._path = Path(hass.config.path(".storage")) / STORAGE_FILENAME_TEMPLATE.format(
            entry_id=entry_id
        )
        # v0.8.17 — effective battery capacity, published by the
        # coordinator's `battery_capacity` property. The WAC replay uses
        # it to re-anchor its pool to the physical pack; None keeps the
        # old purely-additive behaviour.
        self.capacity_hint_kwh: float | None = None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection that is transaction-managed AND closed on exit.

        ``with sqlite3.connect(...) as conn`` only commits/rolls back the
        transaction — it does NOT close the connection, so the underlying file
        descriptor leaks until garbage collection. Under frequent writes that
        exhausts the process FD limit and crashes HA Core
        (``[Errno 24] No file descriptors available``). Closing explicitly
        every time fixes the leak. ``row_factory`` is set unconditionally so
        callers can index rows by name or position without re-setting it.
        """
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        # v0.5.47 — NORMAL sync is safe under WAL (set persistently at
        # init) and skips the per-commit fsync that dominated I/O cost
        # at ~450 connections/hour on flash storage.
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            with conn:  # commit on success / rollback on exception
                yield conn
        finally:
            conn.close()

    async def async_init(self) -> None:
        """Create the schema if needed."""
        await self._hass.async_add_executor_job(self._init_db)

    def _init_db(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # v0.5.47 — WAL is persistent in the DB file: readers no
            # longer block on the writer and commits skip the rollback-
            # journal fsync dance. The integration opens a connection
            # per query (~450/h), so this is the single cheapest win.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Apply additive migrations on existing databases."""
        trip_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(trips)").fetchall()
        }
        if "journey_id" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN journey_id INTEGER")
        if "regen_kwh" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN regen_kwh REAL")
        if "max_speed_kmh" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN max_speed_kmh REAL")
        for col in ("start_lat", "start_lon", "end_lat", "end_lon"):
            if col not in trip_cols:
                conn.execute(f"ALTER TABLE trips ADD COLUMN {col} REAL")
        for col in ("start_address", "end_address"):
            if col not in trip_cols:
                conn.execute(f"ALTER TABLE trips ADD COLUMN {col} TEXT")
        # v0.5.13: stale-SoC fix — see header comment in _SCHEMA.
        for col in ("soc_start_source", "energy_source"):
            if col not in trip_cols:
                conn.execute(f"ALTER TABLE trips ADD COLUMN {col} TEXT")
        if "energy_from_power" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN energy_from_power REAL")
        if "gps_distance_km" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN gps_distance_km REAL")
        for col in ("kwh_charged_before", "kwh_charged_during"):
            if col not in trip_cols:
                conn.execute(f"ALTER TABLE trips ADD COLUMN {col} REAL")
        if "confidence" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN confidence TEXT")
        # v0.5.43: per-trip driver identity.
        if "driver" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN driver TEXT")
        # v0.5.76: weighted-average €/kWh from the WAC pool replay.
        if "cost_basis_per_kwh" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN cost_basis_per_kwh REAL")
        if "cost_lifo" not in trip_cols:
            # v0.8.22 — per-lot companion to `cost`. Backfilled by the
            # next cost replay, which runs on every startup, so no data
            # migration is needed here.
            conn.execute("ALTER TABLE trips ADD COLUMN cost_lifo REAL")
        # v0.5.84: per-trip battery-capacity calibration factor.
        if "calibration_factor_k" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN calibration_factor_k REAL")
        # v0.5.86: consumption confidence band + low-confidence flag.
        for col in (
            "consumption_lower_kwh_100km",
            "consumption_upper_kwh_100km",
        ):
            if col not in trip_cols:
                conn.execute(f"ALTER TABLE trips ADD COLUMN {col} REAL")
        if "low_confidence" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN low_confidence INTEGER")
        # v0.6.0: gross discharge energy alongside regen, OVMS-style split.
        if "discharge_kwh" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN discharge_kwh REAL")
        # v0.6.6: idle_minutes — time spent vehicle_on=on but stationary.
        if "idle_minutes" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN idle_minutes REAL")
        # v0.7.3: V95 + highway ratio for speed-distribution metrics.
        if "v95_speed_kmh" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN v95_speed_kmh REAL")
        if "highway_ratio_pct" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN highway_ratio_pct REAL")
        # v0.7.5: elevation profile stats (opt-in feature).
        for col in ("elevation_gain_m", "elevation_loss_m", "elevation_variance_m2"):
            if col not in trip_cols:
                conn.execute(f"ALTER TABLE trips ADD COLUMN {col} REAL")
        # v0.5.54: weather snapshot — averages of start/close readings
        # from the configured weather.* entity. All optional (NULL when
        # CONF_WEATHER_ENTITY isn't set).
        for col in (
            "ambient_temp_c", "humidity_pct", "wind_kmh", "precipitation_mm",
        ):
            if col not in trip_cols:
                conn.execute(f"ALTER TABLE trips ADD COLUMN {col} REAL")
        if "weather_condition" not in trip_cols:
            conn.execute("ALTER TABLE trips ADD COLUMN weather_condition TEXT")
        # v0.5.54: longitudinal battery capacity tracking. Each row
        # is a snapshot of the calibration result (the median of
        # `kwh/ΔSoC × 100` over recent charges). Comparing earliest
        # vs most recent reveals SoH degradation over time.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capacity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                calibrated_kwh REAL NOT NULL,
                declared_kwh REAL NOT NULL,
                n_charges INTEGER NOT NULL,
                -- v0.5.65: car's lifetime odometer reading at the
                -- snapshot moment. Informational only — see logger_km
                -- below for the SoH-relevant figure.
                odometer_km REAL,
                -- v0.5.66: distance the logger has actually witnessed
                -- (SUM(distance_km)) at the snapshot moment. This is
                -- what the SoH model uses for the cycle-aging
                -- component, since the model only knows habits/climate
                -- during the period the logger has been running.
                logger_km REAL
            )
            """
        )
        # Migration: if the table existed before v0.5.65/66, add columns.
        cap_cols = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(capacity_history)"
            ).fetchall()
        }
        if "odometer_km" not in cap_cols:
            conn.execute("ALTER TABLE capacity_history ADD COLUMN odometer_km REAL")
        if "logger_km" not in cap_cols:
            conn.execute("ALTER TABLE capacity_history ADD COLUMN logger_km REAL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_capacity_history_observed_at "
            "ON capacity_history(observed_at)"
        )
        # Safe to call on fresh or migrated DBs.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trips_journey_id ON trips(journey_id)"
        )
        # v0.5.47 — match the actual query shapes: _get_last orders by
        # ended_at on every startup/service call, and the journey
        # open/absorb logic filters on destination at every trip close.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trips_ended_at ON trips(ended_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trips_destination ON trips(destination)"
        )
        charge_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(charges)").fetchall()
        }
        if "is_dcfc" not in charge_cols:
            conn.execute("ALTER TABLE charges ADD COLUMN is_dcfc INTEGER")
        if "price_locked" not in charge_cols:
            conn.execute("ALTER TABLE charges ADD COLUMN price_locked INTEGER")
        # v0.5.90: AC-side energy + AC→DC efficiency.
        for col in ("evse_energy_kwh", "charging_efficiency_pct"):
            if col not in charge_cols:
                conn.execute(f"ALTER TABLE charges ADD COLUMN {col} REAL")
        # v0.6.0: peak charge power (drives DCFC-stress accounting).
        if "peak_charge_power_kw" not in charge_cols:
            conn.execute("ALTER TABLE charges ADD COLUMN peak_charge_power_kw REAL")
        # v0.6.5: ambient temperature at charge close (SoH sample gate).
        if "temperature_c" not in charge_cols:
            conn.execute("ALTER TABLE charges ADD COLUMN temperature_c REAL")
        # v0.8.14: which method produced `kwh` — see header comment above.
        if "energy_source" not in charge_cols:
            conn.execute("ALTER TABLE charges ADD COLUMN energy_source TEXT")
        # v0.5.0: trip_positions table for route-map drilldown.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trip_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL,
                ts TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trip_positions_trip_id ON trip_positions(trip_id)"
        )

    async def async_insert(self, record: TripRecord) -> int:
        """Persist a completed trip, return its id."""
        return await self._hass.async_add_executor_job(self._insert, record)

    def _insert(self, record: TripRecord) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO trips (
                    started_at, ended_at, duration_min, distance_km,
                    odometer_start, odometer_end, soc_start, soc_end, soc_used_pct,
                    energy_kwh, consumption_kwh_100km, avg_speed_kmh, max_power_kw,
                    max_speed_kmh, regen_kwh,
                    avg_temp_c, origin, destination, cost, currency, journey_id,
                    start_lat, start_lon, end_lat, end_lon,
                    start_address, end_address,
                    soc_start_source, energy_source, energy_from_power,
                    gps_distance_km, kwh_charged_before, kwh_charged_during,
                    confidence, driver,
                    ambient_temp_c, weather_condition, humidity_pct,
                    wind_kmh, precipitation_mm,
                    cost_basis_per_kwh,
                    calibration_factor_k,
                    consumption_lower_kwh_100km,
                    consumption_upper_kwh_100km,
                    low_confidence,
                    discharge_kwh,
                    idle_minutes,
                    v95_speed_kmh,
                    highway_ratio_pct,
                    elevation_gain_m,
                    elevation_loss_m,
                    elevation_variance_m2
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _iso_local(record.started_at),
                    _iso_local(record.ended_at),
                    record.duration_min,
                    record.distance_km,
                    record.odometer_start,
                    record.odometer_end,
                    record.soc_start,
                    record.soc_end,
                    record.soc_used_pct,
                    record.energy_kwh,
                    record.consumption_kwh_100km,
                    record.avg_speed_kmh,
                    record.max_power_kw,
                    record.max_speed_kmh,
                    record.regen_kwh,
                    record.avg_temp_c,
                    record.origin,
                    record.destination,
                    record.cost,
                    record.currency,
                    record.journey_id,
                    record.start_lat,
                    record.start_lon,
                    record.end_lat,
                    record.end_lon,
                    record.start_address,
                    record.end_address,
                    record.soc_start_source,
                    record.energy_source,
                    record.energy_from_power,
                    record.gps_distance_km,
                    record.kwh_charged_before,
                    record.kwh_charged_during,
                    record.confidence,
                    record.driver,
                    record.ambient_temp_c,
                    record.weather_condition,
                    record.humidity_pct,
                    record.wind_kmh,
                    record.precipitation_mm,
                    record.cost_basis_per_kwh,
                    record.calibration_factor_k,
                    record.consumption_lower_kwh_100km,
                    record.consumption_upper_kwh_100km,
                    int(record.low_confidence) if record.low_confidence is not None else None,
                    record.discharge_kwh,
                    record.idle_minutes,
                    record.v95_speed_kmh,
                    record.highway_ratio_pct,
                    record.elevation_gain_m,
                    record.elevation_loss_m,
                    record.elevation_variance_m2,
                ),
            )
            return int(cur.lastrowid or 0)

    async def async_update_trip_destination(
        self, trip_id: int, destination: str
    ) -> None:
        """Amend a trip's destination after the fact.

        Used when the device_tracker lags vehicle_on=off and reports the
        home transition a few minutes after the trip already closed — we
        retroactively correct the destination so journey-close logic and
        history both reflect reality.
        """
        await self._hass.async_add_executor_job(
            self._update_trip_destination, trip_id, destination
        )

    def _update_trip_destination(self, trip_id: int, destination: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE trips SET destination = ? WHERE id = ?",
                (destination, trip_id),
            )

    # Columns the set_trip service may overwrite. Excludes derived/key
    # fields (id, score) and recorder-internal stuff. SoC %, distance,
    # cost, etc. are user-correctable.
    _TRIP_USER_EDITABLE = frozenset({
        "started_at", "ended_at", "duration_min", "distance_km",
        "odometer_start", "odometer_end", "soc_start", "soc_end",
        "soc_used_pct", "energy_kwh", "consumption_kwh_100km",
        "avg_speed_kmh", "max_power_kw", "max_speed_kmh", "regen_kwh",
        "avg_temp_c", "origin", "destination", "cost", "currency",
        "journey_id", "start_lat", "start_lon", "end_lat", "end_lon",
        "start_address", "end_address", "gps_distance_km",
        "kwh_charged_before", "kwh_charged_during", "confidence",
        "driver", "cost_basis_per_kwh", "calibration_factor_k",
        "consumption_lower_kwh_100km", "consumption_upper_kwh_100km",
        "low_confidence",
        "discharge_kwh",
        "idle_minutes",
        "v95_speed_kmh",
        "highway_ratio_pct",
        "elevation_gain_m",
        "elevation_loss_m",
        "elevation_variance_m2",
        # v0.5.77 — the vehicle-heal path overrides energy_kwh and
        # tags the row as `energy_source='vehicle'` for traceability.
        "energy_source",
    })

    async def async_get_trip_by_id(self, trip_id: int) -> TripRecord | None:
        """v0.5.77 — fetch a single trip by primary key."""
        return await self._hass.async_add_executor_job(
            self._get_trip_by_id, trip_id,
        )

    async def async_trips_needing_vehicle_heal(
        self, hours: int = 24,
    ) -> list[int]:
        """v0.5.86 — IDs of recent trips that didn't get vehicle-heal.

        Returns trips closed in the last `hours` whose `energy_source`
        is anything except `'vehicle'`. The coordinator sweeps these
        on startup so trips that missed the live heal (HA restart in
        the 240 s window) get a second chance against the BYD-native
        sensor.
        """
        return await self._hass.async_add_executor_job(
            self._trips_needing_vehicle_heal, hours,
        )

    def _trips_needing_vehicle_heal(self, hours: int) -> list[int]:
        cutoff = (
            datetime.now(UTC) - timedelta(hours=hours)
        ).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM trips "
                "WHERE ended_at >= ? "
                "  AND (energy_source IS NULL OR energy_source != 'vehicle') "
                "  AND distance_km IS NOT NULL AND distance_km > 0 "
                "ORDER BY id DESC LIMIT 50",
                (cutoff,),
            ).fetchall()
        return [int(r[0]) for r in rows]

    async def async_trips_missing_driver(
        self, *, days: int, limit: int,
    ) -> list[tuple[int, datetime, datetime]]:
        """v0.5.97 — IDs + start/end of recent trips with driver=NULL.

        Bounded by `days` (recorder retention is ~10 days, no point
        looking further back) and `limit` (avoid hundreds of recorder
        queries at boot on a fresh-install with a big history).
        Returns rows newest-first so a partial sweep still covers the
        user's most-recent activity.
        """
        return await self._hass.async_add_executor_job(
            self._trips_missing_driver, days, limit,
        )

    def _trips_missing_driver(
        self, days: int, limit: int,
    ) -> list[tuple[int, datetime, datetime]]:
        cutoff = (
            datetime.now(UTC) - timedelta(days=days)
        ).isoformat()
        rows: list[tuple[int, datetime, datetime]] = []
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id, started_at, ended_at FROM trips "
                "WHERE driver IS NULL "
                "  AND started_at IS NOT NULL "
                "  AND ended_at >= ? "
                "ORDER BY id DESC LIMIT ?",
                (cutoff, limit),
            )
            for r in cur.fetchall():
                try:
                    s = datetime.fromisoformat(r[1])
                    e = datetime.fromisoformat(r[2])
                except (TypeError, ValueError):
                    continue
                rows.append((int(r[0]), s, e))
        return rows

    def _get_trip_by_id(self, trip_id: int) -> TripRecord | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM trips WHERE id = ?", (trip_id,),
            ).fetchone()
        return _row_to_record(row) if row else None

    async def async_trip_overlaps(
        self, start: datetime, end: datetime, tolerance_s: int = 120
    ) -> bool:
        """True if any existing trip overlaps [start, end] (with ±tolerance).

        Used by recover_missing_trips so the recovery path never
        duplicates a row that's already in storage. A 2-minute fudge
        absorbs cloud-poll cadence wobble around the window boundaries.
        """
        return await self._hass.async_add_executor_job(
            self._trip_overlaps, start, end, tolerance_s,
        )

    def _trip_overlaps(
        self, start: datetime, end: datetime, tolerance_s: int
    ) -> bool:
        # Overlap if existing.started_at < end+tol AND existing.ended_at > start-tol
        #
        # v0.8.17 — normalise the bounds the same way the rows are
        # written. Rows now go in as local ISO (`_iso_local`) while the
        # recovery sweep hands this method the recorder's UTC stamps, so
        # a bare .isoformat() compared '…+02:00' rows against '…+00:00'
        # bounds as TEXT and the guard stopped matching the very rows the
        # sweep had just inserted — every run re-inserted them.
        lo = _iso_local(start - timedelta(seconds=tolerance_s))
        hi = _iso_local(end + timedelta(seconds=tolerance_s))
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM trips
                WHERE started_at <= ? AND ended_at >= ?
                LIMIT 1
                """,
                (hi, lo),
            ).fetchone()
        return row is not None

    async def async_charges_in_window(
        self, since: datetime, until: datetime
    ) -> dict[str, float | int]:
        """Sum kWh of charges that ENDED within [since, until]. Used at
        trip close to attribute pre-/intra-trip charging energy to the
        trip record so SoC deltas can be interpreted correctly when a
        charge happened in the middle (rare with v0.5.18 mutex, common
        for journey-level analyses).
        """
        return await self._hass.async_add_executor_job(
            self._charges_in_window, since, until
        )

    def _charges_in_window(
        self, since: datetime, until: datetime
    ) -> dict[str, float | int]:
        # Normalise the bounds the same way the rows are written. Rows go
        # in as local ISO (`_iso_local`) and this compares `ended_at` as
        # TEXT, so a bare .isoformat() from a UTC caller — the recovery
        # sweep takes its stamps straight from the recorder — compares
        # '…+00:00' bounds against '…+02:00' rows and matches NOTHING.
        # Same defect v0.8.17 fixed in `_trip_overlaps`; it was never
        # applied here, so this query silently returned zero for every
        # UTC-bounded caller.
        #
        # The upper bound also carries the boundary tolerance, so a charge
        # that ended as the trip opened counts as "before" instead of
        # falling between the two buckets (see
        # `_CHARGE_TRIP_BOUNDARY_TOLERANCE_S`).
        lo = _iso_local(since)
        hi = _iso_local(
            until + timedelta(seconds=_CHARGE_TRIP_BOUNDARY_TOLERANCE_S)
        )
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(kwh), 0) AS kwh,
                       COUNT(*) AS count
                FROM charges
                WHERE ended_at >= ? AND ended_at <= ?
                """,
                (lo, hi),
            ).fetchone()
        return {"kwh": float(row[0] or 0), "count": int(row[1] or 0)}

    async def async_charges_attributable_to_trip(
        self,
        start: datetime,
        end: datetime,
        trip_soc_start: float | None,
    ) -> float:
        """kWh delivered INSIDE a trip's window, apportioned by SoC.

        v0.8.18 — `async_charges_in_window` answers "which sessions
        ended in here", which is the right set but the wrong quantity
        when a session was already delivering as the window opened. The
        v0.8.17 mid-trip-charge correction credited the whole session,
        so a trip that opened mid-charge (67 %) while the cable ran on to
        96 % was credited all 66.83 kWh and reported 78 kWh/100km over
        74 km — physically impossible.

        Only the part delivered after the window opened belongs to it,
        and SoC is the meter that says how much:

            kwh x (soc_end - trip_soc_start) / (soc_end - soc_start)

        A session fully inside the window contributes all of its energy.
        One that straddles the opening without the SoC readings needed to
        apportion it contributes nothing — a guess here lands straight in
        the trip's consumption and cost.
        """
        return await self._hass.async_add_executor_job(
            self._charges_attributable_to_trip, start, end, trip_soc_start,
        )

    def _charges_attributable_to_trip(
        self,
        start: datetime,
        end: datetime,
        trip_soc_start: float | None,
    ) -> float:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT started_at, ended_at, kwh, soc_start, soc_end "
                "FROM charges"
            ).fetchall()
        total = 0.0
        for r in rows:
            c_end = _parse_stored(r["ended_at"])
            if c_end is None or not (start <= c_end <= end):
                continue
            kwh = float(r["kwh"] or 0.0)
            if kwh <= 0:
                continue
            c_start = _parse_stored(r["started_at"]) or c_end
            if c_start >= start:
                total += kwh
                continue
            # Started before the window AND finished within the boundary
            # tolerance of its opening: it delivered nothing inside. The
            # SoC apportion cannot see this, because it trusts the trip's
            # `soc_start` — and when that anchor is wrong (a re-anchor, a
            # late cloud sample) it invents energy for the trip out of a
            # session that had already stopped. Time settles it first.
            if c_end <= start + timedelta(
                seconds=_CHARGE_TRIP_BOUNDARY_TOLERANCE_S
            ):
                continue
            total += _apportion_straddling_charge(
                kwh,
                soc_start=r["soc_start"],
                soc_end=r["soc_end"],
                trip_soc_start=trip_soc_start,
            )
        return round(total, 3)

    async def async_update_trip(
        self, trip_id: int, fields: dict[str, Any]
    ) -> TripRecord | None:
        """Generic trip patch: pass {"origin": "home", "kwh": 12.5, ...}.

        Whitelisted columns only. Datetime values may be passed as
        `datetime` objects OR as ISO strings — both are normalised to
        ISO for storage. Returns the freshly-loaded TripRecord, or None
        if the trip_id doesn't exist.
        """
        return await self._hass.async_add_executor_job(
            self._update_trip, trip_id, fields,
        )

    def _update_trip(
        self, trip_id: int, fields: dict[str, Any]
    ) -> TripRecord | None:
        clean: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in self._TRIP_USER_EDITABLE or v is None:
                continue
            if isinstance(v, datetime):
                clean[k] = _iso_local(v)
            else:
                clean[k] = v
        if not clean:
            return None
        cols = ", ".join(f"{k} = ?" for k in clean)
        params = [*list(clean.values()), trip_id]
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(f"UPDATE trips SET {cols} WHERE id = ?", params)
            if not cur.rowcount:
                return None
            row = conn.execute(
                "SELECT * FROM trips WHERE id = ?", (trip_id,),
            ).fetchone()
        return _row_to_record(row) if row else None

    async def async_fix_avg_speed_outliers(self, margin: float = 1.05) -> int:
        """Null out avg_speed_kmh where it exceeds max_speed_kmh.

        avg_speed_kmh > max_speed_kmh is physically impossible — the max
        is a running ceiling sampled over the exact same window the
        average covers. `_async_close_trip` guards against this at close
        time since v0.8.3 (e.g. the stale-odometer-anchor bug inflating
        distance with km driven before `started_at`), but rows persisted
        before that fix need a one-time backfill. `_update_trip` can't be
        reused here — it silently skips any field passed as `None`, by
        design, so it can never NULL a column. `margin` (default 5%)
        absorbs rounding / sampling-cadence gaps, not genuine corruption.
        Returns the number of rows fixed.
        """
        return await self._hass.async_add_executor_job(
            self._fix_avg_speed_outliers, margin,
        )

    def _fix_avg_speed_outliers(self, margin: float) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE trips SET avg_speed_kmh = NULL
                WHERE avg_speed_kmh IS NOT NULL
                  AND max_speed_kmh IS NOT NULL
                  AND avg_speed_kmh > max_speed_kmh * ?
                """,
                (margin,),
            )
            return cur.rowcount

    async def async_purge_trips(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> int:
        """Delete trips in [since, until]. Either bound may be None for open-ended.

        Returns the number of rows deleted. The matching index is `started_at`,
        consistent with how every other range query in this storage works.
        """
        return await self._hass.async_add_executor_job(
            self._purge_trips, since, until
        )

    def _purge_trips(
        self, since: datetime | None, until: datetime | None
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("started_at <= ?")
            params.append(until.isoformat())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            cur = conn.execute(f"DELETE FROM trips{where}", params)
            return int(cur.rowcount or 0)

    async def async_delete_last(self) -> bool:
        """Drop the most recent trip; returns True if anything was deleted."""
        return await self._hass.async_add_executor_job(self._delete_last)

    def _delete_last(self) -> bool:
        with self._connect() as conn:
            cur = conn.execute("SELECT id FROM trips ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM trips WHERE id = ?", (row[0],))
            return True

    async def async_get_last(self) -> TripRecord | None:
        """Return the most recent trip, if any."""
        return await self._hass.async_add_executor_job(self._get_last)

    def _get_last(self) -> TripRecord | None:
        # Chronologically newest, NOT highest id: a manual backfill or a
        # recovery insert can add an OLDER trip with a higher rowid, and
        # the journey/SoC state machines key off last_trip.ended_at.
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM trips ORDER BY ended_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return _row_to_record(row) if row else None

    async def async_next_journey_id(self) -> int:
        return await self._hass.async_add_executor_job(self._next_journey_id)

    def _next_journey_id(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(journey_id), 0) + 1 FROM trips"
            ).fetchone()
        return int(row[0])

    async def async_journey_stages(self, journey_id: int) -> list[TripRecord]:
        return await self._hass.async_add_executor_job(self._journey_stages, journey_id)

    def _journey_stages(self, journey_id: int) -> list[TripRecord]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trips WHERE journey_id = ? ORDER BY id",
                (journey_id,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    async def async_journey_summary(self, journey_id: int) -> dict[str, Any] | None:
        return await self._hass.async_add_executor_job(self._journey_summary, journey_id)

    def _journey_summary(self, journey_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    MIN(started_at) AS started_at,
                    MAX(ended_at)   AS ended_at,
                    COALESCE(SUM(distance_km), 0) AS distance,
                    COALESCE(SUM(energy_kwh), 0)  AS energy,
                    COALESCE(SUM(cost), 0)        AS cost,
                    SUM(cost_lifo)                AS cost_lifo,
                    COUNT(*) AS stages
                FROM trips WHERE journey_id = ?
                """,
                (journey_id,),
            ).fetchone()
        if not row or row[6] == 0:
            return None
        started_at, ended_at, distance, energy, cost, cost_lifo, stages = row
        return {
            "journey_id": journey_id,
            "started_at": datetime.fromisoformat(started_at) if started_at else None,
            "ended_at": datetime.fromisoformat(ended_at) if ended_at else None,
            "distance_km": float(distance),
            "energy_kwh": float(energy),
            "cost": float(cost),
            # v0.8.22 — SUM, not COALESCE(...,0): a journey whose stages
            # have no per-lot figure must read None, not a free journey.
            "cost_lifo": float(cost_lifo) if cost_lifo is not None else None,
            "stages": int(stages),
        }

    async def async_recent_completed_journeys(
        self, current_journey_id: int | None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return summaries of recent journeys, excluding the in-progress one."""
        return await self._hass.async_add_executor_job(
            self._recent_completed_journeys, current_journey_id, limit
        )

    def _recent_completed_journeys(
        self, current_journey_id: int | None, limit: int
    ) -> list[dict[str, Any]]:
        # v0.5.47 — single aggregated query. The old shape fetched the
        # journey-id list and then ran one _journey_summary query PER
        # journey (N+1): with the default limit that was up to 51
        # connections per trip-close event.
        excl = ""
        params: tuple = ()
        if current_journey_id is not None:
            excl = "AND journey_id != ?"
            params = (current_journey_id,)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    journey_id,
                    MIN(started_at) AS started_at,
                    MAX(ended_at)   AS ended_at,
                    COALESCE(SUM(distance_km), 0) AS distance,
                    COALESCE(SUM(energy_kwh), 0)  AS energy,
                    COALESCE(SUM(cost), 0)        AS cost,
                    SUM(cost_lifo)                AS cost_lifo,
                    COUNT(*) AS stages
                FROM trips
                WHERE journey_id IS NOT NULL {excl}
                GROUP BY journey_id
                ORDER BY MAX(ended_at) DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [
            {
                "journey_id": int(r[0]),
                "started_at": datetime.fromisoformat(r[1]) if r[1] else None,
                "ended_at": datetime.fromisoformat(r[2]) if r[2] else None,
                "distance_km": float(r[3]),
                "energy_kwh": float(r[4]),
                "cost": float(r[5]),
                # v0.8.22 — None rather than 0.0 when no stage carries a
                # per-lot figure; a journey that cost nothing and one the
                # replay never priced must not read the same.
                "cost_lifo": float(r[6]) if r[6] is not None else None,
                "stages": int(r[7]),
            }
            for r in rows
        ]

    async def async_avg_consumption_kwh_per_100km(self) -> float | None:
        """Distance-weighted recent average kWh/100km across all trips.

        Used by `_async_close_trip` as an inline fallback when both
        SoC delta and power integration come back empty (the BYD
        integer-step + cloud-polling combo guarantees this for short
        trips). Without an inline estimate, the trip would persist
        with NULL energy/consumption/cost and the user would only see
        them fill on the next HA restart (via _recompute_trip_costs).
        Returns None when there is no historical data yet.
        """
        return await self._hass.async_add_executor_job(
            self._avg_consumption_kwh_per_100km
        )

    def _avg_consumption_kwh_per_100km(self) -> float | None:
        # v0.5.47 — exclude rows whose energy was itself ESTIMATED from
        # this average: feeding estimates back into the baseline freezes
        # it (real consumption shifts get diluted by prior estimates).
        # Measured rows (soc, power_integration, legacy NULL) all count.
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(energy_kwh), 0) AS total_kwh,
                    COALESCE(SUM(distance_km), 0) AS total_km
                FROM trips
                WHERE energy_kwh IS NOT NULL AND energy_kwh > 0
                  AND distance_km IS NOT NULL AND distance_km > 0
                  AND (energy_source IS NULL OR energy_source != 'estimated')
                """
            ).fetchone()
        total_kwh, total_km = row[0], row[1]
        if not total_km:
            return None
        return float(total_kwh) / float(total_km) * 100.0

    async def async_absorb_orphans_into_journey(
        self, journey_id: int, home_zone: str,
        secondary_home_zones: Sequence[str] | None = None,
    ) -> int:
        """Retro-assign journey_id to orphan trips since the last home arrival.

        Issue #5: a "journey" is the sequence of trips between leaving
        home and returning home. With cloud-polled trackers, individual
        trips can land with origin/destination = `not_home` (geofence
        noise), so the journey state machine never opens an id and the
        legs come out as journey_id=NULL.

        When a trip finally arrives home, we mint a journey for it via
        v0.5.16 auto-stitch — but that produces a 1-stage journey. This
        helper walks back to the most recent home arrival BEFORE this
        trip (or the start of history) and re-stamps every orphan
        (journey_id IS NULL) in that window with the new id, so the
        whole casa→…→casa chain ends up grouped.

        `secondary_home_zones` (v0.8.10): second house / holiday home
        labels count as a home arrival too when bounding the window.

        Returns the number of trips updated (excluding the one already
        carrying `journey_id`).
        """
        return await self._hass.async_add_executor_job(
            self._absorb_orphans_into_journey, journey_id, home_zone,
            secondary_home_zones,
        )

    def _absorb_orphans_into_journey(
        self, journey_id: int, home_zone: str,
        secondary_home_zones: Sequence[str] | None = None,
    ) -> int:
        slugs = [(home_zone or "home").strip().casefold()]
        slugs += [s.strip().casefold() for s in (secondary_home_zones or []) if s]
        placeholders = ",".join("?" for _ in slugs)
        with self._connect() as conn:
            # The "window start" is the most recent trip BEFORE this
            # journey's leg whose destination was home. If there's no
            # such trip, the window starts at the beginning of history.
            row = conn.execute(
                f"""
                SELECT COALESCE(MAX(id), 0) FROM trips
                WHERE LOWER(destination) IN ({placeholders})
                  AND id < (
                      SELECT MIN(id) FROM trips WHERE journey_id = ?
                  )
                """,
                (*slugs, journey_id),
            ).fetchone()
            anchor_id = int(row[0]) if row and row[0] else 0
            cur = conn.execute(
                """
                UPDATE trips SET journey_id = ?
                WHERE journey_id IS NULL
                  AND id > ?
                  AND id < (
                      SELECT MIN(id) FROM trips WHERE journey_id = ?
                  )
                """,
                (journey_id, anchor_id, journey_id),
            )
            return int(cur.rowcount or 0)

    async def async_resolve_open_journey_id(
        self, home_zone: str, secondary_home_zones: Sequence[str] | None = None
    ) -> int | None:
        """Return the journey_id of the currently-open journey, if any.

        Derives from the actual trip history rather than caching state
        in memory. An open journey is the first journey-tagged trip
        whose id is greater than the id of the most recent trip ending
        at home. If no such trip exists (every journey has been closed
        by a subsequent home arrival), returns None.

        Comparison is case-insensitive against `home_zone` (the
        configured device_tracker home slug, e.g. `home`) OR any of
        `secondary_home_zones` (v0.8.10 — second house / holiday home
        labels, which close a journey exactly like the primary home).
        """
        return await self._hass.async_add_executor_job(
            self._resolve_open_journey_id, home_zone, secondary_home_zones
        )

    def _resolve_open_journey_id(
        self, home_zone: str, secondary_home_zones: Sequence[str] | None = None
    ) -> int | None:
        slugs = [(home_zone or "home").strip().casefold()]
        slugs += [s.strip().casefold() for s in (secondary_home_zones or []) if s]
        placeholders = ",".join("?" for _ in slugs)
        with self._connect() as conn:
            # v0.5.16 — DESC: if storage somehow has multiple distinct
            # journey_ids past the last home-arrival (a partial purge,
            # crash-mid-close, or a wrongly minted id), pick the NEWEST
            # one as the open journey. Picking the oldest accreted new
            # stages into a stale journey. Log a warning when ambiguity
            # is observed so the inconsistency is visible.
            rows = conn.execute(
                f"""
                SELECT DISTINCT journey_id FROM trips
                WHERE journey_id IS NOT NULL
                  AND id > COALESCE(
                      (SELECT MAX(id) FROM trips
                       WHERE LOWER(destination) IN ({placeholders})),
                      0)
                """,
                slugs,
            ).fetchall()
            if not rows:
                return None
            if len(rows) > 1:
                _LOGGER.warning(
                    "Multiple open journey_ids past last home-arrival: %s — "
                    "picking newest. Storage may need a manual repair.",
                    sorted(int(r[0]) for r in rows),
                )
            # v0.8.25 — `IN (…)` over every home slug, matching the guard
            # query above. v0.8.10 turned the single `slug` into `slugs`
            # for secondary homes and updated the first query only; this
            # one kept `= ?` bound to a name that no longer existed and
            # raised NameError on every call that got this far — which is
            # every call that found an open journey, i.e. exactly the ones
            # whose answer mattered. Trips then closed with journey_id
            # NULL and no journey was ever resolved.
            row = conn.execute(
                f"""
                SELECT journey_id FROM trips
                WHERE journey_id IS NOT NULL
                  AND id > COALESCE(
                      (SELECT MAX(id) FROM trips
                       WHERE LOWER(destination) IN ({placeholders})),
                      0)
                ORDER BY id DESC
                LIMIT 1
                """,
                slugs,
            ).fetchone()
        return int(row[0]) if row else None

    async def async_last_completed_journey_id(
        self, current_journey_id: int | None
    ) -> int | None:
        return await self._hass.async_add_executor_job(
            self._last_completed_journey_id, current_journey_id
        )

    def _last_completed_journey_id(
        self, current_journey_id: int | None
    ) -> int | None:
        """Most recent journey id that isn't the in-progress one."""
        excl = ""
        params: tuple = ()
        if current_journey_id is not None:
            excl = "AND journey_id != ?"
            params = (current_journey_id,)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT journey_id FROM trips
                WHERE journey_id IS NOT NULL {excl}
                GROUP BY journey_id
                ORDER BY MAX(ended_at) DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return int(row[0]) if row else None

    async def async_recent_trips(self, limit: int = 10) -> list[TripRecord]:
        return await self._hass.async_add_executor_job(self._recent_trips, limit)

    def _recent_trips(self, limit: int) -> list[TripRecord]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            # v0.8.13 — order by ended_at, NOT id. `recover_missing_trips`/
            # `log_manual_trip` insert historical rows that get a fresh
            # (high) autoincrement id despite an old `ended_at` — ordering
            # by id let a bulk backfill bump genuinely recent live trips
            # clean out of this window. `ended_at DESC` matches the
            # convention already used by `async_get_last`.
            rows = conn.execute(
                "SELECT * FROM trips ORDER BY ended_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    async def async_records(self) -> dict[str, Any] | None:
        """All-time best trips + lifetime totals (None when no trips yet)."""
        return await self._hass.async_add_executor_job(self._records)

    def _records(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            tot = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(distance_km), 0) AS d, "
                "COALESCE(SUM(energy_kwh), 0) AS e, COALESCE(SUM(cost), 0) AS k, "
                "COALESCE(SUM(regen_kwh), 0) AS r "
                "FROM trips"
            ).fetchone()
            if not tot or tot["c"] == 0:
                return None
            # `score` is a strictly-decreasing function of consumption, so the
            # best-scoring trip is the most efficient one — a single indexed
            # lookup instead of scoring every row. Tie-break by longer distance.
            efficient = conn.execute(
                "SELECT * FROM trips WHERE consumption_kwh_100km IS NOT NULL "
                "AND consumption_kwh_100km > 0 "
                "ORDER BY consumption_kwh_100km ASC, distance_km DESC LIMIT 1"
            ).fetchone()
            longest = conn.execute(
                "SELECT * FROM trips WHERE distance_km IS NOT NULL "
                "ORDER BY distance_km DESC LIMIT 1"
            ).fetchone()
            cheapest = conn.execute(
                "SELECT * FROM trips WHERE cost IS NOT NULL "
                "ORDER BY cost ASC LIMIT 1"
            ).fetchone()
        return {
            "count": int(tot["c"]),
            "totals": {
                "trips": int(tot["c"]),
                "distance_km": round(float(tot["d"]), 1),
                "energy_kwh": round(float(tot["e"]), 2),
                "cost": round(float(tot["k"]), 2),
                "regen_kwh": round(float(tot["r"]), 2),
            },
            "most_efficient": _row_to_record(efficient) if efficient else None,
            "longest": _row_to_record(longest) if longest else None,
            "cheapest": _row_to_record(cheapest) if cheapest else None,
        }

    async def async_effective_capacity_kwh(
        self,
        *,
        min_delta_pct: float = 30.0,
        min_charges: int = 5,
        window: int = 30,
        min_kwh: float = 5.0,
        temp_min_c: float | None = 5.0,
        temp_max_c: float | None = 35.0,
    ) -> tuple[float | None, int, dict[str, int]]:
        """v0.5.51 — derive effective pack capacity from real charges.

        Returns `(median_kwh, n_used, reject_counts)`. Each eligible
        charge yields a sample `kwh / (soc_end - soc_start) × 100`.

        Gates:
          * `soc_start`, `soc_end`, `kwh` all populated
          * `(soc_end - soc_start) >= min_delta_pct` — keeps SoC
            quantization noise (±1 %) from dominating
          * `kwh >= min_kwh` — Tessie threshold (>5 kWh by default);
            BMS-derived measurements jitter wildly on tiny top-ups.
          * `temperature_c` in `[temp_min_c, temp_max_c]` when present
            — packs reserve a bigger buffer at <5 °C and derate at
            >35 °C, both noise sources for capacity samples. Rows
            without a temperature reading bypass the gate (we don't
            penalise users without an exterior-temp sensor wired).

        Aggregates the median of the last `window` eligible charges
        (median is more robust than mean when one charge had a partially
        depleted state-of-charge reading). Returns `(None, n_used, …)`
        when n_used < min_charges and the caller should keep the
        declared capacity. `reject_counts` exposes per-gate drop
        reasons for the SoH sensor's `extra_state_attributes` so the
        UI can show "9 charges considered, 4 used, 5 rejected (3 too
        small, 2 cold)".
        """
        return await self._hass.async_add_executor_job(
            self._effective_capacity_kwh,
            min_delta_pct, min_charges, window,
            min_kwh, temp_min_c, temp_max_c,
        )

    def _effective_capacity_kwh(
        self,
        min_delta_pct: float,
        min_charges: int,
        window: int,
        min_kwh: float,
        temp_min_c: float | None,
        temp_max_c: float | None,
    ) -> tuple[float | None, int, dict[str, int]]:
        # v0.8.14 — try grounded-only samples first (charges whose kwh
        # came from the power-integration measurement, not the SoC-delta
        # guess this calibration exists to correct — the original query
        # was circular, since most of its input WAS that same guess).
        # Only fall back to every eligible charge (pre-v0.8.14 behaviour)
        # when there aren't yet enough grounded ones, so existing users
        # don't lose calibration on upgrade while it accrues.
        grounded = self._effective_capacity_kwh_query(
            min_delta_pct, window, min_kwh, temp_min_c, temp_max_c,
            energy_source="power_integration",
        )
        if grounded[1] >= min_charges:
            return grounded
        median, n, rejects = self._effective_capacity_kwh_query(
            min_delta_pct, window, min_kwh, temp_min_c, temp_max_c,
            energy_source=None,
        )
        if n < min_charges:
            return (None, n, rejects)
        return (median, n, rejects)

    def _effective_capacity_kwh_query(
        self,
        min_delta_pct: float,
        window: int,
        min_kwh: float,
        temp_min_c: float | None,
        temp_max_c: float | None,
        *,
        energy_source: str | None,
    ) -> tuple[float | None, int, dict[str, int]]:
        source_clause = " AND energy_source = ? " if energy_source else ""
        params: tuple[Any, ...] = (
            (min_delta_pct, energy_source, window)
            if energy_source else (min_delta_pct, window)
        )
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kwh, soc_start, soc_end, temperature_c FROM charges "
                "WHERE kwh IS NOT NULL AND kwh > 0 "
                "  AND soc_start IS NOT NULL AND soc_end IS NOT NULL "
                "  AND (soc_end - soc_start) >= ? "
                + source_clause +
                # v0.8.13 — chronological order, not insertion id (a
                # backfilled/reconstructed charge gets a fresh high id
                # despite an old ended_at and would otherwise crowd out
                # genuinely recent charges from this capped window).
                "ORDER BY ended_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        samples: list[float] = []
        rejects = {
            "kwh_too_small": 0,
            "temp_cold": 0,
            "temp_hot": 0,
            "bad_soc_delta": 0,
        }
        for kwh, s0, s1, temp in rows:
            try:
                kwh_f = float(kwh)
                delta = float(s1) - float(s0)
            except (TypeError, ValueError):
                continue
            if delta <= 0:
                rejects["bad_soc_delta"] += 1
                continue
            # v0.6.5 — Tessie threshold: tiny top-ups are noise.
            if kwh_f < min_kwh:
                rejects["kwh_too_small"] += 1
                continue
            # v0.6.5 — temperature window. Rows without a reading
            # bypass the gate (treat as "unknown, assume normal").
            if temp is not None:
                try:
                    t_f = float(temp)
                    if temp_min_c is not None and t_f < temp_min_c:
                        rejects["temp_cold"] += 1
                        continue
                    if temp_max_c is not None and t_f > temp_max_c:
                        rejects["temp_hot"] += 1
                        continue
                except (TypeError, ValueError):
                    pass
            samples.append(kwh_f / delta * 100.0)
        n = len(samples)
        if n < 1:
            return (None, n, rejects)
        samples.sort()
        mid = n // 2
        median = (
            samples[mid] if n % 2 == 1
            else (samples[mid - 1] + samples[mid]) / 2.0
        )
        return (median, n, rejects)

    async def async_avg_charging_efficiency_pct(
        self,
        *,
        window: int = 30,
        min_charges: int = 3,
    ) -> tuple[float | None, int]:
        """v0.5.90 — rolling-median AC→DC charging efficiency.

        Each charge row with both `kwh` (battery input) and
        `evse_energy_kwh` (charger output) contributes one sample:
        kwh / evse_energy_kwh × 100. Median over the last `window`
        eligible charges; (None, n) when n < `min_charges`. The
        median is more robust than a mean when one DCFC session
        skews the distribution.
        """
        return await self._hass.async_add_executor_job(
            self._avg_charging_efficiency_pct, window, min_charges,
        )

    def _avg_charging_efficiency_pct(
        self, window: int, min_charges: int,
    ) -> tuple[float | None, int]:
        with self._connect() as conn:
            rows = conn.execute(
                # v0.8.17 — chronological, not insertion order. The
                # v0.8.13/v0.8.14 sweep fixed this for recent_charges,
                # recent_trips and the capacity query but missed here:
                # importing historical charges via `log_charge` gives
                # them ids above every live row, so this "recent N"
                # window would be computed entirely from the import.
                # v0.8.29 — the band, not just `> 0`. An impossible row
                # (kwh from the SoC estimate against a real meter can
                # read 156 %) used to both enter the median and consume
                # one of the `window` slots, pushing a good sample out.
                "SELECT charging_efficiency_pct FROM charges "
                "WHERE charging_efficiency_pct IS NOT NULL "
                "  AND charging_efficiency_pct >= ? "
                "  AND charging_efficiency_pct <= ? "
                "ORDER BY ended_at DESC, id DESC LIMIT ?",
                (_EFFICIENCY_MIN_PCT, _EFFICIENCY_MAX_PCT, window),
            ).fetchall()
        samples = [float(r[0]) for r in rows if r[0] is not None]
        n = len(samples)
        if n < min_charges:
            return (None, n)
        samples.sort()
        mid = n // 2
        median = (
            samples[mid] if n % 2 == 1
            else (samples[mid - 1] + samples[mid]) / 2.0
        )
        return (round(median, 2), n)

    async def async_calibration_factor_k_median(
        self,
        *,
        window: int = 30,
        min_trips: int = 5,
    ) -> tuple[float | None, int]:
        """v0.5.84 — rolling-median per-trip battery calibration factor.

        Each trip stores `calibration_factor_k = net_power_kwh /
        (soc_delta_pct/100 × nominal_capacity_kwh)`. Aggregating the
        median over the last `window` non-NULL trips smooths individual-
        trip noise (sampling gaps in power integration) and surfaces a
        proxy for real battery degradation. K ≈ 1.0 → capacity matches
        nominal; persistent drift toward K < 1.0 → effective capacity
        has dropped.

        Returns `(median_k, n_samples)`. `(None, n)` when n < min_trips.
        """
        return await self._hass.async_add_executor_job(
            self._calibration_factor_k_median, window, min_trips,
        )

    def _calibration_factor_k_median(
        self, window: int, min_trips: int
    ) -> tuple[float | None, int]:
        with self._connect() as conn:
            rows = conn.execute(
                # v0.8.17 — chronological, not insertion order (same
                # class of bug as above; a recovery sweep backfilling
                # old trips would otherwise dominate this window).
                "SELECT calibration_factor_k FROM trips "
                "WHERE calibration_factor_k IS NOT NULL "
                "  AND calibration_factor_k > 0 "
                "ORDER BY ended_at DESC, id DESC LIMIT ?",
                (window,),
            ).fetchall()
        samples = [float(r[0]) for r in rows if r[0] is not None]
        n = len(samples)
        if n < min_trips:
            return (None, n)
        samples.sort()
        mid = n // 2
        median = (
            samples[mid] if n % 2 == 1
            else (samples[mid - 1] + samples[mid]) / 2.0
        )
        return (median, n)

    async def async_score_baseline_p5(
        self, *, min_distance_km: float = 5.0, min_trips: int = 10
    ) -> tuple[float | None, int]:
        """v0.5.50 — return (p5_consumption_kwh_100km, eligible_trip_count).

        Used by the coordinator to derive a per-car score baseline. Returns
        `(None, n)` when fewer than `min_trips` eligible trips exist (the
        caller falls back to the 14.5 default). Eligibility filters out
        sub-`min_distance_km` jaunts whose consumption is dominated by warm-
        up and standby drain, and clamps to a sane physical band 5–50 to
        suppress sensor errors.
        """
        return await self._hass.async_add_executor_job(
            self._score_baseline_p5, min_distance_km, min_trips
        )

    def _score_baseline_p5(
        self, min_distance_km: float, min_trips: int
    ) -> tuple[float | None, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT consumption_kwh_100km FROM trips "
                "WHERE consumption_kwh_100km IS NOT NULL "
                "  AND consumption_kwh_100km BETWEEN 5 AND 50 "
                "  AND distance_km IS NOT NULL "
                "  AND distance_km >= ? "
                "ORDER BY consumption_kwh_100km ASC",
                (min_distance_km,),
            ).fetchall()
        values = [float(r[0]) for r in rows]
        n = len(values)
        if n < min_trips:
            return (None, n)
        # P5: pick the value at index floor(0.05 * (n - 1)) of the sorted
        # ascending list. For n=10 → idx=0 (best ever); for n=20 → idx=0;
        # n=40 → idx=1. The "best-but-not-fluke" tail.
        idx = int(0.05 * (n - 1))
        return (values[idx], n)

    async def async_recent_charges(self, limit: int = 10) -> list[ChargeRecord]:
        return await self._hass.async_add_executor_job(self._recent_charges, limit)

    def _recent_charges(self, limit: int) -> list[ChargeRecord]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            # v0.8.13 — same fix as _recent_trips: order by ended_at, not
            # id, so a historical backfill can't bump genuinely recent
            # charges out of this window.
            rows = conn.execute(
                "SELECT * FROM charges ORDER BY ended_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_charge(r) for r in rows]

    async def async_aggregates_since(self, since: datetime) -> dict[str, float | int]:
        """Aggregate distance / energy / cost / count from `since`."""
        return await self._hass.async_add_executor_job(self._aggregates_since, since)

    def _aggregates_since(self, since: datetime) -> dict[str, float | int]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(distance_km), 0) AS distance,
                    COALESCE(SUM(energy_kwh), 0) AS energy,
                    COALESCE(SUM(cost), 0) AS cost,
                    COALESCE(SUM(regen_kwh), 0) AS regen,
                    COALESCE(SUM(discharge_kwh), 0) AS discharge,
                    COUNT(*) AS count,
                    -- v0.8.17 — the consumption ratio needs the same
                    -- subset on both sides. SUM(energy_kwh) skips rows
                    -- whose energy is NULL while SUM(distance_km) keeps
                    -- their kilometres, so one 100 km trip with no
                    -- energy reading dragged a true 15.0 kWh/100km down
                    -- to 13.3 — and that figure drives the remaining-
                    -- range estimate, which then read 495 km instead of
                    -- 440. Same shape for regen_ratio below.
                    COALESCE(SUM(CASE WHEN energy_kwh IS NOT NULL
                        AND energy_kwh > 0 AND distance_km IS NOT NULL
                        AND distance_km > 0 THEN distance_km END), 0)
                        AS measured_distance,
                    COALESCE(SUM(CASE WHEN energy_kwh IS NOT NULL
                        AND energy_kwh > 0 AND distance_km IS NOT NULL
                        AND distance_km > 0 THEN energy_kwh END), 0)
                        AS measured_energy,
                    COALESCE(SUM(CASE WHEN regen_kwh IS NOT NULL
                        AND discharge_kwh IS NOT NULL AND discharge_kwh > 0
                        THEN regen_kwh END), 0) AS paired_regen,
                    COALESCE(SUM(CASE WHEN regen_kwh IS NOT NULL
                        AND discharge_kwh IS NOT NULL AND discharge_kwh > 0
                        THEN discharge_kwh END), 0) AS paired_discharge
                FROM trips WHERE started_at >= ?
                """,
                (since.isoformat(),),
            ).fetchone()
        (
            distance, energy, cost, regen, discharge, count,
            measured_distance, measured_energy,
            paired_regen, paired_discharge,
        ) = row
        avg_consumption = (
            (measured_energy / measured_distance * 100)
            if measured_distance else 0
        )
        # v0.6.0: regen_ratio = recovered / gross_discharge. Mirrors
        # the EV-app convention so dashboards can render "you got 18 %
        # back from braking this month" without bespoke template math.
        regen_ratio = (
            (paired_regen / paired_discharge) if paired_discharge else 0.0
        )
        return {
            "distance_km": float(distance),
            "energy_kwh": float(energy),
            "cost": float(cost),
            "regen_kwh": float(regen),
            "discharge_kwh": float(discharge),
            "regen_ratio": float(regen_ratio),
            "count": int(count),
            "avg_consumption_kwh_100km": float(avg_consumption),
        }

    async def async_insert_charge(self, record: ChargeRecord) -> int:
        return await self._hass.async_add_executor_job(self._insert_charge, record)

    def _insert_charge(self, record: ChargeRecord) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO charges (
                    started_at, ended_at, kwh, price_per_kwh, total_cost,
                    currency, soc_start, soc_end, location, notes, is_dcfc,
                    evse_energy_kwh, charging_efficiency_pct,
                    peak_charge_power_kw, temperature_c, energy_source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _iso_local(record.started_at) if record.started_at else None,
                    _iso_local(record.ended_at),
                    record.kwh,
                    record.price_per_kwh,
                    record.total_cost,
                    record.currency,
                    record.soc_start,
                    record.soc_end,
                    record.location,
                    record.notes,
                    int(record.is_dcfc) if record.is_dcfc is not None else None,
                    record.evse_energy_kwh,
                    record.charging_efficiency_pct,
                    record.peak_charge_power_kw,
                    record.temperature_c,
                    record.energy_source,
                ),
            )
            return int(cur.lastrowid or 0)

    async def async_get_last_charge(self) -> ChargeRecord | None:
        return await self._hass.async_add_executor_job(self._get_last_charge)

    def _get_last_charge(self) -> ChargeRecord | None:
        # Chronologically newest — see _get_last for why id-order is wrong.
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM charges ORDER BY ended_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return _row_to_charge(row) if row else None

    async def async_get_charge_by_id(
        self, charge_id: int
    ) -> ChargeRecord | None:
        return await self._hass.async_add_executor_job(
            self._get_charge_by_id, charge_id,
        )

    def _get_charge_by_id(self, charge_id: int) -> ChargeRecord | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM charges WHERE id = ?", (charge_id,),
            ).fetchone()
        return _row_to_charge(row) if row else None

    async def async_update_charge_by_id(
        self,
        charge_id: int,
        *,
        price_per_kwh: float | None = None,
        total_cost: float | None = None,
        location: str | None = None,
        notes: str | None = None,
        evse_energy_kwh: float | None = None,
    ) -> ChargeRecord | None:
        """Update a specific charge by id. Same semantics as
        update_last_charge but targets the given row instead of the
        most-recent one. Used by set_last_charge_price when a `charge_id`
        argument is passed (so the user can correct any historical
        external charge, not just the latest).
        """
        return await self._hass.async_add_executor_job(
            self._update_charge_by_id, charge_id, price_per_kwh, total_cost,
            location, notes, evse_energy_kwh,
        )

    # Columns the user can correct via the extended set_charge service.
    # v0.5.95 — evse_energy_kwh added so the backfill_charge_evse service
    # (and manual corrections) can write the AC-side integral. Patching
    # this field auto-recomputes charging_efficiency_pct = kwh / evse × 100.
    # v0.6.0 — peak_charge_power_kw added so the user can correct
    # the SoH stress accumulator after the fact (e.g. when a session
    # ran with the EVSE sensor disconnected but the user knows it was DCFC).
    _CHARGE_USER_EDITABLE = frozenset({
        "started_at", "ended_at", "kwh", "soc_start", "soc_end",
        "location", "notes", "is_dcfc", "currency",
        "evse_energy_kwh", "peak_charge_power_kw", "temperature_c",
    })

    async def async_patch_charge(
        self, charge_id: int, fields: dict[str, Any]
    ) -> ChargeRecord | None:
        """Generic charge patch — same model as async_update_trip.

        Used by the set_charge service so the user can correct
        started_at/ended_at/soc_start/soc_end/kwh after the fact.
        kwh changes do NOT auto-recompute total_cost — that's
        async_update_charge_by_id's job and is invoked via
        set_last_charge_price.
        """
        return await self._hass.async_add_executor_job(
            self._patch_charge, charge_id, fields,
        )

    def _patch_charge(
        self, charge_id: int, fields: dict[str, Any]
    ) -> ChargeRecord | None:
        clean: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in self._CHARGE_USER_EDITABLE or v is None:
                continue
            if isinstance(v, datetime):
                clean[k] = _iso_local(v)
            elif k == "is_dcfc":
                clean[k] = 1 if bool(v) else 0
            else:
                clean[k] = v
        if not clean:
            return None
        # v0.8.14 — a hand-corrected kwh is no longer the value the
        # auto-detect measured (power integration or SoC math); tag it
        # 'manual' so a stale 'power_integration' label doesn't keep
        # claiming grounded-measurement status for the capacity
        # calibration (see _effective_capacity_kwh) after the user has
        # overridden it.
        if "kwh" in clean:
            clean["energy_source"] = "manual"
        cols = ", ".join(f"{k} = ?" for k in clean)
        params = [*list(clean.values()), charge_id]
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(f"UPDATE charges SET {cols} WHERE id = ?", params)
            if not cur.rowcount:
                return None
            # v0.8.17 — the money paid is an INPUT, not a derived value.
            # This used to recompute `total_cost = kwh × price`, so
            # correcting a charge's kWh to the battery-side truth
            # silently rewrote a €20.00 invoice to €19.00. When a total
            # is on record we re-derive the €/kWh from it instead; only
            # when there is no total (or it was patched in this same
            # call) do we compute the total from the price.
            if "kwh" in clean:
                row = conn.execute(
                    "SELECT kwh, price_per_kwh, total_cost "
                    "FROM charges WHERE id = ?",
                    (charge_id,),
                ).fetchone()
                if row:
                    new_kwh = float(row["kwh"] or 0.0)
                    total = row["total_cost"]
                    if (
                        "total_cost" not in clean
                        and total is not None
                        and float(total) > 0
                        and new_kwh > 0
                    ):
                        conn.execute(
                            "UPDATE charges SET price_per_kwh = ? WHERE id = ?",
                            (float(total) / new_kwh, charge_id),
                        )
                    elif row["price_per_kwh"] is not None:
                        conn.execute(
                            "UPDATE charges SET total_cost = ? WHERE id = ?",
                            (new_kwh * float(row["price_per_kwh"]), charge_id),
                        )
            # v0.5.95 — if evse_energy_kwh was patched, recompute the
            # AC→DC efficiency from current kwh on row. Skip when evse is
            # zero / missing (efficiency undefined). When kwh was also
            # patched in the same call the SELECT above already reflects
            # both new values.
            if "evse_energy_kwh" in clean or "kwh" in clean:
                row = conn.execute(
                    "SELECT kwh, evse_energy_kwh FROM charges WHERE id = ?",
                    (charge_id,),
                ).fetchone()
                if (
                    row is not None
                    and row["evse_energy_kwh"] is not None
                    and float(row["evse_energy_kwh"]) > 0
                    and row["kwh"] is not None
                ):
                    eff = round(
                        float(row["kwh"]) / float(row["evse_energy_kwh"]) * 100.0,
                        1,
                    )
                    conn.execute(
                        "UPDATE charges SET charging_efficiency_pct = ? "
                        "WHERE id = ?",
                        (eff, charge_id),
                    )
            row = conn.execute(
                "SELECT * FROM charges WHERE id = ?", (charge_id,),
            ).fetchone()
        return _row_to_charge(row) if row else None

    def _update_charge_by_id(
        self,
        charge_id: int,
        price_per_kwh: float | None,
        total_cost: float | None,
        location: str | None,
        notes: str | None,
        evse_energy_kwh: float | None = None,
    ) -> ChargeRecord | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM charges WHERE id = ?", (charge_id,)
            ).fetchone()
            if not row:
                return None
            kwh = row["kwh"]
            if total_cost is not None:
                new_total = float(total_cost)
                new_price = new_total / kwh if kwh else 0.0
            elif price_per_kwh is not None:
                new_price = float(price_per_kwh)
                new_total = kwh * new_price
            else:
                new_price = row["price_per_kwh"]
                new_total = row["total_cost"]
            new_location = location if location is not None else row["location"]
            new_notes = notes if notes is not None else row["notes"]
            price_locked = (
                1 if (price_per_kwh is not None or total_cost is not None) else None
            )
            new_evse_kwh = (
                float(evse_energy_kwh)
                if evse_energy_kwh is not None
                else row["evse_energy_kwh"]
            )
            new_eff = (
                round(kwh / new_evse_kwh * 100.0, 1)
                if new_evse_kwh is not None and kwh and float(new_evse_kwh) > 0
                else row["charging_efficiency_pct"]
            )
            conn.execute(
                "UPDATE charges SET price_per_kwh = ?, total_cost = ?, "
                "location = ?, notes = ?, evse_energy_kwh = ?, "
                "charging_efficiency_pct = ?, "
                "price_locked = COALESCE(?, price_locked) WHERE id = ?",
                (
                    new_price, new_total, new_location, new_notes,
                    new_evse_kwh, new_eff, price_locked, charge_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM charges WHERE id = ?", (charge_id,)
            ).fetchone()
        return _row_to_charge(updated)

    async def async_update_last_charge(
        self,
        *,
        price_per_kwh: float | None = None,
        total_cost: float | None = None,
        location: str | None = None,
        notes: str | None = None,
        evse_energy_kwh: float | None = None,
    ) -> ChargeRecord | None:
        return await self._hass.async_add_executor_job(
            self._update_last_charge, price_per_kwh, total_cost, location, notes,
            evse_energy_kwh,
        )

    def _update_last_charge(
        self,
        price_per_kwh: float | None,
        total_cost: float | None,
        location: str | None,
        notes: str | None,
        evse_energy_kwh: float | None = None,
    ) -> ChargeRecord | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            # v0.8.14 — chronological order, not insertion id (see v0.8.13).
            row = conn.execute(
                "SELECT * FROM charges ORDER BY ended_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            kwh = row["kwh"]
            if total_cost is not None:
                new_total = float(total_cost)
                new_price = new_total / kwh if kwh else 0.0
            elif price_per_kwh is not None:
                new_price = float(price_per_kwh)
                new_total = kwh * new_price
            else:
                # Nothing to update on pricing — keep current values.
                new_price = row["price_per_kwh"]
                new_total = row["total_cost"]
            new_location = location if location is not None else row["location"]
            new_notes = notes if notes is not None else row["notes"]
            # If the caller passed a pricing field (including 0), lock the
            # price so auto-detect doesn't stomp the user's correction.
            price_locked = (
                1 if (price_per_kwh is not None or total_cost is not None) else None
            )
            # evse_energy_kwh from an invoice — same field/formula the home
            # EVSE-sensor integral fills; lets an away charge get an
            # efficiency chip too.
            new_evse_kwh = (
                float(evse_energy_kwh)
                if evse_energy_kwh is not None
                else row["evse_energy_kwh"]
            )
            new_eff = (
                round(kwh / new_evse_kwh * 100.0, 1)
                if new_evse_kwh is not None and kwh and float(new_evse_kwh) > 0
                else row["charging_efficiency_pct"]
            )
            conn.execute(
                "UPDATE charges SET price_per_kwh = ?, total_cost = ?, "
                "location = ?, notes = ?, evse_energy_kwh = ?, "
                "charging_efficiency_pct = ?, "
                "price_locked = COALESCE(?, price_locked) WHERE id = ?",
                (
                    new_price, new_total, new_location, new_notes,
                    new_evse_kwh, new_eff, price_locked, row["id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM charges WHERE id = ?", (row["id"],)
            ).fetchone()
        return _row_to_charge(updated)

    async def async_trips_missing_gps(self, limit: int = 50) -> list[dict[str, Any]]:
        """Trips with NULL start_lat AND NULL end_lat — for GPS backfill.

        Returns the newest matching rows first so a one-shot heal at
        startup is bounded; older rows are picked up on subsequent
        startups (the integration triggers this heal once per launch).
        """
        return await self._hass.async_add_executor_job(
            self._trips_missing_gps, limit
        )

    def _trips_missing_gps(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, started_at, ended_at
                FROM trips
                WHERE start_lat IS NULL AND end_lat IS NULL
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def async_update_trip_gps(
        self,
        trip_id: int,
        *,
        start_lat: float | None = None,
        start_lon: float | None = None,
        end_lat: float | None = None,
        end_lon: float | None = None,
    ) -> None:
        await self._hass.async_add_executor_job(
            self._update_trip_gps, trip_id,
            start_lat, start_lon, end_lat, end_lon,
        )

    def _update_trip_gps(
        self,
        trip_id: int,
        start_lat: float | None,
        start_lon: float | None,
        end_lat: float | None,
        end_lon: float | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trips SET
                    start_lat = COALESCE(?, start_lat),
                    start_lon = COALESCE(?, start_lon),
                    end_lat = COALESCE(?, end_lat),
                    end_lon = COALESCE(?, end_lon)
                WHERE id = ?
                """,
                (start_lat, start_lon, end_lat, end_lon, trip_id),
            )

    async def async_trips_needing_geocode(self, limit: int = 50) -> list[dict[str, Any]]:
        """Trips with GPS coords but no address yet — for the backfill."""
        return await self._hass.async_add_executor_job(self._trips_needing_geocode, limit)

    def _trips_needing_geocode(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            # v0.5.14 — one-shot heal of the v0.5.12-0.5.13 empty-string
            # poisoning: Nominatim sometimes returned an unrecognised
            # address shape, our extractor produced label="", COALESCE
            # persisted that empty string, the backfill query `IS NULL`
            # never picked them up again, and the dashboard's Jinja
            # `start_address or origin` evaluated "" as falsy → fell
            # through to `not_home`. Clear the empties so the backfill
            # can retry them with the fixed extractor.
            conn.execute(
                "UPDATE trips SET start_address = NULL WHERE start_address = ''"
            )
            conn.execute(
                "UPDATE trips SET end_address = NULL WHERE end_address = ''"
            )
            rows = conn.execute(
                """
                SELECT id, start_lat, start_lon, end_lat, end_lon,
                       start_address, end_address
                FROM trips
                WHERE ((start_lat IS NOT NULL AND
                        (start_address IS NULL OR start_address = ''))
                    OR (end_lat IS NOT NULL AND
                        (end_address IS NULL OR end_address = '')))
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def async_update_trip_addresses(
        self, trip_id: int,
        start_address: str | None = None,
        end_address: str | None = None,
    ) -> None:
        await self._hass.async_add_executor_job(
            self._update_trip_addresses, trip_id, start_address, end_address
        )

    def _update_trip_addresses(
        self, trip_id: int,
        start_address: str | None,
        end_address: str | None,
    ) -> None:
        # COALESCE: only set non-null fields, don't blank an existing one.
        with self._connect() as conn:
            conn.execute(
                "UPDATE trips SET "
                "start_address = COALESCE(?, start_address), "
                "end_address = COALESCE(?, end_address) "
                "WHERE id = ?",
                (start_address, end_address, trip_id),
            )

    async def async_recompute_energy_from_capacity(
        self, new_capacity_kwh: float
    ) -> int:
        """v0.5.51 — rewrite `energy_kwh` and `consumption_kwh_100km` for
        every trip that was originally SoC-derived, against the new
        battery capacity.

        Trips with `energy_source = 'power_integration'` are left alone:
        those were measured directly, not estimated from SoC drop.
        Trips with energy_source NULL or 'soc' or 'estimated' are
        rewritten as `soc_used_pct × new_capacity / 100`. The cost is
        re-derived from the same `energy_kwh × price_per_kwh` formula
        we use everywhere else (call
        `async_recompute_trip_costs_from_charges` afterwards to apply
        the user's home tariff).

        Returns the number of rows updated.
        """
        return await self._hass.async_add_executor_job(
            self._recompute_energy_from_capacity, float(new_capacity_kwh)
        )

    def _recompute_energy_from_capacity(self, new_capacity_kwh: float) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE trips SET
                    energy_kwh = ROUND(soc_used_pct * ? / 100.0, 4),
                    consumption_kwh_100km = CASE
                        WHEN consumption_kwh_100km IS NULL THEN NULL
                        ELSE ROUND(
                            soc_used_pct * ? / 100.0 / distance_km * 100.0, 4
                        )
                    END
                WHERE soc_used_pct IS NOT NULL AND soc_used_pct > 0
                  AND distance_km IS NOT NULL AND distance_km > 0
                  -- v0.8.17 — 'soc_plus_charge' rows already fold a
                  -- metered charge into their energy; rescaling them by
                  -- capacity alone would corrupt that. And a NULL
                  -- consumption is a deliberate suppression (v0.8.15
                  -- parked disconnect-orphans, v0.8.17 SoC-rose trips),
                  -- so the CASE above keeps it NULL instead of
                  -- resurrecting a figure the close path refused to
                  -- publish.
                  AND (energy_source IS NULL
                       OR energy_source IN ('soc', 'estimated'))
                """,
                (new_capacity_kwh, new_capacity_kwh),
            )
            return int(cur.rowcount or 0)

    async def async_recompute_trip_costs_from_charges(
        self, default_price: float = 0.0
    ) -> int:
        """Re-cost every trip from the weighted-average battery pool.

        Trip cost DOES reflect actual charge prices: the battery is
        modelled as one blended pool whose €/kWh average dilutes toward
        whatever price the most recent charge added, weighted by kWh —
        a free public charger genuinely lowers what the following
        driving cost, an expensive DC-fast genuinely raises it. Only
        energy that predates any tracked charge (fresh install) or
        exceeds what's been tracked as charged falls back to the
        configured home tariff (`default_price`). Each charge still
        keeps its own actual price in its own record regardless
        (visible in recent_charges and the AC/DC averages).

        Idempotent. Called once on startup so historical €0 trips heal
        themselves when the user fixes a wrongly-configured CONF_ENERGY_PRICE.
        Returns the number of trip rows updated.

        (The method name keeps `_from_charges` for backwards compat with
        the v0.5.4-0.5.6 codepath.)
        """
        return await self._hass.async_add_executor_job(
            self._recompute_trip_costs_from_charges, default_price
        )

    def _recompute_trip_costs_from_charges(self, default_price: float) -> int:
        """v0.8.8 — weighted-average-cost (WAC) battery pool replay.

        Charges blend (kWh, price) into a single running pool average
        (weighted by kWh); each trip withdraws energy_kwh from the pool
        at whatever that blended average currently is. When the pool
        can't cover the full withdrawal the shortfall is priced at the
        configured home tariff (`default_price`). The
        result is written back to `cost` and `cost_basis_per_kwh`.
        """
        from collections import deque  # noqa: PLC0415

        price = float(default_price)
        with self._connect() as conn:
            # v0.5.14 — heal trips poisoned by the unbounded
            # power-integration trapezoid shipped in v0.5.13. We detect
            # them by impossibly-high consumption (>50 kWh/100km — even
            # the worst EVs cap around 35) on trips tagged
            # energy_source='power_integration', and reset their energy
            # so the step-1 distance-based re-fill below replaces it.
            poisoned = conn.execute(
                """
                UPDATE trips SET
                    energy_kwh = NULL,
                    consumption_kwh_100km = NULL,
                    energy_source = 'estimated',
                    energy_from_power = NULL
                WHERE energy_source = 'power_integration'
                  AND consumption_kwh_100km > 50
                """
            ).rowcount or 0
            if poisoned:
                _LOGGER.info(
                    "Storage heal: cleared %d trip(s) with impossible "
                    "power-integration consumption (v0.5.13 regression)",
                    poisoned,
                )
            # Step 1 — estimate missing energy/consumption from the recent
            # distance-weighted average. Cloud-polling integrations sometimes
            # return the same SoC at start and end of a short trip (no
            # refresh within the window), leaving energy_kwh NULL. We fill
            # it in from the user's actual driving baseline so trip detail
            # cards don't show blanks.
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(energy_kwh), 0) AS total_kwh,
                    COALESCE(SUM(distance_km), 0) AS total_km
                FROM trips
                WHERE energy_kwh IS NOT NULL AND energy_kwh > 0
                  AND distance_km IS NOT NULL AND distance_km > 0
                """
            ).fetchone()
            total_kwh, total_km = row[0], row[1]
            avg_per_100 = (total_kwh / total_km * 100.0) if total_km else None

            if avg_per_100 and avg_per_100 > 0:
                # v0.8.17 — two corrections:
                #  * `orphan_odo_only` rows have NULL energy ON PURPOSE
                #    (km appeared but SoC never dropped — a catch-up
                #    odometer snapshot whose kilometres were already
                #    consumed under another row). Fabricating energy for
                #    them invented withdrawals that drained the pool and
                #    pushed later real trips onto the fallback tariff.
                #  * stamp `energy_source = 'estimated'` on whatever we
                #    do fill, so `_avg_consumption_kwh_per_100km` — which
                #    excludes 'estimated' precisely to avoid feeding
                #    estimates back into the baseline — actually excludes
                #    them. Without the stamp they stayed NULL-sourced and
                #    slipped straight back into the average that produced
                #    them.
                conn.execute(
                    """
                    UPDATE trips SET
                        energy_kwh = distance_km * ? / 100.0,
                        consumption_kwh_100km = ?,
                        energy_source = 'estimated'
                    WHERE (energy_kwh IS NULL OR energy_kwh <= 0)
                      AND distance_km IS NOT NULL AND distance_km > 0
                      AND COALESCE(confidence, '') != 'orphan_odo_only'
                      -- v0.8.17 — never overwrite measured provenance. A
                      -- row that genuinely measured 0 kWh via power
                      -- integration or the vehicle's own sensor would
                      -- otherwise be relabelled 'estimated' and given a
                      -- fabricated distance-based energy.
                      AND (energy_source IS NULL
                           OR energy_source IN ('soc', 'estimated'))
                    """,
                    (avg_per_100, avg_per_100),
                )

            # Step 2 — weighted-average-cost (WAC) battery pool replay.
            # v0.8.8 — replaced the earlier FIFO-slice model. A real
            # battery is one blended pool, not separable discrete lots:
            # modelling it as FIFO meant a single free (or cheap) charge
            # created a "slice" that had to be fully drained — 100% free
            # driving for however many km that charge covered, then an
            # abrupt full-price jump the instant it ran out. WAC instead
            # tracks one running (pool_kwh, avg_price_per_kwh) for the
            # whole battery: every charge dilutes/raises that single
            # average (weighted by kWh), and every trip draws from the
            # pool at whatever the blended average currently is — so a
            # free charge lowers the cost per km smoothly across all the
            # driving it covers instead of zeroing then snapping back.
            # v0.8.17 — no ORDER BY here. The replay used to order by
            # `started_at` and then consume the queue on `ended_at`,
            # assuming the two agree. They do not, and the mismatch
            # caused head-of-line blocking that dropped charges from the
            # pool entirely: `started_at IS NULL` sorted FIRST (the
            # documented shape of a `log_charge` with no start time), so
            # one such row with a late `ended_at` stalled every charge
            # behind it for the rest of the replay. A long overnight
            # session did the same to every charge that ended during it.
            # Ordering by an ISO TEXT column is also wrong whenever two
            # rows carry different UTC offsets — including one hour every
            # year at the DST fall-back. So: fetch, parse, sort by the
            # real instant in Python.
            charge_rows = conn.execute(
                """
                SELECT ended_at, kwh, price_per_kwh, total_cost,
                       soc_start, soc_end
                FROM charges
                """
            ).fetchall()
            # v0.5.79 — normalise tz: older rows persisted as tz-naive,
            # newer rows as tz-aware. Comparing them later raises
            # TypeError ("can't compare offset-naive and offset-aware
            # datetimes") and the whole recompute crashes. Treat naive
            # ISO strings as UTC.
            def _as_utc(s: str | None) -> datetime | None:
                if not s:
                    return None
                try:
                    ts = datetime.fromisoformat(s)
                except (TypeError, ValueError):
                    return None
                if ts.tzinfo is None:
                    # v0.8.17 — naive rows were written by the live paths
                    # before v0.8.17, i.e. in LOCAL time. Reading them as
                    # UTC shifted them 1-2 h into the future and
                    # reordered them against tz-aware rows, so a charge
                    # could land after a trip it really preceded and that
                    # trip fell to the fallback tariff.
                    ts = ts.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
                return ts

            parsed_charges: list[
                tuple[datetime, float, float, float | None, float | None]
            ] = []
            for r in charge_rows:
                ts = _as_utc(r["ended_at"])
                if ts is None:
                    continue
                kwh = float(r["kwh"] or 0.0)
                if kwh <= 0:
                    continue
                # v0.8.17 — the money paid is the input, the €/kWh is
                # derived. Pricing the pool off `price_per_kwh` loses the
                # charging losses whenever that price was billed on the
                # charger side (a public tariff always is) while `kwh` is
                # the battery-side figure: measured on this car, 402.88
                # kWh metered against 365.17 kWh into the battery, so
                # 9.4 % of the energy actually paid for reached no trip.
                # `total_cost / kwh` conserves money by construction and
                # is exactly "what the driving cost".
                total_cost = r["total_cost"]
                if total_cost is not None and float(total_cost) > 0:
                    pp = float(total_cost) / kwh
                else:
                    pp = float(r["price_per_kwh"] or 0.0)
                soc_start_c = (
                    float(r["soc_start"]) if r["soc_start"] is not None else None
                )
                soc_end = (
                    float(r["soc_end"]) if r["soc_end"] is not None else None
                )
                parsed_charges.append((ts, kwh, pp, soc_start_c, soc_end))
            parsed_charges.sort(key=lambda c: c[0])
            pending_charges: deque[
                tuple[datetime, float, float, float | None, float | None]
            ] = deque(parsed_charges)

            capacity = self.capacity_hint_kwh

            pool_kwh = 0.0
            pool_avg_price = 0.0
            # v0.8.22 — the per-lot ledger, replayed in lockstep with the
            # pool over the same charges and trips. `[kwh, price]`, oldest
            # first; trips draw from the END.
            lots: list[list[float]] = []
            trip_rows = conn.execute(
                """
                SELECT id, started_at, energy_kwh
                FROM trips
                """
            ).fetchall()

            # v0.8.17 — sort by the parsed instant, not by the ISO text.
            # Text ordering put a row stamped '+00:00' two hours before
            # its real position, so an EARLIER trip could be replayed
            # after a later one and inherit a pool holding energy that
            # did not exist yet.
            parsed_trips: list[tuple[datetime, int, float]] = []
            for trow in trip_rows:
                energy = trow["energy_kwh"]
                if energy is None or float(energy) <= 0:
                    continue
                trip_started = _as_utc(trow["started_at"])
                if trip_started is None:
                    continue
                parsed_trips.append(
                    (trip_started, int(trow["id"]), float(energy))
                )
            parsed_trips.sort(key=lambda t: (t[0], t[1]))

            updated = 0
            for trip_started, trip_id_, energy in parsed_trips:
                # Advance: charges with ended_at <= trip_started blend
                # into the pool now — new_avg is the kWh-weighted mean
                # of what was already there and what just arrived.
                while pending_charges and pending_charges[0][0] <= trip_started:
                    (
                        _ts, c_kwh, c_price, c_soc_start, c_soc_end,
                    ) = pending_charges.popleft()
                    # v0.8.17 — re-anchor BEFORE blending too. The blend
                    # weights the pool's existing kWh against the
                    # incoming ones, so if the pool still carries energy
                    # that physically left the battery (standby drain, or
                    # driving the logger never saw) the old cheap price
                    # gets far more weight than it deserves. `soc_start`
                    # says exactly what was in the pack when the cable
                    # went in.
                    if capacity and c_soc_start is not None:
                        _anchor_lots(
                            lots, c_soc_start / 100.0 * capacity, c_price,
                        )
                    lots.append([c_kwh, c_price])
                    if capacity and c_soc_start is not None:
                        if pool_avg_price <= 0:
                            # Opening inventory of unknown price — the
                            # battery was not empty when tracking began.
                            # Pricing it at 0 would report it as free
                            # forever; the incoming charge's price is the
                            # closest thing to evidence we have.
                            pool_avg_price = c_price
                        pool_kwh = max(0.0, c_soc_start / 100.0 * capacity)
                    new_pool_kwh = pool_kwh + c_kwh
                    pool_avg_price = (
                        (pool_kwh * pool_avg_price + c_kwh * c_price) / new_pool_kwh
                        if new_pool_kwh > 0 else c_price
                    )
                    pool_kwh = new_pool_kwh
                    # v0.8.17 — re-anchor QUANTITY to the physical pack;
                    # the pool keeps tracking PRICE only. The additive
                    # pool had no sink for energy that leaves the battery
                    # without being a trip (standby drain,
                    # preconditioning, parked climate) and no opening
                    # inventory for the charge the battery already held
                    # on day one, so it drifted in both directions at
                    # once: measured on this car, trips consumed 73.78
                    # kWh MORE than was ever charged, while the pool was
                    # free to exceed the pack's physical capacity. After
                    # a charge closes the battery's real content is known
                    # exactly — soc_end — so use it and let the drift go.
                    if capacity and c_soc_end is not None:
                        pool_kwh = max(0.0, c_soc_end / 100.0 * capacity)
                        _anchor_lots(
                            lots, c_soc_end / 100.0 * capacity, c_price,
                        )
                    elif capacity:
                        pool_kwh = min(pool_kwh, capacity)
                        _anchor_lots(
                            lots, min(sum(lot[0] for lot in lots), capacity),
                            c_price,
                        )

                energy_f = float(energy)
                from_pool = min(energy_f, pool_kwh)
                from_fallback = energy_f - from_pool
                # v0.8.17 — price the shortfall at the blend seen SO FAR
                # rather than at the configured home tariff. The tariff
                # was the cheapest assumption available, applied exactly
                # when the driver is on a motorway paying DC prices. The
                # running average is strictly better information and,
                # unlike a whole-history average, it stays causal: only
                # charges that already happened can influence this trip,
                # so a charge logged tomorrow can never re-price
                # yesterday's driving. Before the first charge there is
                # nothing but the tariff, so that is what gets used.
                fallback_price = (
                    pool_avg_price if pool_avg_price > 0 else price
                )
                cost_accum = (
                    from_pool * pool_avg_price
                    + from_fallback * fallback_price
                )
                pool_kwh = max(0.0, pool_kwh - energy_f)
                basis = cost_accum / energy_f if energy_f > 0 else None

                # v0.8.22 — the same withdrawal against the lot stack,
                # newest lot first. The shortfall uses the identical
                # `fallback_price` the pool just used, so with no lots at
                # all the two figures agree instead of this one reading a
                # misleading zero.
                remaining = energy_f
                lifo_cost = 0.0
                while remaining > 1e-9 and lots:
                    lot = lots[-1]
                    take = min(lot[0], remaining)
                    lifo_cost += take * lot[1]
                    lot[0] -= take
                    remaining -= take
                    if lot[0] <= 1e-9:
                        lots.pop()
                if remaining > 1e-9:
                    lifo_cost += remaining * fallback_price

                cur = conn.execute(
                    "UPDATE trips SET cost = ?, cost_basis_per_kwh = ?, "
                    "cost_lifo = ? WHERE id = ?",
                    (
                        round(cost_accum, 4),
                        round(basis, 6) if basis is not None else None,
                        round(lifo_cost, 4),
                        trip_id_,
                    ),
                )
                if cur.rowcount:
                    updated += 1
            return updated

    async def async_extend_last_charge(
        self,
        extra_kwh: float,
        ended_at: datetime,
        soc_end: float | None = None,
        extra_evse_kwh: float | None = None,
        new_peak_power_kw: float | None = None,
    ) -> ChargeRecord | None:
        """Append to the most recent charge instead of inserting a new row.

        Use when the cable hasn't been physically disconnected (plug=on)
        between two charging pulses: we treat the whole plugged interval
        as ONE session. Adds `extra_kwh` to the row's kwh, extends
        ended_at, recomputes total_cost from the existing price_per_kwh,
        and (if provided) sets soc_end to the new ABSOLUTE reading.

        v0.5.45 — soc_end used to be a delta ADDED to the previous
        soc_end; merging several pulses compounded the additions and
        produced impossible values (47 % start, 124 % end). The session
        always ends at the battery's current absolute SoC, so that's
        what we store now.
        """
        return await self._hass.async_add_executor_job(
            self._extend_last_charge, extra_kwh, ended_at, soc_end,
            extra_evse_kwh, new_peak_power_kw,
        )

    def _extend_last_charge(
        self,
        extra_kwh: float,
        ended_at: datetime,
        soc_end: float | None,
        extra_evse_kwh: float | None = None,
        new_peak_power_kw: float | None = None,
    ) -> ChargeRecord | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            # v0.8.13-style fix — chronological order, not insertion id
            # (a backfilled row could otherwise outrank the session this
            # pulse actually belongs to).
            row = conn.execute(
                "SELECT * FROM charges ORDER BY ended_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            new_kwh = float(row["kwh"] or 0) + float(extra_kwh)
            # v0.8.17 — ADD the pulse's cost instead of re-deriving the
            # whole row from the price. Re-deriving multiplied any fixed
            # component of the bill: a €6.00 invoice on 10 kWh (€5
            # energy + €1 connection fee) locks the price at 0.60, and a
            # second 10 kWh pulse turned the row into €12.00 when the
            # real bill is €11.00.
            prev_total = row["total_cost"]
            unit_price = float(row["price_per_kwh"] or 0)
            new_total = (
                float(prev_total) + float(extra_kwh) * unit_price
                if prev_total is not None
                else new_kwh * unit_price
            )
            new_soc_end = (
                float(soc_end) if soc_end is not None else row["soc_end"]
            )
            # v0.5.94 — accumulate EVSE-side energy too. Without this,
            # merged sessions (multi-pulse plugged windows) lost the
            # AC measurement: subsequent merges UPDATEd the row but
            # never wrote evse_energy_kwh / charging_efficiency_pct.
            cur_evse = (
                float(row["evse_energy_kwh"])
                if ("evse_energy_kwh" in row.keys()
                    and row["evse_energy_kwh"] is not None)
                else 0.0
            )
            new_evse: float | None
            if extra_evse_kwh is not None and extra_evse_kwh > 0:
                new_evse = round(cur_evse + float(extra_evse_kwh), 3)
            else:
                new_evse = cur_evse if cur_evse > 0 else None
            new_eff = (
                round(new_kwh / new_evse * 100.0, 1)
                if new_evse and new_evse > 0 else None
            )
            # v0.6.0 — peak power survives merges: keep the max of the
            # existing row's value and the new pulse's peak.
            cur_peak = (
                float(row["peak_charge_power_kw"])
                if ("peak_charge_power_kw" in row.keys()
                    and row["peak_charge_power_kw"] is not None)
                else None
            )
            new_peak: float | None
            if new_peak_power_kw is not None and new_peak_power_kw > 0:
                new_peak = max(cur_peak or 0.0, float(new_peak_power_kw))
                new_peak = round(new_peak, 2)
            else:
                new_peak = cur_peak
            conn.execute(
                "UPDATE charges SET kwh = ?, total_cost = ?, ended_at = ?, "
                "soc_end = ?, evse_energy_kwh = ?, charging_efficiency_pct = ?, "
                "peak_charge_power_kw = ? "
                "WHERE id = ?",
                (
                    new_kwh, new_total, _iso_local(ended_at), new_soc_end,
                    new_evse, new_eff, new_peak, row["id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM charges WHERE id = ?", (row["id"],)
            ).fetchone()
        return _row_to_charge(updated)

    async def async_delete_last_charge(self) -> bool:
        return await self._hass.async_add_executor_job(self._delete_last_charge)

    def _delete_last_charge(self) -> bool:
        with self._connect() as conn:
            # v0.8.14 — chronological order, not insertion id (see v0.8.13).
            cur = conn.execute(
                "SELECT id FROM charges ORDER BY ended_at DESC, id DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM charges WHERE id = ?", (row[0],))
            return True

    async def async_charges_aggregates_since(
        self, since: datetime
    ) -> dict[str, float | int]:
        return await self._hass.async_add_executor_job(
            self._charges_aggregates_since, since
        )

    def _charges_aggregates_since(self, since: datetime) -> dict[str, float | int | None]:
        # v0.5.101 — also pair-sum kwh + evse_energy_kwh over rows that
        # actually have EVSE data, so the dashboard can compute charging
        # efficiency as a properly weighted ratio. Previously the
        # dashboard would divide `energy_charged_this_month` (sum of
        # ALL charges' kwh) by `sum(evse_energy_kwh)` (sum over the
        # SUBSET that had EVSE wired) — a 5x-7x mismatch that produced
        # impossible 700-800 % efficiency numbers.
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(kwh), 0) AS kwh,
                    COALESCE(SUM(total_cost), 0) AS cost,
                    COUNT(*) AS count,
                    COALESCE(SUM(CASE WHEN is_dcfc = 1 THEN kwh ELSE 0 END), 0) AS dc_kwh,
                    COALESCE(SUM(CASE WHEN is_dcfc = 1 THEN total_cost ELSE 0 END), 0) AS dc_cost,
                    -- v0.8.17 — NULL is_dcfc counted as neither, so
                    -- ac_kwh + dc_kwh < kwh and the AC average silently
                    -- excluded those sessions. `is_dcfc` is left NULL
                    -- whenever log_charge runs without a start time or
                    -- the session is under 3 min. Treat NULL as AC, the
                    -- convention _lifetime_dcfc_ratio already documents.
                    COALESCE(SUM(CASE WHEN COALESCE(is_dcfc, 0) = 0 THEN kwh ELSE 0 END), 0) AS ac_kwh,
                    COALESCE(SUM(CASE WHEN COALESCE(is_dcfc, 0) = 0 THEN total_cost ELSE 0 END), 0) AS ac_cost,
                    -- v0.8.29 — all three sums share ONE predicate, and
                    -- it now includes the plausibility band. Gating on
                    -- the ratio itself rather than the stored
                    -- charging_efficiency_pct keeps a row whose derived
                    -- column was never written. Without the band a row
                    -- whose kwh came from the SoC estimate (the user's
                    -- real db held 1.65 kwh against a metered 1.05)
                    -- pushed the numerator above the denominator, so
                    -- the "always 0-100 %" paired ratio was not.
                    COALESCE(SUM(
                        CASE WHEN {_EVSE_USABLE_SQL}
                             THEN kwh ELSE 0 END
                    ), 0) AS kwh_with_evse,
                    COALESCE(SUM(
                        CASE WHEN {_EVSE_USABLE_SQL}
                             THEN evse_energy_kwh ELSE 0 END
                    ), 0) AS evse_kwh,
                    SUM(CASE WHEN {_EVSE_USABLE_SQL}
                             THEN 1 ELSE 0 END
                    ) AS evse_count,
                    -- v0.6.0: high-power-stress accounting. Sessions
                    -- whose peak power exceeded 100 kW are flagged as
                    -- the "heavy DCFC" cohort the Geotab fleet study
                    -- ties to ~3 %/yr SoH loss vs ~1.5 %/yr light-use
                    -- (https://www.geotab.com/blog/ev-battery-health/).
                    COALESCE(SUM(
                        CASE WHEN peak_charge_power_kw IS NOT NULL
                                  AND peak_charge_power_kw >= 100
                             THEN kwh ELSE 0 END
                    ), 0) AS high_power_kwh,
                    SUM(CASE WHEN peak_charge_power_kw IS NOT NULL
                                  AND peak_charge_power_kw >= 100
                             THEN 1 ELSE 0 END
                    ) AS high_power_count,
                    MAX(peak_charge_power_kw) AS peak_power_max
                FROM charges WHERE ended_at >= ?
                """,
                (since.isoformat(),),
            ).fetchone()
        (kwh, cost, count, dc_kwh, dc_cost, ac_kwh, ac_cost,
         kwh_with_evse, evse_kwh, evse_count,
         high_power_kwh, high_power_count, peak_power_max) = row
        avg_price = (cost / kwh) if kwh else 0.0
        # Efficiency is only meaningful when ≥1 charge in the period had
        # EVSE data. Below that, return None so the UI shows "unknown"
        # rather than 0 % (which would read as "100 % losses" — wrong).
        charging_eff: float | None
        if evse_count and evse_kwh > 0:
            charging_eff = round(float(kwh_with_evse) / float(evse_kwh) * 100.0, 1)
        else:
            charging_eff = None
        return {
            "kwh": float(kwh),
            "total_cost": float(cost),
            "count": int(count),
            "avg_price_per_kwh": float(avg_price),
            "ac_kwh": float(ac_kwh),
            "ac_total_cost": float(ac_cost),
            "avg_ac_price_per_kwh": float(ac_cost / ac_kwh) if ac_kwh else 0.0,
            "dc_kwh": float(dc_kwh),
            "dc_total_cost": float(dc_cost),
            "avg_dc_price_per_kwh": float(dc_cost / dc_kwh) if dc_kwh else 0.0,
            # v0.5.101
            "kwh_with_evse": float(kwh_with_evse),
            "evse_kwh": float(evse_kwh),
            "evse_count": int(evse_count or 0),
            "charging_efficiency_pct": charging_eff,
            # v0.6.0 — DCFC-stress accounting (>=100 kW peak threshold).
            "high_power_kwh": float(high_power_kwh),
            "high_power_count": int(high_power_count or 0),
            "peak_power_max_kw": (
                float(peak_power_max) if peak_power_max is not None else None
            ),
        }

    async def async_consumption_by_temp_bucket(
        self, since: datetime, bucket_size_c: float = 5.0
    ) -> dict[str, float | int]:
        """Mean kWh/100km grouped by avg_temp_c bins (size `bucket_size_c`).

        Returns: {"by_bucket": {bucket_label: avg_consumption}, "bucket_size_c": float}.
        Trips without avg_temp_c or consumption are skipped. Buckets are
        labelled by their lower bound (e.g. "0", "5", "10").
        """
        return await self._hass.async_add_executor_job(
            self._consumption_by_temp_bucket, since, bucket_size_c
        )

    def _consumption_by_temp_bucket(
        self, since: datetime, bucket_size_c: float
    ) -> dict[str, Any]:
        with self._connect() as conn:
            # v0.5.62 — COALESCE(avg_temp_c, ambient_temp_c). When the
            # user only has a weather entity but no exterior-temp sensor
            # on the car, the trip's avg_temp_c stays NULL — fall back
            # to the weather snapshot's ambient temp so the bucket
            # sensor still has data.
            rows = conn.execute(
                """
                SELECT COALESCE(avg_temp_c, ambient_temp_c) AS t,
                       consumption_kwh_100km, distance_km
                FROM trips
                WHERE started_at >= ?
                  AND COALESCE(avg_temp_c, ambient_temp_c) IS NOT NULL
                  AND consumption_kwh_100km IS NOT NULL
                """,
                (since.isoformat(),),
            ).fetchall()
        # Distance-weighted mean per bucket so a 5-km commute doesn't
        # outweigh a 200-km motorway leg at the same temperature.
        sums: dict[int, float] = {}
        dists: dict[int, float] = {}
        for temp, cons, dist in rows:
            if dist is None or dist <= 0:
                continue
            bucket = int((temp // bucket_size_c) * bucket_size_c)
            sums[bucket] = sums.get(bucket, 0.0) + cons * dist
            dists[bucket] = dists.get(bucket, 0.0) + dist
        by_bucket = {
            str(b): round(sums[b] / dists[b], 2)
            for b in sorted(sums)
            if dists[b] > 0
        }
        # v0.8.17 — hand the weights out too. The per-bucket figures are
        # correctly distance-weighted, and then the sensor's "no live
        # temp" fallback averaged the bucket LABELS equally: a single
        # 6 km January trip at 22.0 against 4 000 km of summer at 14.5
        # came out as 18.25 instead of 14.51. `sample_count` also used
        # to count rows FETCHED, including the ones the loop skips for
        # having no usable distance.
        return {
            "by_bucket": by_bucket,
            "bucket_distance_km": {
                str(b): round(dists[b], 1) for b in sorted(dists)
            },
            "bucket_size_c": bucket_size_c,
            "sample_count": sum(
                1 for _t, _c, d in rows if d is not None and d > 0
            ),
        }

    async def async_heal_history(
        self, battery_capacity_kwh: float
    ) -> dict[str, int]:
        """v0.8.17 — re-derive historical trips from data that exists NOW.

        Two things changed under these rows after they were written:

        1. Charges were recovered. `kwh_charged_before` / `_during` are
           computed once, at trip close, so a charge inserted afterwards
           (by `log_charge`, `set_charge`, or a recovery sweep) is
           invisible to every trip around it — and with it the v0.8.17
           correction that folds a mid-trip charge into the trip's
           energy. Both fields are recomputed here from the charges
           table as it stands, and the energy correction reapplied.

        2. The SoC anchor was bounded. Rows written before that could
           carry a `soc_start` ABOVE the previous trip's `soc_end` with
           no charge in between, which is physically impossible while
           parked: standby drain only ever removes charge. Those rows
           are re-anchored to the last actually-measured value.

        Nothing is modelled or invented: a rise that a real charge
        explains is left alone, and a row whose energy came from an
        independent measurement (`power_integration`, `vehicle`) keeps
        that energy — only its SoC bookkeeping is made consistent.

        Returns per-action counts. Call the cost replay afterwards.
        """
        return await self._hass.async_add_executor_job(
            self._heal_history, float(battery_capacity_kwh),
        )

    def _heal_history(self, capacity: float) -> dict[str, int]:
        def _parse(value: str | None) -> datetime | None:
            if not value:
                return None
            try:
                ts = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                return None
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            return ts

        counts = {
            "trips_seen": 0,
            "charge_attribution_fixed": 0,
            "soc_start_reanchored": 0,
            "energy_recomputed": 0,
            "consumption_suppressed": 0,
            "stale_energy_source_cleared": 0,
        }
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            charges = []
            for r in conn.execute(
                "SELECT started_at, ended_at, kwh, soc_start, soc_end "
                "FROM charges"
            ).fetchall():
                end = _parse(r["ended_at"])
                if end is None:
                    continue
                start = _parse(r["started_at"]) or end
                kwh = float(r["kwh"] or 0.0)
                if kwh <= 0:
                    continue
                charges.append(
                    (start, end, kwh, r["soc_start"], r["soc_end"])
                )

            trips = []
            for r in conn.execute(
                """
                SELECT id, started_at, ended_at, soc_start, soc_end,
                       soc_used_pct, energy_kwh, energy_source, distance_km,
                       consumption_kwh_100km, kwh_charged_before,
                       kwh_charged_during
                FROM trips
                """
            ).fetchall():
                start = _parse(r["started_at"])
                end = _parse(r["ended_at"])
                if start is None or end is None:
                    continue
                trips.append((start, end, dict(r)))
            trips.sort(key=lambda item: (item[0], item[2]["id"]))

            prev_end_ts: datetime | None = None
            prev_soc_end: float | None = None
            for start, end, row in trips:
                counts["trips_seen"] += 1
                updates: dict[str, Any] = {}

                # --- charge attribution, from the charges that exist now
                # v0.8.17 — same semantics as the close path: a charge
                # counts when it ENDED inside the trip's window. Interval
                # overlap would also catch a session that merely started
                # inside and ran on afterwards.
                # v0.8.18 — and only the part delivered AFTER the window
                # opened belongs to it. Crediting the whole session gave
                # a trip that opened mid-charge all 66.83 kWh and 78
                # kWh/100km over 74 km. SoC apportions it exactly.
                during = 0.0
                straddle_floor = start + timedelta(
                    seconds=_CHARGE_TRIP_BOUNDARY_TOLERANCE_S
                )
                for c_start, c_end, kwh, c_soc_s, c_soc_e in charges:
                    if not (start <= c_end <= end):
                        continue
                    if c_start >= start:
                        during += kwh
                    elif c_end <= straddle_floor:
                        # Started before the window, finished as it opened:
                        # nothing was delivered inside. Apportioning by SoC
                        # here trusts `soc_start`, and a row whose anchor
                        # this very heal re-anchored would be credited
                        # energy from a session that had already stopped.
                        continue
                    else:
                        during += _apportion_straddling_charge(
                            kwh,
                            soc_start=c_soc_s,
                            soc_end=c_soc_e,
                            trip_soc_start=row["soc_start"],
                        )
                # `before` carries the boundary tolerance the exact
                # comparison lacked (see
                # `_CHARGE_TRIP_BOUNDARY_TOLERANCE_S`); `during` above
                # needs none, because SoC already decides how much of a
                # session was still to come when the window opened.
                before = 0.0
                if prev_end_ts is not None and prev_end_ts < start:
                    before = sum(
                        kwh for _cs, c_end, kwh, _ss, _se in charges
                        if prev_end_ts <= c_end <= straddle_floor
                    )
                new_during = round(during, 2) if during > 0.005 else None
                new_before = round(before, 2) if before > 0 else None
                if (
                    new_during != row["kwh_charged_during"]
                    or new_before != row["kwh_charged_before"]
                ):
                    updates["kwh_charged_during"] = new_during
                    updates["kwh_charged_before"] = new_before
                    counts["charge_attribution_fixed"] += 1

                # --- provably impossible SoC rise while parked
                soc_start = row["soc_start"]
                if (
                    soc_start is not None
                    and prev_soc_end is not None
                    and float(soc_start) > float(prev_soc_end)
                    and new_during is None
                    and new_before is None
                ):
                    soc_start = float(prev_soc_end)
                    updates["soc_start"] = soc_start
                    counts["soc_start_reanchored"] += 1

                soc_end = row["soc_end"]
                if soc_start is not None and soc_end is not None:
                    soc_used = round(float(soc_start) - float(soc_end), 1)
                    if soc_used != row["soc_used_pct"]:
                        updates["soc_used_pct"] = soc_used
                else:
                    soc_used = row["soc_used_pct"]

                # --- energy, only where it was NOT independently measured
                distance = row["distance_km"]
                if row["energy_source"] not in ("power_integration", "vehicle"):
                    energy: float | None = None
                    source: str | None = None
                    if new_during is not None and soc_used is not None:
                        candidate = (
                            new_during + (float(soc_used) / 100.0) * capacity
                        )
                        if candidate > 0:
                            energy, source = round(candidate, 2), "soc_plus_charge"
                    elif soc_used is not None and float(soc_used) > 0:
                        energy, source = (
                            round(float(soc_used) / 100.0 * capacity, 2), "soc"
                        )
                    if energy is not None:
                        if energy != row["energy_kwh"]:
                            updates["energy_kwh"] = energy
                            updates["energy_source"] = source
                            counts["energy_recomputed"] += 1
                        # v0.8.17 — a NULL consumption is a deliberate
                        # suppression (v0.8.15 parked orphans, v0.8.17
                        # SoC-rose rows). Recomputing one here would
                        # republish the very figure those paths refused
                        # to publish, so only refresh a value that was
                        # already there.
                        if (
                            distance
                            and float(distance) > 0
                            and row["consumption_kwh_100km"] is not None
                        ):
                            updates["consumption_kwh_100km"] = round(
                                energy / float(distance) * 100.0, 1
                            )
                    elif (
                        soc_used is not None
                        and float(soc_used) < 0
                        and new_during is None
                        and row["consumption_kwh_100km"] is not None
                    ):
                        # SoC rose, nothing charged: no honest per-km figure
                        updates["consumption_kwh_100km"] = None
                        updates["low_confidence"] = 1
                        counts["consumption_suppressed"] += 1

                    # Nothing could be recomputed, so whatever is stored
                    # survives — including a label that this run has just
                    # disproved. `soc_plus_charge` asserts "my energy
                    # includes a session delivered inside my window"; with
                    # `kwh_charged_during` NULL that is provably false, and
                    # leaving it there presents a stranded number as a
                    # derivation nobody can reproduce. Observed on the real
                    # id352: 2.58 kWh from a v0.8.17 run that credited a
                    # whole session, kept through two later runs that both
                    # concluded the session contributed nothing.
                    #
                    # The value stays — cost and the lifetime kWh
                    # aggregates need a number, the same call v0.8.15 and
                    # v0.8.17 made when suppressing a consumption figure.
                    # Only the claim is dropped, to NULL: "no longer known
                    # how this was derived", already a legal state for
                    # rows where neither SoC nor power was available.
                    if (
                        energy is None
                        and new_during is None
                        and row["energy_source"] == "soc_plus_charge"
                    ):
                        updates["energy_source"] = None
                        updates["low_confidence"] = 1
                        counts["stale_energy_source_cleared"] += 1

                if updates:
                    cols = ", ".join(f"{k} = ?" for k in updates)
                    conn.execute(
                        f"UPDATE trips SET {cols} WHERE id = ?",
                        [*list(updates.values()), row["id"]],
                    )

                prev_end_ts = end
                # v0.8.17 — carry the SoC anchor WITH its timestamp. If
                # this row has no soc_end, keeping an older one while the
                # "was anything charged in between?" window only covers
                # the last gap would let the re-anchor fire against a
                # value tens of percent stale.
                prev_soc_end = (
                    float(soc_end) if soc_end is not None else None
                )
        return counts

    async def async_export_csv(self, path: str) -> int:
        """Dump all trips to a CSV at `path`; returns row count."""
        return await self._hass.async_add_executor_job(self._export_csv, path)

    def _export_csv(self, path: str) -> int:
        import csv  # noqa: PLC0415

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM trips ORDER BY id").fetchall()
        if not rows:
            Path(path).write_text("")
            return 0
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            for r in rows:
                writer.writerow(dict(r))
        return len(rows)

    # === v0.5.0 additions: GPS positions + advanced aggregates ===

    async def async_insert_positions(
        self, trip_id: int, samples: list[tuple[datetime, float, float]]
    ) -> int:
        """Persist GPS samples for a trip. Each sample is (ts, lat, lon).

        Called on trip close with whatever the GPS sampler accumulated. Returns
        the number of rows inserted.
        """
        if not samples:
            return 0
        return await self._hass.async_add_executor_job(
            self._insert_positions, trip_id, samples
        )

    def _insert_positions(
        self, trip_id: int, samples: list[tuple[datetime, float, float]]
    ) -> int:
        rows = [(trip_id, ts.isoformat(), lat, lon) for ts, lat, lon in samples]
        with sqlite3.connect(self._path) as conn:
            conn.executemany(
                "INSERT INTO trip_positions (trip_id, ts, lat, lon) VALUES (?,?,?,?)",
                rows,
            )
        return len(rows)

    async def async_trip_positions(self, trip_id: int) -> list[dict[str, Any]]:
        """Return GPS samples for a trip ordered by ts."""
        return await self._hass.async_add_executor_job(
            self._trip_positions, trip_id
        )

    def _trip_positions(self, trip_id: int) -> list[dict[str, Any]]:
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT ts, lat, lon FROM trip_positions WHERE trip_id = ? ORDER BY ts",
                (trip_id,),
            ).fetchall()
        return [
            {"ts": r["ts"], "lat": float(r["lat"]), "lon": float(r["lon"])}
            for r in rows
        ]

    async def async_monthly_history(self, months: int = 12) -> list[dict[str, Any]]:
        """Per-month rollup (km, kWh, cost, trips) for the last N months."""
        return await self._hass.async_add_executor_job(
            self._monthly_history, months
        )

    def _monthly_history(self, months: int) -> list[dict[str, Any]]:
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                """
                SELECT
                    substr(started_at, 1, 7) AS month,
                    COALESCE(SUM(distance_km), 0) AS distance_km,
                    COALESCE(SUM(energy_kwh), 0) AS energy_kwh,
                    COALESCE(SUM(cost), 0) AS cost,
                    COUNT(*) AS trips
                FROM trips
                GROUP BY month
                ORDER BY month DESC
                LIMIT ?
                """,
                (months,),
            ).fetchall()
            # charged_kwh is in a separate table keyed by ended_at — bucket it
            # by the same calendar month so dashboards get a clean per-month
            # "to battery" figure. Charge-only months (no trips) don't add a
            # row here; the lifetime accumulator remains the fallback for those.
            charged = {
                r[0]: float(r[1])
                for r in conn.execute(
                    """
                    SELECT substr(ended_at, 1, 7) AS month,
                           COALESCE(SUM(kwh), 0) AS charged_kwh
                    FROM charges
                    GROUP BY month
                    """
                ).fetchall()
            }
        # Reverse to chronological order for chart consumers.
        out: list[dict[str, Any]] = []
        for r in reversed(rows):
            distance_km = round(float(r[1]), 1)
            energy_kwh = round(float(r[2]), 2)
            out.append(
                {
                    "month": r[0],
                    "distance_km": distance_km,
                    "energy_kwh": energy_kwh,
                    "cost": round(float(r[3]), 2),
                    "trips": int(r[4]),
                    "charged_kwh": round(charged.get(r[0], 0.0), 2),
                    "avg_consumption_kwh_100km": (
                        round(energy_kwh / distance_km * 100, 1)
                        if distance_km > 0
                        else None
                    ),
                }
            )
        return out

    async def async_weekly_history(self, weeks: int = 26) -> list[dict[str, Any]]:
        """Per-week rollup (km, kWh, cost, trips) for the last N ISO weeks."""
        return await self._hass.async_add_executor_job(
            self._weekly_history, weeks
        )

    def _weekly_history(self, weeks: int) -> list[dict[str, Any]]:
        # Bucket by the *Monday* of each trip's week, matching the
        # `distance_this_week` aggregate (`_period_start` uses
        # ``midnight - weekday()``) and the dashboard's Monday-start
        # grouping. SQLite's ``%W`` is avoided: it is Monday-based but
        # off-by-one against true ISO weeks at year boundaries. strftime
        # ``%w`` is Sunday=0, so Monday is ``(%w + 6) % 7`` days back.
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                """
                SELECT
                    date(
                        substr(started_at, 1, 10),
                        '-' || ((CAST(strftime('%w', substr(started_at, 1, 10))
                                 AS INTEGER) + 6) % 7) || ' days'
                    ) AS week_start,
                    COALESCE(SUM(distance_km), 0) AS distance_km,
                    COALESCE(SUM(energy_kwh), 0) AS energy_kwh,
                    COALESCE(SUM(cost), 0) AS cost,
                    COUNT(*) AS trips
                FROM trips
                GROUP BY week_start
                ORDER BY week_start DESC
                LIMIT ?
                """,
                (weeks,),
            ).fetchall()
            # charged_kwh from the charges table, bucketed by the same
            # Monday-of-week expression on ended_at. Weeks with charges but
            # no trips don't add a row; the lifetime accumulator covers those.
            charged = {
                r[0]: float(r[1])
                for r in conn.execute(
                    """
                    SELECT date(
                        substr(ended_at, 1, 10),
                        '-' || ((CAST(strftime('%w', substr(ended_at, 1, 10))
                                 AS INTEGER) + 6) % 7) || ' days'
                    ) AS week_start,
                    COALESCE(SUM(kwh), 0) AS charged_kwh
                    FROM charges
                    GROUP BY week_start
                    """
                ).fetchall()
            }

        out: list[dict[str, Any]] = []
        # Reverse to chronological order for chart consumers.
        for r in reversed(rows):
            week_start = r[0]
            distance_km = round(float(r[1]), 1)
            energy_kwh = round(float(r[2]), 2)
            # week_start is a Monday, so isocalendar() yields the correct
            # ISO year/week even when it straddles a calendar-year change.
            iso_year, iso_week, _ = date.fromisoformat(week_start).isocalendar()
            out.append(
                {
                    "week": f"{iso_year}-W{iso_week:02d}",
                    "week_start": week_start,
                    "distance_km": distance_km,
                    "energy_kwh": energy_kwh,
                    "cost": round(float(r[3]), 2),
                    "trips": int(r[4]),
                    "charged_kwh": round(charged.get(week_start, 0.0), 2),
                    "avg_consumption_kwh_100km": (
                        round(energy_kwh / distance_km * 100, 1)
                        if distance_km > 0
                        else None
                    ),
                }
            )
        return out

    async def async_daily_km_window(self, days: int = 60) -> list[dict[str, Any]]:
        """Per-day km totals for the last N days (zero-filled)."""
        return await self._hass.async_add_executor_job(
            self._daily_km_window, days
        )

    def _daily_km_window(self, days: int) -> list[dict[str, Any]]:
        from datetime import timedelta as _td  # noqa: PLC0415
        # dt_util.now() (HA-configured timezone), NOT datetime.now()
        # (host OS timezone) — stored timestamps come from dt_util.now(),
        # so day boundaries must use the same clock.
        cutoff = (dt_util.now() - _td(days=days)).date().isoformat()
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                """
                SELECT substr(started_at, 1, 10) AS day,
                       COALESCE(SUM(distance_km), 0) AS km
                FROM trips
                WHERE substr(started_at, 1, 10) >= ?
                GROUP BY day
                """,
                (cutoff,),
            ).fetchall()
        by_day = {r[0]: float(r[1]) for r in rows}
        # Zero-fill the full window so chart renderers don't draw gaps.
        out: list[dict[str, Any]] = []
        for i in range(days, -1, -1):
            d = (dt_util.now() - _td(days=i)).date().isoformat()
            out.append({"day": d, "distance_km": round(by_day.get(d, 0.0), 1)})
        return out

    async def async_trip_patterns(self, days: int = 90) -> dict[str, Any]:
        """Trip distribution by hour-of-day and weekday over the last N days.

        Returns: {
            "by_hour":    {"0": count, ..., "23": count},
            "by_weekday": {"0": count, ..., "6": count},  # 0=Mon
            "km_by_weekday": {"0": km, ...},
            "sample_count": int,
        }
        """
        return await self._hass.async_add_executor_job(
            self._trip_patterns, days
        )

    def _trip_patterns(self, days: int) -> dict[str, Any]:
        from datetime import timedelta as _td  # noqa: PLC0415
        cutoff = (dt_util.now() - _td(days=days)).isoformat()
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                "SELECT started_at, distance_km FROM trips WHERE started_at >= ?",
                (cutoff,),
            ).fetchall()
        by_hour: dict[int, int] = dict.fromkeys(range(24), 0)
        by_weekday: dict[int, int] = dict.fromkeys(range(7), 0)
        km_by_weekday: dict[int, float] = dict.fromkeys(range(7), 0.0)
        for started_at, distance in rows:
            try:
                ts = datetime.fromisoformat(started_at)
            except ValueError:
                continue
            by_hour[ts.hour] += 1
            by_weekday[ts.weekday()] += 1
            km_by_weekday[ts.weekday()] += float(distance or 0)
        return {
            "by_hour": {str(k): v for k, v in by_hour.items()},
            "by_weekday": {str(k): v for k, v in by_weekday.items()},
            "km_by_weekday": {
                str(k): round(v, 1) for k, v in km_by_weekday.items()
            },
            "sample_count": len(rows),
        }

    async def async_avg_trip_metrics(
        self, since: datetime
    ) -> dict[str, float | None]:
        """Per-trip averages over the window (distance, duration, speed, etc.).

        Powers the 'last N trips averages' headers from the BYD app.
        """
        return await self._hass.async_add_executor_job(
            self._avg_trip_metrics, since
        )

    def _avg_trip_metrics(self, since: datetime) -> dict[str, float | None]:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                f"""
                SELECT
                    AVG(distance_km) AS d,
                    AVG(duration_min) AS dur,
                    AVG(energy_kwh) AS e,
                    {_WEIGHTED_CONSUMPTION_SQL} AS c,
                    {_WEIGHTED_SPEED_SQL} AS s,
                    AVG(regen_kwh) AS r,
                    SUM(duration_min) AS total_driving,
                    COUNT(*) AS n
                FROM trips
                WHERE started_at >= ?
                """,
                (since.isoformat(),),
            ).fetchone()
        d, dur, e, c, s, r, total_driving, n = row
        return {
            # v0.8.17 — `if d` treated a legitimate 0.0 as missing and
            # published "unknown"; the test is None-ness, not truthiness.
            "avg_distance_km": float(d) if d is not None else None,
            "avg_duration_min": float(dur) if dur is not None else None,
            "avg_energy_kwh": float(e) if e is not None else None,
            "avg_consumption_kwh_100km": float(c) if c is not None else None,
            "avg_speed_kmh": float(s) if s is not None else None,
            # AVG() skips NULL rows, so trips without a power sensor
            # don't drag the regen mean toward zero.
            "avg_regen_kwh": float(r) if r is not None else None,
            "driving_time_min": float(total_driving) if total_driving else 0.0,
            "count": int(n or 0),
        }

    async def async_driver_stats(
        self, since: datetime
    ) -> list[dict[str, Any]]:
        """Per-driver usage over the window: trips, km, driving hours, energy.

        v0.5.43 — powers the 'who drives how much' panel. Trips with
        driver=NULL (no driver sensor configured, or nobody identified)
        are grouped under the 'unknown' bucket so totals still add up.
        """
        return await self._hass.async_add_executor_job(
            self._driver_stats, since
        )

    def _driver_stats(self, since: datetime) -> list[dict[str, Any]]:
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    COALESCE(driver, 'unknown') AS drv,
                    COUNT(*) AS trips,
                    COALESCE(SUM(distance_km), 0) AS km,
                    COALESCE(SUM(duration_min), 0) AS minutes,
                    COALESCE(SUM(energy_kwh), 0) AS kwh,
                    {_WEIGHTED_CONSUMPTION_SQL} AS cons
                FROM trips
                WHERE started_at >= ?
                GROUP BY drv
                ORDER BY km DESC
                """,
                (since.isoformat(),),
            ).fetchall()
        return [
            {
                "driver": r[0],
                "trips": int(r[1]),
                "distance_km": round(float(r[2]), 1),
                "hours": round(float(r[3]) / 60.0, 1),
                "energy_kwh": round(float(r[4]), 2),
                "avg_consumption_kwh_100km": (
                    round(float(r[5]), 1) if r[5] is not None else None
                ),
            }
            for r in rows
        ]

    async def async_tops_lists(self, limit: int = 9) -> dict[str, list[dict[str, Any]]]:
        """Top-N trips per criterion (distance, duration, consumption, efficiency, speed)."""
        return await self._hass.async_add_executor_job(self._tops_lists, limit)

    def _tops_lists(self, limit: int) -> dict[str, list[dict[str, Any]]]:
        criteria = {
            # SQL ORDER BY clause and human-friendly key
            "longest": "distance_km DESC",
            "longest_duration": "duration_min DESC",
            "top_consumption": "energy_kwh DESC",        # most kWh used in one trip
            "top_efficiency": "consumption_kwh_100km ASC",  # lowest kWh/100km = best
            "top_speed": "avg_speed_kmh DESC",
            "cheapest": "cost ASC",
        }
        out: dict[str, list[dict[str, Any]]] = {}
        with sqlite3.connect(self._path) as conn:
            for name, order_by in criteria.items():
                # Skip rows where the sorting key is NULL so 'best' isn't 'unknown'.
                # Also skip rows where the key is <=0: a "cheapest" trip costing
                # €0 because the user's energy_price option was misconfigured is
                # noise, not a record. Same goes for top_speed at 0 km/h etc.
                key = order_by.split()[0]
                rows = conn.execute(
                    f"""
                    SELECT id, started_at, ended_at, distance_km, duration_min,
                           energy_kwh, consumption_kwh_100km, avg_speed_kmh, cost,
                           currency, origin, destination
                    FROM trips
                    WHERE {key} IS NOT NULL AND {key} > 0
                    ORDER BY {order_by}
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                out[name] = [
                    {
                        "trip_id": r[0],
                        "started_at": r[1],
                        "ended_at": r[2],
                        "distance_km": r[3],
                        "duration_min": r[4],
                        "energy_kwh": r[5],
                        "consumption_kwh_100km": r[6],
                        "avg_speed_kmh": r[7],
                        "cost": r[8],
                        "currency": r[9],
                        "origin": r[10],
                        "destination": r[11],
                    }
                    for r in rows
                ]
        return out

    async def async_avg_charge_metrics(
        self, since: datetime
    ) -> dict[str, float | int]:
        """Per-session charge averages (kWh, cost) for the KPI tiles."""
        return await self._hass.async_add_executor_job(
            self._avg_charge_metrics, since
        )

    def _avg_charge_metrics(self, since: datetime) -> dict[str, float | int]:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                """
                SELECT
                    AVG(kwh) AS k,
                    AVG(total_cost) AS c,
                    COUNT(*) AS n,
                    AVG(soc_start) AS ss,
                    AVG(soc_end)   AS se,
                    AVG(CASE
                            WHEN soc_start IS NOT NULL AND soc_end IS NOT NULL
                                 AND soc_end > soc_start
                            THEN soc_end - soc_start
                            ELSE NULL
                        END) AS sd
                FROM charges
                WHERE ended_at >= ?
                """,
                (since.isoformat(),),
            ).fetchone()
        k, c, n, ss, se, sd = row
        return {
            "avg_kwh": float(k) if k else 0.0,
            "avg_cost": float(c) if c else 0.0,
            "count": int(n or 0),
            "avg_soc_start": float(ss) if ss is not None else None,
            "avg_soc_end": float(se) if se is not None else None,
            "avg_soc_added": float(sd) if sd is not None else None,
        }

    # ------------------------------------------------------------------
    # v0.5.54 — degradation tracking + season/weather aggregates
    # ------------------------------------------------------------------

    async def async_insert_capacity_snapshot(
        self, calibrated_kwh: float, declared_kwh: float, n_charges: int,
        when: datetime, odometer_km: float | None = None,
        logger_km: float | None = None,
    ) -> int:
        """v0.5.54/65 — persist a capacity-calibration snapshot.

        The coordinator calls this when `_async_refresh_battery_capacity`
        produces a value that differs from the latest stored row by more
        than `_CAPACITY_HISTORY_MIN_DELTA_KWH` (0.5 kWh). Repeated calls
        with the same value just update the latest row's `n_charges`
        (more data, same conclusion).

        `odometer_km` (v0.5.65) anchors the snapshot to the car's km at
        that moment — letting the dashboard plot SoH vs km, not just
        vs time.
        """
        return await self._hass.async_add_executor_job(
            self._insert_capacity_snapshot,
            calibrated_kwh, declared_kwh, n_charges, when,
            odometer_km, logger_km,
        )

    def _insert_capacity_snapshot(
        self, calibrated_kwh: float, declared_kwh: float,
        n_charges: int, when: datetime, odometer_km: float | None,
        logger_km: float | None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO capacity_history "
                "(observed_at, calibrated_kwh, declared_kwh, n_charges, "
                " odometer_km, logger_km) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (when.isoformat(), calibrated_kwh, declared_kwh,
                 n_charges, odometer_km, logger_km),
            )
            return int(cur.lastrowid or 0)

    async def async_latest_capacity_snapshot(
        self,
    ) -> tuple[datetime, float, float, int, float | None] | None:
        """v0.5.54/65 — return the most recent capacity snapshot or None.

        Tuple: (observed_at, calibrated_kwh, declared_kwh, n_charges,
        odometer_km). `odometer_km` may be None for pre-v0.5.65 rows.
        """
        return await self._hass.async_add_executor_job(
            self._latest_capacity_snapshot
        )

    def _latest_capacity_snapshot(
        self,
    ) -> tuple[datetime, float, float, int, float | None] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT observed_at, calibrated_kwh, declared_kwh, "
                "n_charges, odometer_km, logger_km "
                "FROM capacity_history ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        # Tuple kept 5-wide for callers that only need
        # (when, calibrated, declared, n_charges, odometer_km). The
        # full row including logger_km is available via
        # async_capacity_history if needed.
        return (
            datetime.fromisoformat(row[0]),
            float(row[1]),
            float(row[2]),
            int(row[3]),
            float(row[4]) if row[4] is not None else None,
        )

    async def async_capacity_history(
        self, limit: int = 24,
    ) -> list[dict[str, Any]]:
        """v0.5.54 — return the last `limit` capacity snapshots, oldest first.

        Powers the `battery_capacity_trend` sensor: comparing the first
        snapshot (oldest) with the latest yields the degradation slope.
        """
        return await self._hass.async_add_executor_job(
            self._capacity_history, limit
        )

    def _capacity_history(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT observed_at, calibrated_kwh, declared_kwh, "
                "n_charges, odometer_km, logger_km "
                "FROM capacity_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        # reverse so caller gets oldest → newest for easy diffing
        return [
            {
                "observed_at": r[0],
                "calibrated_kwh": float(r[1]),
                "declared_kwh": float(r[2]),
                "n_charges": int(r[3]),
                "odometer_km": float(r[4]) if r[4] is not None else None,
                "logger_km": float(r[5]) if r[5] is not None else None,
            }
            for r in reversed(rows)
        ]

    async def async_logger_total_km(self) -> float:
        """v0.5.66 — sum of distance_km across every persisted trip.

        Returns the kilometres the logger has actually witnessed. For
        SoH/degradation modelling this is what we want, not the car's
        lifetime odometer: it represents the use under conditions the
        logger knows about (DCFC habits, SoC ceiling, climate). 0.0
        when the trip table is empty.
        """
        return await self._hass.async_add_executor_job(
            self._logger_total_km
        )

    def _logger_total_km(self) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(distance_km), 0) FROM trips "
                "WHERE distance_km IS NOT NULL"
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    async def async_first_odometer_seen(self) -> float | None:
        """v0.5.57 — earliest non-NULL `odometer_start` ever recorded.

        Used to derive "km this car has been used UNDER our logging"
        (current_odometer − first_seen). If the car was bought used,
        this still gives the user a meaningful number (km since they
        started logging), not the lifetime mileage of the pack.
        """
        return await self._hass.async_add_executor_job(
            self._first_odometer_seen
        )

    def _first_odometer_seen(self) -> float | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(odometer_start) FROM trips "
                "WHERE odometer_start IS NOT NULL"
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def async_lifetime_dcfc_ratio(
        self,
    ) -> tuple[float | None, float, float]:
        """v0.5.57 — return (dcfc_kwh / total_kwh, dcfc_kwh, total_kwh).

        Drives the DCFC factor of the expected-SoH model. Returns
        (None, 0, 0) when no charges exist. Only charges with
        `kwh > 0` count; `is_dcfc` NULL is treated as AC (the safer
        assumption — DCFC is the surface we have to flag).
        """
        return await self._hass.async_add_executor_job(
            self._lifetime_dcfc_ratio
        )

    def _lifetime_dcfc_ratio(
        self,
    ) -> tuple[float | None, float, float]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN is_dcfc=1 THEN kwh ELSE 0 END), 0)
                        AS dcfc_kwh,
                    COALESCE(SUM(kwh), 0) AS total_kwh
                FROM charges
                WHERE kwh IS NOT NULL AND kwh > 0
                """
            ).fetchone()
        dcfc, total = float(row[0]), float(row[1])
        ratio = (dcfc / total) if total > 0 else None
        return (ratio, dcfc, total)

    async def async_avg_soc_end_recent(
        self, *, days: int = 30,
    ) -> float | None:
        """v0.5.57 — average `soc_end` over recent charges. Used to
        derive the "100% SoC daily" penalty in the expected-SoH model.
        Returns None when not enough data.
        """
        return await self._hass.async_add_executor_job(
            self._avg_soc_end_recent, days,
        )

    def _avg_soc_end_recent(self, days: int) -> float | None:
        # v0.8.17 — dt_util.now(), like every other window in this file.
        # datetime.now() is naive and in the HOST's timezone, so on a
        # UTC container it built a cutoff both offset-shifted and
        # missing a tzinfo, then compared it as TEXT against rows that
        # carry one.
        cutoff = (dt_util.now() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT AVG(soc_end) FROM charges "
                "WHERE soc_end IS NOT NULL AND ended_at >= ?",
                (cutoff,),
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def async_avg_ambient_temp_recent(
        self, *, days: int = 90,
    ) -> float | None:
        """v0.5.57/68 — average exterior temperature over recent trips.

        Used to classify the climate (cold/temperate/hot) for the SoH
        model. Prefers `avg_temp_c` (the car's own exterior temp
        sensor, sampled every metric tick — best granularity). Falls
        back to `ambient_temp_c` (legacy weather snapshot) for trips
        logged before v0.5.68 when the weather entity was still
        captured. None when neither side has data.
        """
        return await self._hass.async_add_executor_job(
            self._avg_ambient_temp_recent, days,
        )

    def _avg_ambient_temp_recent(self, days: int) -> float | None:
        # v0.8.17 — dt_util.now(), like every other window in this file.
        # datetime.now() is naive and in the HOST's timezone, so on a
        # UTC container it built a cutoff both offset-shifted and
        # missing a tzinfo, then compared it as TEXT against rows that
        # carry one.
        cutoff = (dt_util.now() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT AVG(COALESCE(avg_temp_c, ambient_temp_c)) "
                "FROM trips "
                "WHERE COALESCE(avg_temp_c, ambient_temp_c) IS NOT NULL "
                "  AND started_at >= ?",
                (cutoff,),
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def async_aggregates_by_season(
        self, *, hemisphere: str = "N",
    ) -> dict[str, dict[str, float | int]]:
        """v0.5.54 — lifetime aggregates grouped by meteorological season.

        Northern-hemisphere mapping (matches HA's default): spring=Mar–May,
        summer=Jun–Aug, autumn=Sep–Nov, winter=Dec–Feb. Set
        `hemisphere='S'` to swap. Per bucket returns
        {trips, distance_km, energy_kwh, avg_consumption_kwh_100km,
         avg_ambient_temp_c}.
        """
        return await self._hass.async_add_executor_job(
            self._aggregates_by_season, hemisphere,
        )

    def _aggregates_by_season(
        self, hemisphere: str,
    ) -> dict[str, dict[str, float | int]]:
        # v0.8.17 — read the month out of the stored local ISO text.
        # SQLite's strftime() CONVERTS an offset-bearing string to UTC
        # first, so a trip started 2026-03-01T00:30:00+02:00 was bucketed
        # as February — while naive legacy rows were NOT shifted, so the
        # same clock time landed in different buckets depending on which
        # write path created the row.
        # SQLite's month substring carries the leading zero.
        # Bucket via CASE so it's a single scan, no per-bucket query.
        n_to_s = {
            "winter": ("12", "01", "02"),
            "spring": ("03", "04", "05"),
            "summer": ("06", "07", "08"),
            "autumn": ("09", "10", "11"),
        }
        if hemisphere.upper() == "S":
            n_to_s = {
                "summer": ("12", "01", "02"),
                "autumn": ("03", "04", "05"),
                "winter": ("06", "07", "08"),
                "spring": ("09", "10", "11"),
            }
        out: dict[str, dict[str, float | int]] = {}
        with self._connect() as conn:
            for season, months in n_to_s.items():
                row = conn.execute(
                    f"""
                    SELECT
                        COUNT(*) AS n,
                        COALESCE(SUM(distance_km), 0) AS d,
                        COALESCE(SUM(energy_kwh), 0) AS e,
                        {_WEIGHTED_CONSUMPTION_SQL} AS c,
                        AVG(COALESCE(avg_temp_c, ambient_temp_c)) AS t
                    FROM trips
                    WHERE substr(started_at, 6, 2) IN (?, ?, ?)
                      AND distance_km IS NOT NULL AND distance_km > 0
                    """,
                    months,
                ).fetchone()
                n, d, e, c, t = row
                out[season] = {
                    "trips": int(n or 0),
                    "distance_km": round(float(d or 0), 1),
                    "energy_kwh": round(float(e or 0), 2),
                    "avg_consumption_kwh_100km": (
                        round(float(c), 1) if c is not None else None
                    ),
                    "avg_ambient_temp_c": (
                        round(float(t), 1) if t is not None else None
                    ),
                }
        return out

    async def async_aggregates_by_temp_bucket(
        self,
    ) -> dict[str, dict[str, float | int]]:
        """v0.5.54 — lifetime aggregates bucketed by ambient temperature.

        Buckets: cold (<5°C), cool (5–15), mild (15–25), warm (25–35),
        hot (≥35). Trips without `ambient_temp_c` are excluded; they
        accumulate under `unknown` for visibility.
        """
        return await self._hass.async_add_executor_job(
            self._aggregates_by_temp_bucket
        )

    def _aggregates_by_temp_bucket(
        self,
    ) -> dict[str, dict[str, float | int]]:
        out: dict[str, dict[str, float | int]] = {}
        buckets = {
            "cold": "ambient_temp_c < 5",
            "cool": "ambient_temp_c >= 5 AND ambient_temp_c < 15",
            "mild": "ambient_temp_c >= 15 AND ambient_temp_c < 25",
            "warm": "ambient_temp_c >= 25 AND ambient_temp_c < 35",
            "hot":  "ambient_temp_c >= 35",
            "unknown": "ambient_temp_c IS NULL",
        }
        with self._connect() as conn:
            for name, predicate in buckets.items():
                row = conn.execute(
                    f"""
                    SELECT
                        COUNT(*) AS n,
                        COALESCE(SUM(distance_km), 0) AS d,
                        COALESCE(SUM(energy_kwh), 0) AS e,
                        {_WEIGHTED_CONSUMPTION_SQL} AS c
                    FROM trips
                    WHERE {predicate}
                      AND distance_km IS NOT NULL AND distance_km > 0
                    """,
                ).fetchone()
                n, d, e, c = row
                out[name] = {
                    "trips": int(n or 0),
                    "distance_km": round(float(d or 0), 1),
                    "energy_kwh": round(float(e or 0), 2),
                    "avg_consumption_kwh_100km": (
                        round(float(c), 1) if c is not None else None
                    ),
                }
        return out

    async def async_aggregates_by_time_of_day(
        self,
    ) -> dict[str, dict[str, float | int]]:
        """v0.5.54 — lifetime aggregates bucketed by local start hour.

        night 22–06, morning 06–12, midday 12–15, afternoon 15–19,
        evening 19–22. Uses `substr(started_at, 12, 2)` — works
        because we store ISO strings with timezone offsets.
        """
        return await self._hass.async_add_executor_job(
            self._aggregates_by_time_of_day
        )

    def _aggregates_by_time_of_day(
        self,
    ) -> dict[str, dict[str, float | int]]:
        # Map hour ranges to bucket name. SQLite hour is "00".."23".
        buckets: dict[str, tuple[int, int]] = {
            "night": (22, 30),     # 22..23, 00..05 (handled via OR below)
            "morning": (6, 12),
            "midday": (12, 15),
            "afternoon": (15, 19),
            "evening": (19, 22),
        }
        out: dict[str, dict[str, float | int]] = {}
        with self._connect() as conn:
            for name, (lo, hi) in buckets.items():
                if name == "night":
                    where = (
                        "CAST(substr(started_at, 12, 2) AS INTEGER) >= 22 "
                        "OR CAST(substr(started_at, 12, 2) AS INTEGER) < 6"
                    )
                    params: tuple[Any, ...] = ()
                else:
                    where = (
                        "CAST(substr(started_at, 12, 2) AS INTEGER) >= ? "
                        "AND CAST(substr(started_at, 12, 2) AS INTEGER) < ?"
                    )
                    params = (lo, hi)
                row = conn.execute(
                    f"""
                    SELECT
                        COUNT(*) AS n,
                        COALESCE(SUM(distance_km), 0) AS d,
                        COALESCE(SUM(energy_kwh), 0) AS e,
                        {_WEIGHTED_CONSUMPTION_SQL} AS c
                    FROM trips
                    WHERE ({where})
                      AND distance_km IS NOT NULL AND distance_km > 0
                    """,
                    params,
                ).fetchone()
                n, d, e, c = row
                out[name] = {
                    "trips": int(n or 0),
                    "distance_km": round(float(d or 0), 1),
                    "energy_kwh": round(float(e or 0), 2),
                    "avg_consumption_kwh_100km": (
                        round(float(c), 1) if c is not None else None
                    ),
                }
        return out


def _row_to_record(row: sqlite3.Row) -> TripRecord:
    return TripRecord(
        trip_id=row["id"],
        started_at=datetime.fromisoformat(row["started_at"]),
        ended_at=datetime.fromisoformat(row["ended_at"]),
        duration_min=row["duration_min"],
        distance_km=row["distance_km"],
        odometer_start=row["odometer_start"],
        odometer_end=row["odometer_end"],
        soc_start=row["soc_start"],
        soc_end=row["soc_end"],
        soc_used_pct=row["soc_used_pct"],
        energy_kwh=row["energy_kwh"],
        consumption_kwh_100km=row["consumption_kwh_100km"],
        avg_speed_kmh=row["avg_speed_kmh"],
        max_power_kw=row["max_power_kw"],
        max_speed_kmh=row["max_speed_kmh"] if "max_speed_kmh" in row.keys() else None,
        regen_kwh=row["regen_kwh"] if "regen_kwh" in row.keys() else None,
        avg_temp_c=row["avg_temp_c"],
        origin=row["origin"],
        destination=row["destination"],
        cost=row["cost"],
        currency=row["currency"],
        journey_id=row["journey_id"] if "journey_id" in row.keys() else None,
        start_lat=row["start_lat"] if "start_lat" in row.keys() else None,
        start_lon=row["start_lon"] if "start_lon" in row.keys() else None,
        end_lat=row["end_lat"] if "end_lat" in row.keys() else None,
        end_lon=row["end_lon"] if "end_lon" in row.keys() else None,
        start_address=row["start_address"] if "start_address" in row.keys() else None,
        end_address=row["end_address"] if "end_address" in row.keys() else None,
        soc_start_source=row["soc_start_source"] if "soc_start_source" in row.keys() else None,
        energy_source=row["energy_source"] if "energy_source" in row.keys() else None,
        energy_from_power=row["energy_from_power"] if "energy_from_power" in row.keys() else None,
        gps_distance_km=row["gps_distance_km"] if "gps_distance_km" in row.keys() else None,
        kwh_charged_before=row["kwh_charged_before"] if "kwh_charged_before" in row.keys() else None,
        kwh_charged_during=row["kwh_charged_during"] if "kwh_charged_during" in row.keys() else None,
        confidence=row["confidence"] if "confidence" in row.keys() else None,
        driver=row["driver"] if "driver" in row.keys() else None,
        ambient_temp_c=row["ambient_temp_c"] if "ambient_temp_c" in row.keys() else None,
        weather_condition=row["weather_condition"] if "weather_condition" in row.keys() else None,
        humidity_pct=row["humidity_pct"] if "humidity_pct" in row.keys() else None,
        wind_kmh=row["wind_kmh"] if "wind_kmh" in row.keys() else None,
        precipitation_mm=row["precipitation_mm"] if "precipitation_mm" in row.keys() else None,
        cost_basis_per_kwh=row["cost_basis_per_kwh"] if "cost_basis_per_kwh" in row.keys() else None,
        cost_lifo=row["cost_lifo"] if "cost_lifo" in row.keys() else None,
        calibration_factor_k=row["calibration_factor_k"] if "calibration_factor_k" in row.keys() else None,
        consumption_lower_kwh_100km=row["consumption_lower_kwh_100km"] if "consumption_lower_kwh_100km" in row.keys() else None,
        consumption_upper_kwh_100km=row["consumption_upper_kwh_100km"] if "consumption_upper_kwh_100km" in row.keys() else None,
        low_confidence=bool(row["low_confidence"]) if ("low_confidence" in row.keys() and row["low_confidence"] is not None) else None,
        discharge_kwh=row["discharge_kwh"] if "discharge_kwh" in row.keys() else None,
        idle_minutes=row["idle_minutes"] if "idle_minutes" in row.keys() else None,
        v95_speed_kmh=row["v95_speed_kmh"] if "v95_speed_kmh" in row.keys() else None,
        highway_ratio_pct=row["highway_ratio_pct"] if "highway_ratio_pct" in row.keys() else None,
        elevation_gain_m=row["elevation_gain_m"] if "elevation_gain_m" in row.keys() else None,
        elevation_loss_m=row["elevation_loss_m"] if "elevation_loss_m" in row.keys() else None,
        elevation_variance_m2=row["elevation_variance_m2"] if "elevation_variance_m2" in row.keys() else None,
    )


def _row_to_charge(row: sqlite3.Row) -> ChargeRecord:
    is_dcfc_raw = row["is_dcfc"] if "is_dcfc" in row.keys() else None
    return ChargeRecord(
        charge_id=row["id"],
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        ended_at=datetime.fromisoformat(row["ended_at"]),
        kwh=row["kwh"],
        price_per_kwh=row["price_per_kwh"],
        total_cost=row["total_cost"],
        currency=row["currency"],
        soc_start=row["soc_start"],
        soc_end=row["soc_end"],
        location=row["location"],
        notes=row["notes"],
        is_dcfc=bool(is_dcfc_raw) if is_dcfc_raw is not None else None,
        price_locked=bool(row["price_locked"]) if "price_locked" in row.keys() and row["price_locked"] else False,
        evse_energy_kwh=(
            row["evse_energy_kwh"]
            if "evse_energy_kwh" in row.keys() else None
        ),
        charging_efficiency_pct=(
            row["charging_efficiency_pct"]
            if "charging_efficiency_pct" in row.keys() else None
        ),
        peak_charge_power_kw=(
            row["peak_charge_power_kw"]
            if "peak_charge_power_kw" in row.keys() else None
        ),
        temperature_c=(
            row["temperature_c"]
            if "temperature_c" in row.keys() else None
        ),
        energy_source=(
            row["energy_source"]
            if "energy_source" in row.keys() else None
        ),
    )


def period_start(now: datetime, period: str) -> datetime:
    """Return the start of `today` / `week` / `month` / `year` for aggregations."""
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight - timedelta(days=midnight.weekday())
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "30d":
        return now - timedelta(days=30)
    if period == "lifetime":
        # v0.6.1 — monotonically-growing accumulator anchor. Returning
        # `datetime.min` lets the existing aggregate queries sum over
        # every row without a special-case branch. The resulting
        # sensors are TOTAL_INCREASING and qualify for the HA Energy
        # dashboard (device_class=energy + total_increasing).
        return datetime.min.replace(tzinfo=now.tzinfo)
    raise ValueError(f"unknown period {period!r}")
