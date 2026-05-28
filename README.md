<p align="center">
  <img src="assets/brand/logo.png" alt="EV Trip Logger" width="640">
</p>

# EV Trip Logger for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub Release](https://img.shields.io/github/v/release/boraita/hass-ev-trip-logger?include_prereleases)](https://github.com/boraita/hass-ev-trip-logger/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A trip logger for any electric vehicle in Home Assistant.

## What it does

Your HA already shows odometer, battery and "vehicle on/off" from whatever integration runs your car. On their own, those numbers don't tell you anything. This integration sits on top and turns them into trip data:

- **Each drive becomes a trip** — date, distance, energy used, consumption (kWh/100km), cost, score 0–10.
- **Each charge is logged** — kWh added, what you paid, where, with auto-detection if your car exposes a "charging" sensor.
- **Monthly totals** — kilometres, energy, money spent on charging, estimated cost of the driving.
- **History** — the last 10 drives and charges as a list you can drop straight into a Lovelace card.

It is **vehicle-agnostic**: BYD, Tesla, Kia, Hyundai, MG, ESPHome — anything that exposes the right sensors in HA.

## Try it locally before installing

The repo ships with a one-command dev environment: a Docker Home Assistant with fake EV sensors you can poke from the dashboard.

```bash
git clone https://github.com/boraita/hass-ev-trip-logger
cd hass-ev-trip-logger
docker compose up -d
```

Then open **http://localhost:8124**, create a throwaway admin user, and:

1. Settings → Devices → **Add Integration** → search **EV Trip Logger**.
2. Pick the simulated sensors that come with the dev container:
   - Odometer: `sensor.sim_ev_odometer`
   - Battery: `sensor.sim_ev_battery`
   - Vehicle on: `binary_sensor.sim_ev_vehicle_on`
3. Drop the idle timeout to **1 minute** so trips close fast while testing.

Now simulate a drive from Developer Tools (or any dashboard with sliders):

- Toggle `input_boolean.ev_vehicle_on` to **on**.
- Move `input_number.ev_odometer` from `10000` to `10015` (+15 km).
- Move `input_number.ev_battery` from `80` to `70` (-10%).
- Toggle vehicle on **off** — after ~1 minute the trip closes and `sensor.<your_device>_last_trip_distance`, `_energy`, `_cost`, `_score`… all show real numbers.

To wipe the dev HA and start over: `docker compose down && rm -rf .dev/config/.storage && docker compose up -d`.

## Install in your real HA

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/boraita/hass-ev-trip-logger` as **Integration**.
2. Search and install **EV Trip Logger**, restart HA.
3. Settings → Devices → **Add Integration** → search "EV Trip Logger".

### Manual

Copy `custom_components/ev_trip_logger/` into your HA `config/custom_components/` and restart.

## What it asks during setup

Three required sensors:

- An **odometer** sensor (km or mi).
- A **battery level** sensor (%).
- A **vehicle-on** binary sensor (anything that goes `on` when the car is in use).

Plus a handful of optional ones that unlock more metrics:

- **Power** sensor — for max power per trip.
- **Charging** binary sensor — enables automatic charge logging.
- **Device tracker** — fills in trip origin/destination and tags charges with the zone (home, work, etc.).
- **Exterior temperature** — for consumption correlation.
- Battery capacity (kWh), minimum trip distance, idle timeout, **home charge price** (€/kWh), and currency.

## What you get

A vehicle device with around 30 sensors, grouped:

- **Current trip** — live while driving (distance, battery used, energy, kWh/100km, average speed, max power, average temperature).
- **Last trip** — same metrics frozen at trip end, plus **cost** and a **score 0–10** matched to BYD's app curve.
- **Monthly aggregates** — distance (today/week/month/year), energy, estimated cost, trip count, average consumption (30 days).
- **Charges** — last charge (kWh, cost, €/kWh), monthly totals (kWh charged, money spent, charge count, average €/kWh).
- **`recent_trips` / `recent_charges`** — count as state, last 10 entries as attributes for Lovelace cards.
- **`charge_in_progress`** — `charging` / `idle` so you can see at a glance whether the integration is already tracking a session.

## Logging a charge

If you set a **charging binary sensor** in the config, every charge is detected automatically while the car is plugged in. The default price comes from your config; the location comes from the device tracker.

For chargers with a different price (public, work, friends'…), log them manually:

```yaml
service: ev_trip_logger.log_charge
data:
  kwh: 35.4
  total_cost: 12.40            # or: price_per_kwh: 0.45
  location: Iberdrola Móstoles  # optional — defaults to device tracker zone
```

You can also pass only `kwh`; the integration uses the home price you configured.

## Lovelace example

Markdown card (no HACS needed) that renders a trip list similar to the BYD app:

```yaml
type: markdown
content: |
  ## Last trips ({{ states('sensor.my_ev_recent_trips') }})
  {%- for t in state_attr('sensor.my_ev_recent_trips', 'trips') or [] %}

  **{{ as_timestamp(t.ended_at) | timestamp_custom('%d/%m/%Y · %H:%M') }}**
  | Distancia | Consumo | Eficiencia | Coste | Score |
  |---:|---:|---:|---:|---:|
  | {{ t.distance_km }} km | {{ t.energy_kwh }} kWh | {{ t.consumption_kwh_100km }} kWh/100km | {{ t.cost }} {{ t.currency }} | **{{ t.score }}** |
  {%- endfor %}
```

Replace `sensor.my_ev_*` with whatever you named your device.

## Services

- `ev_trip_logger.start_trip` / `end_trip` — manual control.
- `ev_trip_logger.log_charge` / `delete_last_charge`.
- `ev_trip_logger.delete_last_trip`.
- `ev_trip_logger.export_csv` — dump all trips to a CSV path.

## Events for automations

- `ev_trip_logger_trip_started` / `ev_trip_logger_trip_ended` — fires with full trip data.
- `ev_trip_logger_charge_logged` — fires when a charge is recorded (manual or auto).

## License

MIT. See [LICENSE](LICENSE).
