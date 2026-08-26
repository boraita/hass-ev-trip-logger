"""Tests for the SQLite-backed trip storage."""
from __future__ import annotations

import csv
import uuid
from datetime import datetime, timedelta, UTC
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
    base: dict = {
        "ended_at": dt_util.now(),
        "kwh": 10.0,
        "price_per_kwh": 0.15,
        "total_cost": 1.5,
        "currency": "EUR",
    }
    base.update(overrides)
    return ChargeRecord(**base)


def _trip(**overrides) -> TripRecord:
    now = dt_util.now()
    base: dict = {
        "started_at": now - timedelta(hours=1),
        "ended_at": now,
        "duration_min": 60.0,
        "distance_km": 10.0,
        "soc_used_pct": 10.0,
        "energy_kwh": 7.5,
        "consumption_kwh_100km": 75.0,
        "cost": 1.0,
        "currency": "EUR",
    }
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
        "discharge_kwh": 0.0,
        "regen_ratio": 0.0,
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


async def test_score_baseline_p5_below_threshold_returns_none(
    storage: TripStorage,
) -> None:
    """v0.5.50 — under the min-trips floor, coordinator must fall back."""
    for c in [12.0, 13.0, 14.0]:
        await storage.async_insert(
            _trip(distance_km=20.0, energy_kwh=c * 0.2, consumption_kwh_100km=c)
        )
    p5, n = await storage.async_score_baseline_p5(min_distance_km=5.0, min_trips=10)
    assert p5 is None
    assert n == 3


async def test_score_baseline_p5_uses_lowest_quantile_when_enough_trips(
    storage: TripStorage,
) -> None:
    """v0.5.50 — P5 of consumption with enough trips picks the best tail.

    With 20 trips ascending [10..29] kWh/100km, P5 idx = floor(0.05*19) = 0,
    so it picks the very best — 10.0.
    """
    values = list(range(10, 30))  # 20 trips: 10, 11, ..., 29
    for c in values:
        await storage.async_insert(
            _trip(
                distance_km=20.0,
                energy_kwh=c * 0.2,
                consumption_kwh_100km=float(c),
            )
        )
    p5, n = await storage.async_score_baseline_p5(min_distance_km=5.0, min_trips=10)
    assert n == 20
    assert p5 == pytest.approx(10.0)


async def test_score_baseline_p5_filters_short_trips_and_outliers(
    storage: TripStorage,
) -> None:
    """v0.5.50 — short trips (<5 km) and out-of-band sensor errors must
    not anchor the baseline. Only trips with 5 km+ AND 5..50 kWh/100km
    are eligible.
    """
    # 12 long & realistic trips: baseline candidates
    for c in [12.0, 14.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 22.0, 24.0, 27.0]:
        await storage.async_insert(
            _trip(distance_km=20.0, energy_kwh=c * 0.2, consumption_kwh_100km=c)
        )
    # Two phantom trips that should NOT compete for the baseline:
    await storage.async_insert(  # < 5 km
        _trip(distance_km=1.0, energy_kwh=0.05, consumption_kwh_100km=5.0)
    )
    await storage.async_insert(  # > 50 kWh/100km (sensor glitch)
        _trip(distance_km=20.0, energy_kwh=12.0, consumption_kwh_100km=60.0)
    )
    p5, n = await storage.async_score_baseline_p5(min_distance_km=5.0, min_trips=10)
    assert n == 12
    # P5 with n=12 → idx = floor(0.05*11) = 0 → 12.0 (the actual best
    # eligible, NOT the 5.0 short-trip outlier).
    assert p5 == pytest.approx(12.0)


async def test_recent_trips_respects_limit(storage: TripStorage) -> None:
    for i in range(6):
        await storage.async_insert(_trip(distance_km=float(i)))
    assert len(await storage.async_recent_trips(3)) == 3
    assert len(await storage.async_recent_trips(100)) == 6


async def test_recent_trips_orders_by_date_not_insertion_id(
    storage: TripStorage,
) -> None:
    """A historical backfill (recover_missing_trips, log_manual_trip) can
    insert an OLD trip after a NEWER one already exists — it gets a
    higher autoincrement id despite an earlier ended_at. `recent_trips`
    must still rank it behind the genuinely more recent trip, not in
    front of it (the pre-v0.8.13 bug: `ORDER BY id DESC` let a bulk
    backfill evict real recent trips from the window entirely).
    """
    now = dt_util.now()
    await storage.async_insert(
        _trip(
            started_at=now - timedelta(days=1, hours=1),
            ended_at=now - timedelta(days=1),
            distance_km=20.0,
        )
    )
    # Inserted SECOND (higher id) but chronologically much OLDER —
    # simulates a recover_missing_trips backfill for a week-old gap.
    await storage.async_insert(
        _trip(
            started_at=now - timedelta(days=7, hours=1),
            ended_at=now - timedelta(days=7),
            distance_km=3.0,
        )
    )
    recent = await storage.async_recent_trips(2)
    assert [t.distance_km for t in recent] == [20.0, 3.0]


async def test_recent_charges_orders_by_date_not_insertion_id(
    storage: TripStorage,
) -> None:
    """Same fix as recent_trips, for charges."""
    now = dt_util.now()
    await storage.async_insert_charge(
        _charge(ended_at=now - timedelta(days=1), kwh=20.0)
    )
    await storage.async_insert_charge(
        _charge(ended_at=now - timedelta(days=7), kwh=3.0)
    )
    recent = await storage.async_recent_charges(2)
    assert [c.kwh for c in recent] == [20.0, 3.0]


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


def _at(y: int, m: int, d: int, h: int = 10) -> datetime:
    """A timezone-aware local datetime (matches dt_util.now() used at insert)."""
    return datetime(y, m, d, h, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)


async def test_weekly_history_buckets_by_iso_monday(storage: TripStorage) -> None:
    # Mon 2026-06-22 and Sun 2026-06-28 are the same ISO week (2026-W26);
    # Mon 2026-06-29 starts the next week (2026-W27).
    await storage.async_insert(
        _trip(started_at=_at(2026, 6, 22), ended_at=_at(2026, 6, 22, 11),
              distance_km=100.0, energy_kwh=20.0, cost=3.0)
    )
    await storage.async_insert(
        _trip(started_at=_at(2026, 6, 28), ended_at=_at(2026, 6, 28, 11),
              distance_km=117.0, energy_kwh=22.2, cost=0.1)
    )
    await storage.async_insert(
        _trip(started_at=_at(2026, 6, 29), ended_at=_at(2026, 6, 29, 11),
              distance_km=40.0, energy_kwh=8.0, cost=1.0)
    )

    weeks = await storage.async_weekly_history(26)
    assert len(weeks) == 2
    # Chronological order.
    w26, w27 = weeks
    assert w26["week"] == "2026-W26"
    assert w26["week_start"] == "2026-06-22"
    assert w26["distance_km"] == pytest.approx(217.0)
    assert w26["energy_kwh"] == pytest.approx(42.2)
    assert w26["cost"] == pytest.approx(3.1)
    assert w26["trips"] == 2
    # 42.2 / 217.0 * 100 = 19.4
    assert w26["avg_consumption_kwh_100km"] == pytest.approx(19.4, abs=0.05)

    assert w27["week"] == "2026-W27"
    assert w27["week_start"] == "2026-06-29"
    assert w27["trips"] == 1


async def test_weekly_history_labels_year_boundary_week(storage: TripStorage) -> None:
    # Mon 2025-12-29's week contains Thu 2026-01-01 → ISO week 2026-W01.
    await storage.async_insert(
        _trip(started_at=_at(2025, 12, 29), ended_at=_at(2025, 12, 29, 11),
              distance_km=12.0, energy_kwh=2.0, cost=0.3)
    )
    weeks = await storage.async_weekly_history(26)
    assert len(weeks) == 1
    assert weeks[0]["week"] == "2026-W01"
    assert weeks[0]["week_start"] == "2025-12-29"


async def test_weekly_history_empty_is_empty_list(storage: TripStorage) -> None:
    assert await storage.async_weekly_history(26) == []


async def test_weekly_history_charged_kwh_buckets_on_ended_at(
    storage: TripStorage,
) -> None:
    # Trips anchor the week rows; charges are bucketed by ended_at into the
    # same Monday-of-week. A charge closing late Sunday (2026-06-28 23:00)
    # still lands in week 2026-W26 (Monday 2026-06-22).
    await storage.async_insert(
        _trip(started_at=_at(2026, 6, 22), ended_at=_at(2026, 6, 22, 11))
    )
    await storage.async_insert(
        _trip(started_at=_at(2026, 6, 29), ended_at=_at(2026, 6, 29, 11))
    )
    await storage.async_insert_charge(_charge(ended_at=_at(2026, 6, 23), kwh=30.0))
    await storage.async_insert_charge(_charge(ended_at=_at(2026, 6, 28, 23), kwh=5.0))
    await storage.async_insert_charge(_charge(ended_at=_at(2026, 6, 29), kwh=12.0))

    weeks = {w["week"]: w for w in await storage.async_weekly_history(26)}
    assert weeks["2026-W26"]["charged_kwh"] == pytest.approx(35.0)
    assert weeks["2026-W27"]["charged_kwh"] == pytest.approx(12.0)


async def test_weekly_history_charged_kwh_zero_when_no_charges(
    storage: TripStorage,
) -> None:
    await storage.async_insert(
        _trip(started_at=_at(2026, 6, 22), ended_at=_at(2026, 6, 22, 11))
    )
    weeks = await storage.async_weekly_history(26)
    assert weeks[0]["charged_kwh"] == 0.0


async def test_monthly_history_charged_kwh_buckets_on_ended_at(
    storage: TripStorage,
) -> None:
    await storage.async_insert(
        _trip(started_at=_at(2026, 5, 15), ended_at=_at(2026, 5, 15, 11))
    )
    await storage.async_insert(
        _trip(started_at=_at(2026, 6, 10), ended_at=_at(2026, 6, 10, 11))
    )
    await storage.async_insert_charge(_charge(ended_at=_at(2026, 5, 20), kwh=40.0))
    await storage.async_insert_charge(_charge(ended_at=_at(2026, 6, 1), kwh=8.0))

    months = {m["month"]: m for m in await storage.async_monthly_history(12)}
    assert months["2026-05"]["charged_kwh"] == pytest.approx(40.0)
    assert months["2026-06"]["charged_kwh"] == pytest.approx(8.0)


