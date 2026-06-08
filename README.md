<p align="center">
  <img src="assets/brand/logo.png" alt="EV Trip Logger" width="640">
</p>

# EV Trip Logger for Home Assistant

A vehicle-agnostic Home Assistant custom integration that records every drive and charge from the entities your manufacturer integration already exposes, derives accurate consumption / cost / journey aggregates from them, and surfaces everything through standard HA sensors so any dashboard can consume the data.

Works with **any cloud-polled EV integration** — BYD, Tesla Fleet, OVMS, Bouncie, native CAN-bus dongles, even a manual setup. You point it at the entities you already have; it does the rest.

> Companion dashboard with ~25 ready-to-use cards: **[hass-ev-trip-dashboard](https://github.com/boraita/hass-ev-trip-dashboard)**.

---

## Why

Cloud-polled EVs have stale, integer-step SoC, sparse odometer ticks, and unreliable `vehicle_on` transitions. Out of the box that makes:
- consumption per trip off by 1–2 % (or NULL on short trips),
- single drives split into multiple rows,
- overnight charges silently dropped on reload,
- journeys (casa → … → casa) shown as 1-stage fragments,
- addresses stuck as `not_home`.

This integration solves all of that with explicit state machines, fallbacks, and a recovery service. The hard work of v0.5.x is documented in [the release notes](https://github.com/boraita/hass-ev-trip-logger/releases).

---

## What you get

### Trip detection
- `vehicle_on` off→on opens a trip; on→off closes it (with a 3 s flicker debounce).
- Stuck/missed cycles are reconstructed from monotonic odometer growth (synthetic trips, tagged `confidence='reconstructed'`).
- An idle watchdog force-closes a trip only when **`vehicle_on=off` is also seen** — a long stop mid-drive no longer splits the row.
- Manual `log_manual_trip` and `recover_missing_trips` services for back-filling.

### Energy accounting
- **Stale SoC resolution**: pick `soc_start` from `last_charge.soc_end`, the ring buffer, or current value — whichever is most trustworthy.
- **Power integration backup** (∫ |power| dt during the trip), capped at 250 kW and 20 min trapezoid width. Pessimistic `max(energy_soc, energy_pwr)` is the canonical figure.
- **Inline `distance × avg_consumption` fallback** at close when both SoC delta and power-integration come back empty (BYD-style integer SoC + sparse power).
- **Regen tracking** via negative-power trapezoidal integration. Aggregated to today / week / month / 30d / year / lifetime sensors.

### Journeys
- A journey opens iff a trip starts at home and ends away; closes iff a trip ends at home. Time gaps between intermediate trips are irrelevant.
- Auto-stitch: a trip ending at home with no open journey mints a fresh id AND absorbs orphan trips since the last home-arrival into it, so the full `casa → … → casa` chain renders as one row.
- Resume on restart via SQL (not in-memory state), so a mid-trip reload never loses the open journey.

### Charges
- Auto-detected from your `charge_sensor`. Plug-sensor wired ⇒ multiple charging pulses inside one plugged interval merge into a single session.
- `_maybe_resume_charge` recovers a session that started before HA restarted (without it, the entire charge would be dropped).
- `kwh_charged_before` and `kwh_charged_during` attributes on every trip let the dashboard show "+24 kWh between trips" so a SoC bump isn't mysterious.

### GPS / routing
- Every cloud poll fills a ring buffer of `(ts, lat, lon)` samples. Trips open with a real start anchor; synth trips persist a route to `trip_positions`.
- `gps_distance_km` is the haversine sum over the route — compared against the odometer-derived `distance_km` it surfaces sensor lag.
- New trips reverse-geocode their endpoints via Nominatim; old trips backfilled from recorder history at startup.

### ABRP (A Better Route Planner)
- In-tree client. Configure `abrp_token` + `abrp_api_key` + `abrp_car_model` in the options flow.
- Telemetry piggy-backs on existing metric events — **no new poll forced** on the manufacturer's cloud.
- New `switch.abrp_push` (RestoreEntity) — runtime kill switch your automations can toggle.
- `abrp_push_interval_s` is user-configurable (5..600 s, default 30).
- Sensor `<device>_abrp_next_charge_soc` reads ABRP's next-charge target every 2 min while a route is active.

### Recovery & corrections
- **`recover_missing_trips`** — scans the recorder for odo growth not covered by any existing trip and inserts synth records. Never modifies existing rows.
- **`set_trip(trip_id, …)`** — patch any field on any trip (origin, destination, energy, journey_id, timestamps, GPS, address, etc.).
- **`set_charge(charge_id, …)`** — same for charges. kWh edits auto-recompute total_cost.
- `confidence` column tags every trip as `live`, `reconstructed`, `reconstructed_polling_paused`, or `reconstructed_recovery` so dashboards can warn about low-quality rows.

---

## Install (HACS)

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/boraita/hass-ev-trip-logger`, category **Integration**.
2. Install **EV Trip Logger**, restart HA.
3. Settings → Devices & Services → **Add Integration** → "EV Trip Logger".

---

## Configuration

The wizard asks for the entities the integration consumes. Required first, optional after.

| Field | Required | What it's for |
|---|---|---|
| **Name** | ✅ | Device label (free text). |
| **Odometer sensor** | ✅ | `sensor.…_odometer` — km, monotonic. |
| **Battery sensor** | ✅ | `sensor.…_battery_level` — %, 0..100. |
| **Vehicle-on binary sensor** | ✅ | `binary_sensor.…_vehicle_on`. Primary trip trigger. |
| **Battery capacity (kWh)** | ✅ | E.g. 82.56 for a Sealion 7 Extended Range. Used to derive kWh from SoC delta. |
| **Home zone** | ✅ | Usually `zone.home`. Journey logic uses it. |
| Power sensor | optional | kW, +discharge/-charge. Enables regen + power-integration backup + ABRP push. |
| Charge binary sensor | optional | `binary_sensor.…_charging`. Auto-charge detection. |
| Plug binary sensor | optional | Lets multi-pulse plugged sessions merge into one charge row. |
| Polling-paused sensor | optional | A switch or binary_sensor that goes ON when the manufacturer integration sleeps. Synth trips in that window get tagged `reconstructed_polling_paused`. |
| Location tracker | optional | `device_tracker.…_location`. Drives origin/destination + route map. |
| Outside temp | optional | For per-trip avg temp + temp-bucket consumption analysis. |
| Speed sensor | optional | Refines the idle watchdog + ABRP `speed`. |
| Min trip distance | ✅ | Default 0.5 km. Trips under this are discarded (precon/climate, not real drives). |
| Idle timeout | ✅ | Mid-trip stop tolerance (minutes). |
| Energy price (€/kWh) | ✅ | Home tariff. Trip cost = energy × this price (NOT per-charge price — see [why](#why-trip-cost-is-the-home-tariff)). |
| Currency | ✅ | "EUR", "USD", etc. |
| Recent trips limit | ✅ | How many rows the `_recent_trips` attribute exposes (5..200, default 50). |
| ABRP token / api_key / car_model | optional | Enables ABRP telemetry push. |
| ABRP push interval (s) | optional | Throttle for outbound pushes (5..600, default 30). |

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

**Live/last/aggregates per metric** — `current_trip_*`, `last_trip_*`, `distance_today`, `distance_this_week`, `energy_this_month`, etc., for: distance, duration, energy, consumption, avg_speed, max_speed, max_power, regen, battery_used, score, cost, avg_temperature.

**Charges** — `last_charge_*`, `current_charge_*`, `charges_30d_*`, plus AC/DC price breakdowns.

**Journeys** — `current_journey`, `last_journey`, `recent_journeys` (with stages list).

**Routing** — `recent_trips` (attribute `trips` = list of dicts with everything), `last_trip_route` (attribute `points` = downsampled route), `trip_patterns` (by hour / weekday).

**Records & rankings** — `trip_records.totals.{distance_km, energy_kwh, cost, regen_kwh, trips}`, `tops` (longest, top_efficiency, cheapest, …).

**Battery & range** — `battery_energy` (kWh in battery), `energy_to_full_charge`, `battery_percent`, `range_at_recent_efficiency`.

**ABRP** (only when configured) — `switch.abrp_push`, `abrp_next_charge_soc`.

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

## Reporting issues

The integration logs at INFO when it drops or repairs a trip. Enable debug for the namespace to see every state-machine decision:

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
