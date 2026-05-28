"""Constants for the EV Trip Logger integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "ev_trip_logger"

PLATFORMS: Final = ["sensor"]

CONF_NAME: Final = "name"
CONF_ODOMETER: Final = "odometer_sensor"
CONF_BATTERY: Final = "battery_sensor"
CONF_VEHICLE_ON: Final = "vehicle_on_sensor"
CONF_POWER: Final = "power_sensor"
CONF_CHARGE_SENSOR: Final = "charge_sensor"
CONF_LOCATION: Final = "location_tracker"
CONF_TEMP: Final = "exterior_temp_sensor"

CONF_BATTERY_CAPACITY: Final = "battery_capacity_kwh"
CONF_MIN_TRIP_DISTANCE: Final = "min_trip_distance_km"
CONF_IDLE_TIMEOUT: Final = "idle_timeout_minutes"
CONF_ENERGY_PRICE: Final = "energy_price_kwh"
CONF_CURRENCY: Final = "currency"
CONF_HOME_ZONE: Final = "home_zone"

DEFAULT_BATTERY_CAPACITY: Final = 75.0
DEFAULT_MIN_TRIP_DISTANCE: Final = 0.5
DEFAULT_IDLE_TIMEOUT: Final = 2
DEFAULT_ENERGY_PRICE: Final = 0.15
DEFAULT_CURRENCY: Final = "EUR"
DEFAULT_HOME_ZONE: Final = "home"

EVENT_TRIP_STARTED: Final = f"{DOMAIN}_trip_started"
EVENT_TRIP_ENDED: Final = f"{DOMAIN}_trip_ended"
EVENT_CHARGE_LOGGED: Final = f"{DOMAIN}_charge_logged"

SERVICE_START_TRIP: Final = "start_trip"
SERVICE_END_TRIP: Final = "end_trip"
SERVICE_DELETE_LAST_TRIP: Final = "delete_last_trip"
SERVICE_EXPORT_CSV: Final = "export_csv"
SERVICE_LOG_CHARGE: Final = "log_charge"
SERVICE_DELETE_LAST_CHARGE: Final = "delete_last_charge"
SERVICE_LOG_MANUAL_TRIP: Final = "log_manual_trip"

STORAGE_FILENAME_TEMPLATE: Final = "ev_trip_logger.{entry_id}.db"
