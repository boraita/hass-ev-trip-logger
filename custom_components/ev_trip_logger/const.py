"""Constants for the EV Trip Logger integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "ev_trip_logger"

PLATFORMS: Final = ["sensor", "calendar", "switch"]

CONF_NAME: Final = "name"
CONF_ODOMETER: Final = "odometer_sensor"
CONF_BATTERY: Final = "battery_sensor"
CONF_VEHICLE_ON: Final = "vehicle_on_sensor"
CONF_POWER: Final = "power_sensor"
# v0.5.85 — sign convention of the configured power sensor. Default
# is "discharge_positive": power > 0 when the motor draws energy
# from the battery (Tesla, most EVs). Some integrations (BYD cloud
# entity) report the opposite — discharge as negative. When the user
# sees inflated `regen_kwh` and a `battery_calibration_factor`
# stuck at None for every trip, this flag flips the integration so
# discharge / regen accounting is correct again.
CONF_POWER_SIGN_INVERTED: Final = "power_sign_inverted"
# v0.5.89 — optional EVSE (wallbox) power sensor in WATTS or KW. When
# wired, the integration sums it during the charge session to measure
# AC-side energy delivered (independent of the car-side power sensor).
# Comparing car battery input vs charger output exposes real AC→DC
# losses + onboard charger efficiency (typically 10-15 % delta).
CONF_EVSE_POWER_SENSOR: Final = "evse_power_sensor"
CONF_CHARGE_SENSOR: Final = "charge_sensor"
CONF_LOCATION: Final = "location_tracker"
CONF_TEMP: Final = "exterior_temp_sensor"
# v0.5.54-67 ʟᴇɢᴀᴄʏ — Weather entity support. Dropped in v0.5.68:
# the only field actually consumed was `temperature`, and that was a
# fallback for `CONF_TEMP` (the car's exterior temp sensor — better
# granularity, real-time updates). Other fields (condition, humidity,
# wind, precipitation) were stored but never used. The constant is
# kept so old config entries don't error on load; new installs ignore
# it and the field is hidden from the config flow.
CONF_WEATHER_ENTITY: Final = "weather_entity"
# v0.5.57 — battery chemistry + age for SoH-vs-expected modelling.
# `battery_chemistry`: 'lfp' (BYD Blade, Tesla SR, MG, Atto3, Sealion 7)
# / 'nmc' (Tesla LR, BMW iX, VW ID, most 2018+) / 'nca' (older Tesla).
# Default 'lfp' when capacity ≥ 75 kWh (heuristic — easy to override).
# `vehicle_first_registered`: ISO date (YYYY-MM-DD). Used to compute
# calendar age for the SoH model. Optional; when missing, we estimate
# age from `km / 15000` as a proxy.
CONF_BATTERY_CHEMISTRY: Final = "battery_chemistry"
CONF_VEHICLE_FIRST_REGISTERED: Final = "vehicle_first_registered"
DEFAULT_BATTERY_CHEMISTRY: Final = "lfp"
CONF_SPEED: Final = "speed_sensor"
CONF_PLUG_SENSOR: Final = "plug_sensor"
# v0.5.35 — optional polling-pause sensor (e.g. BYD's
# switch.byd_sealion_7_disable_polling). When ON, the manufacturer
# integration has paused its cloud poll → any trip reconstructed in
# that window will have especially sparse data, and we flag it as
# 'reconstructed_polling_paused' so the dashboard can show low
# confidence.
CONF_POLLING_PAUSED_SENSOR: Final = "polling_paused_sensor"
# v0.5.77 — optional vehicle-native per-trip energy sensor. Many EV
# integrations expose the energy of the last completed trip directly
# (BYD: `last_trip_energy`, Tesla: trip-meter A/B kWh, OVMS, etc.).
# When wired, the logger trusts this value over its own SoC delta or
# power-integration estimate, sidestepping the regen/quantization
# noise that inflates short trips. The coordinator auto-detects
# common suffixes (`_last_trip_energy`, `_last_trip_kwh`, …) using
# the odometer prefix.
CONF_LAST_TRIP_ENERGY_SENSOR: Final = "last_trip_energy_sensor"
# v0.5.77 — optional vehicle-native per-trip distance sensor. Cross-
# check for `CONF_LAST_TRIP_ENERGY_SENSOR`: only override the trip's
# energy when this matches the logger's odometer-derived distance
# (defends against a stale sensor referring to the previous trip).
CONF_LAST_TRIP_DISTANCE_SENSOR: Final = "last_trip_distance_sensor"
# v0.5.43 — optional driver-identity sensor. Any entity whose state
# names the person currently using the car: the manufacturer
# integration's "connected bluetooth device" sensor, an input_select
# the household toggles, or a template sensor mapping BT MAC → person.
# Captured at trip open (re-checked during the trip until it resolves)
# and persisted per trip, enabling per-driver km/hours stats.
CONF_DRIVER_SENSOR: Final = "driver_sensor"
# Driver-sensor states that mean "nobody identified". Compared
# case-insensitively after stripping.
DRIVER_NONE_STATES: Final = frozenset(
    {"none", "off", "not_connected", "disconnected", "no_device", "-", "null"}
)
# v0.5.38 — optional list of numeric sensors whose 7d / 30d averages
# the integration will expose. Typical use: BYD's energy snapshot
# entities (today's consumption, last-50km kWh, lifetime average,
# etc.) that only update when the user presses the fetch button.
# Tracking them here gives the user actual rolling means without
# wiring multiple HA `statistics` platform entries by hand.
CONF_TRACKED_SENSORS: Final = "tracked_sensors"
# How often each tracked-average sensor re-queries the recorder.
# v0.5.47 — 30 min (was 5): a rolling 7/30-day mean doesn't move in
# five minutes, and each refresh drags thousands of raw recorder rows
# (22 sensors x 12/h = 264 heavy queries/hour for no visible change).
TRACKED_AVG_REFRESH_S: Final = 1800
# v0.5.31 — optional ABRP (A Better Route Planner) telemetry push.
# Token+api_key required to activate; car_model is the ABRP slug
# (e.g. "byd:sealion:25:82:rwd"). All three live in the integration's
# config so the user only stores them once.
CONF_ABRP_TOKEN: Final = "abrp_token"
CONF_ABRP_API_KEY: Final = "abrp_api_key"
CONF_ABRP_CAR_MODEL: Final = "abrp_car_model"
# How often we push telemetry to ABRP. We hook off the existing
# metric-change events (no new poll forced on the BYD cloud), but
# throttle so we don't flood ABRP if the upstream emits bursts.
# DEFAULT only — user can change via CONF_ABRP_PUSH_INTERVAL_S.
ABRP_MIN_SEND_INTERVAL_S: Final = 30
CONF_ABRP_PUSH_INTERVAL_S: Final = "abrp_push_interval_s"
DEFAULT_ABRP_PUSH_INTERVAL_S: Final = 30
# How often the next-charge sensor polls ABRP's get_next_charge.
ABRP_NEXT_CHARGE_REFRESH_S: Final = 120

CONF_BATTERY_CAPACITY: Final = "battery_capacity_kwh"
CONF_DCFC_THRESHOLD_KW: Final = "dcfc_threshold_kw"
CONF_IDLE_TRIP_TIMEOUT_MIN: Final = "idle_trip_timeout_minutes"
DEFAULT_IDLE_TRIP_TIMEOUT_MIN: Final = 10
CONF_MIN_TRIP_DISTANCE: Final = "min_trip_distance_km"
CONF_IDLE_TIMEOUT: Final = "idle_timeout_minutes"
CONF_ENERGY_PRICE: Final = "energy_price_kwh"
CONF_CURRENCY: Final = "currency"
CONF_HOME_ZONE: Final = "home_zone"
CONF_RECENT_LIMIT: Final = "recent_trips_limit"

DEFAULT_BATTERY_CAPACITY: Final = 75.0
DEFAULT_MIN_TRIP_DISTANCE: Final = 0.5
DEFAULT_IDLE_TIMEOUT: Final = 2
DEFAULT_ENERGY_PRICE: Final = 0.15
DEFAULT_CURRENCY: Final = "EUR"
DEFAULT_HOME_ZONE: Final = "home"
# How many recent trips/charges/journeys to expose in the list sensors'
# attributes for dashboards. Bounded so one state attribute stays well under
# the recorder's per-state size limit.
DEFAULT_RECENT_LIMIT: Final = 50
# Charges with average power above this kW threshold are classified as DC
# fast-charge. 11 kW sits comfortably above the typical 3-phase 22 kW AC
# floor in continental Europe (Type 2) and below any meaningful DCFC.
DEFAULT_DCFC_THRESHOLD_KW: Final = 11.0

EVENT_TRIP_STARTED: Final = f"{DOMAIN}_trip_started"
EVENT_TRIP_ENDED: Final = f"{DOMAIN}_trip_ended"
EVENT_CHARGE_LOGGED: Final = f"{DOMAIN}_charge_logged"

SERVICE_START_TRIP: Final = "start_trip"
SERVICE_END_TRIP: Final = "end_trip"
SERVICE_DELETE_LAST_TRIP: Final = "delete_last_trip"
SERVICE_EXPORT_CSV: Final = "export_csv"
SERVICE_LOG_CHARGE: Final = "log_charge"
SERVICE_DELETE_LAST_CHARGE: Final = "delete_last_charge"
SERVICE_SET_LAST_CHARGE_PRICE: Final = "set_last_charge_price"
SERVICE_LOG_MANUAL_TRIP: Final = "log_manual_trip"
SERVICE_PURGE_TRIPS: Final = "purge_trips"
SERVICE_SET_TRIP: Final = "set_trip"
SERVICE_SET_CHARGE: Final = "set_charge"
SERVICE_RECOVER_MISSING_TRIPS: Final = "recover_missing_trips"
# v0.5.95 — backfill evse_energy_kwh + charging_efficiency_pct on a
# historical charge by trapezoidal-integrating the configured EVSE
# power sensor's recorder history within [started_at, ended_at],
# optionally masked by the charge_sensor=on windows.
SERVICE_BACKFILL_CHARGE_EVSE: Final = "backfill_charge_evse"

STORAGE_FILENAME_TEMPLATE: Final = "ev_trip_logger.{entry_id}.db"
