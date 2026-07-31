"""Tests for the weighted-average-cost (WAC) battery pool accounting.

`async_recompute_trip_costs_from_charges(default_price)` replays the whole
charge/trip history and models the battery as ONE blended pool: every charge
dilutes/raises a single running €/kWh average (weighted by kWh), and every
trip withdraws energy from that pool at whatever the blended average
currently is. Falls back to the home tariff (`default_price`) only when the
pool can't cover the withdrawal.

v0.8.8 replaced the earlier FIFO-slice model (discrete per-charge lots
drained oldest-first) with this pool model: a free/cheap charge should
smoothly lower the cost of the driving it covers, not create a slice other
energy has to wait behind before its price can change.

These tests share the same fixture style as `test_storage.py`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ev_trip_logger.storage import (
    ChargeRecord,
    TripRecord,
    TripStorage,
)


# Anchor every scenario at a fixed clock so the chronological ordering
# we assert is unambiguous and not coupled to wall-clock drift between
# rows inserted milliseconds apart.
T0 = datetime(2026, 1, 1, 12, 0, 0)


def _charge(
    *,
    started_at: datetime | None = None,
    ended_at: datetime,
    kwh: float,
    price_per_kwh: float,
) -> ChargeRecord:
    return ChargeRecord(
        started_at=started_at or (ended_at - timedelta(hours=1)),
        ended_at=ended_at,
        kwh=kwh,
        price_per_kwh=price_per_kwh,
        total_cost=kwh * price_per_kwh,
        currency="EUR",
    )


def _trip(
    *,
    started_at: datetime,
    ended_at: datetime,
    energy_kwh: float | None,
    distance_km: float | None = 50.0,
) -> TripRecord:
    duration_min = (ended_at - started_at).total_seconds() / 60.0
    consumption = (
        energy_kwh / distance_km * 100.0
        if energy_kwh is not None and distance_km
        else None
    )
    return TripRecord(
        started_at=started_at,
        ended_at=ended_at,
        duration_min=duration_min,
        distance_km=distance_km,
        energy_kwh=energy_kwh,
        consumption_kwh_100km=consumption,
        cost=None,
        currency="EUR",
    )


@pytest.fixture
async def storage(hass: HomeAssistant) -> TripStorage:
    s = TripStorage(hass, f"test_{uuid.uuid4().hex}")
    await s.async_init()
    return s


async def _trips_by_id(storage: TripStorage) -> dict[int, TripRecord]:
    rows = await storage.async_recent_trips(limit=1000)
    return {t.trip_id: t for t in rows}


async def test_wac_single_charge_single_trip(storage: TripStorage) -> None:
    """1 charge of 50 kWh @ 0.07 fully covers a 5 kWh trip."""
    await storage.async_insert_charge(
        _charge(ended_at=T0, kwh=50.0, price_per_kwh=0.07)
    )
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=1),
        ended_at=T0 + timedelta(hours=2),
        energy_kwh=5.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.20)

    trips = await _trips_by_id(storage)
    healed = trips[trip_id]
    assert healed.cost == pytest.approx(0.35)
    assert healed.cost_basis_per_kwh == pytest.approx(0.07)


async def test_wac_two_charges_blend_into_one_average(
    storage: TripStorage,
) -> None:
    """Both charges have ended by the time the trip runs, so they blend
    into ONE pool average before the trip draws anything — unlike FIFO,
    the trip doesn't drain the older charge first and only touch the
    newer one once the older is exhausted.
    """
    await storage.async_insert_charge(
        _charge(ended_at=T0, kwh=10.0, price_per_kwh=0.50)
    )
    await storage.async_insert_charge(
        _charge(ended_at=T0 + timedelta(hours=1), kwh=20.0, price_per_kwh=0.10)
    )
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=3),
        ended_at=T0 + timedelta(hours=4),
        energy_kwh=15.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.20)

    trip = (await _trips_by_id(storage))[trip_id]
    # Blended pool average: (10*0.50 + 20*0.10) / 30 = 7/30.
    # Stored values are rounded (basis to 6dp, cost to 4dp) — allow for that.
    avg = 7.0 / 30.0
    assert trip.cost_basis_per_kwh == pytest.approx(avg, abs=1e-5)
    assert trip.cost == pytest.approx(15.0 * avg, abs=1e-3)


async def test_wac_free_charge_dilutes_average(storage: TripStorage) -> None:
    """A free charge dilutes the pool's blended average — it doesn't
    create a separate free "slice" that has to fully drain before the
    price can change.
    """
    await storage.async_insert_charge(
        _charge(ended_at=T0, kwh=30.0, price_per_kwh=0.07)
    )
    await storage.async_insert_charge(
        _charge(ended_at=T0 + timedelta(hours=1), kwh=20.0, price_per_kwh=0.0)
    )
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=3),
        ended_at=T0 + timedelta(hours=4),
        energy_kwh=40.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.20)

    trip = (await _trips_by_id(storage))[trip_id]
    # Blended average: (30*0.07 + 20*0.0) / 50 = 2.1/50 = 0.042.
    avg = 2.1 / 50.0
    assert trip.cost_basis_per_kwh == pytest.approx(avg)
    assert trip.cost == pytest.approx(40.0 * avg)


async def test_wac_pool_runs_dry_falls_back_to_home_tariff(
    storage: TripStorage,
) -> None:
    """When the pool can't cover the full withdrawal, the shortfall is
    billed at default_price.
    """
    await storage.async_insert_charge(
        _charge(ended_at=T0, kwh=5.0, price_per_kwh=0.07)
    )
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=2),
        ended_at=T0 + timedelta(hours=3),
        energy_kwh=8.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.20)

    trip = (await _trips_by_id(storage))[trip_id]
    # 5 * 0.07 + 3 * 0.20 = 0.35 + 0.60 = 0.95
    assert trip.cost == pytest.approx(0.95)
    assert trip.cost_basis_per_kwh == pytest.approx(0.95 / 8.0)


async def test_wac_no_charges_fallback(storage: TripStorage) -> None:
    """No charges at all → every kWh costs default_price."""
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=1),
        ended_at=T0 + timedelta(hours=2),
        energy_kwh=10.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.10)

    trip = (await _trips_by_id(storage))[trip_id]
    assert trip.cost == pytest.approx(1.00)
    assert trip.cost_basis_per_kwh == pytest.approx(0.10)


async def test_wac_idempotent(storage: TripStorage) -> None:
    """Recompute twice → no drift. The method rebuilds from charges every
    call, so running it again must not double-bill or mutate the result.
    """
    await storage.async_insert_charge(
        _charge(ended_at=T0, kwh=30.0, price_per_kwh=0.12)
    )
    await storage.async_insert_charge(
        _charge(ended_at=T0 + timedelta(hours=1), kwh=20.0, price_per_kwh=0.20)
    )
    a = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=2),
        ended_at=T0 + timedelta(hours=3),
        energy_kwh=10.0,
    ))
    b = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=4),
        ended_at=T0 + timedelta(hours=5),
        energy_kwh=25.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.30)
    first = await _trips_by_id(storage)
    cost_a1 = first[a].cost
    cost_b1 = first[b].cost
    basis_a1 = first[a].cost_basis_per_kwh
    basis_b1 = first[b].cost_basis_per_kwh

    # Blended pool average before either trip: (30*0.12+20*0.20)/50 = 0.152.
    avg = (30.0 * 0.12 + 20.0 * 0.20) / 50.0
    assert basis_a1 == pytest.approx(avg)
    assert cost_a1 == pytest.approx(10.0 * avg)
    assert basis_b1 == pytest.approx(avg)
    assert cost_b1 == pytest.approx(25.0 * avg)

    await storage.async_recompute_trip_costs_from_charges(default_price=0.30)
    second = await _trips_by_id(storage)
    assert second[a].cost == pytest.approx(cost_a1)
    assert second[b].cost == pytest.approx(cost_b1)
    assert second[a].cost_basis_per_kwh == pytest.approx(basis_a1)
    assert second[b].cost_basis_per_kwh == pytest.approx(basis_b1)


async def test_wac_skips_null_energy_trips(storage: TripStorage) -> None:
    """A trip with energy_kwh=None AND distance_km=None must not crash the
    recompute, must not draw from the pool, and must keep cost = NULL (the
    Step 1 healer can't backfill energy without distance, and the WAC
    walker has nothing to bill it for).
    """
    await storage.async_insert_charge(
        _charge(ended_at=T0, kwh=20.0, price_per_kwh=0.10)
    )
    null_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=1),
        ended_at=T0 + timedelta(hours=2),
        energy_kwh=None,
        distance_km=0.0,
    ))
    real_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=3),
        ended_at=T0 + timedelta(hours=4),
        energy_kwh=10.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.50)

    trips = await _trips_by_id(storage)
    # NULL-energy row left untouched.
    assert trips[null_id].cost is None
    # Real trip got the full 10 kWh from the pool (the null trip didn't
    # draw from it), so cost = 10 * 0.10 = 1.00 at basis 0.10.
    assert trips[real_id].cost == pytest.approx(1.00)
    assert trips[real_id].cost_basis_per_kwh == pytest.approx(0.10)


async def test_wac_three_charges_three_trips_blended(
    storage: TripStorage,
) -> None:
    """3 charges interleaved with 3 trips — each trip draws from the
    pool's blended average AT THAT MOMENT (only charges that had ended by
    its own start have blended in), and any leftover pool + price carries
    over into the next charge's blend.
    """
    # Timeline (h offset from T0):
    #   h=0  charge A ends:   10 kWh @ 0.05
    #        pool = 10 kWh @ 0.05
    #   h=1  trip 1: 4 kWh   → draws @ 0.05 → cost 0.20, pool = 6 @ 0.05
    #   h=2  charge B ends:   10 kWh @ 0.20
    #        blend: (6*0.05 + 10*0.20) / 16 = 2.3/16 = 0.14375
    #   h=3  trip 2: 10 kWh  → draws @ 0.14375 → cost 1.4375, pool = 6 @ 0.14375
    #   h=4  charge C ends:   10 kWh @ 0.50
    #        blend: (6*0.14375 + 10*0.50) / 16 = 5.8625/16 = 0.36640625
    #   h=5  trip 3: 8 kWh   → draws @ 0.36640625 → cost 2.93125
    await storage.async_insert_charge(
        _charge(ended_at=T0, kwh=10.0, price_per_kwh=0.05)
    )
    t1 = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=1, minutes=0),
        ended_at=T0 + timedelta(hours=1, minutes=30),
        energy_kwh=4.0,
    ))
    await storage.async_insert_charge(
        _charge(ended_at=T0 + timedelta(hours=2), kwh=10.0, price_per_kwh=0.20)
    )
    t2 = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=3, minutes=0),
        ended_at=T0 + timedelta(hours=3, minutes=30),
        energy_kwh=10.0,
    ))
    await storage.async_insert_charge(
        _charge(ended_at=T0 + timedelta(hours=4), kwh=10.0, price_per_kwh=0.50)
    )
    t3 = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=5, minutes=0),
        ended_at=T0 + timedelta(hours=5, minutes=30),
        energy_kwh=8.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=1.00)

    trips = await _trips_by_id(storage)
    assert trips[t1].cost_basis_per_kwh == pytest.approx(0.05)
    assert trips[t1].cost == pytest.approx(0.20)

    avg2 = (6.0 * 0.05 + 10.0 * 0.20) / 16.0
    assert trips[t2].cost_basis_per_kwh == pytest.approx(avg2)
    assert trips[t2].cost == pytest.approx(10.0 * avg2)

    pool_after_t2 = 16.0 - 10.0
    avg3 = (pool_after_t2 * avg2 + 10.0 * 0.50) / (pool_after_t2 + 10.0)
    assert trips[t3].cost_basis_per_kwh == pytest.approx(avg3, abs=1e-5)
    assert trips[t3].cost == pytest.approx(8.0 * avg3, abs=1e-3)


async def test_wac_set_last_charge_price_propagates(
    storage: TripStorage,
) -> None:
    """Correcting the only charge's price retroactively re-prices every
    trip that drew from it on the next recompute pass.
    """
    await storage.async_insert_charge(
        _charge(ended_at=T0, kwh=100.0, price_per_kwh=0.07)
    )
    ids = []
    for i in range(3):
        ids.append(await storage.async_insert(_trip(
            started_at=T0 + timedelta(hours=1 + i),
            ended_at=T0 + timedelta(hours=1 + i, minutes=30),
            energy_kwh=5.0,
        )))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.00)
    before = await _trips_by_id(storage)
    for tid in ids:
        assert before[tid].cost == pytest.approx(5.0 * 0.07)
        assert before[tid].cost_basis_per_kwh == pytest.approx(0.07)

    # User corrects the latest (only) charge to 0.40 €/kWh.
    await storage.async_update_last_charge(price_per_kwh=0.40)
    await storage.async_recompute_trip_costs_from_charges(default_price=0.00)

    after = await _trips_by_id(storage)
    for tid in ids:
        assert after[tid].cost == pytest.approx(5.0 * 0.40)
        assert after[tid].cost_basis_per_kwh == pytest.approx(0.40)


async def test_wac_charge_after_trip_does_not_pollute(
    storage: TripStorage,
) -> None:
    """A charge that ended AFTER a trip started must not contribute to
    that trip's cost — only to subsequent ones.
    """
    # Trip at h=1 (5 kWh). Charge ends at h=2, AFTER the trip started.
    # With zero prior pool, the trip is fully billed at default_price.
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=1),
        ended_at=T0 + timedelta(hours=1, minutes=30),
        energy_kwh=5.0,
    ))
    await storage.async_insert_charge(
        _charge(ended_at=T0 + timedelta(hours=2), kwh=20.0, price_per_kwh=0.05)
    )
    # Later trip at h=3 — by now the late charge has ended, so it's
    # available in the pool.
    later_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=3),
        ended_at=T0 + timedelta(hours=3, minutes=30),
        energy_kwh=10.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.30)

    trips = await _trips_by_id(storage)
    # First trip: pool empty when it started → all 5 kWh @ 0.30.
    assert trips[trip_id].cost == pytest.approx(1.50)
    assert trips[trip_id].cost_basis_per_kwh == pytest.approx(0.30)
    # Later trip pulls cleanly from the (now-available) charge.
    assert trips[later_id].cost == pytest.approx(0.50)
    assert trips[later_id].cost_basis_per_kwh == pytest.approx(0.05)
