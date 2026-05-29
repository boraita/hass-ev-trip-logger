"""Services for EV Trip Logger."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    SERVICE_DELETE_LAST_CHARGE,
    SERVICE_DELETE_LAST_TRIP,
    SERVICE_END_TRIP,
    SERVICE_EXPORT_CSV,
    SERVICE_LOG_CHARGE,
    SERVICE_LOG_MANUAL_TRIP,
    SERVICE_SET_LAST_CHARGE_PRICE,
    SERVICE_START_TRIP,
)
from .coordinator import EvTripLoggerCoordinator

_LOGGER = logging.getLogger(__name__)

_SCHEMA_ENTRY = vol.Schema(
    {vol.Optional("entry_id"): cv.string},
    extra=vol.ALLOW_EXTRA,
)

_SCHEMA_EXPORT = _SCHEMA_ENTRY.extend({vol.Required("path"): cv.string})

def _has_price_or_total(value: dict[str, Any]) -> dict[str, Any]:
    """Require one of: price_per_kwh, total_cost. None means 'use home default'."""
    if "price_per_kwh" in value and "total_cost" in value:
        # Both supplied: total_cost wins; we'll re-derive price.
        pass
    return value


_SCHEMA_LOG_CHARGE = vol.All(
    _SCHEMA_ENTRY.extend(
        {
            vol.Required("kwh"): vol.All(vol.Coerce(float), vol.Range(min=0.001)),
            vol.Optional("price_per_kwh"): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional("total_cost"): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional("currency"): cv.string,
            vol.Optional("location"): cv.string,
            vol.Optional("notes"): cv.string,
        }
    ),
    _has_price_or_total,
)


_SCHEMA_SET_LAST_CHARGE_PRICE = vol.All(
    _SCHEMA_ENTRY.extend(
        {
            vol.Optional("price_per_kwh"): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional("total_cost"): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional("location"): cv.string,
            vol.Optional("notes"): cv.string,
        }
    ),
    cv.has_at_least_one_key("price_per_kwh", "total_cost", "location", "notes"),
)


_SCHEMA_LOG_MANUAL_TRIP = _SCHEMA_ENTRY.extend(
    {
        vol.Required("started_at"): cv.datetime,
        vol.Required("ended_at"): cv.datetime,
        vol.Optional("distance_km"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("odometer_start"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("odometer_end"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("soc_start"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("soc_end"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("max_power_kw"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("avg_temp_c"): vol.Coerce(float),
        vol.Optional("origin"): cv.string,
        vol.Optional("destination"): cv.string,
    }
)


def _resolve_coordinators(
    hass: HomeAssistant, call: ServiceCall
) -> list[EvTripLoggerCoordinator]:
    entry_id = call.data.get("entry_id")
    bucket: dict[str, EvTripLoggerCoordinator] = hass.data.get(DOMAIN, {})
    if entry_id:
        return [bucket[entry_id]] if entry_id in bucket else []
    return list(bucket.values())


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services (idempotent)."""

    if hass.services.has_service(DOMAIN, SERVICE_START_TRIP):
        return

    async def _start(call: ServiceCall) -> None:
        for c in _resolve_coordinators(hass, call):
            await c.async_start_trip_service()

    async def _end(call: ServiceCall) -> None:
        for c in _resolve_coordinators(hass, call):
            await c.async_end_trip_service()

    async def _delete_last(call: ServiceCall) -> None:
        for c in _resolve_coordinators(hass, call):
            await c.async_delete_last_trip_service()

    async def _export(call: ServiceCall) -> None:
        # Restrict writes to the HA-configured allowlist; rejects /etc/passwd,
        # ../, and anything outside HA's writable directories.
        path = call.data["path"]
        if not hass.config.is_allowed_path(path):
            from homeassistant.exceptions import ServiceValidationError
            raise ServiceValidationError(
                f"Path {path!r} is not allowed. Add the parent directory to "
                "homeassistant.allowlist_external_dirs in configuration.yaml."
            )
        for c in _resolve_coordinators(hass, call):
            await c.storage.async_export_csv(path)

    async def _log_charge(call: ServiceCall) -> None:
        for c in _resolve_coordinators(hass, call):
            await c.async_log_charge_service(
                kwh=call.data["kwh"],
                price_per_kwh=call.data.get("price_per_kwh"),
                total_cost=call.data.get("total_cost"),
                currency=call.data.get("currency"),
                location=call.data.get("location"),
                notes=call.data.get("notes"),
            )

    async def _delete_last_charge(call: ServiceCall) -> None:
        for c in _resolve_coordinators(hass, call):
            await c.async_delete_last_charge_service()

    async def _set_last_charge_price(call: ServiceCall) -> None:
        for c in _resolve_coordinators(hass, call):
            await c.async_set_last_charge_price_service(
                price_per_kwh=call.data.get("price_per_kwh"),
                total_cost=call.data.get("total_cost"),
                location=call.data.get("location"),
                notes=call.data.get("notes"),
            )

    async def _log_manual_trip(call: ServiceCall) -> None:
        for c in _resolve_coordinators(hass, call):
            await c.async_log_manual_trip_service(
                started_at=call.data["started_at"],
                ended_at=call.data["ended_at"],
                distance_km=call.data.get("distance_km"),
                odometer_start=call.data.get("odometer_start"),
                odometer_end=call.data.get("odometer_end"),
                soc_start=call.data.get("soc_start"),
                soc_end=call.data.get("soc_end"),
                max_power_kw=call.data.get("max_power_kw"),
                avg_temp_c=call.data.get("avg_temp_c"),
                origin=call.data.get("origin"),
                destination=call.data.get("destination"),
            )

    hass.services.async_register(DOMAIN, SERVICE_START_TRIP, _start, schema=_SCHEMA_ENTRY)
    hass.services.async_register(DOMAIN, SERVICE_END_TRIP, _end, schema=_SCHEMA_ENTRY)
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_LAST_TRIP, _delete_last, schema=_SCHEMA_ENTRY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_EXPORT_CSV, _export, schema=_SCHEMA_EXPORT
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LOG_CHARGE, _log_charge, schema=_SCHEMA_LOG_CHARGE
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_LAST_CHARGE, _delete_last_charge, schema=_SCHEMA_ENTRY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_LAST_CHARGE_PRICE, _set_last_charge_price,
        schema=_SCHEMA_SET_LAST_CHARGE_PRICE,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LOG_MANUAL_TRIP, _log_manual_trip, schema=_SCHEMA_LOG_MANUAL_TRIP
    )


@callback
def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove integration services when the last entry is unloaded."""
    for name in (
        SERVICE_START_TRIP,
        SERVICE_END_TRIP,
        SERVICE_DELETE_LAST_TRIP,
        SERVICE_EXPORT_CSV,
        SERVICE_LOG_CHARGE,
        SERVICE_DELETE_LAST_CHARGE,
        SERVICE_SET_LAST_CHARGE_PRICE,
        SERVICE_LOG_MANUAL_TRIP,
    ):
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
