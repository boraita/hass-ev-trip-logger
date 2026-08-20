"""Services for EV Trip Logger."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    SERVICE_BACKFILL_CHARGE_EVSE,
    SERVICE_DELETE_LAST_CHARGE,
    SERVICE_DELETE_LAST_TRIP,
    SERVICE_END_TRIP,
    SERVICE_EXPORT_CSV,
    SERVICE_FIX_SPEED_STATS,
    SERVICE_HEAL_HISTORY,
    SERVICE_LOG_CHARGE,
    SERVICE_LOG_MANUAL_TRIP,
    SERVICE_PURGE_TRIPS,
    SERVICE_RECOVER_MISSING_TRIPS,
    SERVICE_SET_CHARGE,
    SERVICE_SET_LAST_CHARGE_PRICE,
    SERVICE_SET_TRIP,
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
            vol.Optional("charge_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional("evse_energy_kwh"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        }
    ),
    cv.has_at_least_one_key(
        "price_per_kwh", "total_cost", "location", "notes", "evse_energy_kwh"
    ),
)


_SCHEMA_PURGE_TRIPS = vol.All(
    _SCHEMA_ENTRY.extend(
        {
            vol.Optional("since"): cv.datetime,
            vol.Optional("until"): cv.datetime,
        }
    ),
    cv.has_at_least_one_key("since", "until"),
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
        vol.Optional("driver"): cv.string,
    }
)


# v0.5.21 — manual corrections. Any trip/charge field can be amended
# after the fact; useful when the logger missed an off-edge, recorded
# the wrong origin from a stale device_tracker, or persisted an
# auto-detected charge with the wrong timestamps.
_SCHEMA_SET_TRIP = vol.All(
    _SCHEMA_ENTRY.extend(
        {
            vol.Required("trip_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional("started_at"): cv.datetime,
            vol.Optional("ended_at"): cv.datetime,
            vol.Optional("duration_min"): vol.Coerce(float),
            vol.Optional("distance_km"): vol.Coerce(float),
            vol.Optional("odometer_start"): vol.Coerce(float),
            vol.Optional("odometer_end"): vol.Coerce(float),
            vol.Optional("soc_start"): vol.Coerce(float),
            vol.Optional("soc_end"): vol.Coerce(float),
            vol.Optional("soc_used_pct"): vol.Coerce(float),
            vol.Optional("energy_kwh"): vol.Coerce(float),
            vol.Optional("consumption_kwh_100km"): vol.Coerce(float),
            vol.Optional("avg_speed_kmh"): vol.Coerce(float),
            vol.Optional("max_power_kw"): vol.Coerce(float),
            vol.Optional("max_speed_kmh"): vol.Coerce(float),
            vol.Optional("regen_kwh"): vol.Coerce(float),
            vol.Optional("avg_temp_c"): vol.Coerce(float),
            vol.Optional("origin"): cv.string,
            vol.Optional("destination"): cv.string,
            vol.Optional("cost"): vol.Coerce(float),
            vol.Optional("currency"): cv.string,
            vol.Optional("journey_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional("start_lat"): vol.Coerce(float),
            vol.Optional("start_lon"): vol.Coerce(float),
            vol.Optional("end_lat"): vol.Coerce(float),
            vol.Optional("end_lon"): vol.Coerce(float),
            vol.Optional("start_address"): cv.string,
            vol.Optional("end_address"): cv.string,
            vol.Optional("driver"): cv.string,
        }
    ),
)


_SCHEMA_SET_CHARGE = vol.All(
    _SCHEMA_ENTRY.extend(
        {
            vol.Required("charge_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional("started_at"): cv.datetime,
            vol.Optional("ended_at"): cv.datetime,
            vol.Optional("kwh"): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional("soc_start"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
            vol.Optional("soc_end"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
            vol.Optional("location"): cv.string,
            vol.Optional("notes"): cv.string,
            vol.Optional("is_dcfc"): cv.boolean,
            vol.Optional("currency"): cv.string,
        }
    ),
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
                charge_id=call.data.get("charge_id"),
                evse_energy_kwh=call.data.get("evse_energy_kwh"),
            )

    async def _purge_trips(call: ServiceCall) -> None:
        since = call.data.get("since")
        until = call.data.get("until")
        for c in _resolve_coordinators(hass, call):
            count = await c.async_purge_trips_service(since=since, until=until)
            _LOGGER.info(
                "Purged %d trip(s) in [%s, %s] for entry %s",
                count, since, until, c.entry_id,
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
                driver=call.data.get("driver"),
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
    hass.services.async_register(
        DOMAIN, SERVICE_PURGE_TRIPS, _purge_trips, schema=_SCHEMA_PURGE_TRIPS
    )

    async def _fix_speed_stats(call: ServiceCall) -> None:
        for c in _resolve_coordinators(hass, call):
            n = await c.async_fix_speed_stats_service()
            _LOGGER.info(
                "fix_speed_stats: cleared avg_speed_kmh on %d trip(s) for entry %s",
                n, c.entry_id,
            )

    hass.services.async_register(
        DOMAIN, SERVICE_FIX_SPEED_STATS, _fix_speed_stats, schema=_SCHEMA_ENTRY,
    )

    async def _heal_history(call: ServiceCall) -> None:
        for c in _resolve_coordinators(hass, call):
            counts = await c.async_heal_history_service()
            _LOGGER.info(
                "heal_history: %d trip(s) seen — charge attribution fixed on "
                "%d, soc_start re-anchored on %d, energy recomputed on %d, "
                "consumption suppressed on %d (entry %s)",
                counts["trips_seen"],
                counts["charge_attribution_fixed"],
                counts["soc_start_reanchored"],
                counts["energy_recomputed"],
                counts["consumption_suppressed"],
                c.entry_id,
            )

    hass.services.async_register(
        DOMAIN, SERVICE_HEAL_HISTORY, _heal_history, schema=_SCHEMA_ENTRY,
    )

    async def _set_trip(call: ServiceCall) -> None:
        # Build the patch dict from every field the user passed.
        # entry_id and trip_id are stripped; everything else is forwarded.
        fields = {k: v for k, v in call.data.items()
                  if k not in {"entry_id", "trip_id"}}
        trip_id = int(call.data["trip_id"])
        for c in _resolve_coordinators(hass, call):
            await c.async_set_trip_service(trip_id=trip_id, fields=fields)

    async def _set_charge(call: ServiceCall) -> None:
        fields = {k: v for k, v in call.data.items()
                  if k not in {"entry_id", "charge_id"}}
        charge_id = int(call.data["charge_id"])
        for c in _resolve_coordinators(hass, call):
            await c.async_set_charge_service(
                charge_id=charge_id, fields=fields,
            )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_TRIP, _set_trip, schema=_SCHEMA_SET_TRIP
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CHARGE, _set_charge, schema=_SCHEMA_SET_CHARGE
    )

    async def _recover_missing_trips(call: ServiceCall) -> None:
        for c in _resolve_coordinators(hass, call):
            n = await c.async_recover_missing_trips_service(
                since=call.data["since"],
                until=call.data.get("until"),
            )
            _LOGGER.info(
                "recover_missing_trips: %d trip(s) inserted for entry %s",
                n, c.entry_id,
            )

    hass.services.async_register(
        DOMAIN, SERVICE_RECOVER_MISSING_TRIPS, _recover_missing_trips,
        schema=_SCHEMA_ENTRY.extend(
            {
                vol.Required("since"): cv.datetime,
                vol.Optional("until"): cv.datetime,
            }
        ),
    )

    async def _backfill_charge_evse(call: ServiceCall) -> None:
        charge_id = int(call.data["charge_id"])
        sensor = call.data.get("evse_power_sensor")
        mask = bool(call.data.get("mask_by_charge_sensor", True))
        for c in _resolve_coordinators(hass, call):
            patched = await c.async_backfill_charge_evse_service(
                charge_id=charge_id,
                evse_power_sensor=sensor,
                mask_by_charge_sensor=mask,
            )
            if patched is not None:
                _LOGGER.info(
                    "backfill_charge_evse: entry=%s charge=%s evse=%.3f "
                    "eff=%s",
                    c.entry_id, charge_id,
                    patched.evse_energy_kwh or 0.0,
                    patched.charging_efficiency_pct,
                )

    hass.services.async_register(
        DOMAIN, SERVICE_BACKFILL_CHARGE_EVSE, _backfill_charge_evse,
        schema=_SCHEMA_ENTRY.extend(
            {
                vol.Required("charge_id"): vol.All(
                    vol.Coerce(int), vol.Range(min=1),
                ),
                vol.Optional("evse_power_sensor"): cv.string,
                vol.Optional("mask_by_charge_sensor", default=True): cv.boolean,
            }
        ),
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
        SERVICE_PURGE_TRIPS,
        SERVICE_SET_TRIP,
        SERVICE_SET_CHARGE,
        SERVICE_RECOVER_MISSING_TRIPS,
        SERVICE_BACKFILL_CHARGE_EVSE,
        SERVICE_FIX_SPEED_STATS,
        SERVICE_HEAL_HISTORY,
    ):
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
