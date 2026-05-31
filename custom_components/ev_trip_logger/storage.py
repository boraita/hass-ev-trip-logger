"""SQLite-backed storage for trip records."""
from __future__ import annotations

import logging
import sqlite3
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
    journey_id INTEGER
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
    is_dcfc INTEGER
);
CREATE INDEX IF NOT EXISTS idx_charges_ended_at ON charges(ended_at);
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

    async def async_init(self) -> None:
        """Create the schema if needed."""
        await self._hass.async_add_executor_job(self._init_db)

    def _init_db(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as conn:
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
        # Safe to call on fresh or migrated DBs.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trips_journey_id ON trips(journey_id)"
        )
        charge_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(charges)").fetchall()
        }
        if "is_dcfc" not in charge_cols:
            conn.execute("ALTER TABLE charges ADD COLUMN is_dcfc INTEGER")

    async def async_insert(self, record: TripRecord) -> int:
        """Persist a completed trip, return its id."""
        return await self._hass.async_add_executor_job(self._insert, record)

    def _insert(self, record: TripRecord) -> int:
        with sqlite3.connect(self._path) as conn:
            cur = conn.execute(
                """
                INSERT INTO trips (
                    started_at, ended_at, duration_min, distance_km,
                    odometer_start, odometer_end, soc_start, soc_end, soc_used_pct,
                    energy_kwh, consumption_kwh_100km, avg_speed_kmh, max_power_kw,
                    max_speed_kmh, regen_kwh,
                    avg_temp_c, origin, destination, cost, currency, journey_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                "UPDATE trips SET destination = ? WHERE id = ?",
                (destination, trip_id),
            )

    async def async_delete_last(self) -> bool:
        """Drop the most recent trip; returns True if anything was deleted."""
        return await self._hass.async_add_executor_job(self._delete_last)

    def _delete_last(self) -> bool:
        with sqlite3.connect(self._path) as conn:
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
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM trips ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _row_to_record(row) if row else None

    async def async_next_journey_id(self) -> int:
        return await self._hass.async_add_executor_job(self._next_journey_id)

    def _next_journey_id(self) -> int:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(journey_id), 0) + 1 FROM trips"
            ).fetchone()
        return int(row[0])

    async def async_journey_stages(self, journey_id: int) -> list[TripRecord]:
        return await self._hass.async_add_executor_job(self._journey_stages, journey_id)

    def _journey_stages(self, journey_id: int) -> list[TripRecord]:
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trips WHERE journey_id = ? ORDER BY id",
                (journey_id,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    async def async_journey_summary(self, journey_id: int) -> dict[str, Any] | None:
        return await self._hass.async_add_executor_job(self._journey_summary, journey_id)

    def _journey_summary(self, journey_id: int) -> dict[str, Any] | None:
        with sqlite3.connect(self._path) as conn:
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
        with sqlite3.connect(self._path) as conn:
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
        with sqlite3.connect(self._path) as conn:
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
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trips ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    async def async_recent_charges(self, limit: int = 10) -> list[ChargeRecord]:
        return await self._hass.async_add_executor_job(self._recent_charges, limit)

    def _recent_charges(self, limit: int) -> list[ChargeRecord]:
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM charges ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_charge(r) for r in rows]

    async def async_aggregates_since(self, since: datetime) -> dict[str, float | int]:
        """Aggregate distance / energy / cost / count from `since`."""
        return await self._hass.async_add_executor_job(self._aggregates_since, since)

    def _aggregates_since(self, since: datetime) -> dict[str, float | int]:
        with sqlite3.connect(self._path) as conn:
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
        with sqlite3.connect(self._path) as conn:
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
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM charges ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _row_to_charge(row) if row else None

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
        with sqlite3.connect(self._path) as conn:
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
            conn.execute(
                "UPDATE charges SET price_per_kwh = ?, total_cost = ?, "
                "location = ?, notes = ? WHERE id = ?",
                (new_price, new_total, new_location, new_notes, row["id"]),
            )
            updated = conn.execute(
                "SELECT * FROM charges WHERE id = ?", (row["id"],)
            ).fetchone()
        return _row_to_charge(updated)

    async def async_delete_last_charge(self) -> bool:
        return await self._hass.async_add_executor_job(self._delete_last_charge)

    def _delete_last_charge(self) -> bool:
        with sqlite3.connect(self._path) as conn:
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
        with sqlite3.connect(self._path) as conn:
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
        with sqlite3.connect(self._path) as conn:
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

        with sqlite3.connect(self._path) as conn:
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