async def test_monthly_history_avg_consumption_per_100km(
    storage: TripStorage,
) -> None:
    # Two trips in June: 60 km / 9 kWh and 40 km / 11 kWh → 20 kWh over 100 km
    # = 20.0 kWh/100km for the month.
    await storage.async_insert(
        _trip(started_at=_at(2026, 6, 5), ended_at=_at(2026, 6, 5, 11),
              distance_km=60.0, energy_kwh=9.0)
    )
    await storage.async_insert(
        _trip(started_at=_at(2026, 6, 12), ended_at=_at(2026, 6, 12, 11),
              distance_km=40.0, energy_kwh=11.0)
    )
    months = {m["month"]: m for m in await storage.async_monthly_history(12)}
    assert months["2026-06"]["avg_consumption_kwh_100km"] == pytest.approx(20.0)


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


@pytest.mark.parametrize(
    "consumption, baseline, expected",
    [
        # On the baseline → 10/10
        (12.0, 12.0, pytest.approx(10.0)),
        # 5 kWh/100km above baseline of 12 → 10 - 3 = 7
        (17.0, 12.0, pytest.approx(7.0)),
        # 29.5 vs the old 14.5 default → 1.0 (the Tesla-in-Alps case)
        (29.5, 14.5, pytest.approx(1.0)),
        # Same 29.5 against a Tesla-calibrated 19.5 baseline → 4.0
        (29.5, 19.5, pytest.approx(4.0)),
        # Below baseline never goes above 10
        (8.0, 14.5, 10.0),
        # No consumption → None
        (None, 14.5, None),
        (0.0, 14.5, None),
    ],
)
def test_score_with_baseline_shifts_anchor(consumption, baseline, expected) -> None:
    """v0.5.50 — score curve re-anchors to the per-car baseline."""
    trip = TripRecord(
        started_at=dt_util.now(),
        ended_at=dt_util.now(),
        duration_min=10.0,
        distance_km=1.0,
        consumption_kwh_100km=consumption,
    )
    assert trip.score_with_baseline(baseline) == expected


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


async def test_discharge_kwh_round_trip_and_regen_ratio(
    storage: TripStorage,
) -> None:
    """v0.6.0 — discharge_kwh is persisted and the period aggregate
    derives regen_ratio = sum(regen)/sum(discharge), letting dashboards
    render 'you got X % back from braking' without bespoke templates.
    """
    now = dt_util.now()
    # Trip with regen 1.0 / discharge 5.0 → ratio 0.20
    await storage.async_insert(_trip(
        ended_at=now, energy_kwh=4.0, regen_kwh=1.0, discharge_kwh=5.0,
    ))
    # Trip with regen 0.5 / discharge 5.0 → second trip contributes
    # 5.5/10.0 = 0.55 ratio when summed: regen=1.5, discharge=10.0
    await storage.async_insert(_trip(
        ended_at=now, energy_kwh=4.5, regen_kwh=0.5, discharge_kwh=5.0,
    ))
    aggs = await storage.async_aggregates_since(now - timedelta(days=1))
    assert aggs["regen_kwh"] == pytest.approx(1.5)
    assert aggs["discharge_kwh"] == pytest.approx(10.0)
    assert aggs["regen_ratio"] == pytest.approx(0.15)
    # Round-trip: reading the latest trip back picks up discharge_kwh.
    last = await storage.async_get_last()
    assert last is not None
    assert last.discharge_kwh == pytest.approx(5.0)


async def test_peak_charge_power_persists_and_aggregates_high_power(
    storage: TripStorage,
) -> None:
    """v0.6.0 — peak_charge_power_kw is persisted on insert AND on
    extend (max-merge), and the period aggregate flags the >=100 kW
    cohort separately so the SoH model can score high-stress sessions
    (Geotab fleet study).
    """
    now = dt_util.now()
    # AC home charge — peak ~7 kW.
    await storage.async_insert_charge(_charge(
        kwh=15.0, ended_at=now, peak_charge_power_kw=7.2,
    ))
    # DCFC session — peak 150 kW.
    await storage.async_insert_charge(_charge(
        kwh=40.0, ended_at=now, peak_charge_power_kw=150.0,
    ))
    # Merge an extra pulse onto the most recent charge with a lower
    # peak (50 kW). The session's peak must STAY at 150 kW — i.e. the
    # merge does a max, not a replace.
    merged = await storage.async_extend_last_charge(
        extra_kwh=2.0,
        ended_at=now + timedelta(minutes=5),
        new_peak_power_kw=50.0,
    )
    assert merged is not None
    assert merged.peak_charge_power_kw == pytest.approx(150.0)
    # Period aggregate: only the DCFC counts toward high_power.
    agg = await storage.async_charges_aggregates_since(now - timedelta(days=1))
    assert agg["high_power_kwh"] == pytest.approx(42.0)
    assert agg["high_power_count"] == 1
    assert agg["peak_power_max_kw"] == pytest.approx(150.0)


async def test_charges_aggregates_pairs_kwh_with_evse(
    storage: TripStorage,
) -> None:
    """v0.5.101 — period aggregates expose a paired kwh_with_evse +
    evse_kwh so dashboards can compute charging efficiency as a
    proper ratio.

    The dashboard's pre-fix template divided `energy_charged_this_month`
    (all charges, including ones with NULL evse) by sum(evse_energy_kwh)
    over only the rows that had EVSE — five charges of 15 kWh each
    against one EVSE-bearing 16 kWh row produced 75/16 × 100 = 469 %.
    The fixed aggregate pairs both sides so the ratio is always
    0-100 %.
    """
    now = dt_util.now()
    # 3 charges WITHOUT evse, 1 WITH.
    for kwh in (15.0, 15.0, 15.0):
        await storage.async_insert_charge(_charge(kwh=kwh, ended_at=now))
    await storage.async_insert_charge(
        _charge(kwh=16.0, ended_at=now,
                evse_energy_kwh=18.0, charging_efficiency_pct=88.9),
    )
    agg = await storage.async_charges_aggregates_since(
        now - timedelta(days=30),
    )
    # Total kwh includes all 4 charges. Paired sums only the row with
    # EVSE: 16 kwh / 18 evse × 100 ≈ 88.9 %.
    assert agg["kwh"] == pytest.approx(61.0)
    assert agg["kwh_with_evse"] == pytest.approx(16.0)
    assert agg["evse_kwh"] == pytest.approx(18.0)
    assert agg["evse_count"] == 1
    assert agg["charging_efficiency_pct"] == pytest.approx(88.9, abs=0.2)


async def test_period_start_lifetime_anchors_at_datetime_min(
    storage: TripStorage,
) -> None:
    """v0.6.1 — `period_start(now, 'lifetime')` returns a sentinel
    early enough that every persisted row falls inside, so a
    lifetime-period ChargesAggregateSensor sums the entire history
    without a special-case branch in the query path.
    """
    from custom_components.ev_trip_logger.storage import period_start

    now = dt_util.now()
    sentinel = period_start(now, "lifetime")
    # 50 years before "now" still has to fall after the sentinel —
    # otherwise rows older than a couple of years wouldn't be counted
    # in the lifetime accumulator.
    very_old = now - timedelta(days=365 * 50)
    assert sentinel < very_old
    # Tz-aware (so the SQL `>= ?` comparison against ISO strings
    # doesn't blow up on a tz-naive sentinel vs tz-aware row).
    assert sentinel.tzinfo is not None

    # Round-trip: a row inserted with an ancient `ended_at` is summed
    # by `_charges_aggregates_since(period_start(now, 'lifetime'))`.
    await storage.async_insert_charge(_charge(
        kwh=10.0, ended_at=very_old, evse_energy_kwh=11.0,
        charging_efficiency_pct=90.9,
    ))
    await storage.async_insert_charge(_charge(
        kwh=20.0, ended_at=now, evse_energy_kwh=22.0,
        charging_efficiency_pct=90.9, peak_charge_power_kw=120.0,
    ))
    agg = await storage.async_charges_aggregates_since(sentinel)
    # Both charges counted.
    assert agg["kwh"] == pytest.approx(30.0)
    assert agg["evse_kwh"] == pytest.approx(33.0)
    # Only the 120 kW one falls in the high-power cohort.
    assert agg["high_power_kwh"] == pytest.approx(20.0)
    assert agg["high_power_count"] == 1


async def test_charges_aggregates_efficiency_none_without_evse(
    storage: TripStorage,
) -> None:
    """When no charge in the period has EVSE data, charging_efficiency_pct
    is None (state 'unknown' on the sensor) — never 0, which would look
    like a 100 % loss."""
    now = dt_util.now()
    for kwh in (15.0, 15.0):
        await storage.async_insert_charge(_charge(kwh=kwh, ended_at=now))
    agg = await storage.async_charges_aggregates_since(
        now - timedelta(days=30),
    )
    assert agg["charging_efficiency_pct"] is None
    assert agg["evse_count"] == 0


async def test_patch_charge_evse_recomputes_efficiency(
    storage: TripStorage,
) -> None:
    """v0.5.95 — patching evse_energy_kwh writes efficiency.

    The backfill_charge_evse service writes the integrated AC energy
    onto a historical charge via async_patch_charge. The patch must
    auto-compute charging_efficiency_pct = kwh / evse × 100 so the
    dashboard can render both numbers without a separate write.
    """
    cid = await storage.async_insert_charge(
        _charge(kwh=10.0, energy_source="power_integration")
    )
    patched = await storage.async_patch_charge(
        cid, {"evse_energy_kwh": 11.5},
    )
    assert patched is not None
    assert patched.evse_energy_kwh == pytest.approx(11.5)
    # kwh 10 / evse 11.5 ≈ 86.96 → rounded to 87.0
    assert patched.charging_efficiency_pct == pytest.approx(87.0, abs=0.1)
    # Patching kwh later updates total_cost AND re-derives efficiency
    # from the stored evse value, so a corrected kwh stays consistent.
    patched2 = await storage.async_patch_charge(cid, {"kwh": 9.2})
    assert patched2 is not None
    assert patched2.kwh == pytest.approx(9.2)
    assert patched2.charging_efficiency_pct == pytest.approx(80.0, abs=0.1)
    # v0.8.14 — a hand-corrected kwh is no longer a grounded measurement;
    # the stale 'power_integration' tag must not survive the correction
    # (it would otherwise keep counting toward capacity calibration as
    # if it were still directly measured).
    assert patched2.energy_source == "manual"


