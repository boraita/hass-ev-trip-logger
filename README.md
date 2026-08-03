<p align="center">
  <img src="assets/brand/logo.png" alt="EV Trip Logger" width="640">
</p>

# EV Trip Logger for Home Assistant

<p>
  <a href="https://github.com/boraita/hass-ev-trip-logger/actions/workflows/validate.yml"><img src="https://github.com/boraita/hass-ev-trip-logger/actions/workflows/validate.yml/badge.svg?branch=main" alt="Validate (hassfest + HACS + tests)"></a>
  <a href="https://github.com/boraita/hass-ev-trip-logger/actions/workflows/release.yml"><img src="https://github.com/boraita/hass-ev-trip-logger/actions/workflows/release.yml/badge.svg" alt="Release"></a>
  <a href="https://github.com/boraita/hass-ev-trip-logger/releases/latest"><img src="https://img.shields.io/github/v/release/boraita/hass-ev-trip-logger?label=version" alt="Latest release"></a>
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-custom-41BDF5.svg" alt="HACS custom repository"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/boraita/hass-ev-trip-logger" alt="License"></a>
</p>

**Health at a glance:** the *Validate* badge runs on every push and every night (hassfest, HACS validation, and the full pytest suite of 108+ tests) — green means the integration installs and its trip/charge logic passes against the latest Home Assistant.

A vehicle-agnostic Home Assistant custom integration that records every drive and charge from the entities your manufacturer integration already exposes, derives accurate consumption / cost / journey aggregates, **calibrates itself to your specific car over time**, and surfaces everything through standard HA sensors so any dashboard can consume the data.

Works with **any cloud-polled EV integration** — BYD, Tesla Fleet, OVMS, Bouncie, native CAN-bus dongles, even a manual setup. You point it at the entities you already have; it does the rest.

