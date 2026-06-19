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
| **Weather correlation** | Optional `weather.*` entity captures temperature / condition / humidity / wind / precipitation at trip open + close. Three new bucket sensors: consumption-by-season, by-time-of-day, by-temperature. |
| **Battery health (SoH)** | Live `battery_soh` (% of declared capacity actually delivered). Plus `expected_battery_soh` modelled from your km / age / chemistry / climate / DCFC habits with constants from Geotab + Tesla + ADAC + NREL + BYD warranty (8 yr / 250 k km, ≥ 70 %). `battery_health_vs_expected` enum tells you if you're ahead/on-track/behind the curve. |
| **Degradation tracking** | `capacity_history` table appends a snapshot whenever the calibrated capacity drifts ≥ 0.5 kWh — long-term degradation curve visible from day one. |

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
- The 10/10 anchor (the kWh/100 km that maps to a perfect score) is no longer hardcoded to 14.5 (the BYD app curve). It's the P5 of YOUR consumption over trips ≥ 5 km, once 10+ such trips exist.
- Clamped to `[14.5, 20.0]` — the calibration can only **raise the bar** (a Tesla needing 18 kWh/100 km for 10/10 is realistic) but never **lower it** (a freak downhill trip at 5 kWh/100 km can't pin the curve unfairly).
- Live, last-trip and best-ever scores all use the per-car anchor.
- `score_baseline_kwh_100km` and `score_baseline_trip_count` exposed as attributes of `recent_trips` for dashboards.

### Battery health & degradation tracking (v0.5.54 / v0.5.57)
- **`sensor.<device>_battery_soh`** — observed state of health (calibrated capacity / declared × 100). Stays at 100 until 5+ valid charges build the calibration.
- **`sensor.<device>_expected_battery_soh`** — modelled SoH from your km, age, chemistry, climate and habits. Floor at 70 % (BYD warranty floor).
- **`sensor.<device>_battery_health_vs_expected`** — enum: `calibrating` / `ahead` (> +2 pp) / `on_track` (±2 pp) / `behind` (< −2 pp).
- **`capacity_history`** table: every shift ≥ 0.5 kWh in calibrated capacity gets a row. Lets the dashboard plot the degradation curve.
- Three chemistry profiles supported with constants derived from real research:
  - `lfp` (BYD Blade, Tesla SR, MG, Atto3, Sealion 7 ← default for ≥ 75 kWh packs)
  - `nmc` (Tesla LR, BMW iX, VW ID, most 2018+)
  - `nca` (older Tesla Model S/X)

### Weather & seasonal analytics (v0.5.54)
- Optional `weather.*` entity is sampled at trip open and close; averages persisted on the trip row as `ambient_temp_c`, `weather_condition`, `humidity_pct`, `wind_kmh`, `precipitation_mm`.
- **`sensor.<device>_consumption_by_season`** — winter / spring / summer / autumn (Northern hemisphere). State = current season; attributes carry all four.
- **`sensor.<device>_consumption_by_time_of_day`** — night (22-06) / morning (06-12) / midday (12-15) / afternoon (15-19) / evening (19-22).
- **`sensor.<device>_consumption_by_temp_bucket`** — < 5 / 5-15 / 15-25 / 25-35 / ≥ 35 °C (uses the optional `exterior_temp_sensor`).

### Journeys
- A journey opens iff a trip starts at home and ends away; closes iff a trip ends at home. Time gaps between intermediate trips are irrelevant.
- Auto-stitch: a trip ending at home with no open journey mints a fresh id AND absorbs orphan trips since the last home-arrival into it, so the full `casa → … → casa` chain renders as one row.
- Resume on restart via SQL (not in-memory state), so a mid-trip reload never loses the open journey.

### Charges
- Auto-detected from your `charge_sensor`. Plug-sensor wired ⇒ multiple charging pulses inside one plugged interval merge into a single session.
- `_maybe_resume_charge` recovers a session that started before HA restarted (without it, the entire charge would be dropped).
- `kwh_charged_before` and `kwh_charged_during` attributes on every trip let the dashboard show "+24 kWh between trips" so a SoC bump isn't mysterious.
- Per-session `soc_start` + `soc_end` are stored so the battery-capacity calibration can derive the real pack size from observation.

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
| Power sensor | optional | kW, +discharge/-charge. Enables regen + power-integration backup + ABRP push. |
| Charge sensor | optional | `binary_sensor.…_charging` OR any `sensor.*` whose state names the charging mode. Recognised "charging" values: `on`, `true`, `1`, `Charging`, `Starting`, `Engaged`, `ac_charging`, `dc_charging`, `slow_charging`, `fast_charging` (case-insensitive). Anything else (`off`, `Disconnected`, `Complete`, `Stopped`, `NoPower`, `idle`, `done`…) counts as "not charging". |
| Plug binary sensor | optional | Lets multi-pulse plugged sessions merge into one charge row. |
| Polling-paused sensor | optional | A switch or binary_sensor that goes ON when the manufacturer integration sleeps. Synth trips in that window get tagged `reconstructed_polling_paused`. |
| Location tracker | optional | `device_tracker.…_location`. Drives origin/destination + route map. |
| **Weather entity** | optional | Any `weather.*` (AEMET, Met.no, OpenWeatherMap…). Enables the season / temperature / time-of-day buckets. See [Get the most out of it](#get-the-most-out-of-it). |
| Outside temp sensor | optional | Per-trip avg temp + the historical `consumption_by_temp_bucket` sensor. |
| Driver sensor | optional | Entity whose state names who is driving (e.g. car's bluetooth-connected-device sensor). Powers per-driver stats. |
| Speed sensor | optional | Refines the idle watchdog + ABRP `speed`. |
| **Battery chemistry** | optional | `lfp` (default for packs ≥ 75 kWh — covers BYD Blade, Sealion 7, MG, Tesla SR), `nmc`, `nca`. Drives the `expected_battery_soh` model. |
| **Vehicle first-registered date** | optional | ISO date (YYYY-MM-DD). Feeds the calendar-aging component of expected SoH. When missing, falls back to a `km / 15 000` proxy and lowers `confidence` to `medium`. |
| Min trip distance | ✅ | Default 0.5 km. Trips under this are discarded (precon/climate, not real drives). |
| Idle timeout | ✅ | Mid-trip stop tolerance (minutes). |
| Energy price (€/kWh) | ✅ | Home tariff. Trip cost = energy × this price (NOT per-charge price — see [why](#why-trip-cost-is-the-home-tariff)). |
| Energy price entity | optional | Live €/kWh tariff sensor (Octopus/Nordpool/PVPC…). When set, overrides the fixed price for trip/charge cost — read at trip/charge close, so it follows time-of-use periods. Falls back to the fixed price when unavailable or non-numeric. |
| Currency | ✅ | "EUR", "USD", etc. |
| Recent trips limit | ✅ | How many rows the `_recent_trips` attribute exposes (5..200, default 50). |
| ABRP token / api_key / car_model | optional | Enables ABRP telemetry push. |
| ABRP push interval (s) | optional | Throttle for outbound pushes (5..600, default 30). |

---

## Get the most out of it

The integration works with just the 6 required fields, but **enabling the optionals unlocks the analytics that make the dashboard useful**. Here's the minimum-effort path to full tracking:

### 1. Add a `weather.*` integration to HA (5 minutes, free)

Any one of these:
- **AEMET OpenData** (Spain): Settings → Devices → Add Integration → AEMET OpenData → request a free token at `opendata.aemet.es`. Produces `weather.casa`.
- **Met.no**: built-in HA integration, no API key. Just add the integration.
- **OpenWeatherMap**: free tier, requires API key.

Then in EV Trip Logger → Configure → set **Weather entity** = `weather.casa` (or whatever yours is called).

This unlocks:
- `ambient_temp_c`, `humidity_pct`, `wind_kmh`, `precipitation_mm`, `weather_condition` on every new trip
- `sensor.<device>_consumption_by_season` (state = current season's avg consumption)
- `sensor.<device>_consumption_by_time_of_day` (night vs morning vs ...)
- The `climate_hot` factor in the expected-SoH model gets real input (raising your `confidence` from `medium` to `high`)

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

## Why trip cost is the home tariff

A charge at a €0.40/kWh public DC fast-charger is a one-off event; the energy already mixed with home-charged kWh in the battery. Trip cost is therefore modelled as `energy × home_tariff`. Each individual charge record keeps its **actual** price in its own row, visible in the AC / DC monthly averages.

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
| `export_csv(path)` | Dump every trip to CSV. |

---

## Sensors exposed

A complete list lives in the source (`sensor.py`). The headline ones, all prefixed `sensor.<device>_`:

### Live/last/aggregates per metric
`current_trip_*`, `last_trip_*`, `distance_today`, `distance_this_week`, `energy_this_month`, etc., for: distance, duration, energy, consumption, avg_speed, max_speed, max_power, regen, battery_used, score, cost, avg_temperature.

### Charges
`last_charge_*`, `current_charge_*`, `charges_30d_*`, plus AC/DC price breakdowns.

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
- [ ] **`weather.*` entity** configured (unlocks season / temp / time-of-day buckets and the climate factor of the SoH model)
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