async def test_extend_last_charge_uses_absolute_soc_end(
    storage: TripStorage,
) -> None:
    """v0.5.45 — soc_end is the new ABSOLUTE reading, not prev + delta.

    The old delta semantics compounded across merged pulses and produced
    impossible rows (soc_start 47, soc_end 124).
    """
    await storage.async_insert_charge(
        _charge(soc_start=47.0, soc_end=60.0, kwh=10.0)
    )
    later = dt_util.now() + timedelta(hours=2)
    merged = await storage.async_extend_last_charge(
        extra_kwh=5.0, ended_at=later, soc_end=66.0
    )
    assert merged is not None
    assert merged.kwh == pytest.approx(15.0)
    assert merged.soc_end == pytest.approx(66.0)
    assert merged.total_cost == pytest.approx(15.0 * 0.15)
    # soc_end=None keeps the existing value.
    merged2 = await storage.async_extend_last_charge(
        extra_kwh=1.0, ended_at=later + timedelta(hours=1), soc_end=None
    )
    assert merged2 is not None
    assert merged2.soc_end == pytest.approx(66.0)


async def test_effective_capacity_below_threshold(storage: TripStorage) -> None:
    """v0.5.51 — under min_charges → None (coordinator falls back to spec)."""
    # 4 eligible charges with ΔSoC ≥ 30, but threshold needs 5.
    for soc_s, soc_e, kwh in [
        (20.0, 80.0, 50.0),
        (15.0, 75.0, 51.0),
        (10.0, 70.0, 49.0),
        (5.0, 65.0, 50.5),
    ]:
        await storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=1),
            ended_at=dt_util.now(),
            kwh=kwh, price_per_kwh=0.2, total_cost=kwh * 0.2,
            soc_start=soc_s, soc_end=soc_e,
        ))
    cap, n, _rejects, _src = await storage.async_effective_capacity_kwh(
        min_delta_pct=30.0, min_charges=5
    )
    assert cap is None
    assert n == 4


async def test_effective_capacity_median_of_eligible_charges(
    storage: TripStorage,
) -> None:
    """v0.5.51 — capacity is the median of kwh/ΔSoC over eligible charges.

    With samples [80, 82, 83, 85, 90] kWh implied, median = 83.
    """
    samples = [
        (20.0, 70.0, 40.0),   # 50% Δ, 40 kWh → 80
        (10.0, 60.0, 41.0),   # 50% Δ, 41 kWh → 82
        (15.0, 65.0, 41.5),   # 50% Δ, 41.5 kWh → 83
        (20.0, 60.0, 34.0),   # 40% Δ, 34 kWh → 85
        (25.0, 75.0, 45.0),   # 50% Δ, 45 kWh → 90
    ]
    for s0, s1, kwh in samples:
        await storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=1),
            ended_at=dt_util.now(),
            kwh=kwh, price_per_kwh=0.2, total_cost=kwh * 0.2,
            soc_start=s0, soc_end=s1,
        ))
    cap, n, _rejects, _src = await storage.async_effective_capacity_kwh(
        min_delta_pct=30.0, min_charges=5
    )
    assert n == 5
    assert cap == pytest.approx(83.0)


async def test_effective_capacity_gates_min_kwh_and_temperature(
    storage: TripStorage,
) -> None:
    """v0.6.5 — sample gates: ΔkWh ≥ 5 (Tessie threshold) and
    temperature in [5, 35] °C. Rows with NULL temperature bypass the
    temperature gate (we don't penalise users without an exterior
    temp sensor). Reject counts surface in the third tuple element.
    """
    now = dt_util.now()
    # 6 eligible charges by ΔSoC alone:
    #   - 1 big at normal temp → SAMPLE (40 kWh / 60 % = 66.67)
    #   - 1 big at 2 °C → rejected (cold)
    #   - 1 big at 40 °C → rejected (hot)
    #   - 1 with NULL temp → SAMPLE (no temp gate)
    #   - 1 with kwh=3.0 (below Tessie 5) → rejected (kwh_too_small)
    #   - 1 with kwh=4.5 (below 5) → rejected (kwh_too_small)
    rows = [
        (40.0, 20.0, 80.0, 22.0),   # 60 % Δ, 22 °C → used (66.67)
        (40.0, 20.0, 80.0,  2.0),   # cold
        (40.0, 20.0, 80.0, 40.0),   # hot
        (40.0, 20.0, 80.0, None),   # temp NULL → used (66.67)
        ( 3.0, 70.0, 80.0, 22.0),   # 10 % Δ but kwh<5 — rejected as
                                    #   kwh_too_small; ΔSoC > 30 ? no:
                                    #   delta = 10, so rejected pre-gate
                                    #   (skipped from SELECT). Adjust:
        ( 4.5, 20.0, 80.0, 22.0),   # 60 % Δ, kwh=4.5 → kwh_too_small
    ]
    for kwh, s0, s1, t in rows:
        await storage.async_insert_charge(ChargeRecord(
            started_at=now - timedelta(hours=1),
            ended_at=now,
            kwh=kwh, price_per_kwh=0.2, total_cost=kwh * 0.2,
            soc_start=s0, soc_end=s1,
            temperature_c=t,
        ))
    # Use min_charges=1 so the small N doesn't suppress the result.
    cap, n, rejects, _src = await storage.async_effective_capacity_kwh(
        min_delta_pct=30.0, min_charges=1,
    )
    # Two samples survive: the 22 °C row and the temp=NULL row.
    # Both imply 66.67 kWh → median = 66.67.
    assert n == 2
    assert cap == pytest.approx(66.67, abs=0.05)
    assert rejects["temp_cold"] == 1
    assert rejects["temp_hot"] == 1
    assert rejects["kwh_too_small"] == 1


async def test_effective_capacity_filters_small_top_ups(
    storage: TripStorage,
) -> None:
    """v0.5.51 — charges with ΔSoC < 30 % are ignored (SoC quantization noise)."""
    # 5 big charges (eligible) + 10 small top-ups (excluded).
    for _ in range(10):
        await storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=1),
            ended_at=dt_util.now(),
            kwh=2.0, price_per_kwh=0.2, total_cost=0.4,
            soc_start=78.0, soc_end=80.0,  # 2 % → would imply 100 kWh, ignored
        ))
    for _ in range(5):
        await storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=1),
            ended_at=dt_util.now(),
            kwh=40.0, price_per_kwh=0.2, total_cost=8.0,
            soc_start=20.0, soc_end=80.0,  # 60 % Δ → 66.67 kWh
        ))
    cap, n, _rejects, _src = await storage.async_effective_capacity_kwh(
        min_delta_pct=30.0, min_charges=5
    )
    assert n == 5
    assert cap == pytest.approx(66.67, abs=0.05)


async def test_effective_capacity_prefers_power_integration_samples(
    storage: TripStorage,
) -> None:
    """v0.8.14 — once enough power-integration-sourced charges exist,
    calibration should use ONLY those (grounded measurements), not the
    SoC-delta-sourced rows it exists to correct — the pre-v0.8.14 query
    was circular: most of its own input WAS the SoC-delta guess.

    5 power_integration charges imply 70 kWh; 5 soc_delta charges (that
    would otherwise dominate the same window) imply 90 kWh. The result
    must be 70, and only the 5 grounded charges should be counted.
    """
    for _ in range(5):
        await storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=1),
            ended_at=dt_util.now(),
            kwh=35.0, price_per_kwh=0.2, total_cost=7.0,
            soc_start=20.0, soc_end=70.0,  # 50 % Δ → 70 kWh
            energy_source="power_integration",
        ))
    for _ in range(5):
        await storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=1),
            ended_at=dt_util.now(),
            kwh=45.0, price_per_kwh=0.2, total_cost=9.0,
            soc_start=20.0, soc_end=70.0,  # 50 % Δ → 90 kWh
            energy_source="soc_delta",
        ))
    cap, n, _rejects, _src = await storage.async_effective_capacity_kwh(
        min_delta_pct=30.0, min_charges=5,
    )
    assert n == 5
    assert cap == pytest.approx(70.0)


async def test_effective_capacity_falls_back_when_not_enough_grounded_samples(
    storage: TripStorage,
) -> None:
    """v0.8.14 — with fewer than `min_charges` power_integration-sourced
    charges, fall back to the pre-v0.8.14 behaviour (every eligible
    charge regardless of source) rather than reporting no calibration
    at all — preserves existing users' calibration immediately after
    upgrade, while it accrues grounded samples over time.
    """
    for _ in range(2):
        await storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=1),
            ended_at=dt_util.now(),
            kwh=35.0, price_per_kwh=0.2, total_cost=7.0,
            soc_start=20.0, soc_end=70.0,  # 50 % Δ → 70 kWh
            energy_source="power_integration",
        ))
    for _ in range(3):
        await storage.async_insert_charge(ChargeRecord(
            started_at=dt_util.now() - timedelta(hours=1),
            ended_at=dt_util.now(),
            kwh=35.0, price_per_kwh=0.2, total_cost=7.0,
            soc_start=20.0, soc_end=70.0,  # 50 % Δ → 70 kWh
            energy_source=None,
        ))
    cap, n, _rejects, _src = await storage.async_effective_capacity_kwh(
        min_delta_pct=30.0, min_charges=5,
    )
    assert n == 5
    assert cap == pytest.approx(70.0)


async def test_recompute_energy_from_capacity_rewrites_soc_trips(
    storage: TripStorage,
) -> None:
    """v0.5.51 — heal updates SoC-derived trips against a new capacity."""
    # Insert: 1 trip from SoC at the OLD 80-kWh capacity, 1 from power.
    soc_trip = _trip(
        distance_km=50.0, soc_used_pct=10.0,
        energy_kwh=8.0,  # was: 10% * 80 / 100
        consumption_kwh_100km=16.0,
        energy_source="soc",
    )
    power_trip = _trip(
        distance_km=50.0, soc_used_pct=10.0,
        energy_kwh=7.5,  # measured directly; smaller than SoC-derived
        consumption_kwh_100km=15.0,
        energy_source="power_integration",
    )
    soc_id = await storage.async_insert(soc_trip)
    power_id = await storage.async_insert(power_trip)

    # New calibrated capacity: 70 kWh (degraded pack).
    n = await storage.async_recompute_energy_from_capacity(70.0)
    assert n == 1  # only the SoC-derived row

    # SoC trip rewritten to 10% * 70 / 100 = 7.0 kWh, 14 kWh/100km.
    rewritten = await storage.async_get_last()
    # Most recent is power_trip — unchanged.
    assert rewritten.trip_id == power_id
    assert rewritten.energy_kwh == pytest.approx(7.5)

    # Pull the SoC row directly to verify the rewrite.
    recent = await storage.async_recent_trips(limit=10)
    by_id = {t.trip_id: t for t in recent}
    healed = by_id[soc_id]
    assert healed.energy_kwh == pytest.approx(7.0)
    assert healed.consumption_kwh_100km == pytest.approx(14.0)


