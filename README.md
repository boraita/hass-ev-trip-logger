# EV Trip Logger for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub Release](https://img.shields.io/github/v/release/boraita/hass-ev-trip-logger?include_prereleases)](https://github.com/boraita/hass-ev-trip-logger/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Vehicle-agnostic trip logger for electric vehicles in Home Assistant.

Plug in **any** odometer / battery / vehicle-on sensors you already have (BYD, Tesla, Kia, Hyundai, MG, Volvo, BMW, custom ESPHome — it doesn't matter), and the integration takes care of detecting trips, computing live and historical statistics, and storing the full session log.

## Why

The HA ecosystem has plenty of vehicle integrations, but trip logging is fragmented:

- GPS-only loggers (Movement, blueprints) don't know about battery / energy.
- EV trip *planners* plan future trips but don't log past ones.
- Vehicle-specific integrations (Tesla, BMW, Kia, etc.) lock you in.

This integration sits **above** any of those: it consumes the entities they expose and gives you a proper trip log — independent of the vehicle.

## Features

- **Live trip sensors** while driving — distance, duration, avg speed, battery used, energy consumed (kWh), kWh/100km, avg temperature.
- **Last completed trip** — same metrics, frozen at trip end.
- **Aggregations** — today / week / month / year totals (distance, energy, cost, count).
- **Persistent history** — SQLite-backed trip log, queryable, exportable to CSV.
- **Events** — `ev_trip_logger_trip_started` / `ev_trip_logger_trip_ended` for your own automations.
- **Reconfigurable** — swap a sensor without losing history.
- **Multi-vehicle** — one entry per car.

## Installation

### HACS (custom repository)

1. Open HACS in Home Assistant.
2. Menu (⋮) → **Custom repositories**.
3. Add `https://github.com/boraita/hass-ev-trip-logger` with category **Integration**.
4. Install **EV Trip Logger**, restart HA.
5. Settings → Devices & Services → **Add Integration** → search **EV Trip Logger**.

### Manual

Copy `custom_components/ev_trip_logger/` into your HA `config/custom_components/` directory and restart.

## Configuration

Configured entirely via the UI. You'll be asked for:

**Required sensors:**

| Field | What to pick |
|---|---|
| Odometer sensor | A `sensor` with `device_class: distance` and unit km/mi |
| Battery level sensor | A `sensor` with `device_class: battery` (%) |
| Vehicle-on binary sensor | A `binary_sensor` that goes `on` when the car is in use |

**Optional sensors** (each one unlocks more metrics):

- Power sensor (kW) — instantaneous power
- Range sensor (km/mi) — remaining range
- Location tracker (`device_tracker`) — origin/destination
- Exterior temperature sensor — for consumption correlation

**Vehicle parameters:**

- Battery capacity (kWh) — default 75
- Minimum trip distance (km) — trips shorter than this are discarded; default 0.5
- Idle timeout (minutes) — wait this long before closing a trip after vehicle-off; default 2
- Energy price (€/kWh or your currency) — for cost estimation

## Provided entities

Once configured, the integration exposes:

### Live (during a trip)

- `sensor.ev_trip_current_distance`
- `sensor.ev_trip_current_duration`
- `sensor.ev_trip_current_avg_speed`
- `sensor.ev_trip_current_battery_used`
- `sensor.ev_trip_current_energy`
- `sensor.ev_trip_current_consumption`
- `sensor.ev_trip_current_avg_temperature`

### Last completed trip

Same set, prefixed `sensor.ev_trip_last_*`.

### Aggregations

- `sensor.ev_trip_total_distance_today` / `_week` / `_month` / `_year`
- `sensor.ev_trip_total_energy_month` / `_cost_month`
- `sensor.ev_trip_avg_consumption_30d`
- `sensor.ev_trip_count_month`

## Events

```yaml
# Triggered when vehicle_on goes from off → on (after debounce)
ev_trip_logger_trip_started:
  entry_id: <config_entry_id>
  started_at: 2026-05-28T08:21:34+02:00
  odometer_start: 12345.6
  soc_start: 78
  location_start: home

# Triggered when vehicle_on goes from on → off (after idle_timeout)
ev_trip_logger_trip_ended:
  entry_id: <config_entry_id>
  trip_id: 42
  started_at: ...
  ended_at: ...
  distance_km: 23.4
  duration_min: 31
  avg_speed_kmh: 45.3
  soc_used_pct: 12
  energy_kwh: 9.6
  consumption_kwh_100km: 41.0
  avg_temp_c: 18.5
  origin: home
  destination: work
  cost: 1.25
```

## Services

- `ev_trip_logger.start_trip` — manually open a trip
- `ev_trip_logger.end_trip` — manually close a trip
- `ev_trip_logger.delete_last_trip` — recover from an accidental detection
- `ev_trip_logger.export_csv` — dump full history to a path

## License

MIT. See [LICENSE](LICENSE).
