"""Tests for the SQLite-backed trip storage."""
from __future__ import annotations

import csv
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.ev_trip_logger.storage import (
    ChargeRecord,
    TripRecord,
    TripStorage,
    period_start,
)


def _charge(**overrides) -> ChargeRecord:
    base: dict = dict(
        ended_at=dt_util.now(),
        kwh=10.0,
        price_per_kwh=0.15,
        total_cost=1.5,
        currency="EUR",
    )
    base.update(overrides)
    return ChargeRecord(**base)


def _trip(**overrides) -> TripRecord:
    now = dt_util.now()
    base: dict = dict(
        started_at=now - timedelta(hours=1),
        ended_at=now,
        duration_min=60.0,
        distance_km=10.0,
        soc_used_pct=10.0,
        energy_kwh=7.5,
        consumption_kwh_100km=75.0,
        cost=1.0,
        currency="EUR",
    )
    base.update(overrides)
    return TripRecord(**base)


@pytest.fixture
async def storage(hass: HomeAssistant) -> TripStorage:
    s = TripStorage(hass, f"test_{uuid.uuid4().hex}")
    await s.async_init()
    return s


async def test_insert_and_get_last_roundtrip(storage: TripStorage) -> None:
    trip = _trip(distance_km=12.3, energy_kwh=8.4)
    trip_id = await storage.async_insert(trip)
    assert trip_id > 0

    fetched = await storage.async_get_last()
    assert fetched is not None
    assert fetched.trip_id == trip_id
    assert fetched.distance_km == pytest.approx(12.3)
    assert fetched.energy_kwh == pytest.approx(8.4)


async def test_get_last_returns_none_when_empty(storage: TripStorage) -> None:
    assert await storage.async_get_last() is None


async def test_delete_last_removes_only_most_recent(storage: TripStorage) -> None:
    first_id = await storage.async_insert(_trip(distance_km=5.0))
    await storage.async_insert(_trip(distance_km=15.0))

    assert await storage.async_delete_last() is True

    remaining = await storage.async_get_last()
    assert remaining is not None
    assert remaining.trip_id == first_id
    assert remaining.distance_km == pytest.approx(5.0)


async def test_delete_last_on_empty_returns_false(storage: TripStorage) -> None:
    assert await storage.async_delete_last() is False


async def test_aggregates_since_filters_and_sums(storage: TripStorage) -> None:
    now = dt_util.now()
    long_ago = now - timedelta(days=20)
    week_ago = now - timedelta(days=7)

    await storage.async_insert(
        _trip(started_at=long_ago, ended_at=long_ago + timedelta(minutes=30),
              distance_km=100.0, energy_kwh=20.0, cost=3.0)
    )
    await storage.async_insert(
        _trip(started_at=week_ago, ended_at=week_ago + timedelta(minutes=30),
              distance_km=50.0, energy_kwh=10.0, cost=1.5)
    )
    await storage.async_insert(
        _trip(started_at=now - timedelta(hours=2), ended_at=now - timedelta(hours=1),
              distance_km=30.0, energy_kwh=6.0, cost=0.9)
    )

    last_24h = await storage.async_aggregates_since(now - timedelta(days=1))
    assert last_24h["distance_km"] == pytest.approx(30.0)
    assert last_24h["energy_kwh"] == pytest.approx(6.0)
    assert last_24h["cost"] == pytest.approx(0.9)
    assert last_24h["count"] == 1
    assert last_24h["avg_consumption_kwh_100km"] == pytest.approx(20.0)  # 6/30*100

    last_30d = await storage.async_aggregates_since(now - timedelta(days=30))
    assert last_30d["distance_km"] == pytest.approx(180.0)
    assert last_30d["count"] == 3


async def test_aggregates_when_empty(storage: TripStorage) -> None:
    aggs = await storage.async_aggregates_since(dt_util.now() - timedelta(days=1))
    assert aggs == {
        "distance_km": 0.0,
        "energy_kwh": 0.0,
        "regen_kwh": 0.0,
        "cost": 0.0,
        "count": 0,
        "avg_consumption_kwh_100km": 0.0,
    }


async def test_records_returns_none_when_empty(storage: TripStorage) -> None:
    assert await storage.async_records() is None


async def test_records_picks_bests_and_totals(storage: TripStorage) -> None:
    await storage.async_insert(
        _trip(distance_km=10.0, energy_kwh=2.0, consumption_kwh_100km=20.0, cost=1.0)
    )
    eff_id = await storage.async_insert(
        _trip(distance_km=8.0, energy_kwh=0.8, consumption_kwh_100km=10.0, cost=0.5)
    )
    long_id = await storage.async_insert(
        _trip(distance_km=50.0, energy_kwh=12.0, consumption_kwh_100km=24.0, cost=3.0)
    )
    cheap_id = await storage.async_insert(
        _trip(distance_km=5.0, energy_kwh=1.0, consumption_kwh_100km=20.0, cost=0.1)
    )

    rec = await storage.async_records()
    assert rec is not None
    assert rec["count"] == 4
    # lowest consumption == most efficient == highest score
    assert rec["most_efficient"].trip_id == eff_id
    assert rec["longest"].trip_id == long_id
    assert rec["cheapest"].trip_id == cheap_id
    others = [rec["longest"].score, rec["cheapest"].score]
    assert rec["most_efficient"].score >= max(s for s in others if s is not None)
    assert rec["totals"]["trips"] == 4
    assert rec["totals"]["distance_km"] == pytest.approx(73.0)
    assert rec["totals"]["cost"] == pytest.approx(4.6)