async def test_recompute_energy_skips_rows_without_soc_used(
    storage: TripStorage,
) -> None:
    """v0.5.51 — rows without soc_used_pct can't be rescaled. The heal
    must skip them rather than producing NaN/None corruption.
    """
    await storage.async_insert(_trip(
        distance_km=50.0, soc_used_pct=None, energy_kwh=8.0,
        energy_source="soc",
    ))
    await storage.async_insert(_trip(
        distance_km=50.0, soc_used_pct=10.0, energy_kwh=8.0,
        energy_source="soc",
    ))
    n = await storage.async_recompute_energy_from_capacity(70.0)
    assert n == 1  # only the row with soc_used_pct survives the guard


async def test_aggregates_by_season_groups_trips(storage: TripStorage) -> None:
    """v0.5.54 — trips bucketed by Northern-hemisphere meteorological season."""
    base = datetime(2026, 1, 15, 9, 0)  # January = winter
    await storage.async_insert(_trip(
        started_at=base, ended_at=base + timedelta(hours=1),
        distance_km=20.0, energy_kwh=4.0, consumption_kwh_100km=20.0,
        ambient_temp_c=3.0,
    ))
    summer = datetime(2026, 7, 15, 9, 0)
    await storage.async_insert(_trip(
        started_at=summer, ended_at=summer + timedelta(hours=1),
        distance_km=20.0, energy_kwh=3.0, consumption_kwh_100km=15.0,
        ambient_temp_c=28.0,
    ))
    out = await storage.async_aggregates_by_season()
    assert out["winter"]["trips"] == 1
    assert out["winter"]["avg_consumption_kwh_100km"] == pytest.approx(20.0)
    assert out["winter"]["avg_ambient_temp_c"] == pytest.approx(3.0)
    assert out["summer"]["trips"] == 1
    assert out["summer"]["avg_consumption_kwh_100km"] == pytest.approx(15.0)
    assert out["spring"]["trips"] == 0
    assert out["autumn"]["trips"] == 0


async def test_aggregates_by_temp_bucket_filters_unknown(
    storage: TripStorage,
) -> None:
    """v0.5.54 — ambient_temp_c=None goes to `unknown` bucket."""
    await storage.async_insert(_trip(distance_km=20.0, ambient_temp_c=2.0))
    await storage.async_insert(_trip(distance_km=20.0, ambient_temp_c=18.0))
    await storage.async_insert(_trip(distance_km=20.0, ambient_temp_c=None))
    out = await storage.async_aggregates_by_temp_bucket()
    assert out["cold"]["trips"] == 1     # 2 < 5
    assert out["mild"]["trips"] == 1     # 15 ≤ 18 < 25
    assert out["unknown"]["trips"] == 1
    assert out["cool"]["trips"] == 0


async def test_capacity_history_round_trip(storage: TripStorage) -> None:
    """v0.5.54/65 — snapshots are returned oldest → newest with odo."""
    t0 = datetime(2026, 1, 1, 12, 0)
    await storage.async_insert_capacity_snapshot(82.5, 82.5, 10, t0, odometer_km=15000.0)
    await storage.async_insert_capacity_snapshot(
        81.8, 82.5, 14, t0 + timedelta(days=90), odometer_km=22000.0
    )
    await storage.async_insert_capacity_snapshot(
        81.2, 82.5, 18, t0 + timedelta(days=180), odometer_km=30000.0
    )
    history = await storage.async_capacity_history(limit=10)
    assert len(history) == 3
    assert history[0]["calibrated_kwh"] == pytest.approx(82.5)
    assert history[0]["odometer_km"] == pytest.approx(15000.0)
    assert history[-1]["calibrated_kwh"] == pytest.approx(81.2)
    assert history[-1]["odometer_km"] == pytest.approx(30000.0)

    latest = await storage.async_latest_capacity_snapshot()
    assert latest is not None
    assert latest[1] == pytest.approx(81.2)
    assert latest[3] == 18
    assert latest[4] == pytest.approx(30000.0)


async def test_capacity_snapshot_without_odometer_is_accepted(
    storage: TripStorage,
) -> None:
    """v0.5.65 — `odometer_km` is optional. Callers without an odometer
    sensor (or where the reading is unavailable at snapshot time) must
    still be able to persist the calibration."""
    await storage.async_insert_capacity_snapshot(
        82.5, 82.5, 10, datetime(2026, 1, 1, 12, 0),
    )
    latest = await storage.async_latest_capacity_snapshot()
    assert latest is not None
    assert latest[4] is None


async def test_logger_total_km_sums_distance(storage: TripStorage) -> None:
    """v0.5.66 — logger_km is the SUM of distance_km across trips."""
    assert await storage.async_logger_total_km() == 0.0
    await storage.async_insert(_trip(distance_km=15.0))
    await storage.async_insert(_trip(distance_km=23.5))
    assert await storage.async_logger_total_km() == pytest.approx(38.5)


async def test_avg_trip_metrics_are_weighted_not_means_of_ratios(
    storage: TripStorage,
) -> None:
    """v0.8.17 — consumption and speed must be totals over totals.

    `AVG(consumption_kwh_100km)` and `AVG(avg_speed_kmh)` gave a short
    city hop the same weight as a long motorway run, so the three 30-day
    sensors contradicted each other: distance / duration did not equal
    the published speed.
    """
    since = dt_util.now() - timedelta(days=30)
    base = dt_util.now() - timedelta(days=1)

    # 2 km at 40 kWh/100km, 6 min  → 20 km/h
    await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(minutes=6),
        duration_min=6.0, distance_km=2.0, energy_kwh=0.8,
        consumption_kwh_100km=40.0, avg_speed_kmh=20.0,
    ))
    # 200 km at 15 kWh/100km, 2 h → 100 km/h
    await storage.async_insert(TripRecord(
        started_at=base + timedelta(hours=1),
        ended_at=base + timedelta(hours=3),
        duration_min=120.0, distance_km=200.0, energy_kwh=30.0,
        consumption_kwh_100km=15.0, avg_speed_kmh=100.0,
    ))

    m = await storage.async_avg_trip_metrics(since)

    # 30.8 kWh over 202 km, not (40+15)/2 = 27.5
    assert m["avg_consumption_kwh_100km"] == pytest.approx(15.25, abs=0.01)
    # 202 km over 126 min, not (20+100)/2 = 60
    assert m["avg_speed_kmh"] == pytest.approx(96.19, abs=0.05)
    # and the trio is now self-consistent
    implied = m["avg_distance_km"] / (m["avg_duration_min"] / 60.0)
    assert implied == pytest.approx(m["avg_speed_kmh"], abs=0.05)


async def test_null_energy_trip_does_not_dilute_avg_consumption(
    storage: TripStorage,
) -> None:
    """v0.8.17 — a row with kilometres but no energy reading used to
    contribute to the denominator only, understating consumption. That
    figure drives the remaining-range estimate.
    """
    since = dt_util.now() - timedelta(days=30)
    base = dt_util.now() - timedelta(days=1)

    await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(hours=1),
        duration_min=60.0, distance_km=800.0, energy_kwh=120.0,
        consumption_kwh_100km=15.0,
    ))
    await storage.async_insert(TripRecord(
        started_at=base + timedelta(hours=2), ended_at=base + timedelta(hours=3),
        duration_min=60.0, distance_km=100.0, energy_kwh=None,
    ))

    totals = await storage.async_aggregates_since(since)
    # 120 / 800, not 120 / 900
    assert totals["avg_consumption_kwh_100km"] == pytest.approx(15.0)


async def test_heal_history_reattributes_a_charge_recovered_later(
    storage: TripStorage,
) -> None:
    """v0.8.17 — a charge inserted after the fact is invisible to the
    trips around it, because `kwh_charged_*` is computed once at close.

    Here a 40 kWh session that really happened mid-leg is recovered
    afterwards: the trip's SoC only fell 10 points, so its stored energy
    badly understates what it burned, and its `soc_start` looks like it
    rose out of nowhere.
    """
    base = dt_util.now() - timedelta(days=5)
    # Leg 1 ends at 30 %.
    await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(hours=1),
        duration_min=60.0, distance_km=100.0, soc_start=60.0, soc_end=30.0,
        soc_used_pct=30.0, energy_kwh=24.75, consumption_kwh_100km=24.75,
        energy_source="soc",
    ))
    # Leg 2 starts at 85 % — impossible without a charge, and none was known.
    trip2 = await storage.async_insert(TripRecord(
        started_at=base + timedelta(hours=3), ended_at=base + timedelta(hours=4, minutes=30),
        duration_min=60.0, distance_km=100.0, soc_start=85.0, soc_end=75.0,
        soc_used_pct=10.0, energy_kwh=8.25, consumption_kwh_100km=8.25,
        energy_source="soc",
    ))
    # The session is recovered later. It sits wholly INSIDE leg 2's
    # window — the driver stopped mid-leg and plugged in — so all of its
    # energy belongs to that leg. (A session that was already running as
    # the window opened is apportioned by SoC instead; see
    # test_charge_straddling_the_trip_start_is_apportioned_by_soc.)
    await storage.async_insert_charge(ChargeRecord(
        started_at=base + timedelta(hours=3, minutes=10),
        ended_at=base + timedelta(hours=3, minutes=40),
        kwh=40.0, price_per_kwh=0.50, total_cost=20.0, currency="EUR",
        soc_start=30.0, soc_end=85.0,
    ))

    counts = await storage.async_heal_history(battery_capacity_kwh=82.5)

    assert counts["charge_attribution_fixed"] >= 1
    healed = {t.trip_id: t for t in await storage.async_recent_trips(10)}[trip2]
    assert healed.kwh_charged_during == pytest.approx(40.0)
    # 40 kWh in, SoC net -10 points → 40 + 8.25 = 48.25 kWh really used
    assert healed.energy_kwh == pytest.approx(48.25)
    assert healed.energy_source == "soc_plus_charge"
    # the rise is explained by a real charge, so soc_start is left alone
    assert healed.soc_start == 85.0


