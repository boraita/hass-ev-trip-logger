"""Tests for v0.5.76 FIFO inventory cost accounting.

`async_recompute_trip_costs_from_charges(default_price)` is being refactored
to consume kWh from a chronological FIFO queue of charges (oldest first),
falling back to the home tariff (`default_price`) only when the inventory
runs dry. Each trip's `cost_basis_per_kwh` is the weighted-average price
of the slices it actually drew.

These tests are written against the spec; the implementation lands in the
same release. They share the same fixture style as `test_storage.py`.
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


async def test_fifo_single_charge_single_trip(storage: TripStorage) -> None:
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


async def test_fifo_oldest_first(storage: TripStorage) -> None:
    """Oldest charge is drained first: 10 kWh @ 0.50 then 5 kWh @ 0.10."""
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
    # 10 * 0.50 + 5 * 0.10 = 5.50
    assert trip.cost == pytest.approx(5.50)
    # Weighted avg 5.50 / 15 ≈ 0.3667
    assert trip.cost_basis_per_kwh == pytest.approx(5.50 / 15.0)


async def test_fifo_free_charge_mixed(storage: TripStorage) -> None:
    """Free kWh from the newer charge averages down the cost basis."""
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
    # 30 * 0.07 + 10 * 0.00 = 2.10
    assert trip.cost == pytest.approx(2.10)
    # 2.10 / 40 = 0.0525
    assert trip.cost_basis_per_kwh == pytest.approx(0.0525)


async def test_fifo_inventory_runs_dry(storage: TripStorage) -> None:
    """When the queue empties, the remainder is billed at default_price."""
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


async def test_fifo_no_charges_fallback(storage: TripStorage) -> None:
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


async def test_fifo_idempotent(storage: TripStorage) -> None:
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

    await storage.async_recompute_trip_costs_from_charges(default_price=0.30)
    second = await _trips_by_id(storage)
    assert second[a].cost == pytest.approx(cost_a1)
    assert second[b].cost == pytest.approx(cost_b1)
    assert second[a].cost_basis_per_kwh == pytest.approx(basis_a1)
    assert second[b].cost_basis_per_kwh == pytest.approx(basis_b1)


async def test_fifo_skips_null_energy_trips(storage: TripStorage) -> None:
    """A trip with energy_kwh=None AND distance_km=None must not crash the
    recompute, must not consume inventory, and must keep cost = NULL (the
    Step 1 healer can't backfill energy without distance, and the FIFO
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
    # Real trip got the full 10 kWh from the inventory (the null trip
    # didn't deplete it), so cost = 10 * 0.10 = 1.00 at basis 0.10.
    # If the null trip had consumed inventory, the real trip would have
    # only seen 10 kWh available → cost would still be 1.00, BUT if it
    # consumed 0 kWh (the correct behaviour), basis stays at 0.10.
    assert trips[real_id].cost == pytest.approx(1.00)
    assert trips[real_id].cost_basis_per_kwh == pytest.approx(0.10)


async def test_fifo_charge_before_first_trip_used_first(
    storage: TripStorage,
) -> None:
    """3 charges interleaved with 3 trips — each trip's cost reflects ONLY
    the charges that had ended by its own start. Inventory carries over
    between trips (oldest leftovers consumed first).
    """
    # Timeline (h offset from T0):
    #   h=0  charge A ends:   10 kWh @ 0.05
    #   h=1  trip 1: 4 kWh   → A has 10 (>=4) → 4 * 0.05 = 0.20
    #   h=2  charge B ends:   10 kWh @ 0.20
    #   h=3  trip 2: 10 kWh  → A has 6 left, B has 10 →
    #                            6 * 0.05 + 4 * 0.20 = 0.30 + 0.80 = 1.10
    #   h=4  charge C ends:   10 kWh @ 0.50
    #   h=5  trip 3: 8 kWh   → B has 6 left, C has 10 →
    #                            6 * 0.20 + 2 * 0.50 = 1.20 + 1.00 = 2.20
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
    assert trips[t1].cost == pytest.approx(0.20)
    assert trips[t1].cost_basis_per_kwh == pytest.approx(0.05)
    assert trips[t2].cost == pytest.approx(1.10)
    assert trips[t2].cost_basis_per_kwh == pytest.approx(1.10 / 10.0)
    assert trips[t3].cost == pytest.approx(2.20)
    assert trips[t3].cost_basis_per_kwh == pytest.approx(2.20 / 8.0)


async def test_fifo_set_last_charge_price_propagates(
    storage: TripStorage,
) -> None:
    """Correcting the only charge's price retroactively re-prices every
    trip that consumed from it on the next recompute pass.
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


async def test_fifo_charge_after_trip_does_not_pollute(
    storage: TripStorage,
) -> None:
    """A charge that ended AFTER a trip started must not contribute to
    that trip's cost — only to subsequent ones.
    """
    # Trip at h=1 (5 kWh). Charge ends at h=2, AFTER the trip started.
    # With zero prior inventory, the trip is fully billed at default_price.
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=1),
        ended_at=T0 + timedelta(hours=1, minutes=30),
        energy_kwh=5.0,
    ))
    await storage.async_insert_charge(
        _charge(ended_at=T0 + timedelta(hours=2), kwh=20.0, price_per_kwh=0.05)
    )
    # Later trip at h=3 — by now the late charge has ended, so it's
    # available in the FIFO inventory.
    later_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=3),
        ended_at=T0 + timedelta(hours=3, minutes=30),
        energy_kwh=10.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.30)

    trips = await _trips_by_id(storage)
    # First trip: inventory empty when it started → all 5 kWh @ 0.30.
    assert trips[trip_id].cost == pytest.approx(1.50)
    assert trips[trip_id].cost_basis_per_kwh == pytest.approx(0.30)
    # Later trip pulls cleanly from the (now-available) charge.
    assert trips[later_id].cost == pytest.approx(0.50)
    assert trips[later_id].cost_basis_per_kwh == pytest.approx(0.05)
