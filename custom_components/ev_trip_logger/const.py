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
CONF_CHARGE_SENSOR: Final = "charge_sensor"
CONF_LOCATION: Final = "location_tracker"
CONF_TEMP: Final = "exterior_temp_sensor"
CONF_SPEED: Final = "speed_sensor"
CONF_PLUG_SENSOR: Final = "plug_sensor"
# v0.5.35 — optional polling-pause sensor (e.g. BYD's
# switch.byd_sealion_7_disable_polling). When ON, the manufacturer
# integration has paused its cloud poll → any trip reconstructed in
# that window will have especially sparse data, and we flag it as
# 'reconstructed_polling_paused' so the dashboard can show low
# confidence.
CONF_POLLING_PAUSED_SENSOR: Final = "polling_paused_sensor"
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

STORAGE_FILENAME_TEMPLATE: Final = "ev_trip_logger.{entry_id}.db"