async def test_heal_history_reanchors_an_unexplained_soc_rise(
    storage: TripStorage,
) -> None:
    """v0.8.17 — SoC cannot rise while parked with nothing charging."""
    base = dt_util.now() - timedelta(days=5)
    await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(hours=1),
        duration_min=60.0, distance_km=100.0, soc_start=80.0, soc_end=70.0,
        soc_used_pct=10.0, energy_kwh=8.25, consumption_kwh_100km=8.25,
        energy_source="soc",
    ))
    # Anchored up to 72 by the old post-charge/short-park snap.
    trip2 = await storage.async_insert(TripRecord(
        started_at=base + timedelta(hours=2), ended_at=base + timedelta(hours=3),
        duration_min=60.0, distance_km=50.0, soc_start=72.0, soc_end=68.0,
        soc_used_pct=4.0, energy_kwh=3.3, consumption_kwh_100km=6.6,
        energy_source="soc",
    ))

    counts = await storage.async_heal_history(battery_capacity_kwh=82.5)

    assert counts["soc_start_reanchored"] == 1
    healed = {t.trip_id: t for t in await storage.async_recent_trips(10)}[trip2]
    assert healed.soc_start == 70.0
    assert healed.soc_used_pct == pytest.approx(2.0)
    # 2 points of 82.5 kWh, not 4 — the other two were never the trip's
    assert healed.energy_kwh == pytest.approx(1.65)
    assert healed.consumption_kwh_100km == pytest.approx(3.3)


async def test_trip_overlaps_matches_locally_stored_rows_from_utc_bounds(
    storage: TripStorage,
) -> None:
    """v0.8.17 regression — the recovery dedup must still see its own rows.

    Trips are written as local ISO since v0.8.17, but the recovery sweep
    asks this question with the recorder's UTC stamps. Comparing those as
    TEXT ('…+02:00' rows against '…+00:00' bounds) made the guard miss
    the very rows the sweep had just inserted, so every run re-inserted
    them — and the sweep is also fired automatically after a telemetry
    silence, so they would pile up unattended.
    """
    local_start = dt_util.now() - timedelta(hours=5)
    local_end = local_start + timedelta(hours=1)
    await storage.async_insert(TripRecord(
        started_at=local_start, ended_at=local_end,
        duration_min=60.0, distance_km=80.0,
    ))

    # Same instants, expressed in UTC, as the recorder hands them back.
    assert await storage.async_trip_overlaps(
        local_start.astimezone(UTC),
        local_end.astimezone(UTC),
    ) is True

    # A genuinely different window must still report no overlap.
    far = local_start - timedelta(days=3)
    assert await storage.async_trip_overlaps(
        far.astimezone(UTC),
        (far + timedelta(hours=1)).astimezone(UTC),
    ) is False


async def test_heal_history_ignores_a_charge_that_outlives_the_trip(
    storage: TripStorage,
) -> None:
    """v0.8.17 regression — the heal must match the close path exactly.

    `async_charges_in_window` counts a charge that ENDED inside the trip's
    window. Interval overlap would also catch one that merely started
    inside and ran on afterwards — the plug-in-at-the-end case — and then
    fold its whole kWh into the trip, turning a correct row into a
    physically impossible one.
    """
    base = dt_util.now() - timedelta(days=4)
    trip_id = await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(hours=1, minutes=10),
        duration_min=70.0, distance_km=60.0, soc_start=70.0, soc_end=55.0,
        soc_used_pct=15.0, energy_kwh=12.38, consumption_kwh_100km=20.6,
        energy_source="soc",
    ))
    # Plugged in five minutes before the trip closed; session runs on for
    # another 40 minutes after it.
    await storage.async_insert_charge(ChargeRecord(
        started_at=base + timedelta(hours=1, minutes=5),
        ended_at=base + timedelta(hours=1, minutes=50),
        kwh=30.0, price_per_kwh=0.50, total_cost=15.0, currency="EUR",
        soc_start=55.0, soc_end=91.0,
    ))

    await storage.async_heal_history(battery_capacity_kwh=82.5)

    healed = {t.trip_id: t for t in await storage.async_recent_trips(10)}[trip_id]
    assert healed.kwh_charged_during is None, "charge ended after the trip"
    assert healed.energy_kwh == pytest.approx(12.38, abs=0.01)
    assert healed.energy_source == "soc"


async def test_charge_straddling_the_trip_start_is_apportioned_by_soc(
    storage: TripStorage,
) -> None:
    """v0.8.18 — regression from the real 2026-08-19 leg.

    The trip opened while the cable was still delivering (67 %) and the
    session ran on to 96 %. Crediting the whole 66.83 kWh gave 57.75 kWh
    over 74 km — 78 kWh/100km, impossible for the car. Only the 29 of 81
    SoC points delivered after the trip opened belong to it.
    """
    base = dt_util.now() - timedelta(days=2)
    await storage.async_insert_charge(ChargeRecord(
        started_at=base, ended_at=base + timedelta(hours=1),
        kwh=66.83, price_per_kwh=0.50, total_cost=33.42, currency="EUR",
        soc_start=15.0, soc_end=96.0,
    ))

    # Trip opened mid-charge at 67 %, closed after the cable stopped.
    attributable = await storage.async_charges_attributable_to_trip(
        base + timedelta(minutes=35), base + timedelta(hours=2),
        trip_soc_start=67.0,
    )
    # 66.83 * (96-67)/(96-15) = 23.92
    assert attributable == pytest.approx(23.92, abs=0.01)

    # A session fully inside the window still counts in full.
    assert await storage.async_charges_attributable_to_trip(
        base - timedelta(minutes=5), base + timedelta(hours=2),
        trip_soc_start=15.0,
    ) == pytest.approx(66.83, abs=0.01)

    # Finished exactly as the trip began → nothing belongs to the trip.
    assert await storage.async_charges_attributable_to_trip(
        base + timedelta(minutes=59), base + timedelta(hours=2),
        trip_soc_start=96.0,
    ) == pytest.approx(0.0)

    # No SoC readings to apportion with → contribute nothing, don't guess.
    await storage.async_insert_charge(ChargeRecord(
        started_at=base + timedelta(hours=3),
        ended_at=base + timedelta(hours=4),
        kwh=20.0, price_per_kwh=0.30, total_cost=6.0, currency="EUR",
    ))
    assert await storage.async_charges_attributable_to_trip(
        base + timedelta(hours=3, minutes=30), base + timedelta(hours=5),
        trip_soc_start=50.0,
    ) == pytest.approx(0.0)


async def test_heal_history_apportions_a_straddling_charge(
    storage: TripStorage,
) -> None:
    """v0.8.18 — and the heal must apportion it too, or running it makes
    the numbers worse than leaving them alone (measured: a lifetime
    driven/charged ratio of 1.07 became 1.22).
    """
    base = dt_util.now() - timedelta(days=3)
    await storage.async_insert_charge(ChargeRecord(
        started_at=base, ended_at=base + timedelta(hours=1),
        kwh=66.83, price_per_kwh=0.50, total_cost=33.42, currency="EUR",
        soc_start=15.0, soc_end=96.0,
    ))
    trip_id = await storage.async_insert(TripRecord(
        started_at=base + timedelta(minutes=35),
        ended_at=base + timedelta(hours=2, minutes=5),
        duration_min=90.0, distance_km=74.0,
        soc_start=67.0, soc_end=78.0, soc_used_pct=-11.0,
        energy_kwh=14.81, energy_source="estimated",
        consumption_kwh_100km=20.0,
    ))

    await storage.async_heal_history(battery_capacity_kwh=82.5)

    healed = {t.trip_id: t for t in await storage.async_recent_trips(10)}[trip_id]
    assert healed.kwh_charged_during == pytest.approx(23.92, abs=0.01)
    # 23.92 in, 11 points (9.08 kWh) more stored at the end → 14.84 used
    assert healed.energy_kwh == pytest.approx(14.84, abs=0.02)
    assert healed.consumption_kwh_100km == pytest.approx(20.1, abs=0.1)


async def test_charge_ending_on_the_trip_boundary_lands_in_before_not_nowhere(
    storage: TripStorage,
) -> None:
    """The real 2026-08-17 collision: charge id53 and trip id352.

    The v0.8.17 mutex has the charge-off handler open the trip, so the two
    stamps come out of the same event cascade — charge id53 ended at
    14:21:56.961478 and trip id352 started at 14:21:56.961301, 177
    MICROSECONDS earlier. That is one instant recorded twice, but the two
    buckets disagreed about it and the session fell through the gap:

    * `kwh_charged_before` asks for `ended_at <= trip_start`, which fails
      by those 177 us, so the charge is not "before".
    That NULL is not cosmetic — `heal_history` reads it as "nothing
    charged in the gap" (see
    `test_heal_does_not_reanchor_soc_over_a_boundary_charge`).

    All 1.76 kWh belong to `before`. `during` needs no time tolerance of
    its own: the trip anchored at the SoC the charge finished on, so SoC
    already says none of the session was still to come.
    """
    trip_start = dt_util.now() - timedelta(hours=3)
    charge_end = trip_start + timedelta(microseconds=177)
    prev_trip_end = trip_start - timedelta(hours=1)
    await storage.async_insert_charge(ChargeRecord(
        started_at=charge_end - timedelta(minutes=12, seconds=25),
        ended_at=charge_end,
        kwh=1.76, price_per_kwh=0.07, total_cost=0.12, currency="EUR",
        soc_start=63.0, soc_end=66.0,
    ))

    before = await storage.async_charges_in_window(prev_trip_end, trip_start)
    assert before["count"] == 1, "the charge ended AT the trip start"
    assert before["kwh"] == pytest.approx(1.76)

    during = await storage.async_charges_attributable_to_trip(
        trip_start, trip_start + timedelta(minutes=27), trip_soc_start=66.0,
    )
    assert during == pytest.approx(0.0), "it had already finished"


