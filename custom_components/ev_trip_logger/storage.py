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
    avg_temp_c REAL,
    origin TEXT,
    destination TEXT,
    cost REAL,
    currency TEXT
);
CREATE INDEX IF NOT EXISTS idx_trips_started_at ON trips(started_at);
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
    avg_temp_c: float | None = None
    origin: str | None = None
    destination: str | None = None
    cost: float | None = None
    currency: str | None = None
    trip_id: int | None = field(default=None, compare=False)

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
            "avg_temp_c": self.avg_temp_c,
            "origin": self.origin,
            "destination": self.destination,
            "cost": self.cost,
            "currency": self.currency,
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
                    avg_temp_c, origin, destination, cost, currency
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    record.avg_temp_c,
                    record.origin,
                    record.destination,
                    record.cost,
                    record.currency,
                ),
            )
            return int(cur.lastrowid or 0)

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
        avg_temp_c=row["avg_temp_c"],
        origin=row["origin"],
        destination=row["destination"],
        cost=row["cost"],
        currency=row["currency"],
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