async def test_recent_trips_respects_limit(storage: TripStorage) -> None:
    for i in range(6):
        await storage.async_insert(_trip(distance_km=float(i)))
    assert len(await storage.async_recent_trips(3)) == 3
    assert len(await storage.async_recent_trips(100)) == 6


async def test_export_csv_writes_all_rows(
    storage: TripStorage, tmp_path: Path
) -> None:
    await storage.async_insert(_trip(distance_km=11.1))
    await storage.async_insert(_trip(distance_km=22.2))

    path = tmp_path / "trips.csv"
    rows = await storage.async_export_csv(str(path))

    assert rows == 2
    with open(path) as fh:
        records = list(csv.DictReader(fh))
    assert len(records) == 2
    assert {float(r["distance_km"]) for r in records} == {11.1, 22.2}


async def test_export_csv_empty(storage: TripStorage, tmp_path: Path) -> None:
    path = tmp_path / "trips.csv"
    rows = await storage.async_export_csv(str(path))
    assert rows == 0
    assert path.read_text() == ""


async def test_insert_and_get_last_charge(storage: TripStorage) -> None:
    cid = await storage.async_insert_charge(_charge(kwh=18.5, price_per_kwh=0.42, total_cost=7.77))
    assert cid > 0
    fetched = await storage.async_get_last_charge()
    assert fetched is not None
    assert fetched.charge_id == cid
    assert fetched.kwh == pytest.approx(18.5)
    assert fetched.price_per_kwh == pytest.approx(0.42)
    assert fetched.total_cost == pytest.approx(7.77)


async def test_get_last_charge_when_empty(storage: TripStorage) -> None:
    assert await storage.async_get_last_charge() is None


async def test_delete_last_charge_keeps_previous(storage: TripStorage) -> None:
    first = await storage.async_insert_charge(_charge(kwh=10.0, total_cost=1.5))
    await storage.async_insert_charge(_charge(kwh=20.0, total_cost=8.0))
    assert await storage.async_delete_last_charge() is True
    remaining = await storage.async_get_last_charge()
    assert remaining is not None
    assert remaining.charge_id == first
    assert remaining.kwh == pytest.approx(10.0)


async def test_charges_aggregates_avg_price(storage: TripStorage) -> None:
    now = dt_util.now()
    await storage.async_insert_charge(
        _charge(ended_at=now - timedelta(days=5), kwh=20.0, total_cost=3.0)
    )
    await storage.async_insert_charge(
        _charge(ended_at=now - timedelta(days=1), kwh=10.0, total_cost=5.0)
    )
    aggs = await storage.async_charges_aggregates_since(now - timedelta(days=10))
    assert aggs["kwh"] == pytest.approx(30.0)
    assert aggs["total_cost"] == pytest.approx(8.0)
    assert aggs["count"] == 2
    # Weighted: 8.0 / 30.0 = 0.2667
    assert aggs["avg_price_per_kwh"] == pytest.approx(8.0 / 30.0)


@pytest.mark.parametrize(
    "consumption, expected",
    [
        (14.54, pytest.approx(9.98, abs=0.05)),  # BYD app: 10.0
        (16.72, pytest.approx(8.67, abs=0.05)),  # BYD app: 8.6
        (17.68, pytest.approx(8.09, abs=0.05)),  # BYD app: 8.0
        (19.09, pytest.approx(7.25, abs=0.05)),  # BYD app: 7.2
        (21.92, pytest.approx(5.55, abs=0.05)),  # BYD app: 5.4
        (5.0, 10.0),     # very efficient → capped at 10
        (50.0, 0.0),     # very wasteful → capped at 0
        (0.0, None),     # no consumption → no score
        (None, None),
    ],
)
def test_trip_score_curve_matches_byd_app(consumption, expected) -> None:
    trip = TripRecord(
        started_at=dt_util.now(),
        ended_at=dt_util.now(),
        duration_min=10.0,
        distance_km=1.0,
        consumption_kwh_100km=consumption,
    )
    assert trip.score == expected


def test_period_start_today() -> None:
    now = datetime(2026, 5, 28, 14, 35, 12)
    assert period_start(now, "today") == datetime(2026, 5, 28, 0, 0, 0)


def test_period_start_week_starts_on_monday() -> None:
    # 2026-05-28 is a Thursday → week starts Monday 2026-05-25
    now = datetime(2026, 5, 28, 14, 35, 12)
    assert period_start(now, "week") == datetime(2026, 5, 25, 0, 0, 0)


def test_period_start_month_year_30d() -> None:
    now = datetime(2026, 5, 28, 14, 35, 12)
    assert period_start(now, "month") == datetime(2026, 5, 1, 0, 0, 0)
    assert period_start(now, "year") == datetime(2026, 1, 1, 0, 0, 0)
    assert period_start(now, "30d") == now - timedelta(days=30)


def test_period_start_unknown_raises() -> None:
    with pytest.raises(ValueError):
        period_start(dt_util.now(), "decade")