async def test_charge_well_inside_the_trip_is_still_attributed(
    storage: TripStorage,
) -> None:
    """The boundary tolerance must not swallow a real mid-trip session.

    A stop that plugs in ten minutes into a drive genuinely delivered
    inside the window, and stays apportioned by SoC.
    """
    trip_start = dt_util.now() - timedelta(hours=4)
    await storage.async_insert_charge(ChargeRecord(
        started_at=trip_start + timedelta(minutes=10),
        ended_at=trip_start + timedelta(minutes=30),
        kwh=20.0, price_per_kwh=0.50, total_cost=10.0, currency="EUR",
        soc_start=40.0, soc_end=70.0,
    ))
    during = await storage.async_charges_attributable_to_trip(
        trip_start, trip_start + timedelta(hours=1), trip_soc_start=40.0,
    )
    assert during == pytest.approx(20.0), "started inside → counts in full"


async def test_charges_in_window_matches_across_mixed_timezone_bounds(
    storage: TripStorage,
) -> None:
    """Rows are written as local ISO (`_iso_local`); the recovery sweep and
    the correction services hand these bounds recorder/UTC stamps. The
    window query compares `ended_at` as TEXT, so un-normalised bounds
    compare '+00:00' against '+02:00' rows and silently match nothing —
    the same defect v0.8.17 fixed in `_trip_overlaps` and never applied
    here.
    """
    ended = dt_util.now() - timedelta(hours=6)
    await storage.async_insert_charge(ChargeRecord(
        started_at=ended - timedelta(hours=1), ended_at=ended,
        kwh=11.0, price_per_kwh=0.20, total_cost=2.20, currency="EUR",
        soc_start=30.0, soc_end=45.0,
    ))
    local = await storage.async_charges_in_window(
        ended - timedelta(hours=2), ended + timedelta(hours=2),
    )
    assert local["kwh"] == pytest.approx(11.0)

    as_utc = await storage.async_charges_in_window(
        (ended - timedelta(hours=2)).astimezone(UTC),
        (ended + timedelta(hours=2)).astimezone(UTC),
    )
    assert as_utc["kwh"] == pytest.approx(11.0), "UTC bounds must match too"


async def test_heal_does_not_reanchor_soc_over_a_boundary_charge(
    storage: TripStorage,
) -> None:
    """The heal keeps its OWN copy of the charge-window logic, so fixing
    `_charges_in_window` / `_charges_attributable_to_trip` does not reach
    it. Without the same tolerance there, the real 2026-08-17 rows healed
    into a worse state than they started:

    charge id53 (63 -> 66 %, 1.76 kWh) ended 177 us after trip id352
    opened at 66 %. Both buckets came out NULL, so the "provably
    impossible SoC rise while parked" rule concluded the 66 % start could
    not be real and re-anchored it down to the previous trip's 64 % —
    erasing the charge's three points and, with them, its energy.

    The charge is visible as `before`, so nothing is impossible and the
    66 % start must survive.
    """
    prev_end = dt_util.now() - timedelta(hours=5)
    trip_start = prev_end + timedelta(hours=1)
    charge_end = trip_start + timedelta(microseconds=177)

    await storage.async_insert(TripRecord(
        started_at=prev_end - timedelta(minutes=20), ended_at=prev_end,
        duration_min=20.0, distance_km=10.0,
        soc_start=70.0, soc_end=64.0, soc_used_pct=6.0,
        energy_kwh=4.95, energy_source="soc",
    ))
    await storage.async_insert_charge(ChargeRecord(
        started_at=charge_end - timedelta(minutes=12, seconds=25),
        ended_at=charge_end,
        kwh=1.76, price_per_kwh=0.07, total_cost=0.12, currency="EUR",
        soc_start=63.0, soc_end=66.0,
    ))
    trip_id = await storage.async_insert(TripRecord(
        started_at=trip_start,
        ended_at=trip_start + timedelta(minutes=27),
        duration_min=27.0, distance_km=3.0,
        soc_start=66.0, soc_end=65.0, soc_used_pct=1.0,
        energy_kwh=0.83, energy_source="soc",
    ))

    await storage.async_heal_history(battery_capacity_kwh=82.5)

    healed = {t.trip_id: t for t in await storage.async_recent_trips(10)}[trip_id]
    assert healed.soc_start == pytest.approx(66.0), "must not re-anchor to 64"
    assert healed.kwh_charged_before == pytest.approx(1.76)
    assert healed.kwh_charged_during is None, "the charge had finished"
    # 1 SoC point of an 82.5 kWh pack, no charge inside the window.
    assert healed.energy_kwh == pytest.approx(0.83, abs=0.02)
    assert healed.energy_source == "soc"


async def test_heal_drops_an_energy_source_its_own_fields_contradict(
    storage: TripStorage,
) -> None:
    """The heal must not leave a row asserting a premise it just disproved.

    Real state of trip id352 after two heal runs. `energy_source` says
    `soc_plus_charge` — "this energy includes a charge delivered inside my
    window" — while `kwh_charged_during` is NULL, which says the opposite.
    The 2.58 kWh is a fossil: the v0.8.17 run computed it by crediting the
    whole 1.76 kWh session plus one SoC point, v0.8.18 then re-derived the
    session's contribution as zero, and because `soc_used` had gone
    negative in between neither run could recompute the number, so it was
    stranded with a label that no longer describes it.

    The value itself stays — cost and the lifetime kWh aggregates need a
    number, the same call v0.8.15 and v0.8.17 made when suppressing a
    consumption figure. It is the *claim* that cannot survive.
    """
    prev_end = dt_util.now() - timedelta(hours=5)
    trip_start = prev_end + timedelta(hours=1)

    await storage.async_insert(TripRecord(
        started_at=prev_end - timedelta(minutes=20), ended_at=prev_end,
        duration_min=20.0, distance_km=10.0,
        soc_start=70.0, soc_end=64.0, soc_used_pct=6.0,
        energy_kwh=4.95, energy_source="soc",
    ))
    await storage.async_insert_charge(ChargeRecord(
        started_at=trip_start - timedelta(minutes=12),
        ended_at=trip_start + timedelta(microseconds=177),
        kwh=1.76, price_per_kwh=0.07, total_cost=0.12, currency="EUR",
        soc_start=63.0, soc_end=66.0,
    ))
    trip_id = await storage.async_insert(TripRecord(
        started_at=trip_start, ended_at=trip_start + timedelta(minutes=27),
        duration_min=27.0, distance_km=3.0,
        # soc_start already re-anchored down by the earlier run, so the
        # SoC delta is negative and no recompute is possible.
        soc_start=64.0, soc_end=65.0, soc_used_pct=-1.0,
        energy_kwh=2.58, energy_source="soc_plus_charge",
        consumption_kwh_100km=None,
    ))

    await storage.async_heal_history(battery_capacity_kwh=82.5)

    healed = {t.trip_id: t for t in await storage.async_recent_trips(10)}[trip_id]
    assert healed.kwh_charged_during is None
    assert healed.energy_source is None, "the soc_plus_charge claim is false"
    assert healed.energy_kwh == pytest.approx(2.58), "the number is kept"
    assert healed.low_confidence


async def test_heal_keeps_a_consistent_energy_source_untouched(
    storage: TripStorage,
) -> None:
    """Only a contradicted claim is cleared. A `soc_plus_charge` row that
    really does have a charge inside its window keeps its label.
    """
    base = dt_util.now() - timedelta(days=1)
    await storage.async_insert_charge(ChargeRecord(
        started_at=base + timedelta(minutes=10),
        ended_at=base + timedelta(minutes=40),
        kwh=20.0, price_per_kwh=0.30, total_cost=6.0, currency="EUR",
        soc_start=40.0, soc_end=64.0,
    ))
    trip_id = await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(hours=1),
        duration_min=60.0, distance_km=50.0,
        soc_start=50.0, soc_end=60.0, soc_used_pct=-10.0,
        energy_kwh=20.0, energy_source="soc_plus_charge",
        kwh_charged_during=20.0, consumption_kwh_100km=40.0,
    ))

    await storage.async_heal_history(battery_capacity_kwh=82.5)

    healed = {t.trip_id: t for t in await storage.async_recent_trips(10)}[trip_id]
    assert healed.kwh_charged_during == pytest.approx(20.0)
    assert healed.energy_source == "soc_plus_charge"


async def test_resolve_open_journey_id_does_not_crash(storage: TripStorage) -> None:
    """v0.8.25 — `_resolve_open_journey_id` raised NameError on every call
    that found an open journey.

    v0.8.10 taught the function about secondary homes by turning the single
    `slug` into a list of `slugs` and the first query's `= ?` into
    `IN (...)`. The SECOND query — the one that actually returns the id —
    kept `LOWER(destination) = ?` bound to `(slug,)`, a name that no longer
    existed. The first query is a guard that returns early when there is no
    candidate, so the bug only fired when there WAS an open journey, i.e.
    exactly when the answer mattered.

    Observed on the real install: every trip closed on 2026-08-23 came out
    with `journey_id = None`, and the traceback was in the HA log the whole
    time — `NameError: name 'slug' is not defined. Did you mean: 'slugs'?`
    """
    base = dt_util.now() - timedelta(hours=6)
    # Arrived home, closing whatever came before.
    await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(minutes=20),
        duration_min=20.0, distance_km=10.0, destination="home",
        journey_id=7, energy_kwh=2.0,
    ))
    # Left again: this journey is open.
    await storage.async_insert(TripRecord(
        started_at=base + timedelta(hours=1),
        ended_at=base + timedelta(hours=1, minutes=30),
        duration_min=30.0, distance_km=25.0,
        origin="home", destination="not_home",
        journey_id=8, energy_kwh=5.0,
    ))

    assert await storage.async_resolve_open_journey_id("home") == 8

    # And with a secondary home configured, the same call still works.
    assert await storage.async_resolve_open_journey_id(
        "home", ["casa_domi", "casa domi"],
    ) == 8


async def test_resolve_open_journey_id_sees_a_secondary_home_arrival(
    storage: TripStorage,
) -> None:
    """A journey closed by arriving at a SECONDARY home must not be
    reported as still open — the whole point of passing the extra slugs.
    """
    base = dt_util.now() - timedelta(hours=6)
    await storage.async_insert(TripRecord(
        started_at=base, ended_at=base + timedelta(minutes=20),
        duration_min=20.0, distance_km=10.0,
        destination="not_home", journey_id=9, energy_kwh=2.0,
    ))
    await storage.async_insert(TripRecord(
        started_at=base + timedelta(hours=1),
        ended_at=base + timedelta(hours=1, minutes=30),
        duration_min=30.0, distance_km=25.0,
        destination="Casa Domi", journey_id=9, energy_kwh=5.0,
    ))
    assert await storage.async_resolve_open_journey_id(
        "home", ["casa_domi", "casa domi"],
    ) is None, "arriving at a secondary home closes the journey"


