"""SQLite-backed storage for trip records."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import STORAGE_FILENAME_TEMPLATE

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
    kwh_charged_during REAL
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
    price_locked INTEGER
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
    trip_id: int | None = field(default=None, compare=False)

    @property
    def score(self) -> float | None:
        """Efficiency rating 0–10 derived from kWh/100km.

        Matches the BYD app curve: 14.5 kWh/100km ≈ 10, slope 0.6 per excess kWh.
        Returns None when we don't have a consumption figure.
        """
        e = self.consumption_kwh_100km
        if e is None or e <= 0:
            return None
        return max(0.0, min(10.0, 10.0 - max(0.0, e - 14.5) * 0.6))

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
        # Safe to call on fresh or migrated DBs.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trips_journey_id ON trips(journey_id)"
        )
        charge_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(charges)").fetchall()
        }
        if "is_dcfc" not in charge_cols:
            conn.execute("ALTER TABLE charges ADD COLUMN is_dcfc INTEGER")
        if "price_locked" not in charge_cols:
            conn.execute("ALTER TABLE charges ADD COLUMN price_locked INTEGER")
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
                    gps_distance_km, kwh_charged_before, kwh_charged_during
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.started_at.isoformat(),
                    record.ended_at.isoformat(),
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
        "kwh_charged_before", "kwh_charged_during",
    })

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
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(kwh), 0) AS kwh,
                       COUNT(*) AS count
                FROM charges
                WHERE ended_at >= ? AND ended_at <= ?
                """,
                (since.isoformat(), until.isoformat()),
            ).fetchone()
        return {"kwh": float(row[0] or 0), "count": int(row[1] or 0)}

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
                clean[k] = v.isoformat()
            else:
                clean[k] = v
        if not clean:
            return None
        cols = ", ".join(f"{k} = ?" for k in clean)
        params = list(clean.values()) + [trip_id]
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(f"UPDATE trips SET {cols} WHERE id = ?", params)
            if not cur.rowcount:
                return None
            row = conn.execute(
                "SELECT * FROM trips WHERE id = ?", (trip_id,),
            ).fetchone()
        return _row_to_record(row) if row else None

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
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM trips ORDER BY id DESC LIMIT 1"
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
                    COUNT(*) AS stages
                FROM trips WHERE journey_id = ?
                """,
                (journey_id,),
            ).fetchone()
        if not row or row[5] == 0:
            return None
        started_at, ended_at, distance, energy, cost, stages = row
        return {
            "journey_id": journey_id,
            "started_at": datetime.fromisoformat(started_at) if started_at else None,
            "ended_at": datetime.fromisoformat(ended_at) if ended_at else None,
            "distance_km": float(distance),
            "energy_kwh": float(energy),
            "cost": float(cost),
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
        excl = ""
        params: tuple = ()
        if current_journey_id is not None:
            excl = "AND journey_id != ?"
            params = (current_journey_id,)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT journey_id FROM trips
                WHERE journey_id IS NOT NULL {excl}
                GROUP BY journey_id
                ORDER BY MAX(id) DESC
                LIMIT ?
                """,
                params + (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            s = self._journey_summary(int(r[0]))
            if s is not None:
                out.append(s)
        return out

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
        with self._connect() as conn:
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
        if not total_km:
            return None
        return float(total_kwh) / float(total_km) * 100.0

    async def async_resolve_open_journey_id(
        self, home_zone: str
    ) -> int | None:
        """Return the journey_id of the currently-open journey, if any.

        Derives from the actual trip history rather than caching state
        in memory. An open journey is the first journey-tagged trip
        whose id is greater than the id of the most recent trip ending
        at home. If no such trip exists (every journey has been closed
        by a subsequent home arrival), returns None.

        Comparison is case-insensitive against `home_zone` (the
        configured device_tracker home slug, e.g. `home`).
        """
        return await self._hass.async_add_executor_job(
            self._resolve_open_journey_id, home_zone
        )

    def _resolve_open_journey_id(self, home_zone: str) -> int | None:
        slug = (home_zone or "home").strip().casefold()
        with self._connect() as conn:
            # v0.5.16 — DESC: if storage somehow has multiple distinct
            # journey_ids past the last home-arrival (a partial purge,
            # crash-mid-close, or a wrongly minted id), pick the NEWEST
            # one as the open journey. Picking the oldest accreted new
            # stages into a stale journey. Log a warning when ambiguity
            # is observed so the inconsistency is visible.
            rows = conn.execute(
                """
                SELECT DISTINCT journey_id FROM trips
                WHERE journey_id IS NOT NULL
                  AND id > COALESCE(
                      (SELECT MAX(id) FROM trips
                       WHERE LOWER(destination) = ?),
                      0)
                """,
                (slug,),
            ).fetchall()
            if not rows:
                return None
            if len(rows) > 1:
                _LOGGER.warning(
                    "Multiple open journey_ids past last home-arrival: %s — "
                    "picking newest. Storage may need a manual repair.",
                    sorted(int(r[0]) for r in rows),
                )
            row = conn.execute(
                """
                SELECT journey_id FROM trips
                WHERE journey_id IS NOT NULL
                  AND id > COALESCE(
                      (SELECT MAX(id) FROM trips
                       WHERE LOWER(destination) = ?),
                      0)
                ORDER BY id DESC
                LIMIT 1
                """,
                (slug,),
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
                ORDER BY MAX(id) DESC
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
            rows = conn.execute(
                "SELECT * FROM trips ORDER BY id DESC LIMIT ?", (limit,)
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
                "COALESCE(SUM(energy_kwh), 0) AS e, COALESCE(SUM(cost), 0) AS k "
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
            },
            "most_efficient": _row_to_record(efficient) if efficient else None,
            "longest": _row_to_record(longest) if longest else None,
            "cheapest": _row_to_record(cheapest) if cheapest else None,
        }

    async def async_recent_charges(self, limit: int = 10) -> list[ChargeRecord]:
        return await self._hass.async_add_executor_job(self._recent_charges, limit)

    def _recent_charges(self, limit: int) -> list[ChargeRecord]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM charges ORDER BY id DESC LIMIT ?", (limit,)
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
                    COUNT(*) AS count
                FROM trips WHERE started_at >= ?
                """,
                (since.isoformat(),),
            ).fetchone()
        distance, energy, cost, count = row
        avg_consumption = (energy / distance * 100) if distance else 0
        return {
            "distance_km": float(distance),
            "energy_kwh": float(energy),
            "cost": float(cost),
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
                    currency, soc_start, soc_end, location, notes, is_dcfc
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.started_at.isoformat() if record.started_at else None,
                    record.ended_at.isoformat(),
                    record.kwh,
                    record.price_per_kwh,
                    record.total_cost,
                    record.currency,
                    record.soc_start,
                    record.soc_end,
                    record.location,
                    record.notes,
                    int(record.is_dcfc) if record.is_dcfc is not None else None,
                ),
            )
            return int(cur.lastrowid or 0)

    async def async_get_last_charge(self) -> ChargeRecord | None:
        return await self._hass.async_add_executor_job(self._get_last_charge)

    def _get_last_charge(self) -> ChargeRecord | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM charges ORDER BY id DESC LIMIT 1"
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
    ) -> ChargeRecord | None:
        """Update a specific charge by id. Same semantics as
        update_last_charge but targets the given row instead of the
        most-recent one. Used by set_last_charge_price when a `charge_id`
        argument is passed (so the user can correct any historical
        external charge, not just the latest).
        """
        return await self._hass.async_add_executor_job(
            self._update_charge_by_id, charge_id, price_per_kwh, total_cost,
            location, notes,
        )

    # Columns the user can correct via the extended set_charge service.
    _CHARGE_USER_EDITABLE = frozenset({
        "started_at", "ended_at", "kwh", "soc_start", "soc_end",
        "location", "notes", "is_dcfc", "currency",
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
                clean[k] = v.isoformat()
            elif k == "is_dcfc":
                clean[k] = 1 if bool(v) else 0
            else:
                clean[k] = v
        if not clean:
            return None
        cols = ", ".join(f"{k} = ?" for k in clean)
        params = list(clean.values()) + [charge_id]
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(f"UPDATE charges SET {cols} WHERE id = ?", params)
            if not cur.rowcount:
                return None
            # If kwh changed and price_per_kwh exists, recompute total_cost.
            if "kwh" in clean:
                row = conn.execute(
                    "SELECT kwh, price_per_kwh FROM charges WHERE id = ?",
                    (charge_id,),
                ).fetchone()
                if row and row["price_per_kwh"] is not None:
                    conn.execute(
                        "UPDATE charges SET total_cost = ? WHERE id = ?",
                        (float(row["kwh"]) * float(row["price_per_kwh"]), charge_id),
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
            conn.execute(
                "UPDATE charges SET price_per_kwh = ?, total_cost = ?, "
                "location = ?, notes = ?, "
                "price_locked = COALESCE(?, price_locked) WHERE id = ?",
                (
                    new_price, new_total, new_location, new_notes,
                    price_locked, charge_id,
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
    ) -> ChargeRecord | None:
        return await self._hass.async_add_executor_job(
            self._update_last_charge, price_per_kwh, total_cost, location, notes
        )

    def _update_last_charge(
        self,
        price_per_kwh: float | None,
        total_cost: float | None,
        location: str | None,
        notes: str | None,
    ) -> ChargeRecord | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM charges ORDER BY id DESC LIMIT 1"
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
            conn.execute(
                "UPDATE charges SET price_per_kwh = ?, total_cost = ?, "
                "location = ?, notes = ?, price_locked = COALESCE(?, price_locked) "
                "WHERE id = ?",
                (
                    new_price, new_total, new_location, new_notes,
                    price_locked, row["id"],
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

    async def async_recompute_trip_costs_from_charges(
        self, default_price: float = 0.0
    ) -> int:
        """Re-cost every trip at the configured home tariff (`default_price`).

        Trip cost is **NOT** inherited from individual charges. External
        one-off charges (free public charger, expensive DC-fast on a road
        trip) should not change the cost basis for the trips that follow —
        the user's home tariff is the right reference. Each charge keeps
        its own actual price in its own record (visible in recent_charges
        and the AC/DC averages).

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
                conn.execute(
                    """
                    UPDATE trips SET
                        energy_kwh = distance_km * ? / 100.0,
                        consumption_kwh_100km = ?
                    WHERE (energy_kwh IS NULL OR energy_kwh <= 0)
                      AND distance_km IS NOT NULL AND distance_km > 0
                    """,
                    (avg_per_100, avg_per_100),
                )

            # Step 2 — re-cost every trip with energy from the configured
            # home tariff (free / external one-off charges don't propagate).
            cur = conn.execute(
                "UPDATE trips SET cost = energy_kwh * ? "
                "WHERE energy_kwh IS NOT NULL AND energy_kwh > 0",
                (price,),
            )
            return int(cur.rowcount or 0)

    async def async_extend_last_charge(
        self, extra_kwh: float, ended_at: datetime, extra_soc_pct: float | None = None
    ) -> ChargeRecord | None:
        """Append to the most recent charge instead of inserting a new row.

        Use when the cable hasn't been physically disconnected (plug=on)
        between two charging pulses: we treat the whole plugged interval
        as ONE session. Adds `extra_kwh` to the row's kwh, extends
        ended_at, recomputes total_cost from the existing price_per_kwh,
        and (if provided) bumps soc_end by `extra_soc_pct`.
        """
        return await self._hass.async_add_executor_job(
            self._extend_last_charge, extra_kwh, ended_at, extra_soc_pct
        )

    def _extend_last_charge(
        self,
        extra_kwh: float,
        ended_at: datetime,
        extra_soc_pct: float | None,
    ) -> ChargeRecord | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM charges ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            new_kwh = float(row["kwh"] or 0) + float(extra_kwh)
            new_total = new_kwh * float(row["price_per_kwh"] or 0)
            new_soc_end = row["soc_end"]
            if extra_soc_pct is not None and new_soc_end is not None:
                new_soc_end = float(new_soc_end) + float(extra_soc_pct)
            conn.execute(
                "UPDATE charges SET kwh = ?, total_cost = ?, ended_at = ?, "
                "soc_end = ? WHERE id = ?",
                (new_kwh, new_total, ended_at.isoformat(), new_soc_end, row["id"]),
            )
            updated = conn.execute(
                "SELECT * FROM charges WHERE id = ?", (row["id"],)
            ).fetchone()
        return _row_to_charge(updated)

    async def async_delete_last_charge(self) -> bool:
        return await self._hass.async_add_executor_job(self._delete_last_charge)

    def _delete_last_charge(self) -> bool:
        with self._connect() as conn:
            cur = conn.execute("SELECT id FROM charges ORDER BY id DESC LIMIT 1")
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

    def _charges_aggregates_since(self, since: datetime) -> dict[str, float | int]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(kwh), 0) AS kwh,
                    COALESCE(SUM(total_cost), 0) AS cost,
                    COUNT(*) AS count,
                    COALESCE(SUM(CASE WHEN is_dcfc = 1 THEN kwh ELSE 0 END), 0) AS dc_kwh,
                    COALESCE(SUM(CASE WHEN is_dcfc = 1 THEN total_cost ELSE 0 END), 0) AS dc_cost,
                    COALESCE(SUM(CASE WHEN is_dcfc = 0 THEN kwh ELSE 0 END), 0) AS ac_kwh,
                    COALESCE(SUM(CASE WHEN is_dcfc = 0 THEN total_cost ELSE 0 END), 0) AS ac_cost
                FROM charges WHERE ended_at >= ?
                """,
                (since.isoformat(),),
            ).fetchone()
        kwh, cost, count, dc_kwh, dc_cost, ac_kwh, ac_cost = row
        avg_price = (cost / kwh) if kwh else 0.0
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
            rows = conn.execute(
                """
                SELECT avg_temp_c, consumption_kwh_100km, distance_km
                FROM trips
                WHERE started_at >= ?
                  AND avg_temp_c IS NOT NULL
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
        return {
            "by_bucket": by_bucket,
            "bucket_size_c": bucket_size_c,
            "sample_count": len(rows),
        }

    async def async_export_csv(self, path: str) -> int:
        """Dump all trips to a CSV at `path`; returns row count."""
        return await self._hass.async_add_executor_job(self._export_csv, path)

    def _export_csv(self, path: str) -> int:
        import csv

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
        # Reverse to chronological order for chart consumers.
        return list(reversed([
            {
                "month": r[0],
                "distance_km": round(float(r[1]), 1),
                "energy_kwh": round(float(r[2]), 2),
                "cost": round(float(r[3]), 2),
                "trips": int(r[4]),
            }
            for r in rows
        ]))

    async def async_daily_km_window(self, days: int = 60) -> list[dict[str, Any]]:
        """Per-day km totals for the last N days (zero-filled)."""
        return await self._hass.async_add_executor_job(
            self._daily_km_window, days
        )

    def _daily_km_window(self, days: int) -> list[dict[str, Any]]:
        from datetime import timedelta as _td
        cutoff = (datetime.now() - _td(days=days)).date().isoformat()
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
            d = (datetime.now() - _td(days=i)).date().isoformat()
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
        from datetime import timedelta as _td
        cutoff = (datetime.now() - _td(days=days)).isoformat()
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                "SELECT started_at, distance_km FROM trips WHERE started_at >= ?",
                (cutoff,),
            ).fetchall()
        by_hour: dict[int, int] = {h: 0 for h in range(24)}
        by_weekday: dict[int, int] = {w: 0 for w in range(7)}
        km_by_weekday: dict[int, float] = {w: 0.0 for w in range(7)}
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
                """
                SELECT
                    AVG(distance_km) AS d,
                    AVG(duration_min) AS dur,
                    AVG(energy_kwh) AS e,
                    AVG(consumption_kwh_100km) AS c,
                    AVG(avg_speed_kmh) AS s,
                    SUM(duration_min) AS total_driving,
                    COUNT(*) AS n
                FROM trips
                WHERE started_at >= ?
                """,
                (since.isoformat(),),
            ).fetchone()
        d, dur, e, c, s, total_driving, n = row
        return {
            "avg_distance_km": float(d) if d else None,
            "avg_duration_min": float(dur) if dur else None,
            "avg_energy_kwh": float(e) if e else None,
            "avg_consumption_kwh_100km": float(c) if c else None,
            "avg_speed_kmh": float(s) if s else None,
            "driving_time_min": float(total_driving) if total_driving else 0.0,
            "count": int(n or 0),
        }

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
                SELECT AVG(kwh) AS k, AVG(total_cost) AS c, COUNT(*) AS n
                FROM charges
                WHERE ended_at >= ?
                """,
                (since.isoformat(),),
            ).fetchone()
        k, c, n = row
        return {
            "avg_kwh": float(k) if k else 0.0,
            "avg_cost": float(c) if c else 0.0,
            "count": int(n or 0),
        }


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
    raise ValueError(f"unknown period {period!r}")
