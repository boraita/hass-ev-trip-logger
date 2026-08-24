# Changelog

Summarised, human-readable history from v0.8.0 onward. Full technical detail for every release (including everything before v0.8.0) lives in [GitHub Releases](https://github.com/boraita/hass-ev-trip-logger/releases) and the commit history.

## v0.8.25 — 2026-08-24
**Fix — journeys stopped being resolved at all: `NameError` on every call that found one.** v0.8.10 taught `_resolve_open_journey_id` about secondary homes by turning its single `slug` into a list of `slugs` and its first query's `LOWER(destination) = ?` into `IN (…)`. The **second** query — the one that actually returns the id — kept `= ?` bound to `(slug,)`, a name that no longer existed.

The first query is a guard that returns early when there is no candidate, so the crash only fired when there *was* an open journey: exactly the calls whose answer mattered. `async_resolve_open_journey_id` runs at startup and at every trip close, so from v0.8.10 onward a journey could still be minted on arrival at a home, but an open one could never be resumed.

Found by reading the live HA log while investigating why a 499 km round trip produced six trips with `journey_id = NULL`. The traceback had been sitting there the whole time:

    File "storage.py", line 1619, in _resolve_open_journey_id
        (slug,),
    NameError: name 'slug' is not defined. Did you mean: 'slugs'?

Two tests now cover it: one that an open journey is returned rather than raising, and one that an arrival at a *secondary* home closes it — the case the `slugs` list exists for and the one a single-slug query silently got wrong even before the rename broke it outright.

## v0.8.24 — 2026-08-24
**Fix — v0.8.23's unit fix was dead code.** Extending `_retry_needed()` to keep retrying while the unit is unknown was correct and did nothing, because `_schedule_startup_retry()` is called from exactly one place: the branch that runs when the recorder query *fails*. On the success path — the one the restart race actually takes, where the query works, a mean is computed, and only the source entity is missing — the retry was never scheduled at all, so the sensor still sat on `unit_of_measurement: None` until the next 1800 s tick.

Verified against the live install after deploying v0.8.23: all 22 rolling-average sensors still had no unit. The retry is now scheduled after a successful refresh too.

The lesson is the test's: `test_tracked_avg_retries_while_only_the_unit_is_missing` asserted that `_retry_needed()` returns the right answer, which it did. Nothing asserted that anybody asks it.

## v0.8.23 — 2026-08-24
Three defects found by reading a real install's logs after a 500 km day, all of them things the integration reported about itself rather than about the car.

**Fix — a stale odometer catching up released the "still charging" guard, and a trip opened on a stationary car.** v0.8.17 defers opening a trip while the charge session is still delivering, with an escape hatch: ≥1 km on the odometer since the session opened means the charge sensor is stuck `on` and the trip must open regardless. The hatch keyed on distance alone.

On 2026-08-23 polling was paused for 4 h 31 min. When it resumed, the odometer caught up **46 km, the charge was detected, and `vehicle_on` went high — inside the same second**. The hatch read the catch-up as driving, released the guard, and a trip opened while 87 kW was going into the battery. That trip then swallowed the whole charge and reported 197 km at 20.0 kWh/100 km; the real figure was 24.8.

The first attempt keyed on elapsed time — reject a delta implying an impossible speed — and it broke the v0.8.17 regression test on the first run: a compressed test timeline moves a legitimately-driving car just as fast as a stale reading lands, so time cannot separate the two.

Power can, and physically. A car taking 87 kW is not driving away from the charger. The hatch is now vetoed while the battery is visibly drawing ≥1 kW; below that the reading is idle noise or a trickle and the odometer stays the arbiter, so a sensor genuinely stuck `on` mid-drive still releases the guard, which is what it is for. With no power sensor configured there is nothing to veto with and the pre-v0.8.23 behaviour stands.

**Fix — 22 rolling-average sensors published without their unit for half an hour after every restart, and the recorder stopped compiling their statistics.** The v0.5.48 sticky-unit protection adopts the source's unit and keeps it across upstream blips, and a fast startup retry exists for when the first refresh runs before the recorder is ready. That retry gave up as soon as a *value* existed.

Measured: after a restart the average sensor published at 20:48:13 and its source entity was created at 20:48:16 — three seconds later. The mean was already good, so the retry bailed; with an 1800 s cadence the sensor went on reporting `unit_of_measurement: None` until the next tick. The recorder read that as a units change and refused to compile statistics for all 22 at once. The retry now also runs while the unit is unknown: a value whose unit is missing is not finished starting up.

**Fix — two permanently-unknown entities both displayed as "Distance".** `elevation_gain_m` and `elevation_loss_m` were built as `current_trip_` sensors as well as `last_trip_` ones, but elevation is derived from the trip's GPS route at close, so the live pair can never hold a value. Neither had a `current_trip_` translation either, so both fell back to their device-class name and collided — the real install carried `sensor.<device>_distance` and `sensor.<device>_distance_2`. Both keys join `cost_lifo` in the last-trip-only set.

**Fix — `cost_lifo` never reached the journey cards.** v0.8.22 added it to the journey SQL and to the dicts storage returns, but all three journey sensors rebuild their attribute payload key by key and none copied it across, so the figure was recomputed on every refresh and dropped. On the current journey it is deliberately the closed stages only — the active stage has no per-lot figure until the replay prices it — exactly as `cost` already behaves on that payload.

Existing entities are not renamed or removed by an upgrade: `sensor.<device>_distance` and `_distance_2` stay in the registry until deleted by hand.

## v0.8.22 — 2026-08-23
**Feature — `cost_lifo`, a per-lot answer to "what did the energy I just bought cost me".** The weighted-average pool that prices `cost` is the honest average of the whole battery, but blending is lossy by design: fill up cheap on top of an expensive pack and the pool reports a €/kWh you never paid for anything. Measured on a real 245 km drive — a 39.63 kWh charge at 0.2735 €/kWh sitting on top of DC energy bought at ~0.57 — the pool priced the trip's 54.49 kWh at a blended 0.432 and reported **23.55 €**, while the driver had actually bought most of that energy at the cheap rate.

`cost_lifo` keeps the charges as discrete lots and draws **newest first**, so a drive taken right after a cheap charge is priced at that charge until it runs out and only then spills into older, dearer energy. The same trip reads **19.35 €**. Neither number is wrong; they answer different questions, and both are now available.

It is computed inside the existing cost replay, which already walks every charge and trip in chronological order to build the pool — so the lot ledger is replayed in lockstep, stays idempotent, re-prices itself when a charge's price is corrected, and **backfills the whole history on the next startup** with no data migration.

The lot stack re-anchors to the pack's real content at `soc_start` and `soc_end` exactly as the pool does, because energy leaves a battery without being a trip (standby drain, preconditioning). Which lots absorb that discrepancy is a decision, not an accident: a surplus is trimmed from the *oldest* end, since unexplained loss is far likelier to be energy that has sat there for days than the charge that just went in — and trimming the newest would delete the very lot the next trip is about to be priced against. A shortfall (opening inventory on a fresh install) is padded at the incoming charge's price, mirroring the pool's own rule. When there are no lots at all the shortfall uses the identical fallback the pool uses, so the two figures agree rather than this one reading a misleading zero.

Surfaces: `cost_lifo` on every trip in `recent_trips`, summed per journey in `recent_journeys` and the journey summary (as `SUM`, not `COALESCE(...,0)` — a journey the replay never priced must read null, not free), and one new sensor, `last_trip_cost_lifo`. Deliberately no `current_trip_` counterpart: the figure only exists once a closed trip has been replayed against the lots, so a live one would be a permanently-empty entity.

`cost` and `cost_basis_per_kwh` are untouched. This is a second opinion alongside `cost_at_avg_tariff`, not a return to the FIFO-slice model v0.8.8 removed — that model was the *primary* price and made a cheap charge create a slice other energy had to wait behind. Dry-run against the real 60-trip history before release: every row with energy got a figure, none left null.

## v0.8.21 — 2026-08-22
**Fix — a charge that ended as a trip began was reported as charging neither before it nor during it, and the heal then destroyed the trip's SoC anchor.** The v0.8.17 mutex has the charge-off handler open the trip, so both stamps come out of the same event cascade. On the real 2026-08-17 rows they landed 177 **microseconds** apart: charge id53 ended at `14:21:56.961478`, trip id352 started at `14:21:56.961301`. One instant, recorded twice — and compared exactly, `ended_at <= trip_start` fails, so the session was not reported as having charged before the trip.

That NULL is not cosmetic. `heal_history`'s "provably impossible SoC rise while parked" rule reads NULL in both buckets as "nothing charged in the gap", concluded a 66 % start could not follow a 64 % end, and re-anchored `soc_start` down to 64 — erasing the charge's three points and the energy they stood for.

A 60 s tolerance now settles the boundary, applied in the two places it belongs:

- The **`before`** window carries it on its upper bound, so a charge ending as the trip opens is reported there instead of vanishing.
- The **straddling** branch of the `during` apportion skips a charge that started before the window and finished within the tolerance of its opening. Such a session delivered nothing inside, and the SoC apportion cannot see that on its own: it trusts the trip's `soc_start`, so a row whose anchor had already been re-anchored got credited energy from a session that had stopped before it moved. On id352 that was a phantom 1.17 kWh.

The tolerance is *not* a blanket window filter. A first attempt excluded any charge ending near the boundary from `during` outright, and that dropped a genuine mid-trip top-up whose timeline is compressed into the same few seconds as the trip open — caught by the v0.8.17 regression test. A charge that *started* inside the window still counts in full, unconditionally.

`heal_history` keeps its own copy of this logic, since it runs inside one executor job over an already-loaded charge list, so both guards are applied there too.

**Fix — the heal left behind an `energy_source` its own output contradicted.** When nothing can be recomputed, whatever is stored survives — including a label this run has just disproved. `soc_plus_charge` asserts "my energy includes a session delivered inside my window"; with `kwh_charged_during` NULL that is provably false, and it presents a stranded number as a derivation nobody can reproduce. Real case: id352 carried 2.58 kWh from a v0.8.17 run that credited a whole session, kept through two later runs that both concluded the session contributed nothing.

The value stays — cost and the lifetime kWh aggregates need a number, the same call v0.8.15 and v0.8.17 made when suppressing a consumption figure. Only the claim is dropped, to NULL ("no longer known how this was derived", already a legal state), with `low_confidence` set and a new `stale_energy_source_cleared` counter in the heal's report.

**Fix — `async_charges_in_window` silently returned zero for every UTC-bounded caller.** Rows are written as local ISO (`_iso_local`) and the query compares `ended_at` as TEXT, so bounds passed as `.isoformat()` from a UTC caller — the recovery sweep takes its stamps straight from the recorder — compared `'…+00:00'` against `'…+02:00'` rows and matched nothing. Same defect v0.8.17 fixed in `_trip_overlaps`, never applied here. Verified by a test that runs the identical window with local and UTC bounds and requires both to find the charge; the UTC form returned 0.0 kWh before.

**Dry-run against the real 60-trip / 60-charge history before release.** Counters: `charge_attribution_fixed: 1`, `soc_start_reanchored: 0`, `stale_energy_source_cleared: 1`, `consumption_suppressed: 0`. Exactly one non-rounding row moves — id352, to `kwh_charged_before = 1.76`, `kwh_charged_during` still NULL, `energy_source` cleared, `soc_start` untouched. The remaining 12 changed rows are pre-existing `0.82 → 0.83` rounding churn unrelated to these fixes; total trip energy moves +0.12 kWh across all 60.

This prevents recurrence rather than repairing the past: id352's `soc_start` was already re-anchored to 64 by an earlier run, and no heal can tell that 66 was the true value, so that row still needs `set_trip` to restore it.

## v0.8.20 — 2026-08-22
**Fix — ABRP telemetry rejected by the server was counted as delivered.** The Iternio Telemetry API uses HTTP status codes only for serious errors — a bad API key, a malformed call. A *rejected sample* comes back as HTTP 200 with `{"status": "error", "errors": [...]}` in the body, most commonly because `car_model` is not a slug ABRP recognises (it is a free-text field, so a typo is easy and nothing validates it). The client stopped at the status code and never read the body, so every rejected push was recorded as a success: the failure counter reset, the backoff never engaged, `last_sent_at` kept advancing, and the switch reported a healthy connection while ABRP was storing nothing.

The body is now parsed. A positively-read non-ok status fails the send and feeds the same backoff as a transport error, and ABRP's own reason is logged at warning level and published as a `last_error` attribute on `switch.abrp_push` (None when the last push was accepted). A 200 whose body cannot be parsed — proxy stripped it, empty response — still counts as delivered: only a status we actually read is allowed to turn a working push into a failure.

**Fix — the two 30-day energy roll-ups were excluded from long-term statistics.** `regen_30d` and `discharge_30_days` registered `state_class: measurement` alongside `device_class: energy`, a pair HA rejects outright ("impossible considering device class; expected None or one of total, total_increasing"), so neither sensor ever produced a statistic.

The `measurement` came from a deliberate downgrade: unlike today/week/month/year, "30d" is a rolling lookback whose sum legitimately *drops* as old high-value days age out of the window, and `total_increasing` reads every such decrease as a counter reset. The downgrade fixed that and broke the device-class pairing instead. `total` is the state class that does both — it derives statistics from signed deltas, so a decrease is just a negative delta, and it is valid on an energy sensor. Nothing else changes; the calendar periods keep `total_increasing`.

A new test walks every period x key combination the roll-ups can be built with and asserts the pair against `DEVICE_CLASS_STATE_CLASSES`, HA's own validity table, so a future state-class change picked for its recorder behaviour cannot silently break the pairing again.

## v0.8.19 — 2026-08-22
**Fix — a home charge could close with no AC-side energy even with a healthy wallbox.** The AC integration is fed by state changes on `evse_power_sensor` and accumulates into the *open* charge session. With a cloud-polled `charge_sensor` that session doesn't always exist while the wallbox is delivering: it opens late, or the session is reconstructed after the fact, and every sample that arrived in between had nowhere to go. Those charges were written with `evse_energy_kwh` NULL, and with it no `charging_efficiency_pct` — reviewing a real 60-charge history, 19 of the 39 home charges had no AC side, all of them from before the v0.8.x charge work.

The live integral is no longer the only chance to measure it. When a home charge closes — or a continuation pulse merges into one — with nothing accumulated, the session window is replayed from the recorder 30 s later: the same integration the manual `backfill_charge_evse` service performs, masked by `charge_sensor` so the wallbox's standby draw between pulses isn't counted as delivery. The delay lets the recorder commit the tail of the session rather than racing it.

Scope is deliberately narrow: it only runs with an `evse_power_sensor` configured and at a home (or secondary-home) location, since an away DCFC has no wallbox samples to find and its AC side comes from the operator's invoice instead. A replay still queued when the entry reloads is cancelled, so it can't write through a stopped coordinator.

## v0.8.18 — 2026-08-21
**Fix — a charge that was already running when a trip opened had its whole session billed to that trip.** Regression in v0.8.17's mid-trip-charge correction, caught by running `heal_history` against real data: the lifetime driven/charged ratio went the wrong way, from 1.07 to 1.22, and two rows came out physically impossible — a 74 km leg credited all 66.83 kWh of the session it opened in the middle of (78 kWh/100km), and a 3 km hop credited a session that had finished as it set off (86 kWh/100km).

`kwh_charged_during` selects sessions that *ended* inside the trip's window, which is the right set — but the quantity was the whole session, and only the part delivered after the window opened belongs to it. SoC is the meter that says how much:

    kwh x (soc_end - trip_soc_start) / (soc_end - soc_start)

For the reported leg that is 66.83 x (96-67)/(96-15) = 23.92 kWh in the window, so 23.92 delivered against 11 points more stored at the end leaves 14.84 kWh really used — 20.1 kWh/100km over 74 km, which is what the car actually does. A session wholly inside the window still counts in full. One that straddles the opening without the SoC readings needed to apportion it contributes nothing: a guess here lands straight in the trip's consumption and its cost.

Applied to the live close, the synthetic close and `heal_history` alike, so re-running the heal corrects rows an earlier run got wrong.

## v0.8.17 — 2026-08-20
**Fix — consecutive charging sessions inside a 2-hour window were discarded, not recorded.** Reported case: a four-day road trip lost roughly 150 kWh across three sessions. The car's own sensors saw every one of them (`charge_in_progress` went to `charging`, `current_charge_energy` integrated up to 42.9 and 50.3 kWh), yet no row was ever written.

The cause was the time-based dedup in `_async_close_auto_charge`: if the previous charge had started less than 2 h earlier and continuity couldn't be proven from the plug sensor, the close path simply returned — throwing the session's kWh away. That heuristic assumes charges are hours apart, which holds for overnight home charging and fails completely on a motorway: DCFC stops sit 40-90 min apart, so on a long drive every stop after the first was deleted. Public fast chargers also expose no plug telemetry, so the `can_merge` escape hatch (which requires `plug=on` plus proven continuity) can never fire there.

Discarding is now reserved for the one case the gate was actually written for — the user calling `log_charge` manually for the same session the auto-detect just watched, which shows up as a previous charge whose window *overlaps* this pulse (or whose window is unknown, since `log_charge` leaves `started_at` NULL). Everything else keeps its energy:

- **SoC dropped since the previous charge ended** → the car was driven in between, so this is unambiguously a separate session. Insert it as its own row.
- **Otherwise** → it's a continuation pulse that couldn't be proven with the plug sensor (a cloud dropout mid-session, or `charging` momentarily reported off while 44 kW was still flowing). Extend the previous row instead of vetoing it.

The `≥2 %` SoC-delta gate immediately above already filters the battery-balancing blips this dedup was feared to let through, so nothing is loosened by not discarding. Fragmentation is recoverable through `set_charge`; silently deleted kWh is not.

**Fix — turning the car on during a charge wrecked both records.** Same road trip, same afternoon: the driver sat in the car at a fast charger (screens on, planning the next leg) while the cable was still delivering 44 kW. The v0.5.16 mutual-exclusion rule reacted to `vehicle_on` by force-closing the charge and opening a trip immediately, so the session was cut 27 minutes early — its remaining SoC 67 → 96 % landed nowhere, and the truncated row was then deleted by the dedup above — while the trip anchored `soc_start` to 67 % and closed at 78 %, i.e. `soc_used = -11 %`, which pushed its energy through to the distance-based estimate.

Nothing takes over from a live session any more: while the configured charge sensor still reads charging, the trip open is deferred, and the charge-off handler opens it afterwards so `_resolve_soc_start` can anchor to the session's real end SoC. The odometer is the escape hatch — ≥1 km covered since the session opened means the sensor is stuck 'on' and the trip opens regardless. The metric-arrival path (a battery or odometer tick with `vehicle_on` up) was a second door into the same bug and never checked for an open charge at all; it now shares the guard.

**Fix — a trip whose SoC rose reported the rolling average as if it were a measurement.** When SoC climbs across a drive and no charge falls inside the trip's window, the two measurements contradict each other — a long mountain descent where regen outweighs draw, a charge the cloud never reported, or (the reported case) SoC samples landing late after driving out of coverage. `energy_kwh` correctly falls through to `distance × rolling-average kWh/100km`, but the consumption derived from it was then published as a real figure, restating the fleet average as a measurement and feeding the very average it was copied from. The raw energy estimate stays (cost and kWh aggregates still need a number); the per-100 km value is now suppressed, as v0.8.15 already does for parked disconnect-orphans.

**New action — `heal_history`.** Every fix above applies to rows written from now on; the rows already in the database keep whatever they were written with. This action re-derives them from the data as it stands today, and it is deliberately narrow about what it will touch:

- **Charge attribution is recomputed.** `kwh_charged_before` / `kwh_charged_during` are calculated once, at trip close, so a charge inserted afterwards — by `log_charge`, `set_charge`, or a recovery sweep — is invisible to every trip around it. On the reporting car, two sessions worth ~130 kWh were recovered *after* the trips they belong to had closed, so those trips still showed a SoC that appeared to rise out of nowhere and an energy figure that badly understated what was burned. Recomputing the attribution also reapplies the mid-trip-charge energy correction.
- **Physically impossible SoC rises are re-anchored.** A `soc_start` above the previous trip's `soc_end` with no charge in between cannot happen while parked — standby drain only removes charge. Those rows are re-anchored to the last actually-measured value.
- **Nothing is modelled.** A rise a real charge explains is left alone. A trip whose energy came from an independent measurement (`power_integration`, `vehicle`) keeps that energy; only its SoC bookkeeping is made consistent. Where the corrected SoC delta is negative with nothing charged, the per-km figure is suppressed rather than invented. Parked standby drain is *not* redistributed onto trips — it belongs to no trip, which is the whole point.

It reports what it changed (trips seen, attribution fixed, re-anchored, energy recomputed, consumption suppressed), re-costs every trip afterwards, and is safe to run more than once.

**Fix — averages of ratios, and windows that read the wrong clock.** A sweep through every mean and period aggregate in the package:

- **Consumption averages were means of ratios.** `AVG(consumption_kwh_100km)` in five places (30-day metrics, per-driver stats, and the season / time-of-day / temperature buckets) gave a 2 km cold start the same weight as a 200 km motorway run: 2 km at 40 kWh/100km plus 200 km at 15 came out as 27.5 instead of 15.25. Three of those queries already had `SUM(distance_km)` and `SUM(energy_kwh)` selected two lines above and discarded them. All five are now distance-weighted, and over rows that carry *both* columns — a plain `SUM(e)/SUM(d)` still lets a NULL-energy row put its kilometres in the denominator with nothing in the numerator.
- **`avg_trip_speed_30_days` was a mean of means.** 29 city trips of 5 km/20 min plus one 400 km motorway run reported 18.2 km/h against a true 39.9. It is now total distance over total time — which also makes the three 30-day sensors agree with each other for the first time: on the reporting car the published 49.0 km/h could not be reconciled with its own 29.04 km and 47.63 min (36.6 km/h).
- **Season and time-of-day buckets read UTC, not the local clock.** SQLite's `strftime()` converts an offset-bearing ISO string to UTC first, so a 07:30 local summer commute was filed under *night* (05 UTC) and a trip started 1 March 00:30 local counted as *winter* — while naive legacy rows were not shifted at all, so the same clock time landed in different buckets depending on which write path created the row. Both now read the stored local text directly with `substr()`, the approach the weekly/monthly rollups already used.
- **Two "recent N" windows still ordered by insertion id** (`avg_charging_efficiency`, the calibration-factor median) — the same class of bug fixed for `recent_charges`/`recent_trips` in v0.8.13. Importing historical charges gives them ids above every live row, so those windows could be computed entirely from the import.
- **Two SoH windows built their cutoff from `datetime.now()`** — naive, in the *host's* timezone, then compared as text against rows that carry an offset. Now `dt_util.now()`, like every other window in the file.
- **`is_dcfc IS NULL` vanished from both price splits.** `CASE WHEN is_dcfc = 1 / = 0` matches neither for NULL, which `log_charge` leaves whenever it runs without a start time or the session is under three minutes — so `ac_kwh + dc_kwh < kwh` and the AC average silently excluded those sessions. NULL now counts as AC, the convention the lifetime DCFC ratio already documented.
- **`regen_ratio` and the 30-day consumption used mismatched subsets** — a row with `discharge_kwh` but NULL `regen_kwh` inflated only the denominator. Both sides are now paired.
- **The tracked-sensor rolling mean was sample-count-weighted.** The recorder emits one row per state *change*, so a value held for 20 hours counted once while a value reported every 30 seconds counted thousands of times: 0 kW for 29 days against 50 kW for 20 h of charging reported a 30-day average of 49.98 kW instead of 1.39. It is now time-weighted, and a `covered_hours` attribute exposes how much of the advertised window the recorder could actually reach.
- **`degradation_kwh_per_year` annualised any two snapshots.** Two readings 10 days apart differing by 1.1 kWh — inside the noise of a median-of-30-charges estimate — extrapolated to −40.2 kWh/year, i.e. the pack emptying in two years. It now needs at least six snapshots spanning three months and fits the whole series by least squares instead of trusting two endpoints.
- **The temperature-bucket fallback threw away its own weighting.** Each bucket is carefully distance-weighted in storage, and the sensor's "temp source asleep" fallback then averaged the bucket *labels* equally: one 6 km January trip at 22.0 against 4 000 km of summer at 14.5 gave 18.25 instead of 14.51 — firing in exactly the scenario the fallback exists for.
- A legitimate aggregate of `0.0` is no longer coerced to "unknown" (`if value` where `if value is not None` was meant), and the temperature bucket's `sample_count` now counts the rows that actually contributed rather than the rows fetched.

**Rework — the cost pool is now anchored to the physical battery, and the money paid is the input.** The weighted-average-cost pool introduced in v0.8.8 was right in principle — the battery is one blended mixture, and what stays in it is energy no trip pays for — but its quantity was a running total that never touched reality, and its price came from the wrong side of the meter. Measured on the reporting car: trips had consumed 73.78 kWh *more* than had ever been charged (a ratio invariant under any capacity value, so a counting error, not a calibration one), while nothing stopped the pool from holding more kWh than the pack can physically store.

- **Quantity now comes from SoC, price from the pool.** A charge's `soc_start` re-anchors the pool before its energy blends in, and `soc_end` re-anchors it after. Energy that leaves the battery without being a trip (standby drain, preconditioning, parked climate) no longer lingers as cheap inventory pricing tomorrow's driving, and the charge the battery already held on day one no longer has to be invented. Concretely: two 45 kWh charges either side of a pack that drops back to 5 % used to blend to €0.289/kWh by crediting 45 kWh that had already left; the answer is €0.46, because only what was physically in the pack can be drawn from.
- **The invoice is the input; €/kWh is derived from it.** The pool priced battery-side `kwh` at `price_per_kwh`, but a public tariff bills the charger-side figure, 5-15 % higher. On this car, 402.88 kWh metered against 365.17 kWh stored — 9.4 % of the energy genuinely paid for reached no trip. The replay now prices at `total_cost / kwh` whenever a total is recorded.
- **The queue is ordered by when charges actually ended.** It was built on `started_at` and consumed on `ended_at`, which is head-of-line blocking: a session that starts early and ends late stalled every charge that ended in between, and `started_at IS NULL` (the documented shape of a bare `log_charge`) sorted *first*, so one such row could stall the rest of the replay. Both charges and trips are now sorted by their parsed instant rather than by ISO text, which also fixes ordering across rows with different UTC offsets — including the hour every autumn where text ordering inverts. Naive legacy rows are read as local time, not UTC, so they stop jumping 1-2 h into the future.
- **A shortfall is priced at the blend already seen, not at the home tariff.** A shortfall means the car consumed more than we tracked charging, so the best estimate of what that energy cost is what this car's energy has cost so far. The configured tariff was the cheapest number available and was applied precisely when the driver was on a motorway paying DC rates. It stays causal — only charges that already happened count, so a charge logged tomorrow can never re-price today's driving.
- **Corrections no longer destroy recorded money.** Patching a charge's kWh used to recompute `total_cost = kwh × price`, rewriting a €20.00 invoice to €19.00; it now re-derives the €/kWh from the total instead. Merging a pulse into a session used to re-derive the whole row's cost, multiplying any fixed connection fee (€6.00 on 10 kWh became €12.00 on 20 kWh instead of €11.00); it now adds the pulse's own cost.
- **Every path that mutates a pool input replays it.** `set_charge`, `delete_last_charge` and the merge path all changed the pool's inputs and none triggered a recompute, so trip costs stayed wrong until an unrelated trip closed or Home Assistant restarted.

Step 1 of the replay also stopped fabricating energy for `orphan_odo_only` rows, whose energy is NULL *on purpose* (kilometres appeared but SoC never dropped — a catch-up odometer reading whose distance was already consumed under another row); the invented withdrawals drained the pool and pushed later real trips onto the fallback price. Rows it does fill are now stamped `energy_source = 'estimated'`, so the consumption baseline — which excludes estimates precisely to avoid feeding them back — actually excludes them.

**Fix — parked standby drain was billed to the next drive.** The post-charge SoC anchor (branch (a) of `_resolve_soc_start`) exists to defeat integer-step SoC staleness, but it can only ever *raise* `soc_start`, so every percent it adds becomes trip energy. With a 2 % budget over a 12-hour window, a car that charged to 80 % overnight, sat losing 2 % to standby, and then drove 5 km recorded `soc_used = 3 %` — 2.475 kWh, i.e. 49.5 kWh/100km — when the drive itself used about a third of that. The other 1.65 kWh was parked drain, which belongs to no trip. The anchor is now bounded: it may absorb at most one integer SoC step (the sensor's own resolution, the only part attributable to staleness), and past a 3-hour park it stops correcting at all, because by then even one step is more likely drain than a stale reading. The same two-step allowance on the short-park snap is down to one.

**Fix — the gross power integral over-counted every zero crossing by up to 2×.** `energy_from_power` integrated a trapezoid over |P|, but |P| has a kink at zero that a straight line cuts across; the regen term next to it already used exact sub-areas. A +40 kW sample followed by −30 kW eight minutes later integrated 4.666 kWh where the true area is 2.381 — a factor of 1.96. That over-count fed `discharge_kwh` and the month/year/30-day discharge totals directly, biased `regen_ratio` low through its inflated denominator, and became the trip's own `energy_kwh` whenever the SoC delta was under one integer step. Crossing segments now use the same exact sub-area maths as regen; same-sign segments are unchanged, since a trapezoid is already exact there.

**Fix — fictional regen could be injected without limit.** The per-trapezoid 5 kWh ceiling guarded only the gross term. Two consecutive −100 kW samples 20 minutes apart — the shape of a cloud back-fill after a dropout — added 33.3 kWh of regen to an 82.5 kWh pack, violating the `gross = discharge + regen` invariant and landing permanently in the year totals. Both terms now share the ceiling.

**Fix — a charge inside a trip's window was measured and then ignored.** `kwh_charged_during` has been recorded since v0.5.27 and used for nothing but display, while `energy_kwh` came from the raw SoC delta — which that charge has already partly refilled. A 120 km leg from 60 % to 50 % with a 10 kWh top-up inside recorded 8.25 kWh (6.9 kWh/100km, impossible for the car) instead of 18.25 kWh. Conservation gives `consumed = charged − Δstored`, and the SoC delta already carries the sign of `Δstored`, so one expression covers both a net drop and a net gain; the row is tagged `energy_source = 'soc_plus_charge'`. Power-integration and vehicle-native rows are excluded — they measured discharge directly, so adding the charge would double-count it.

**Also folded in:** the two corrections above, plus the v0.8.17 SoC-rose suppression, now run on the **synthetic** close path as well, not only the live one — on a cloud-polled car nearly every trip is synthetic, so most rows were missing both. `_iso_local` now covers the trip and charge INSERT paths, not just the two correction services, so an auto-detected charge no longer stores `started_at` as UTC (from `last_changed`) alongside a local `ended_at` in the same row. And the capacity heal no longer resurrects a deliberately-suppressed `consumption_kwh_100km`, nor rescales `soc_plus_charge` rows whose energy already folds in a metered charge.

**Fix — rows written with a different UTC offset sorted out of order.** Trips and charges are stored as ISO text and every "most recent" query orders by `ended_at` *as a string*, which only matches chronological order when every row carries the same offset. Live inserts use `dt_util.now()` (local), but the recovery sweep takes its stamps straight from the recorder (UTC) and the correction services take whatever the caller passed — a naive datetime serialises with no offset at all and sorts below everything. Reported case: a recovered 13:55 leg was stamped `11:55:50+00:00` and listed *ahead* of the 12:15 trip that really preceded it. Both correction paths and the recovery insert now normalise to local time; the instant is unchanged, only its representation.

**Fix — `set_trip` left corrected rows contradicting themselves.** The service is a raw column UPDATE, so patching `soc_start` left `soc_used_pct` still quoting the old delta, and patching `energy_kwh` left the kWh/100km built on the old figure — the row disagreed with the very columns those numbers are computed from, and the existing recompute pass only touches cost. Both fields are derived from the row alone and are now recomputed after any patch that changes their inputs.

## v0.8.16 — 2026-08-17
**Feature — invoice kWh for away charges.** `set_last_charge_price` now accepts an optional `evse_energy_kwh`, so a public charge (no EVSE sensor) can get the operator's invoice kWh entered after the fact and have `charging_efficiency_pct` computed from it (`kwh / evse_energy_kwh × 100`) — the same field/formula home charges already fill live from the EVSE integral. Home charges don't need this: the EVSE sensor already gives the exact figure.

## v0.8.15 — 2026-08-08
**Fix — disconnect-orphan trips could report physically-impossible consumption (e.g. 5 km / 19 h read as ~50 kWh/100km).** Reported case: several short drives below `min_trip_distance_km` got correctly discarded through the day (each one individually not worth logging), but discarding a short trip never refreshed the internal odometer/SoC checkpoint used to detect real connectivity gaps — it stayed pinned to the previous night's last *persisted* trip. A routine HA restart then re-seeded that checkpoint from the same stale value, and the very next update folded the entire ~19 h span — a handful of real short drives plus a full day of parked/standby drain — into one `orphan_disconnect` row, dividing the day's whole SoC drop by only the day's few actual kilometres.

Two changes: (1) `_async_close_trip` now advances the checkpoint to the discarded trip's own observed end-state instead of leaving it frozen, so a run of correctly-discarded short trips no longer erodes gap-detection accuracy while the coordinator keeps running; (2) `_async_insert_disconnect_orphan` now checks the reconstructed window's own implied average speed, and suppresses `consumption_kwh_100km` (keeping the raw `energy_kwh`, which is still a real quantity) whenever that speed is under 3 km/h — a reliable sign the window was overwhelmingly parked, not driven, so a per-km consumption figure can't mean anything. This narrows but doesn't fully close the gap: a restart occurring while the checkpoint is stale can still misattribute a mix of real driving and standby drain into one row — the suppression in (2) is what stops that row from ever showing a nonsensical number.

## v0.8.14 — 2026-08-05
**Improvement — coverage-aware charge energy, and calibration no longer partly circular.** Reported case: a public charge cost €5.23 but was recorded as 17.43 kWh (≈€0.30/kWh) — a number that didn't reconcile, and turned out to trace back to the SoC-delta estimate (`Δ% × nominal_capacity`) being trusted more than it should on an away-from-home top-up, where ±1 % SoC quantization is a big share of the total.

The power-integration measurement (∫|P|·dt over the vehicle's own power sensor during the session, added in v0.5.89) already existed as a cross-check, but was only accepted within a fixed ±30 % of the SoC-delta guess — meaning a *correct* reading that legitimately disagreed with a *wrong* guess got thrown out, while a sparse, coincidentally-close reading could sail through. A fast public DCFC session is exactly the case most likely to have sparse cloud-reported power samples (the vehicle's cloud API may only relay the initial power spike before going quiet for the rest of a 20-40 min session), so numerical closeness to the SoC guess was never a reliable signal on its own.

Now the power-integration is graded on actual coverage first — at least 3 contributing samples spanning ≥50 % of the session — before its proximity to the SoC-delta number is even checked. With good coverage, it's accepted within a wider ±40 % band (it's earned the benefit of the doubt); without it, the SoC math is used regardless of how plausible the sparse reading looks. Every charge now records which method won as `energy_source` (`power_integration` / `soc_delta` / `manual` after a hand correction), visible as an attribute on `recent_charges`/`last_charge`.

This also breaks a circularity in the capacity self-calibration: the median `kwh / ΔSoC × 100` used to be computed mostly from the very SoC-delta guess it's meant to correct. It now prefers `energy_source = 'power_integration'` charges once ≥5 exist, falling back to the old (all-charges) behaviour until then so existing users don't lose calibration on upgrade.

Also folded in while touching this code: three more `charges` queries (`set_last_charge_price`, `delete_last_charge`, the auto-detect merge lookup) still ordered by insertion id instead of `ended_at` — the same class of bug fixed for `recent_charges`/`recent_trips` in v0.8.13, here affecting which row a price correction / deletion / merge would land on if a historical charge had been backfilled.

## v0.8.13 — 2026-08-04
**Fix — `recent_trips`/`recent_charges` ordered by insertion id, not by date.** Both queried `ORDER BY id DESC`, which is only equivalent to "most recent" when every row is inserted in chronological order — true for ordinary live logging, but not for `recover_missing_trips` or `log_manual_trip`, which insert a historical row that gets a fresh (high) autoincrement id despite an old `ended_at`. A recovery sweep covering a multi-day gap could bump genuinely recent live trips completely out of the `recent_trips` window, since the backfilled-but-old rows now ranked "more recent" by id alone. Found while investigating a real report of a trip not showing up: recovering it (correctly) surfaced this second bug, because the recovery scan's own historical inserts then pushed that day's later trips out of view. Both queries now order by `ended_at DESC, id DESC` — matching the convention `async_get_last` already used — so backfilled rows sort into their real chronological position instead of jumping to the front.

Also (v0.8.12, released without a manifest version bump — the version jump from 0.8.11 to 0.8.13 covers both):
**Fix — `regen_kwh`/`discharge_kwh` 30d sensors flagged as "not strictly increasing".** The `30d` period is a rolling lookback (`now - 30 days`), not a calendar-boundary reset like `today`/`week`/`month`/`year` — the sum can legitimately drop as a high-value day ages out of the window. Both sensors were declared `state_class: total_increasing`, which only tolerates an increase or a reset-to-~0; every ordinary rolling decrease got logged by the recorder as an invalid state. `AggregateSensor` now forces `measurement` for any `total_increasing` key at the `30d` period.

## v0.8.12 — 2026-08-04
**Fix — `regen_kwh`/`discharge_kwh` 30d sensors flagged as "not strictly increasing".** The `30d` period is a rolling lookback (`now - 30 days`), not a calendar-boundary reset like `today`/`week`/`month`/`year` — the sum can legitimately drop as a high-value day ages out of the window. Both sensors were declared `state_class: total_increasing`, which only tolerates an increase or a reset-to-~0; every ordinary rolling decrease got logged by the recorder as an invalid state. `AggregateSensor` now forces `measurement` for any `total_increasing` key at the `30d` period (`avg_consumption_kwh_100km` and `regen_ratio` were already `measurement` there and are unaffected). `ChargesAggregateSensor` has no `total_increasing` key paired with `30d` today, so it needed no change — but the same rule would apply if one is added later.

## v0.8.11 — 2026-08-03
**Fix (#12) — auto-detect could adopt the logger's own sensors.** When the vehicle integration's device slug collides with the logger's own device slug (e.g. both named "relampago"), the prefix-walk used by the last-trip-energy/distance and exterior-temp auto-detects could land on `sensor.<prefix>_last_trip_energy` — the logger's OWN output sensor — and adopt it as a "vehicle-native" source. That healed trips from data the logger just wrote itself, mislabelling SoC-derived estimates as vehicle-native with a confidence band they hadn't earned, on every reload. Both auto-detects now skip any candidate entity registered under this integration's own platform.

Also: the README's `weather_entity` documentation was stale — that field has been dead (logged-and-ignored) since v0.5.68. Replaced with accurate docs for the **Outside temp sensor** field, which is what actually drives the by-temperature bucket and the SoH model's climate factor; season/time-of-day buckets need no temperature source at all.

**Fix (#11) — OpenTopoData elevation providers always returned HTTP 400.** `fetch_elevations()` sent every provider open-elevation's request shape (a list of `{"latitude", "longitude"}` objects). OpenTopoData — `opentopodata-eudem`, `opentopodata-srtm`, and a self-hosted `custom` instance — instead wants a single pipe-delimited `"lat,lon|lat,lon|..."` string; sending it the wrong shape got a 400 on every request, silently (logged at INFO), leaving `elevation_gain_m`/`_loss_m`/`_variance_m2` permanently null with no error surfaced anywhere. Added a per-provider request encoder, and 4xx responses now log at WARNING instead of INFO so a misconfiguration doesn't go unnoticed for a month. Also documented the elevation-provider config field in the README — it wasn't there before, so leaving it unset (the default) read as a broken feature rather than an unconfigured one.

## v0.8.10 — 2026-08-02
**Feature — secondary home locations.** A second house, holiday home, etc. can now close/open a journey exactly like the primary `home_zone`. Two new optional config fields: `secondary_home_zones` (pick any number of existing `zone.*` entities) and `secondary_home_coords` (free-typed `lat,lon[,radius_m][,label]`, one per line, for a place you don't want a permanent HA zone for). Wired into every journey-membership decision point (live trip close, both orphan-reconstruction paths, manual trip logging, late-zone-arrival amendment) and the storage-level open-journey/orphan-absorption queries.

## v0.8.9 — 2026-07-31
**Fix.** `CONF_TRACKED_SENSORS`' entity-id slug generation only stripped the device title as an exact prefix match. A car-integration source entity puts its own prefix in front of the title (e.g. BYD's `sensor.byd_sealion_7_energy_consumption`), so the check never matched, producing doubled ids like `sensor.sealion_7_byd_sealion_7_energy_consumption_avg_30d`. Now searches for the device title as a substring wherever it falls.

Only prevents the doubling on *newly added* tracked sensors — already-registered doubled entities need manual removal (Settings → Devices & services → Entities) if unwanted.

## v0.8.8 — 2026-07-31
**Fix — trip cost accounting model changed.** Trip cost was computed via a FIFO queue: each charge was a discrete "lot" fully drained before the next was touched, so a one-off free/cheap charge created a stretch of €0-cost trips that then jumped abruptly back to full price the instant it ran out. Replaced with weighted-average-cost (WAC): every charge blends its (kWh, price) into one running battery-average €/kWh, and every trip draws from that pool at whatever the blended average is at the time. A free charge now smoothly lowers cost across the driving it covers — no discrete lot, no abrupt jump back.

The existing startup heal (`async_recompute_trip_costs_from_charges`, runs once per integration load) retroactively re-costs the whole trip history under the new model automatically — no manual service call needed after updating.

## v0.8.7 — 2026-07-30
**Feature — more ABRP telemetry fields.** ABRP's API also accepts cabin temperature, HVAC setpoint, and tire pressures (plus elevation/voltage/current/battery-temp, for which this integration has no generic source). New optional config fields: `cabin_temp_sensor`, `hvac_setpoint_sensor`, `tire_pressure_{fl,fr,rl,rr}_sensor` — same ABRP-only pattern as the existing `range_sensor`/`heading_sensor`. Tire pressure sensors are read in whatever unit the source reports (bar/psi/kPa/hPa) and converted to kPa.

## v0.8.6 — 2026-07-30
**Fix.** ABRP's `soh` field was already being sent, but sourced from `expected_battery_soh` — an age/mileage/climate *model* meant only for the "ahead/on-track/behind" diagnostic comparison, not a measurement. Now sends the calibrated `battery_soh` (grounded in this car's own observed charge behaviour) instead.

## v0.8.5 — 2026-07-30
**Fix.** Two trip-reconstruction paths — `_async_insert_orphan_trip` and `_async_insert_disconnect_orphan`, both used when `vehicle_on` never reports a real on/off transition (cloud outage, HA restart) — hardcoded `destination=None` and never touched journey membership or `last_trip`. A gap that actually ended with the car back home rendered as "Outside known zones" instead of closing its journey, and every `last_trip_*` sensor stayed stuck on the trip *before* the gap until the next ordinary live trip closed. Both paths now resolve the real end location, apply the same home-arrival journey rule the live-close path uses, and correctly adopt the result as `last_trip`.

## v0.8.4 — 2026-07-28
**Feature.** New `fix_speed_stats` maintenance service: clears `avg_speed_kmh` on any historical trip where it exceeds `max_speed_kmh` by more than 5% (physically impossible — usually a symptom of the stale-odometer-anchor bug fixed in v0.8.3, for trips logged before that fix existed).

## v0.8.3 — 2026-07-28
**Fix.** A short `vehicle_on` blip (e.g. opening the garage) that starts and ends faster than the cloud delivers a fresh odometer sample gets discarded as noise — but the stale odometer reading it left behind used to silently anchor the *next* real trip, inflating that trip's distance with the discarded blip's km (one real case produced an implied 382 km/h average speed). The live trip-opener now requires the odometer reading to be fresher than 90s before trusting it. Also added: an `avg_speed_kmh > max_speed_kmh × 1.05` guard at trip-close time, dropping the average speed rather than persisting an impossible value.

Also folds in **v0.8.2** (previously merged, never tagged): optional `energy_price_entity` for live dynamic-tariff cost tracking (Octopus/Nordpool/PVPC/…), falling back to the fixed home tariff when unset or non-numeric.

## v0.8.1 — 2026-07-24
**Fixes.**
- ABRP push ignored `power_sign_inverted` and re-read the raw power sensor directly, cancelling out `build_tlm`'s own sign negation — sign-inverted sources sent discharge as negative and charging as positive to ABRP, backwards from its +discharge/-charge convention.
- Orphan trips reconstructed after a gap (e.g. a short HA restart mid-drive) could inherit the full elapsed time — including parked/offline time — as their duration, producing implausibly low average speeds. Now capped to the longest duration consistent with a 15 km/h floor.

**Addition.** ABRP payload gained `soe` (state of energy, kWh), derived from `soc × capacity`.

## v0.8.0 — 2026-07-05
**Feature.** Richer ABRP telemetry: `est_battery_range` (was being dropped), `capacity`, modelled `soh`, `kwh_charged`, and `heading` (via two new optional sensors, `range_sensor`/`heading_sensor`). **Config cleanup**: DCFC threshold exposed in the options flow, ABRP push interval minimum raised to 30s, energy price unit clarified, currency dropdown, home-zone default aligned with HA's own `zone.home`.

---

## Configuration quick-reference for anything added since v0.8.0

All of the following are **optional**, ABRP-telemetry-only (no effect on trip logging), added via Settings → Devices & services → EV Trip Logger → Configure:

| Field | Added | ABRP field it feeds |
|---|---|---|
| `range_sensor` | v0.8.0 | `est_battery_range` |
| `heading_sensor` | v0.8.0 | `heading` |
| `energy_price_entity` | v0.8.2 | *(trip/charge cost, not ABRP)* |
| `cabin_temp_sensor` | v0.8.7 | `cabin_temp` |
| `hvac_setpoint_sensor` | v0.8.7 | `hvac_setpoint` |
| `tire_pressure_fl_sensor` / `_fr_` / `_rl_` / `_rr_` | v0.8.7 | `tire_pressure_fl/fr/rl/rr` |

New maintenance service: `fix_speed_stats` (v0.8.4) — see [Services](README.md#services).

New behaviour to be aware of when reasoning about historical data:
- Trip `cost` / `cost_basis_per_kwh` reflect the weighted-average battery pool (v0.8.8), not a FIFO queue — see [How trip cost is computed](README.md#how-trip-cost-is-computed) in the README.
- ABRP's `soh` field is the calibrated `battery_soh`, not `expected_battery_soh` (v0.8.6).
- `confidence='orphan'` and `confidence='orphan_disconnect'` trips now correctly resolve destination and journey membership (v0.8.5) — trips logged before that release may still show a stale destination / missing journey and won't be retroactively corrected by the code fix alone (use `set_trip` to patch `destination` by hand if it matters for a specific historical trip).