async def test_impossible_efficiency_is_excluded_from_the_rolling_median(
    storage: TripStorage,
) -> None:
    """v0.8.29 — a row above 100 % must not enter the efficiency median.

    Energy reaching the battery cannot exceed what the meter delivered,
    so `charging_efficiency_pct > 100` means one of the two figures is
    wrong — in practice `kwh` came from the SoC estimate, not a
    measurement. Two such rows sat in the user's real database at
    106.0 % and 156.8 %. The old query filtered only `> 0`, so they
    both averaged in AND consumed a window slot each.
    """
    now = dt_util.now()
    good = (88.0, 90.0, 92.0)
    for i, eff in enumerate(good):
        await storage.async_insert_charge(_charge(
            ended_at=now - timedelta(hours=10 + i),
            kwh=10.0, evse_energy_kwh=10.0 / eff * 100.0,
            charging_efficiency_pct=eff,
        ))
    median, n = await storage.async_avg_charging_efficiency_pct()
    assert (median, n) == (90.0, 3)

    # The impossible rows are the most recent, so an unfiltered query
    # would put them at the front of the window.
    for i, eff in enumerate((106.0, 156.8)):
        await storage.async_insert_charge(_charge(
            ended_at=now - timedelta(minutes=i),
            kwh=10.0, evse_energy_kwh=10.0 / eff * 100.0,
            charging_efficiency_pct=eff,
        ))
    median_after, n_after = await storage.async_avg_charging_efficiency_pct()
    assert (median_after, n_after) == (90.0, 3), (
        "impossible rows must change neither the median nor the count"
    )


async def test_implausibly_low_efficiency_is_excluded_too(
    storage: TripStorage,
) -> None:
    """Losing over half the delivered energy is not a charge session.

    The band is symmetric on purpose: 30 % is as much a broken
    measurement as 156 % is, and averaging it in would report the
    wallbox as failing when the real fault is a bad `kwh`.
    """
    now = dt_util.now()
    for i, eff in enumerate((88.0, 90.0, 92.0)):
        await storage.async_insert_charge(_charge(
            ended_at=now - timedelta(hours=10 + i),
            kwh=10.0, evse_energy_kwh=10.0 / eff * 100.0,
            charging_efficiency_pct=eff,
        ))
    await storage.async_insert_charge(_charge(
        ended_at=now, kwh=3.0, evse_energy_kwh=10.0,
        charging_efficiency_pct=30.0,
    ))
    assert await storage.async_avg_charging_efficiency_pct() == (90.0, 3)


async def test_efficiency_just_over_100_is_kept_as_measurement_slop(
    storage: TripStorage,
) -> None:
    """101 % is meter resolution plus ±1 % SoC quantization, not a fault.

    The band tops out at 105 % rather than 100 so a genuinely-99 %
    session that rounds over the line still counts. Excluding it would
    bias the median downward on exactly the best-measured charges.
    """
    now = dt_util.now()
    for i, eff in enumerate((99.0, 101.0, 103.0)):
        await storage.async_insert_charge(_charge(
            ended_at=now - timedelta(hours=i),
            kwh=10.0, evse_energy_kwh=10.0 / eff * 100.0,
            charging_efficiency_pct=eff,
        ))
    assert await storage.async_avg_charging_efficiency_pct() == (101.0, 3)


async def test_paired_evse_aggregate_drops_impossible_rows(
    storage: TripStorage,
) -> None:
    """v0.8.29 — the paired kwh/evse sums honour the same band.

    v0.5.101 introduced the paired sums so the period efficiency was a
    properly weighted ratio, and its docstring promised "always 0-100 %".
    That held only while every row's kwh really came from a measurement.
    A row whose kwh came from the SoC estimate can exceed its own meter
    reading — the user's real database held 1.65 kWh against a metered
    1.05 — and it landed in the numerator, so the promise did not hold.
    """
    now = dt_util.now()
    # Two honest rows: 18/20 and 9/10, i.e. 27/30 = 90 %.
    await storage.async_insert_charge(_charge(
        ended_at=now, kwh=18.0, evse_energy_kwh=20.0,
    ))
    await storage.async_insert_charge(_charge(
        ended_at=now, kwh=9.0, evse_energy_kwh=10.0,
    ))
    agg = await storage.async_charges_aggregates_since(now - timedelta(days=30))
    assert agg["evse_count"] == 2
    assert agg["charging_efficiency_pct"] == pytest.approx(90.0, abs=0.1)

    # An impossible row must not join either sum.
    await storage.async_insert_charge(_charge(
        ended_at=now, kwh=1.65, evse_energy_kwh=1.05,
    ))
    agg = await storage.async_charges_aggregates_since(now - timedelta(days=30))
    assert agg["evse_count"] == 2, "impossible row must not be counted"
    assert agg["charging_efficiency_pct"] == pytest.approx(90.0, abs=0.1)
    # It still counts as delivered energy — only the efficiency pair drops it.
    assert agg["kwh"] == pytest.approx(28.65)


async def _cap_charge(**over) -> ChargeRecord:
    """A charge big enough to clear every capacity gate but the new one."""
    base = {
        "started_at": dt_util.now() - timedelta(hours=1),
        "ended_at": dt_util.now(),
        "price_per_kwh": 0.2,
        "temperature_c": 20.0,
    }
    base.update(over)
    base.setdefault("total_cost", base["kwh"] * 0.2)
    return ChargeRecord(**base)


async def test_metered_tier_wins_and_excludes_under_integrated_charges(
    storage: TripStorage,
) -> None:
    """v0.8.30 — a measured `kwh` that its own meter contradicts is out.

    `kwh` is the shared numerator of the efficiency and of the capacity
    sample, so an under-integrated power measurement drags both down
    together. On the author's real history, sessions at 76-83 %
    efficiency implied a 76-78 kWh pack and sessions at 91-97 % implied
    87-90 kWh — the same physical battery. Five well-metered 82 kWh
    charges plus three under-integrated 70 kWh ones must calibrate to
    82, not to the median of all eight.
    """
    for _ in range(5):
        await storage.async_insert_charge(await _cap_charge(
            kwh=41.0, soc_start=10.0, soc_end=60.0,   # 82.0 kWh implied
            evse_energy_kwh=43.6, energy_source="power_integration",
        ))                                            # 94.0 % efficiency
    for _ in range(3):
        await storage.async_insert_charge(await _cap_charge(
            kwh=35.0, soc_start=10.0, soc_end=60.0,   # 70.0 kWh implied
            evse_energy_kwh=43.6, energy_source="power_integration",
        ))                                            # 80.3 % efficiency
    cap, n, _rejects, src = await storage.async_effective_capacity_kwh(
        min_delta_pct=30.0, min_charges=5,
    )
    assert src == "metered"
    assert n == 5, "the three under-integrated charges must not be samples"
    assert cap == pytest.approx(82.0, abs=0.05)


async def test_metered_tier_falls_through_when_too_few_qualify(
    storage: TripStorage,
) -> None:
    """Below `min_charges` the metered tier yields to the old behaviour.

    This is what makes the new tier safe to add: it can only ever
    improve on the previous release, never take a calibration away. An
    install with three well-metered charges keeps calibrating from all
    of its power-integration charges, exactly as v0.8.14 did.
    """
    for _ in range(3):
        await storage.async_insert_charge(await _cap_charge(
            kwh=41.0, soc_start=10.0, soc_end=60.0,
            evse_energy_kwh=43.6, energy_source="power_integration",
        ))
    for _ in range(3):
        await storage.async_insert_charge(await _cap_charge(
            kwh=35.0, soc_start=10.0, soc_end=60.0,
            evse_energy_kwh=43.6, energy_source="power_integration",
        ))
    cap, n, _rejects, src = await storage.async_effective_capacity_kwh(
        min_delta_pct=30.0, min_charges=5,
    )
    assert src == "grounded"
    assert n == 6
    assert cap == pytest.approx(76.0, abs=0.05)  # median of 82 and 70


async def test_charges_without_a_meter_are_not_counted_as_rejects(
    storage: TripStorage,
) -> None:
    """No meter wired means never a candidate for the metered tier.

    Counting those as rejects would make the reject totals unreadable —
    every install without an EVSE sensor would show its whole history
    as rejected. They fall through to `grounded` instead.
    """
    for _ in range(6):
        await storage.async_insert_charge(await _cap_charge(
            kwh=41.0, soc_start=10.0, soc_end=60.0,
            energy_source="power_integration",     # no evse_energy_kwh
        ))
    cap, n, rejects, src = await storage.async_effective_capacity_kwh(
        min_delta_pct=30.0, min_charges=5,
    )
    assert src == "grounded"
    assert n == 6
    assert cap == pytest.approx(82.0, abs=0.05)
    assert sum(rejects.values()) == 0


async def test_soc_pool_is_labelled_as_the_tautology_it_is(
    storage: TripStorage,
) -> None:
    """The last-resort pool must be identifiable from the outside.

    For a row whose `kwh` was itself computed as ΔSoC × capacity, the
    sample `kwh / ΔSoC × 100` returns the capacity it started from. The
    number is not wrong, it is simply not evidence, and until v0.8.30
    nothing distinguished it from a measured one.
    """
    for _ in range(5):
        await storage.async_insert_charge(await _cap_charge(
            kwh=41.25, soc_start=10.0, soc_end=60.0,   # 82.5 × 50 %
            energy_source=None,
        ))
    cap, n, _rejects, src = await storage.async_effective_capacity_kwh(
        min_delta_pct=30.0, min_charges=5,
    )
    assert src == "soc"
    assert n == 5
    assert cap == pytest.approx(82.5, abs=0.05)


