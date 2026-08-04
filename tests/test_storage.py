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
    cid = await storage.async_insert_charge(_charge(kwh=10.0))
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
    cap, n, _rejects = await storage.async_effective_capacity_kwh(
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
    cap, n, _rejects = await storage.async_effective_capacity_kwh(
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
    cap, n, rejects = await storage.async_effective_capacity_kwh(
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
    cap, n, _rejects = await storage.async_effective_capacity_kwh(
        min_delta_pct=30.0, min_charges=5
    )
    assert n == 5
    assert cap == pytest.approx(66.67, abs=0.05)


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
