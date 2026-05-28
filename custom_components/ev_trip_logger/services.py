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
        for c in _resolve_coordinators(hass, call):
            await c.storage.async_export_csv(call.data["path"])

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
    ):
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
