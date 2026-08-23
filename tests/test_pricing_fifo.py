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


async def test_wac_pool_runs_dry_uses_the_blend_seen_so_far(
    storage: TripStorage,
) -> None:
    """v0.8.17 — the shortfall is priced at the blend already seen, not
    at the configured home tariff.

    A shortfall means the car consumed more than we tracked charging, so
    the missing energy came from somewhere untracked. The best available
    estimate of what it cost is what this car's energy has cost so far —
    not a tariff that may have nothing to do with it. On a road trip the
    old behaviour was systematically optimistic: every kWh the pool
    couldn't cover was billed at the cheapest number in the config while
    every real charge was DC.

    It stays causal: only charges that already happened feed the average,
    so a charge logged tomorrow can never re-price today's driving (see
    `test_wac_charge_after_trip_does_not_pollute`).
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
    # 5 kWh from the pool @0.07 + 3 kWh of shortfall at that same 0.07
    assert trip.cost == pytest.approx(0.56)
    assert trip.cost_basis_per_kwh == pytest.approx(0.07)


async def test_wac_long_charge_does_not_block_later_ones(
    storage: TripStorage,
) -> None:
    """v0.8.17 — head-of-line blocking used to drop charges entirely.

    The queue was ordered by `started_at` but consumed on `ended_at`, and
    the loop stopped at the head. A session that starts early and ends
    late therefore stalled every charge that ended in between — those
    kWh reached no trip at all and the trip fell to the fallback price.
    """
    # Starts first, ends much later — the blocker.
    await storage.async_insert_charge(ChargeRecord(
        started_at=T0,
        ended_at=T0 + timedelta(hours=50),
        kwh=40.0, price_per_kwh=0.07, total_cost=2.8, currency="EUR",
    ))
    # Starts after it, but ends long before it.
    await storage.async_insert_charge(ChargeRecord(
        started_at=T0 + timedelta(hours=1),
        ended_at=T0 + timedelta(hours=2),
        kwh=30.0, price_per_kwh=0.45, total_cost=13.5, currency="EUR",
    ))
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=3),
        ended_at=T0 + timedelta(hours=4),
        energy_kwh=20.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.99)

    trip = (await _trips_by_id(storage))[trip_id]
    # The 30 kWh @0.45 session had ended an hour earlier, so the trip
    # draws from it — not from the 0.99 fallback.
    assert trip.cost == pytest.approx(9.0)
    assert trip.cost_basis_per_kwh == pytest.approx(0.45)


async def test_wac_pool_is_reanchored_to_real_soc_after_each_charge(
    storage: TripStorage,
) -> None:
    """v0.8.17 — the pool tracks price; the battery decides quantity.

    The additive pool had no sink for energy that leaves the battery
    without being a trip (standby drain, preconditioning) and no opening
    inventory, so it drifted free of the physical pack — on the reporter's
    car it was able to hold more kWh than the battery can physically
    store. After a charge closes, `soc_end` states the real content
    exactly, so the pool is re-anchored to it and the drift is dropped.
    """
    storage.capacity_hint_kwh = 100.0  # 1 % = 1 kWh, keeps the maths obvious

    # Two charges totalling 90 kWh, but the pack ends at 50 % = 50 kWh:
    # 40 kWh left the battery without ever being a trip.
    await storage.async_insert_charge(ChargeRecord(
        started_at=T0 - timedelta(hours=2), ended_at=T0 - timedelta(hours=1),
        kwh=45.0, price_per_kwh=0.10, total_cost=4.5, currency="EUR",
        soc_start=5.0, soc_end=50.0,
    ))
    await storage.async_insert_charge(ChargeRecord(
        started_at=T0, ended_at=T0 + timedelta(minutes=30),
        kwh=45.0, price_per_kwh=0.50, total_cost=22.5, currency="EUR",
        soc_start=5.0, soc_end=50.0,
    ))
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=1),
        ended_at=T0 + timedelta(hours=2),
        energy_kwh=60.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.99)

    trip = (await _trips_by_id(storage))[trip_id]
    # At the second cable-in the pack held 5 kWh left over at 0.10, and
    # 45 kWh arrived at 0.50: (5*0.10 + 45*0.50)/50 = 0.46. The 45 kWh
    # that vanished between the two sessions is NOT credited — with the
    # old additive pool it was, and it dragged the price down to 0.289.
    # Of the 60 kWh withdrawn, 50 were physically there; the remaining
    # 10 is a shortfall priced at the same blend.
    assert trip.cost_basis_per_kwh == pytest.approx(0.46)
    assert trip.cost == pytest.approx(27.6)


async def test_wac_prices_the_invoice_not_the_battery_side_kwh(
    storage: TripStorage,
) -> None:
    """v0.8.17 — money paid is the input; €/kWh is derived from it.

    A public tariff is billed on the charger-side kWh, which is 5-15 %
    above what reaches the battery. Pricing the pool off `price_per_kwh`
    against the battery-side `kwh` silently drops the charging losses, so
    energy that was genuinely paid for reached no trip.
    """
    # 30 kWh metered and invoiced at €16.50; 27 kWh actually stored.
    await storage.async_insert_charge(ChargeRecord(
        started_at=T0 - timedelta(hours=1), ended_at=T0,
        kwh=27.0, price_per_kwh=0.55, total_cost=16.50, currency="EUR",
    ))
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=1),
        ended_at=T0 + timedelta(hours=2),
        energy_kwh=27.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.07)

    trip = (await _trips_by_id(storage))[trip_id]
    # The whole invoice reaches the driving: 16.50 / 27 = 0.6111 per
    # battery-kWh, not the 0.55 charger-side rate.
    assert trip.cost == pytest.approx(16.50)
    assert trip.cost_basis_per_kwh == pytest.approx(16.50 / 27.0)


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


# ---------------------------------------------------------------------------
# v0.8.22 — `cost_lifo`, the per-lot companion to the pool's `cost`.
#
# The pool answers "what does a kWh out of this battery cost on average".
# It cannot answer "what did the energy I actually just bought cost me",
# because blending is lossy by design: fill up cheap on top of an expensive
# pack and the pool reports a price you never paid for anything.
#
# `cost_lifo` keeps the discrete lots and draws NEWEST-FIRST, so a trip taken
# right after a cheap charge is priced at that charge until it runs out.
# `cost` and `cost_basis_per_kwh` are untouched — this is a second opinion,
# not a replacement, exactly as `cost_at_avg_tariff` is.
# ---------------------------------------------------------------------------


async def test_lifo_draws_from_the_newest_charge_first(
    storage: TripStorage,
) -> None:
    """Expensive charge, then a cheap one, then a trip small enough to fit
    inside the cheap one. The pool blends both; LIFO spends the cheap one.
    """
    storage.capacity_hint_kwh = 100.0
    await storage.async_insert_charge(
        _charge(ended_at=T0, kwh=40.0, price_per_kwh=0.60)
    )
    await storage.async_insert_charge(
        _charge(ended_at=T0 + timedelta(hours=1), kwh=30.0, price_per_kwh=0.20)
    )
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=2),
        ended_at=T0 + timedelta(hours=3),
        energy_kwh=10.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.99)
    t = (await _trips_by_id(storage))[trip_id]

    assert t.cost_lifo == pytest.approx(10.0 * 0.20, abs=0.01)
    # The pool still blends, and still reports its own number.
    assert t.cost is not None and t.cost > t.cost_lifo


async def test_lifo_spills_into_the_older_lot_when_the_newest_runs_out(
    storage: TripStorage,
) -> None:
    """The real 2026-08-23 shape: a cheap charge on top of expensive DC
    energy, and a trip bigger than the cheap charge.

    39.63 kWh at 0.2735 = 10.84, then 14.86 kWh of the older 0.57 energy
    = 8.47 -> 19.31. The pool blends the two into ~0.43 and reports 23.55
    for the same 54.49 kWh.
    """
    storage.capacity_hint_kwh = 82.5
    await storage.async_insert_charge(
        _charge(ended_at=T0, kwh=60.0, price_per_kwh=0.57)
    )
    await storage.async_insert_charge(
        _charge(ended_at=T0 + timedelta(hours=1), kwh=39.63,
                price_per_kwh=0.2735)
    )
    trip_id = await storage.async_insert(_trip(
        started_at=T0 + timedelta(hours=2),
        ended_at=T0 + timedelta(hours=5),
        energy_kwh=54.49, distance_km=245.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.07)
    t = (await _trips_by_id(storage))[trip_id]

    expected = 39.63 * 0.2735 + (54.49 - 39.63) * 0.57
    assert t.cost_lifo == pytest.approx(expected, abs=0.05)
    assert t.cost_lifo < t.cost, "LIFO must be cheaper here than the blend"


async def test_lifo_falls_back_to_the_tariff_with_no_charges(
    storage: TripStorage,
) -> None:
    """No lots at all: the shortfall is priced exactly as the pool prices
    it, so the two figures agree rather than one silently reading zero.
    """
    trip_id = await storage.async_insert(_trip(
        started_at=T0, ended_at=T0 + timedelta(hours=1), energy_kwh=8.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.30)
    t = (await _trips_by_id(storage))[trip_id]

    assert t.cost_lifo == pytest.approx(8.0 * 0.30, abs=0.01)
    assert t.cost_lifo == pytest.approx(t.cost, abs=0.01)


async def test_lifo_is_null_when_the_trip_has_no_energy(
    storage: TripStorage,
) -> None:
    """A trip the replay skips (no energy) must not be given a fabricated
    zero — NULL means "not computed", 0.0 would mean "free".
    """
    trip_id = await storage.async_insert(_trip(
        started_at=T0, ended_at=T0 + timedelta(hours=1),
        energy_kwh=None, distance_km=0.0,
    ))

    await storage.async_recompute_trip_costs_from_charges(default_price=0.30)
    t = (await _trips_by_id(storage))[trip_id]

    assert t.cost_lifo is None