> Companion dashboard with ~25 ready-to-use cards: **[hass-ev-trip-dashboard](https://github.com/boraita/hass-ev-trip-dashboard)**.

---

## Screenshots

A few views from the companion [`hass-ev-trip-dashboard`](https://github.com/boraita/hass-ev-trip-dashboard) — every value below comes from sensors / attributes this integration exposes.

| | |
|---|---|
| ![Status, live charging & driving](docs/screenshots/01-status-charging-driving.png) | ![Trips list with search & filter](docs/screenshots/02-trips-list-search.png) |
| Live status: battery curve, plug state, today's journey + charging / driving sparklines. | Recent trips with kWh/100km, score, cost — filter by date / score / max consumption. |
| ![Calendar activity](docs/screenshots/03-activity-calendar.png) | ![Trends & savings vs petrol](docs/screenshots/04-trends-savings.png) |
| Monthly calendar with trips + charges per day. | Savings vs petrol baseline, monthly cost projection, long-trip / avg-trip records. |
| ![Driving patterns + per-driver stats](docs/screenshots/05-patterns-drivers.png) | ![Efficiency analytics](docs/screenshots/06-efficiency-analysis.png) |
| When you drive (hour-of-day, weekday) + per-driver breakdown when a `driver_sensor` is wired. | Consumption by speed / season / time-of-day / temperature, scatter chart, battery health (SoH 100 %). |
| ![Charges history & insights](docs/screenshots/07-charges-history.png) | |
| Per-period kWh charged / spent / driving, charging insights (cheapest / DCFC / avg session), full charge history. | |

The dashboard is **optional** — every metric is a regular HA sensor or sensor-attribute, so you can build your own card or template against them.

---

## Why

Cloud-polled EVs have stale, integer-step SoC, sparse odometer ticks, and unreliable `vehicle_on` transitions. Out of the box that makes:
- consumption per trip off by 1–2 % (or NULL on short trips),
- single drives split into multiple rows,
- overnight charges silently dropped on reload,
- journeys (casa → … → casa) shown as 1-stage fragments,
- addresses stuck as `not_home`,
- score curves that assume a Tesla and unfairly rate a BYD in the Alps as 1/10.

This integration solves all of that with explicit state machines, **per-car self-calibration**, weather correlation, and a battery-health model anchored in real fleet data (Geotab 22,700 EVs, Tesla 2023 Impact Report, BYD warranty, ADAC, Recurrent). The hard work of v0.5.x is documented in [the release notes](https://github.com/boraita/hass-ev-trip-logger/releases).

---

## Highlights (v0.5.49 – v0.5.59)

| Area | What you get |
|---|---|
| **Trip detection** | Live-open retry chain when odometer lags `vehicle_on=on` flanco (BYD cloud poll 6 min off-edge protection). 180 s off-grace so a red-light / pickup stop no longer closes the trip. Synthetic finalize cancelled when ignition returns. |
| **Score calibration** | The 10/10 anchor is the car's own **P5 historical consumption** (over trips ≥ 5 km, ≥ 10 trips). Falls back to 14.5 kWh/100 km — the calibration can only RAISE the bar, never lower it. Configurable battery chemistry. |
| **Energy precision** | Effective battery capacity auto-calibrated from real charges (median of `kwh / ΔSoC × 100` over recent charges ≥ 30 % ΔSoC). Heals all historical SoC-derived trips when calibration shifts. |
| **Seasonal & temperature analytics** | Three bucket sensors: consumption-by-season and by-time-of-day (no extra config needed), plus by-temperature (needs the optional outside temp sensor). |
| **Battery health (SoH)** | Live `battery_soh` (% of declared capacity actually delivered). Plus `expected_battery_soh` modelled from your km / age / chemistry / climate / DCFC habits with constants from Geotab + Tesla + ADAC + NREL + BYD warranty (8 yr / 250 k km, ≥ 70 %). `battery_health_vs_expected` enum tells you if you're ahead/on-track/behind the curve. |
| **Degradation tracking** | `capacity_history` table appends a snapshot whenever the calibrated capacity drifts ≥ 0.5 kWh — long-term degradation curve visible from day one. |

### More recent (v0.8.0 – v0.8.9)

See [CHANGELOG.md](CHANGELOG.md) for the full list; headline changes since the table above:
- Trip cost accounting rebuilt as a weighted-average battery pool instead of FIFO charge slices — see [How trip cost is computed](#how-trip-cost-is-computed).
- Two trip-reconstruction paths (orphan / disconnect-orphan, used when `vehicle_on` misses a real transition) now resolve the real end location and journey membership, and correctly adopt the result as the latest trip — previously they could leave every `last_trip_*` sensor stuck on a stale trip.
- ABRP telemetry gained `cabin_temp`, `hvac_setpoint`, and tire pressures, and its `soh` field now sends the calibrated (measured) SoH instead of the modelled/expected one.
- New `fix_speed_stats` maintenance service; new `Tracked sensors` config option (see [Tracking arbitrary sensors](#tracking-arbitrary-sensors)).

---

## What you get (full feature set)

### Trip detection
- `vehicle_on` off → on opens a trip via a **retry chain** (15 / 30 / 60 / 120 s) — covers cloud-polled cars where the odometer lags the ignition flag.
- on → off applies a **180 s grace window**. A flicker, a red light, a brief stop with engine off all keep the trip open. Grace is cancelled by either a fresh on-edge or by the timer expiring; close time backdates to the actual off-edge.
- Stuck / missed cycles reconstructed from monotonic odometer growth (synthetic trips, tagged `confidence='reconstructed'` or `'reconstructed_polling_paused'`).
- Synthetic finalize **aborts** if `vehicle_on=on` at fire time — prevents the path-mix bug where a trip closed mid-drive losing the remaining km.
- Idle watchdog defers to the off-grace when a close is already pending.
- Manual `log_manual_trip` and `recover_missing_trips` services for back-filling.

### Energy accounting
- **Effective battery capacity calibration** (v0.5.51): each charge with ≥ 30 % ΔSoC and `soc_start` populated contributes a sample `kwh / ΔSoC × 100`. Median of the last 30 such charges (minimum 5) replaces the declared capacity. Heals all historic SoC-derived trips on each shift.
- **Stale SoC resolution**: pick `soc_start` from `last_charge.soc_end`, the ring buffer, or current value — whichever is most trustworthy.
- **Power integration backup** (∫ |power| dt during the trip), capped at 250 kW and 20 min trapezoid width. Pessimistic `max(energy_soc, energy_pwr)` is the canonical figure.
- **Inline `distance × avg_consumption` fallback** at close when both SoC delta and power-integration come back empty.
- **Regen tracking** via negative-power trapezoidal integration. Aggregated to today / week / month / 30d / year / lifetime sensors.

### Score (per-car calibrated, v0.5.50/52)
- The 10/10 anchor (the kWh/100 km that maps to a perfect score) is no longer hardcoded to a vendor's marketing curve (the original 14.5 came from the BYD app, but Tesla's, Hyundai's, Ford's would all give different defaults). It's the P5 of YOUR consumption over trips ≥ 5 km, once 10+ such trips exist.
- Clamped to `[14.5, 20.0]` — the calibration can only **raise the bar** (a Tesla needing 18 kWh/100 km for 10/10 is realistic) but never **lower it** (a freak downhill trip at 5 kWh/100 km can't pin the curve unfairly).
- Live, last-trip and best-ever scores all use the per-car anchor.
- `score_baseline_kwh_100km` and `score_baseline_trip_count` exposed as attributes of `recent_trips` for dashboards.

### Battery health & degradation tracking (v0.5.54 / v0.5.57)
- **`sensor.<device>_battery_soh`** — observed state of health (calibrated capacity / declared × 100). Stays at 100 until 5+ valid charges build the calibration.
- **`sensor.<device>_expected_battery_soh`** — modelled SoH from your km, age, chemistry, climate and habits. Floor at 70 % (most manufacturers' battery warranty floor — BYD, Tesla, Hyundai/Kia, VW all guarantee ≥ 70 % at the warranty horizon).
- **`sensor.<device>_battery_health_vs_expected`** — enum: `calibrating` / `ahead` (> +2 pp) / `on_track` (±2 pp) / `behind` (< −2 pp).
- **`capacity_history`** table: every shift ≥ 0.5 kWh in calibrated capacity gets a row. Lets the dashboard plot the degradation curve.
- Three chemistry profiles supported with constants derived from real research:
  - `lfp` (BYD Blade, Tesla SR, MG, Atto3, Sealion 7 ← default for ≥ 75 kWh packs)
  - `nmc` (Tesla LR, BMW iX, VW ID, most 2018+)
  - `nca` (older Tesla Model S/X)

### Seasonal & time-of-day analytics (v0.5.54)
- **`sensor.<device>_consumption_by_season`** — winter / spring / summer / autumn (Northern hemisphere). State = current season; attributes carry all four. No extra config needed.
- **`sensor.<device>_consumption_by_time_of_day`** — night (22-06) / morning (06-12) / midday (12-15) / afternoon (15-19) / evening (19-22). No extra config needed.
- **`sensor.<device>_consumption_by_temp_bucket`** — < 5 / 5-15 / 15-25 / 25-35 / ≥ 35 °C. Needs the optional **Outside temp sensor** (`avg_temp_c` is sampled from it directly during each trip — there's no `weather.*` integration involved; a `weather_entity` config field exists only for backwards compatibility with entries created before v0.5.68 and has no effect).

### Journeys
- A journey opens iff a trip starts at home and ends away; closes iff a trip ends at home. Time gaps between intermediate trips are irrelevant.
- Auto-stitch: a trip ending at home with no open journey mints a fresh id AND absorbs orphan trips since the last home-arrival into it, so the full `casa → … → casa` chain renders as one row.
- Resume on restart via SQL (not in-memory state), so a mid-trip reload never loses the open journey.
- **Secondary home zones (v0.8.10)** — a second house, holiday home, etc. Configure any number of existing `zone.*` entities as **Secondary home zones**, and/or paste raw `lat,lon[,radius_m][,label]` coordinates (one per line) as **Secondary home coordinates** for a place you don't want a permanent HA zone for. Arriving there closes a journey and starting from there opens one, exactly like the primary `home_zone` — a coordinate match gets its own label (auto-numbered `secondary_home_N` if you didn't set one) so the trip's destination reads something meaningful instead of `not_home`.

### Charges
- Auto-detected from your `charge_sensor`. Plug-sensor wired ⇒ multiple charging pulses inside one plugged interval merge into a single session.
- `_maybe_resume_charge` recovers a session that started before HA restarted (without it, the entire charge would be dropped).
- `kwh_charged_before` and `kwh_charged_during` attributes on every trip let the dashboard show "+24 kWh between trips" so a SoC bump isn't mysterious.
- Per-session `soc_start` + `soc_end` are stored so the battery-capacity calibration can derive the real pack size from observation.
- **Power-integration during charge (v0.5.89+)** — if your `power_sensor` is wired, the integration sums ∫P·dt over the whole charge and uses it as the kWh persisted when it's within ±30 % of the SoC-derived number. Captures the full charge curve including the high-SoC taper where each percent represents more time → more accurate than `Δ% × nominal_capacity`.
- **EVSE / wallbox tracking (v0.5.90+, optional)** — wire any wallbox power sensor as `evse_power_sensor` (W or kW auto-detected). The integration measures AC-side energy delivered and computes real AC→DC efficiency = `battery_kwh / evse_kwh × 100`. Works with **any wallbox**: V2C Trydan, Shelly EM/Pro 3EM, Wallbox Pulsar / Quasar, Tesla Wall Connector, Easee, Zappi, Smartfox, OpenEVSE, etc. Five new sensors expose the data — see [Sensors exposed](#sensors-exposed). A 30-day rolling-median efficiency catches lossy cables / derated chargers (drop below 85 %).

### GPS / routing
- Every cloud poll fills a ring buffer of `(ts, lat, lon)` samples. Trips open with a real start anchor; synth trips persist a route to `trip_positions`.
- `gps_distance_km` is the haversine sum over the route — compared against the odometer-derived `distance_km` it surfaces sensor lag or fused trips.
- New trips reverse-geocode their endpoints via Nominatim; old trips backfilled from recorder history at startup.

### Driver tracking (v0.5.43+)
- Optional `driver_sensor` entity (any sensor whose state names who is driving — e.g. the car's "connected bluetooth device" entity, an `input_select`, or a template sensor mapping BT MAC → person).
- Captured at trip open with retry on every live tick until BT pairs.
- `sensor.<device>_driver_stats_30_days` and `sensor.<device>_current_driver` surface per-driver km, hours, trip counts.

### ABRP (A Better Route Planner)
- In-tree client. Configure `abrp_token` + `abrp_api_key` + `abrp_car_model` in the options flow.
- Telemetry piggy-backs on existing metric events — **no new poll forced** on the manufacturer's cloud.
- `switch.abrp_push` (RestoreEntity) — runtime kill switch your automations can toggle. Pushes immediately when turned on (no waiting for the next metric tick).
- `abrp_push_interval_s` is user-configurable (5..600 s, default 30).
- Sensor `<device>_abrp_next_charge_soc` reads ABRP's next-charge target every 2 min while a route is active.
- **Fields sent** (each optional/car-dependent — dropped from the payload when unavailable): `utc`, `soc`, `power`, `speed`, `lat`/`lon`, `is_charging`, `is_dcfc`, `is_parked`, `ext_temp`, `est_battery_range` (needs `range_sensor`), `odometer`, `heading` (needs `heading_sensor`), `soh` (the calibrated `battery_soh`, not the modelled `expected_battery_soh` — see below), `capacity`, `soe` (derived from soc × capacity), `kwh_charged`, `cabin_temp` (needs `cabin_temp_sensor`), `hvac_setpoint` (needs `hvac_setpoint_sensor`), `tire_pressure_fl/fr/rl/rr` (needs the four tire pressure sensors), `car_model`.
- ABRP's API also accepts `voltage`, `current`, `batt_temp` (separate from cabin), and `elevation` — not sent because this integration has no generic source for them; wire them yourself in `abrp.py`/`coordinator.py` if your car exposes matching sensors.
- `soh` is deliberately the **calibrated** figure (`battery_capacity / battery_capacity_baseline × 100`, grounded in this car's own observed charge behaviour) rather than `expected_battery_soh` (an age/mileage/climate *model* meant only for the "ahead/on-track/behind" diagnostic comparison) — sending the generic model as if it were measured would mislead ABRP's range predictions.

### Tracking arbitrary sensors
- Configure any list of `sensor.*` entities as **Tracked sensors** and each gets two extra sensors: a 7-day and a 30-day rolling arithmetic mean (via HA's recorder), e.g. to compare several of your car integration's own consumption-estimate variants against each other.
- Resulting entity_id: `sensor.<device>_<source-suffix>_avg_<7|30>d`, where `<source-suffix>` strips the `sensor.` prefix and the device's own title wherever it appears in the source entity_id (handles both a bare `sensor.<title>_foo` source and a car-integration-prefixed one like `sensor.byd_<title>_foo`).
- Non-numeric samples (`unknown`/`unavailable`/strings) are dropped from the average; the unit follows the source sensor and stays sticky through a source's `unavailable` blips.
- Housekeeping: if you added tracked sensors before v0.8.9, entities created against a car-integration-prefixed source may show a doubled device prefix in their name (e.g. `sensor.sealion_7_byd_sealion_7_energy_consumption_avg_30d`) — a slug-generation bug fixed in that release. The fix only prevents the doubling on newly-added tracked sensors; existing doubled entities need manual removal (Settings → Devices & services → Entities, filter by device) if you don't want them lingering.

### Recovery & corrections
- **`recover_missing_trips`** — scans the recorder for odo growth not covered by any existing trip and inserts synth records. Never modifies existing rows.
- **`set_trip(trip_id, …)`** — patch any field on any trip (origin, destination, energy, journey_id, timestamps, GPS, address, etc.).
- **`set_charge(charge_id, …)`** — same for charges. kWh edits auto-recompute total_cost.
- `confidence` column tags every trip as `live` / `reconstructed` / `reconstructed_polling_paused` / `reconstructed_recovery` / `orphan` / `orphan_odo_only` so dashboards can warn about low-quality rows.

---

## Install (HACS)

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/boraita/hass-ev-trip-logger`, category **Integration**.
2. Install **EV Trip Logger**, restart HA.
3. Settings → Devices & Services → **Add Integration** → "EV Trip Logger".

---

## Configuration

The wizard asks for the entities the integration consumes. **Required** first, optional after. Everything except the four required-✅ fields can be added later via *Configure* without losing history.

| Field | Required | What it's for |
|---|---|---|
| **Name** | ✅ | Device label (free text). |
| **Odometer sensor** | ✅ | `sensor.…_odometer` — km, monotonic. |
| **Battery sensor** | ✅ | `sensor.…_battery_level` — %, 0..100. |
| **Vehicle-on binary sensor** | ✅ | `binary_sensor.…_vehicle_on`. Primary trip trigger. |
| **Battery capacity (kWh)** | ✅ | E.g. 82.5 for a Sealion 7 Extended Range. Used as the **declared** capacity; the integration auto-calibrates the effective capacity from charges. |
| **Home zone** | ✅ | Usually `zone.home`. Journey logic uses it. |
| Secondary home zones | optional | Any number of existing `zone.*` entities that count as "home" for journeys (second house, holiday home, …). |
| Secondary home coordinates | optional | Free text, one `lat,lon[,radius_m][,label]` per line, for a home-equivalent place you don't want a permanent HA zone for. |
| Power sensor | optional | kW, +discharge/-charge. Enables regen + power-integration backup + ABRP push. If your car reports the opposite convention (e.g. some BYD cloud entities are -discharge/+charge), toggle **Power sign inverted**. |
| Power sign inverted | optional | Default off (positive = discharge). Flip ON when your car's `power_sensor` reports the inverse — telltale sign: persistent `regen_kwh` higher than `energy_kwh` even on flat trips. Auto-detected sensors that need this: BYD cloud-API `*_power`. |
| **EVSE power sensor** | optional | Any wallbox or socket-meter power entity (W or kW auto-detected). Enables AC-side energy + AC→DC efficiency measurement. Works with V2C Trydan, Shelly Pro/EM, Wallbox Pulsar/Quasar, Tesla Wall Connector, Easee, Zappi, Smartfox, OpenEVSE, generic Shelly relays. See [Wallbox examples](#wallbox-examples). |
| Charge sensor | optional | `binary_sensor.…_charging` OR any `sensor.*` whose state names the charging mode. Recognised "charging" values: `on`, `true`, `1`, `Charging`, `Starting`, `Engaged`, `ac_charging`, `dc_charging`, `slow_charging`, `fast_charging` (case-insensitive). Anything else (`off`, `Disconnected`, `Complete`, `Stopped`, `NoPower`, `idle`, `done`…) counts as "not charging". |
| Plug binary sensor | optional | Lets multi-pulse plugged sessions merge into one charge row. |
| Polling-paused sensor | optional | A switch or binary_sensor that goes ON when the manufacturer integration sleeps. Synth trips in that window get tagged `reconstructed_polling_paused`. |
| Location tracker | optional | `device_tracker.…_location`. Drives origin/destination + route map. |
| Outside temp sensor | optional | Per-trip avg temp + the historical `consumption_by_temp_bucket` sensor. See [Get the most out of it](#get-the-most-out-of-it). A `weather_entity` field also exists but is dead since v0.5.68 (backwards-compat only, ignored) — don't configure it. |
| Driver sensor | optional | Entity whose state names who is driving (e.g. car's bluetooth-connected-device sensor). Powers per-driver stats. |
| Speed sensor | optional | Refines the idle watchdog + ABRP `speed`. |
| Range sensor | optional | Estimated remaining range (km) — sent to ABRP as `est_battery_range` only, no other effect. |
| Heading sensor | optional | GPS heading/course (°, 0-360) — sent to ABRP as `heading` only, improves its route matching. |
| Cabin temperature sensor | optional | Interior temperature (°C) — sent to ABRP as `cabin_temp` only. |
| HVAC setpoint sensor | optional | Climate target temperature (°C) — sent to ABRP as `hvac_setpoint` only. |
| Tire pressure sensors (FL/FR/RL/RR) | optional | Any pressure `sensor.*`, any unit (bar/psi/kPa/hPa auto-converted) — sent to ABRP as `tire_pressure_fl/fr/rl/rr` (kPa) only. |
| **Battery chemistry** | optional | `lfp` (default for packs ≥ 75 kWh — covers BYD Blade, Sealion 7, MG, Tesla SR), `nmc`, `nca`. Drives the `expected_battery_soh` model. |
| **Vehicle first-registered date** | optional | ISO date (YYYY-MM-DD). Feeds the calendar-aging component of expected SoH. When missing, falls back to a `km / 15 000` proxy and lowers `confidence` to `medium`. |
| Min trip distance | ✅ | Default 0.5 km. Trips under this are discarded (precon/climate, not real drives). |
| Idle timeout | ✅ | Mid-trip stop tolerance (minutes). |
| Energy price (€/kWh) | ✅ | Home tariff. Fallback price when the battery pool has no tracked charge history to draw from yet — see [how trip cost is computed](#how-trip-cost-is-computed). |
| Energy price entity | optional | Live €/kWh tariff sensor (Octopus/Nordpool/PVPC…). When set, overrides the fixed price for trip/charge cost — read at trip/charge close, so it follows time-of-use periods. Falls back to the fixed price when unavailable or non-numeric. |
| Currency | ✅ | "EUR", "USD", etc. |
| Recent trips limit | ✅ | How many rows the `_recent_trips` attribute exposes (5..200, default 50). |
| ABRP token / api_key / car_model | optional | Enables ABRP telemetry push. |
| ABRP push interval (s) | optional | Throttle for outbound pushes (5..600, default 30). |
| Tracked sensors | optional | Arbitrary extra `sensor.*` entities to compute rolling 7d/30d averages for. See [Tracking arbitrary sensors](#tracking-arbitrary-sensors). |
| Elevation provider | optional | `none` (default), `open-elevation`, `opentopodata-eudem`, `opentopodata-srtm`, or `custom` (paired with **Elevation provider URL** for a self-hosted instance). Populates `elevation_gain_m` / `elevation_loss_m` / `elevation_variance_m2` per trip from the route's GPS points — off by default because it sends those points to an external service. Leaving it at `none` is why those three fields read `unknown`/blank on any dashboard card that shows them; it isn't a bug, just unconfigured. |

---

### Vehicle examples (the integration is car-agnostic)

| Car / source | `odometer` | `battery` | `vehicle_on` | `power` | Notes |
|---|---|---|---|---|---|
| **BYD** (cloud) | `sensor.<car>_odometer` | `sensor.<car>_battery_level` | `binary_sensor.<car>_vehicle_on` | `sensor.<car>_power` | Enable **Power sign inverted** (BYD cloud uses +charge/-discharge). |
| **Tesla** (Fleet / Teslemetry) | `sensor.<car>_odometer` | `sensor.<car>_battery_level` | `binary_sensor.<car>_vehicle_on` | `sensor.<car>_power` (when exposed) | Power sign inverted **OFF** (Tesla uses +discharge). |
| **BMW** (BimmerConnected) | `sensor.<car>_mileage` | `sensor.<car>_remaining_battery_percent` | `binary_sensor.<car>_charging_status` (inverted) | — | No live power → SoC-only trip math, still works. |
| **Hyundai/Kia** (Bluelink) | `sensor.<car>_odometer` | `sensor.<car>_ev_battery_level` | template from `Engine` enum | — | Same as BMW. |
| **OVMS** (CAN dongle) | `sensor.ovms_<id>_v_p_odometer` | `sensor.ovms_<id>_v_b_soc` | `binary_sensor.ovms_<id>_v_e_on` | `sensor.ovms_<id>_v_b_power` | Best-case: real CAN-rate power → consumption math near-perfect. |
| **MQTT manual** | any `sensor.*` | any `sensor.*` | any `binary_sensor.*` | optional | DIY setups: just publish the metrics. |

### Wallbox examples

Any HA entity that reports **charger output power in W or kW** works as `evse_power_sensor`:

| Wallbox / meter | Typical entity_id | Unit |
|---|---|---|
| V2C Trydan | `sensor.evse_<ip>_charge_power` | W |
| Shelly EM / Pro 3EM (meter on the cable) | `sensor.shellyemxxx_channel_<n>_power` | W |
| Wallbox Pulsar Plus / Quasar | `sensor.wallbox_<id>_charging_power` | kW |
| Tesla Wall Connector | `sensor.tesla_wall_connector_power` | kW |
| Easee | `sensor.easee_<id>_session_energy_or_power` | W or kW |
| Zappi (myenergi) | `sensor.zappi_<sn>_ct_ev` | W |
| Smartfox | `sensor.smartfox_charging_power` | W |
| OpenEVSE | `sensor.openevse_<host>_charge_power` | W |
| Generic Shelly relay metering charger socket | `sensor.<shelly>_power` | W |

The integration auto-detects the unit (`W` → divides by 1000 internally). Pick the sensor that reports **only the EV charging power**, not the whole house — otherwise the AC→DC efficiency will be skewed by background loads.

---

## Get the most out of it

The integration works with just the 6 required fields, but **enabling the optionals unlocks the analytics that make the dashboard useful**. Here's the minimum-effort path to full tracking:

### 1. Configure the outside temperature sensor (2 minutes, free)

`sensor.<device>_consumption_by_season` and `_by_time_of_day` work out of the box, no config needed. Two things need a temperature reading, though:

- `sensor.<device>_consumption_by_temp_bucket`
- The `climate_hot` factor in the expected-SoH model (raises your `confidence` from `medium` to `high`)

In EV Trip Logger → Configure → set **Outside temp sensor** to any `sensor.*` reporting outside/ambient temperature — your car's own exterior-temp sensor if it has one (many EV integrations expose this already), otherwise any local weather-station or `weather.*`-integration temperature sensor works too, since it just needs a plain numeric °C entity. If your odometer entity is named `sensor.<prefix>_odometer`, the logger also auto-detects `sensor.<prefix>_exterior_temperature` / `_outside_temperature` / `_ambient_temperature` on startup — check the log for an "Auto-detected exterior temp sensor" line before configuring this by hand.

### 2. Set battery chemistry + first-registered date

In Configure:
- **Battery chemistry**: `lfp` for BYD Blade / Tesla SR / MG / Atto 3 / Sealion 7. `nmc` for Tesla LR / BMW iX / VW ID / most 2018+ premium EVs. `nca` for older Tesla S/X.
- **Vehicle first-registered**: pick the matriculation date of your car.

This makes the `expected_battery_soh` curve align with real research data for YOUR chemistry, and pushes the model `confidence` from `low` → `medium` → `high`.

### 3. Drive normally for a week

The integration calibrates itself automatically as data flows in:

| Self-calibrating thing | What it needs |
|---|---|
| **Effective battery capacity** | 5 charges with ΔSoC ≥ 30 % AND `soc_start` populated. After that, all historical trips' energy/consumption auto-update. |
| **Score per-car anchor** | 10+ trips with `distance ≥ 5 km` and `consumption ∈ [5, 50]` kWh/100 km. After that, the 10/10 reference moves to your P5. |
| **Weather aggregates** | 5+ trips with the weather entity sampled (i.e. trips opened/closed after step 1). |
| **Degradation curve** | Each ≥ 0.5 kWh shift in calibrated capacity adds a row to `capacity_history`. Real degradation typically becomes visible at 6-12 months. |

### 4. (Optional) Install the dashboard

The companion dashboard at [hass-ev-trip-dashboard](https://github.com/boraita/hass-ev-trip-dashboard) consumes every sensor and attribute documented here, with ready-made cards for trips, journeys, charges, weather, seasonal stats, battery health, SoH curve, driver stats, etc.

---

## How trip cost is computed

The battery is modelled as **one blended pool**, not separate discrete purchases: every charge dilutes/raises a single running €/kWh average, weighted by kWh, and every trip draws energy from that pool at whatever the blended average is at the moment it happens (weighted-average-cost accounting — the same method used for fungible inventory in general, not FIFO/LIFO lot tracking).

Concretely: charge 30 kWh at your €0.07 home tariff, then a public DC-fast top-up adds 20 kWh at €0.40 → the pool's average becomes `(30×0.07 + 20×0.40) / 50 = €0.202/kWh`, and every trip until the next charge costs at that blended rate. A free/promotional charge dilutes the average down smoothly across whatever driving it covers instead of creating a "free until it runs out, then a sudden jump back to full price" discontinuity.

`cost_basis_per_kwh` on each trip is the pool's blended average at that moment; `cost = energy_kwh × cost_basis_per_kwh`. Energy that predates any tracked charge (fresh install) or exceeds what's been tracked as charged falls back to the configured home tariff. Each individual charge record still keeps its own **actual** price in its own row regardless, visible in `recent_charges` and the AC/DC monthly averages — only the derived trip cost is pooled.

`cost_at_avg_tariff` is a companion figure (`energy_kwh × recent_avg_tariff_per_kwh`, a trailing 30-day weighted average) that's always monotonic with kWh — useful for a "typical cost" comparison alongside the pool-accurate `cost`.

The pool is replayed idempotently from the full charge + trip history — not a service you call directly, but it runs automatically on every integration startup and after `set_charge` / `set_last_charge_price`, so correcting a charge's price retroactively re-prices every trip that drew from it.

---

## Services

All services accept an optional `entry_id` to target a specific config entry when you have multiple vehicles.

| Service | Purpose |
|---|---|
| `start_trip` / `end_trip` | Manually bracket a trip when sensors fail you. |
| `log_charge` | Manually insert a charge (kwh + optional price/location/notes). |
| `log_manual_trip` | Insert a full trip backfill (started_at, ended_at, distance, soc, etc.). |
| `set_trip(trip_id, …)` | Patch any column on a logged trip. |
| `set_charge(charge_id, …)` | Patch any column on a logged charge. |
| `set_last_charge_price(price_per_kwh \| total_cost \| charge_id, …)` | Correct a charge's price; triggers a trip cost recompute. |
| `delete_last_trip` / `delete_last_charge` | Drop the most recent row. |
| `purge_trips(since, until)` | Bulk delete in a date range. |
| `recover_missing_trips(since, until?)` | **Recovery mode** — scan recorder for odo growth not covered by any trip and insert synth rows. Existing trips are never modified. |
| `fix_speed_stats` | Maintenance — clears `avg_speed_kmh` on any trip where it exceeds `max_speed_kmh` (a physically impossible reading; `set_trip` can't null a field, hence this dedicated service). Safe to run any time; only touches already-corrupted rows. |
| `export_csv(path)` | Dump every trip to CSV. |

---

## Sensors exposed

A complete list lives in the source (`sensor.py`). The headline ones, all prefixed `sensor.<device>_`:

### Live/last/aggregates per metric
`current_trip_*`, `last_trip_*`, `distance_today`, `distance_this_week`, `energy_this_month`, etc., for: distance, duration, energy, consumption, avg_speed, max_speed, max_power, regen, battery_used, score, cost, avg_temperature.

### Charges
`last_charge_*`, `current_charge_*`, `charges_30d_*`, plus AC/DC price breakdowns.

**EVSE / efficiency (v0.5.89+, requires `evse_power_sensor` wired)**
- `last_charge_evse_kwh` / `current_charge_evse_kwh` — AC-side energy delivered by the wallbox.
- `last_charge_efficiency` / `current_charge_efficiency` — `battery_kwh / evse_kwh × 100`.
- `avg_charging_efficiency_30d` — rolling median across the last 30 charge sessions with an EVSE sensor wired. AC home charger: 88–94 %. DCFC: 92–97 %. Below 85 % signals a lossy cable / wallbox derating / inefficient onboard charger.

### Journeys
`current_journey`, `last_journey`, `recent_journeys` (with stages list).

### Routing
`recent_trips` (attribute `trips` = list of dicts with everything; also `score_baseline_kwh_100km`, `effective_battery_capacity_kwh`, `battery_capacity_calibration_charges` for transparency).
`last_trip_route` (attribute `points` = downsampled route).
`trip_patterns` (by hour / weekday).

### Records & rankings
`trip_records.totals.{distance_km, energy_kwh, cost, regen_kwh, trips}`, `tops` (longest, top_efficiency, cheapest, …).

### Battery & range
`battery_energy` (kWh in battery), `energy_to_full_charge`, `battery_percent`, `range_at_recent_efficiency`.

### Battery health (v0.5.54 / v0.5.57)
- **`battery_soh`** — observed state of health (%). Stays at 100 until the calibration kicks in (5+ charges).
- **`expected_battery_soh`** — modelled SoH for your km/age/chemistry/climate/habits. Attributes break the loss down: `year1_knee`, `calendar`, `cycle`, `climate_hot`, `dcfc`, `soc_habit`. Also: `confidence` (low/medium/high based on what config you've filled).
- **`battery_health_vs_expected`** — enum: `calibrating` / `ahead` / `on_track` / `behind`. Attributes: `observed_soh_pct`, `expected_soh_pct`, `delta_pp`.

### Seasonal / temporal (v0.5.54)
- **`consumption_by_season`** — state = current season's avg consumption; attributes carry the full `by_season` dict.
- **`consumption_by_time_of_day`** — state = current bucket's avg consumption (night / morning / midday / afternoon / evening).
- **`consumption_by_temp_bucket`** — < 5 / 5-15 / 15-25 / 25-35 / ≥ 35 °C (requires `exterior_temp_sensor`).

### Driver
`current_driver`, `driver_stats_30_days` (per-driver km, hours, trip counts).

### ABRP (only when configured)
`switch.abrp_push`, `abrp_next_charge_soc`.

---

## ABRP setup (optional)

1. In the ABRP app: car → *Edit Car Connection Details → Generic OEM → Link*. Copy **user token** and **api key**.
2. HA → Settings → Devices & Services → EV Trip Logger → **Configure** → paste them. `car_model` example: `byd:sealion:25:82:rwd`.
3. The new `switch.abrp_push` defaults to ON. To replicate the legacy `abrp_telemetry` "only while driving" pattern, point your automations at it:

```yaml
- alias: ABRP only while driving
  triggers:
    - trigger: state
      entity_id: binary_sensor.<vehicle>_vehicle_on
      to: "on"
      id: "on"
    - trigger: state
      entity_id: binary_sensor.<vehicle>_vehicle_on
      to: "off"
      id: "off"
  actions:
    - choose:
        - conditions: [{ condition: trigger, id: "on" }]
          sequence: [{ action: switch.turn_on, target: { entity_id: switch.abrp_push } }]
      default:
        - action: switch.turn_off
          target: { entity_id: switch.abrp_push }
```

---

## Recovery workflow

When you notice a trip missing or with wrong data:

```yaml
# Developer Tools → Actions
service: ev_trip_logger.recover_missing_trips
data:
  since: 2026-06-01 00:00:00
  until: 2026-06-08 23:59:59     # optional, defaults to now
```

Existing rows are never touched (±2 min tolerance). New rows are tagged `confidence='reconstructed_recovery'` so the dashboard can show a "low confidence" badge.

For one-off corrections:

```yaml
service: ev_trip_logger.set_trip
data:
  trip_id: 130
  origin: home
  started_at: 2026-06-07 22:20:00
  journey_id: 13
```

---

## Tracking checklist

For dashboards / analytics to make full use of the integration, every box should be ticked. Most of them are zero-effort one-off setups.

- [ ] **Required entities configured** (odometer, battery, vehicle_on, capacity, home zone)
- [ ] **Charge binary sensor** wired so charges are auto-detected
- [ ] **Power sensor** wired so regen / max_power / power-integration backup work
- [ ] **Location tracker** wired so origin/destination + route map work
- [ ] **Outside temp sensor** configured (unlocks the by-temperature bucket and the climate factor of the SoH model — season/time-of-day buckets work without it)
- [ ] **Battery chemistry** set explicitly (`lfp` / `nmc` / `nca`)
- [ ] **Vehicle first-registered date** set (raises SoH model confidence to `high`)
- [ ] **Driver sensor** configured if multiple drivers (BT-connected device entity or input_select)
- [ ] **Plug binary sensor** wired so multi-pulse charging sessions merge into one row
- [ ] **Polling-paused sensor** wired if your manufacturer integration sleeps the cloud poll
- [ ] **Outside temperature sensor** wired (separate from weather entity — this is the car's own probe)

---

## Reporting issues

The integration logs at INFO when it drops or repairs a trip, and emits a `_LOGGER.exception` when the expected-SoH model fails — so the stack lands in `system_log` directly. Enable debug for the namespace to see every state-machine decision:

```yaml
logger:
  default: info
  logs:
    custom_components.ev_trip_logger: debug
```

Open issues at https://github.com/boraita/hass-ev-trip-logger/issues with the relevant log lines + a description.

---

## License

MIT.