async def test_km_before_sums_trips_between_consecutive_charges(
    storage: TripStorage,
) -> None:
    """v0.8.32 — each charge reports the km driven since the previous one.

    That figure is the practical proxy for "the pack arrived warm", which
    is what explains most of why one DC session accepted 150 kW and the
    next one 40 kW. It is derived on read rather than stored precisely
    because it depends on the *previous* charge: a manual recovery lands
    with a fresh id and an old timestamp, and a stored value on its
    neighbour would go stale without anything noticing.
    """
    base = dt_util.now() - timedelta(days=2)

    async def charge(hours: float, **over) -> None:
        await storage.async_insert_charge(_charge(
            started_at=base + timedelta(hours=hours),
            ended_at=base + timedelta(hours=hours, minutes=30),
            kwh=over.pop("kwh", 40.0), **over,
        ))

    async def trip(hours: float, km: float) -> None:
        await storage.async_insert(TripRecord(
            started_at=base + timedelta(hours=hours),
            ended_at=base + timedelta(hours=hours, minutes=45),
            duration_min=45.0, distance_km=km, energy_kwh=km * 0.2,
        ))

    await charge(0)                 # first charge: no previous, so no window
    await trip(2, 120.0)
    await trip(5, 77.0)
    await charge(8)                 # 197 km since the first charge
    await trip(20, 30.0)
    await charge(24)                # 30 km since the second

    recent = await storage.async_recent_charges(limit=10)
    by_start = {c.started_at.hour: c for c in recent}
    assert len(recent) == 3
    # Newest first: 30 km, then 197 km, then the first charge with no
    # predecessor at all.
    assert [c.km_before for c in recent] == [
        pytest.approx(30.0), pytest.approx(197.0), None,
    ]
    assert by_start[base.hour].km_before is None, (
        "the oldest charge has no previous charge to measure from"
    )


async def test_min_before_measures_driving_time_not_distance(
    storage: TripStorage,
) -> None:
    """v0.8.35 — the gap also reports how long the car was driving.

    Distance and time are not interchangeable inputs. The same 150 km
    covered in 90 minutes of motorway leaves the pack hot; covered in
    four hours of town driving it does not. This fixture gives two
    charges identical `km_before` and different `min_before` so a
    regression that derived one from the other would fail here.
    """
    base = dt_util.now() - timedelta(days=3)

    async def charge(hours: float) -> None:
        await storage.async_insert_charge(_charge(
            started_at=base + timedelta(hours=hours),
            ended_at=base + timedelta(hours=hours, minutes=20),
            kwh=30.0,
        ))

    async def trip(hours: float, km: float, minutes: float) -> None:
        await storage.async_insert(TripRecord(
            started_at=base + timedelta(hours=hours),
            ended_at=base + timedelta(hours=hours, minutes=minutes),
            duration_min=minutes, distance_km=km, energy_kwh=km * 0.2,
        ))

    await charge(0)
    await trip(1, 150.0, 90.0)          # motorway
    await charge(4)
    await trip(6, 75.0, 120.0)          # town, half the distance
    await trip(9, 75.0, 120.0)          # ... twice
    await charge(12)

    recent = await storage.async_recent_charges(limit=10)
    assert [c.km_before for c in recent] == [
        pytest.approx(150.0), pytest.approx(150.0), None,
    ], "both windows cover the same distance on purpose"
    assert [c.min_before for c in recent] == [
        pytest.approx(240.0), pytest.approx(90.0), None,
    ], "the time is what separates them"


async def test_min_before_is_zero_not_none_when_the_car_did_not_move(
    storage: TripStorage,
) -> None:
    """Same None-vs-zero rule as km_before, for the same reason.

    Zero minutes of driving before a charge is a real observation — the
    pack arrived cold. `None` means there was no previous charge to
    measure a window from.
    """
    base = dt_util.now() - timedelta(days=1)
    for h in (0, 4):
        await storage.async_insert_charge(_charge(
            started_at=base + timedelta(hours=h),
            ended_at=base + timedelta(hours=h, minutes=30),
            kwh=10.0,
        ))
    recent = await storage.async_recent_charges(limit=5)
    assert [c.min_before for c in recent] == [pytest.approx(0.0), None]


async def test_km_before_is_zero_not_none_when_the_car_did_not_move(
    storage: TripStorage,
) -> None:
    """Zero km and no data are different answers and must look different.

    A charge right after another with no driving in between really is
    0 km — the pack had no chance to warm up, which is exactly the
    diagnosis. `None` is reserved for "there is no previous charge".
    """
    base = dt_util.now() - timedelta(days=1)
    for h in (0, 4):
        await storage.async_insert_charge(_charge(
            started_at=base + timedelta(hours=h),
            ended_at=base + timedelta(hours=h, minutes=30),
            kwh=10.0,
        ))
    recent = await storage.async_recent_charges(limit=5)
    assert [c.km_before for c in recent] == [pytest.approx(0.0), None]


async def test_km_before_ignores_trips_during_the_charge_itself(
    storage: TripStorage,
) -> None:
    """The window ends when the charge starts, not when it ends.

    Km logged while the session was open belong to the *next* charge's
    window. Counting them here would credit warmth to a charge that had
    already begun cold.
    """
    base = dt_util.now() - timedelta(days=1)
    await storage.async_insert_charge(_charge(
        started_at=base, ended_at=base + timedelta(minutes=30), kwh=10.0,
    ))
    await storage.async_insert(TripRecord(     # before the 2nd charge starts
        started_at=base + timedelta(hours=1),
        ended_at=base + timedelta(hours=2),
        duration_min=60.0, distance_km=50.0, energy_kwh=10.0,
    ))
    await storage.async_insert_charge(_charge(
        started_at=base + timedelta(hours=3),
        ended_at=base + timedelta(hours=5), kwh=40.0,
    ))
    await storage.async_insert(TripRecord(     # DURING that 2nd charge
        started_at=base + timedelta(hours=3, minutes=30),
        ended_at=base + timedelta(hours=4),
        duration_min=30.0, distance_km=999.0, energy_kwh=10.0,
    ))
    recent = await storage.async_recent_charges(limit=5)
    assert recent[0].km_before == pytest.approx(50.0), (
        "the 999 km trip closed after the charge started and must not count"
    )


async def test_charger_rated_power_round_trips_and_can_be_set_later(
    storage: TripStorage,
) -> None:
    """v0.8.33 — the charger's rating survives, and can arrive days late.

    Nothing publishes it, so the realistic flow is: the charge auto-logs
    with the field empty, and the user fills it in from memory later. That
    must work without touching the price — the row already has one, and
    re-pricing it as a side effect would silently overwrite a receipt.
    """
    now = dt_util.now()
    cid = await storage.async_insert_charge(_charge(
        ended_at=now, kwh=42.23, price_per_kwh=0.30, total_cost=12.67,
    ))
    fetched = await storage.async_get_charge_by_id(cid)
    assert fetched.charger_power_kw is None

    patched = await storage.async_update_charge_by_id(cid, charger_power_kw=150.0)
    assert patched.charger_power_kw == pytest.approx(150.0)
    assert patched.price_per_kwh == pytest.approx(0.30), "price must be untouched"
    assert patched.total_cost == pytest.approx(12.67)
    assert patched.price_locked is False, (
        "setting the charger rating is not a pricing correction and must not lock"
    )


async def test_charger_rated_power_separates_the_two_41_kw_cases(
    storage: TripStorage,
) -> None:
    """The reason the field exists: 41 kW observed means two opposite things.

    Pegged at a 50 kW unit's real ceiling is a normal session. The same
    41 kW out of a 150 kW unit is a problem — cold pack, shared cabinet,
    or a derating charger. Inferring the charger's class from the observed
    peak cannot tell them apart, because in the second case the peak IS
    the low number. Only the rating separates them.
    """
    now = dt_util.now()
    ids = []
    for rated in (50.0, 150.0):
        ids.append(await storage.async_insert_charge(_charge(
            ended_at=now, kwh=40.0, peak_charge_power_kw=41.0,
            charger_power_kw=rated,
        )))
    got = [await storage.async_get_charge_by_id(i) for i in ids]
    assert [c.charger_power_kw for c in got] == [50.0, 150.0]
    # Same observed peak, ratios 82 % and 27 %.
    ratios = [c.peak_charge_power_kw / c.charger_power_kw for c in got]
    assert ratios[0] == pytest.approx(0.82, abs=0.01)
    assert ratios[1] == pytest.approx(0.273, abs=0.01)


async def test_charger_rated_power_survives_a_price_correction(
    storage: TripStorage,
) -> None:
    """Correcting the price later must not wipe the rating, or vice versa.

    Both fields ride the same service and the same UPDATE, so each has to
    fall back to the stored value when the caller omits it.
    """
    now = dt_util.now()
    cid = await storage.async_insert_charge(_charge(ended_at=now, kwh=40.0))
    await storage.async_update_charge_by_id(cid, charger_power_kw=350.0)
    patched = await storage.async_update_charge_by_id(cid, total_cost=20.0)
    assert patched.charger_power_kw == pytest.approx(350.0)
    assert patched.total_cost == pytest.approx(20.0)
    assert patched.price_locked is True
    # And the other direction.
    again = await storage.async_update_charge_by_id(cid, charger_power_kw=150.0)
    assert again.total_cost == pytest.approx(20.0)
    assert again.charger_power_kw == pytest.approx(150.0)


async def test_charge_gps_backfill_lists_and_writes_once(
    storage: TripStorage,
) -> None:
    """v0.8.34 — position is the only key that identifies a charger.

    `location` reads 'not_home' at every public charger, so without
    coordinates there is nothing to match one session against another.
    The write is guarded so a backfill can never clobber a position
    already recorded at close, which is the better of the two.
    """
    now = dt_util.now()
    older = await storage.async_insert_charge(_charge(ended_at=now - timedelta(hours=2)))
    live = await storage.async_insert_charge(_charge(
        ended_at=now, charge_lat=38.42443, charge_lon=-6.41494,
    ))

    pending = await storage.async_charges_missing_gps()
    assert [p["id"] for p in pending] == [older], (
        "the charge that already knows where it was is not pending"
    )

    await storage.async_set_charge_gps(older, 41.52119, -5.75497)
    got = await storage.async_get_charge_by_id(older)
    assert got.charge_lat == pytest.approx(41.52119)
    assert got.charge_lon == pytest.approx(-5.75497)
    assert await storage.async_charges_missing_gps() == []

    # A second pass must not move it.
    await storage.async_set_charge_gps(older, 0.0, 0.0)
    again = await storage.async_get_charge_by_id(older)
    assert again.charge_lat == pytest.approx(41.52119), "already-set position is final"

    kept = await storage.async_get_charge_by_id(live)
    assert kept.charge_lat == pytest.approx(38.42443)
