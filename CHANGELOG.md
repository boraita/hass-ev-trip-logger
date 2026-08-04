# Changelog

Summarised, human-readable history from v0.8.0 onward. Full technical detail for every release (including everything before v0.8.0) lives in [GitHub Releases](https://github.com/boraita/hass-ev-trip-logger/releases) and the commit history.

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
