"""Sensors exposed by EV Trip Logger."""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EvTripLoggerCoordinator
from .storage import period_start

_LOGGER = logging.getLogger(__name__)

_AGGREGATE_REFRESH = timedelta(minutes=5)


@dataclass(frozen=True, kw_only=True)
class TripSensorMeta:
    """Metadata for a current/last trip sensor."""

    key: str
    description: SensorEntityDescription


def _desc(
    key: str,
    *,
    unit: str | None = None,
    device_class: SensorDeviceClass | None = None,
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT,
    icon: str | None = None,
    suggested_precision: int | None = None,
) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=unit,
        device_class=device_class,
        state_class=state_class,
        icon=icon,
        suggested_display_precision=suggested_precision,
    )


_TRIP_FIELDS_EXTRA_LAST: dict[str, dict[str, Any]] = {
    "cost": {
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": None,
        "precision": 2,
        "slug": "cost",
    },
    "score": {
        "icon": "mdi:speedometer",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
        "slug": "score",
    },
}


_TRIP_FIELDS: list[TripSensorMeta] = [
    TripSensorMeta(
        key="distance_km",
        description=_desc(
            "distance",
            unit=UnitOfLength.KILOMETERS,
            device_class=SensorDeviceClass.DISTANCE,
            suggested_precision=1,
        ),
    ),
    TripSensorMeta(
        key="duration_min",
        description=_desc(
            "duration",
            unit=UnitOfTime.MINUTES,
            device_class=SensorDeviceClass.DURATION,
            suggested_precision=0,
        ),
    ),
    TripSensorMeta(
        key="avg_speed_kmh",
        description=_desc(
            "avg_speed",
            unit=UnitOfSpeed.KILOMETERS_PER_HOUR,
            device_class=SensorDeviceClass.SPEED,
            suggested_precision=1,
        ),
    ),
    TripSensorMeta(
        key="soc_used_pct",
        description=_desc(
            "battery_used",
            unit=PERCENTAGE,
            device_class=SensorDeviceClass.BATTERY,
            suggested_precision=1,
        ),
    ),
    TripSensorMeta(
        key="energy_kwh",
        description=_desc(
            "energy",
            unit=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
            suggested_precision=2,
        ),
    ),
    TripSensorMeta(
        key="consumption_kwh_100km",
        description=_desc(
            "consumption",
            unit="kWh/100km",
            icon="mdi:car-electric",
            suggested_precision=1,
        ),
    ),
    TripSensorMeta(
        key="avg_temp_c",
        description=_desc(
            "avg_temperature",
            unit=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            suggested_precision=1,
        ),
    ),
    TripSensorMeta(
        key="max_power_kw",
        description=_desc(
            "max_power",
            unit=UnitOfPower.KILO_WATT,
            device_class=SensorDeviceClass.POWER,
            suggested_precision=1,
        ),
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for a config entry."""
    coordinator: EvTripLoggerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []
    for meta in _TRIP_FIELDS:
        entities.append(CurrentTripSensor(coordinator, meta))
        entities.append(LastTripSensor(coordinator, meta))

    for key, cfg in _TRIP_FIELDS_EXTRA_LAST.items():
        entities.append(LastTripExtraSensor(coordinator, key=key, cfg=cfg))

    entities.extend(
        [
            AggregateSensor(coordinator, period="today", key="distance_km"),
            AggregateSensor(coordinator, period="week", key="distance_km"),
            AggregateSensor(coordinator, period="month", key="distance_km"),
            AggregateSensor(coordinator, period="year", key="distance_km"),
            AggregateSensor(coordinator, period="month", key="energy_kwh"),
            AggregateSensor(coordinator, period="month", key="cost"),
            AggregateSensor(coordinator, period="month", key="count"),
            AggregateSensor(coordinator, period="30d", key="avg_consumption_kwh_100km"),
        ]
    )

    entities.append(RecentTripsSensor(coordinator))
    entities.append(RecentChargesSensor(coordinator))
    entities.append(ChargeInProgressSensor(coordinator))
    entities.append(LastJourneySensor(coordinator))
    entities.append(CurrentJourneySensor(coordinator))

    entities.extend(
        [
            LastChargeSensor(coordinator, key="kwh"),
            LastChargeSensor(coordinator, key="total_cost"),
            LastChargeSensor(coordinator, key="price_per_kwh"),
            ChargesAggregateSensor(coordinator, period="month", key="kwh"),
            ChargesAggregateSensor(coordinator, period="month", key="total_cost"),
            ChargesAggregateSensor(coordinator, period="month", key="count"),
            ChargesAggregateSensor(coordinator, period="30d", key="avg_price_per_kwh"),
        ]
    )

    async_add_entities(entities)


class _BaseTripSensor(SensorEntity):
    """Common boilerplate for live/last/aggregate sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry_id)},
            name=coordinator.entry.title,
            manufacturer="EV Trip Logger",
            model="Vehicle-agnostic trip logger",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self.async_write_ha_state))


class CurrentTripSensor(_BaseTripSensor):
    """Live metric while a trip is in progress.

    When idle (no active trip), additive metrics show 0 instead of going
    'unavailable' — cleaner in dashboards. Ratios (avg speed, consumption,
    avg temperature) stay None because they're undefined without data.
    """

    _RATIO_KEYS = {"avg_speed_kmh", "consumption_kwh_100km", "avg_temp_c"}

    def __init__(
        self, coordinator: EvTripLoggerCoordinator, meta: TripSensorMeta
    ) -> None:
        super().__init__(coordinator)
        self._meta = meta
        self.entity_description = replace(
            meta.description,
            key=f"current_{meta.description.key}",
            translation_key=f"current_{meta.description.translation_key}",
        )
        self._attr_unique_id = f"{coordinator.entry_id}_current_{meta.key}"

    @property
    def native_value(self) -> float | None:
        snapshot = self._coordinator.current_snapshot()
        if snapshot is None:
            return None if self._meta.key in self._RATIO_KEYS else 0.0
        return snapshot.get(self._meta.key)


class LastTripSensor(_BaseTripSensor):
    """Metric from the most recently completed trip."""

    def __init__(
        self, coordinator: EvTripLoggerCoordinator, meta: TripSensorMeta
    ) -> None:
        super().__init__(coordinator)
        self._meta = meta
        self.entity_description = replace(
            meta.description,
            key=f"last_{meta.description.key}",
            translation_key=f"last_{meta.description.translation_key}",
        )
        self._attr_unique_id = f"{coordinator.entry_id}_last_{meta.key}"

    @property
    def native_value(self) -> float | None:
        trip = self._coordinator.last_trip
        if trip is None:
            return None
        return getattr(trip, self._meta.key, None)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        trip = self._coordinator.last_trip
        if trip is None:
            return None
        return {
            "started_at": trip.started_at.isoformat(),
            "ended_at": trip.ended_at.isoformat(),
            "origin": trip.origin,
            "destination": trip.destination,
        }


class LastTripExtraSensor(_BaseTripSensor):
    """Per-trip cost and score sensors (sourced from the last completed trip)."""

    def __init__(
        self, coordinator: EvTripLoggerCoordinator, *, key: str, cfg: dict[str, Any]
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        slug = f"last_trip_{cfg['slug']}"
        self.entity_description = SensorEntityDescription(
            key=slug,
            translation_key=slug,
            native_unit_of_measurement=(
                coordinator.currency
                if cfg.get("device_class") == SensorDeviceClass.MONETARY
                else cfg.get("unit")
            ),
            device_class=cfg.get("device_class"),
            state_class=cfg.get("state_class"),
            icon=cfg.get("icon"),
            suggested_display_precision=cfg.get("precision"),
        )
        self._attr_unique_id = f"{coordinator.entry_id}_{slug}"

    @property
    def native_value(self) -> float | None:
        trip = self._coordinator.last_trip
        if trip is None:
            return None
        return getattr(trip, self._key, None)


class AggregateSensor(_BaseTripSensor):
    """Roll-up sensor: today / week / month / year totals."""

    _PERIODIC_KEYS_UNITS: dict[str, tuple[str | None, SensorDeviceClass | None, str | None]] = {
        "distance_km": (UnitOfLength.KILOMETERS, SensorDeviceClass.DISTANCE, None),
        "energy_kwh": (UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, None),
        "cost": (None, SensorDeviceClass.MONETARY, "mdi:currency-eur"),
        "count": (None, None, "mdi:counter"),
        "avg_consumption_kwh_100km": ("kWh/100km", None, "mdi:car-electric"),
    }

    _SLUG_BY_KEY: dict[str, str] = {
        "distance_km": "distance",
        "energy_kwh": "energy",
        "cost": "cost",
        "count": "count",
        "avg_consumption_kwh_100km": "avg_consumption",
    }

    # HA rejects state_class=measurement for device_class=monetary, so cost stays None.
    _STATE_CLASS_BY_KEY: dict[str, SensorStateClass | None] = {
        "distance_km": SensorStateClass.TOTAL_INCREASING,
        "energy_kwh": SensorStateClass.TOTAL_INCREASING,
        "cost": None,
        "count": SensorStateClass.MEASUREMENT,
        "avg_consumption_kwh_100km": SensorStateClass.MEASUREMENT,
    }

    def __init__(
        self, coordinator: EvTripLoggerCoordinator, *, period: str, key: str
    ) -> None:
        super().__init__(coordinator)
        self._period = period
        self._key = key
        self._value: float | int | None = None

        unit, device_class, icon = self._PERIODIC_KEYS_UNITS[key]
        slug = f"{period}_{self._SLUG_BY_KEY[key]}"
        self.entity_description = SensorEntityDescription(
            key=f"total_{slug}",
            translation_key=f"total_{slug}",
            native_unit_of_measurement=(
                unit if key != "cost" else coordinator.currency
            ),
            device_class=device_class,
            state_class=self._STATE_CLASS_BY_KEY[key],
            icon=icon,
            suggested_display_precision=0 if key == "count" else 1,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_total_{slug}"

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_refresh, _AGGREGATE_REFRESH)
        )
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, *_: Any) -> None:
        since = period_start(dt_util.now(), self._period)
        aggregates = await self._coordinator.storage.async_aggregates_since(since)
        self._value = aggregates.get(self._key)
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | int | None:
        return self._value


def _trip_to_attr(trip: Any) -> dict[str, Any]:
    return {
        "id": trip.trip_id,
        "journey_id": trip.journey_id,
        "started_at": trip.started_at.isoformat(),
        "ended_at": trip.ended_at.isoformat(),
        "distance_km": round(trip.distance_km, 1),
        "duration_min": round(trip.duration_min, 1),
        "energy_kwh": round(trip.energy_kwh, 2) if trip.energy_kwh is not None else None,
        "consumption_kwh_100km": (
            round(trip.consumption_kwh_100km, 1)
            if trip.consumption_kwh_100km is not None
            else None
        ),
        "cost": round(trip.cost, 2) if trip.cost is not None else None,
        "currency": trip.currency,
        "score": round(trip.score, 1) if trip.score is not None else None,
        "origin": trip.origin,
        "destination": trip.destination,
    }


def _charge_to_attr(charge: Any) -> dict[str, Any]:
    return {
        "id": charge.charge_id,
        "ended_at": charge.ended_at.isoformat(),
        "kwh": round(charge.kwh, 2),
        "price_per_kwh": round(charge.price_per_kwh, 4),
        "total_cost": round(charge.total_cost, 2),
        "currency": charge.currency,
        "location": charge.location,
    }


class LastJourneySensor(_BaseTripSensor):
    """Summary of the most recently completed home-to-home journey.

    State is the number of stages; attributes hold totals: distance, energy,
    cost, started_at, ended_at.
    """

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self._summary: dict[str, Any] | None = None
        self.entity_description = SensorEntityDescription(
            key="last_journey",
            translation_key="last_journey",
            icon="mdi:map-marker-path",
            state_class=SensorStateClass.MEASUREMENT,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_last_journey"

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        jid = self._coordinator.last_completed_journey_id
        self._summary = (
            await self._coordinator.storage.async_journey_summary(jid)
            if jid is not None
            else None
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        return self._summary["stages"] if self._summary else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._summary:
            return None
        s = self._summary
        return {
            "journey_id": s["journey_id"],
            "started_at": s["started_at"].isoformat() if s["started_at"] else None,
            "ended_at": s["ended_at"].isoformat() if s["ended_at"] else None,
            "distance_km": round(s["distance_km"], 1),
            "energy_kwh": round(s["energy_kwh"], 2),
            "cost": round(s["cost"], 2),
        }


class CurrentJourneySensor(_BaseTripSensor):
    """Running journey if the car has left home and hasn't returned yet."""

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self._summary: dict[str, Any] | None = None
        self.entity_description = SensorEntityDescription(
            key="current_journey",
            translation_key="current_journey",
            icon="mdi:map-marker-distance",
            state_class=SensorStateClass.MEASUREMENT,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_current_journey"

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        jid = self._coordinator.current_journey_id
        self._summary = (
            await self._coordinator.storage.async_journey_summary(jid)
            if jid is not None
            else None
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return self._summary["stages"] if self._summary else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._summary:
            return None
        s = self._summary
        return {
            "journey_id": s["journey_id"],
            "started_at": s["started_at"].isoformat() if s["started_at"] else None,
            "distance_km": round(s["distance_km"], 1),
            "energy_kwh": round(s["energy_kwh"], 2),
            "cost": round(s["cost"], 2),
        }


class ChargeInProgressSensor(_BaseTripSensor):
    """Exposes whether a charging session is currently being auto-tracked.

    State: 'charging' / 'idle'. Attributes show progress so the user can confirm
    the integration is following the session and avoid double-logging manually.
    """

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="charge_in_progress",
            translation_key="charge_in_progress",
            icon="mdi:battery-charging",
        )
        self._attr_unique_id = f"{coordinator.entry_id}_charge_in_progress"

    @property
    def native_value(self) -> str:
        return "charging" if self._coordinator.current_charge is not None else "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        active = self._coordinator.current_charge
        if active is None:
            return None
        soc_now = active.last_seen_soc if active.last_seen_soc is not None else active.soc_start
        kwh_so_far = (
            (soc_now - active.soc_start) / 100.0 * self._coordinator.battery_capacity
            if soc_now is not None and active.soc_start is not None
            else None
        )
        return {
            "since": active.started_at.isoformat(),
            "soc_start": active.soc_start,
            "soc_now": soc_now,
            "kwh_so_far": round(kwh_so_far, 2) if kwh_so_far is not None else None,
        }


class RecentTripsSensor(_BaseTripSensor):
    """List of the most recent trips, exposed via attributes for Lovelace cards."""

    _LIMIT = 10

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self._trips: list[Any] = []
        self.entity_description = SensorEntityDescription(
            key="recent_trips",
            translation_key="recent_trips",
            icon="mdi:format-list-bulleted",
            state_class=SensorStateClass.MEASUREMENT,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_recent_trips"

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        self._trips = await self._coordinator.storage.async_recent_trips(self._LIMIT)
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return len(self._trips)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"trips": [_trip_to_attr(t) for t in self._trips]}


class RecentChargesSensor(_BaseTripSensor):
    """List of the most recent charges, exposed via attributes."""

    _LIMIT = 10

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self._charges: list[Any] = []
        self.entity_description = SensorEntityDescription(
            key="recent_charges",
            translation_key="recent_charges",
            icon="mdi:ev-station",
            state_class=SensorStateClass.MEASUREMENT,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_recent_charges"

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        self._charges = await self._coordinator.storage.async_recent_charges(self._LIMIT)
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return len(self._charges)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"charges": [_charge_to_attr(c) for c in self._charges]}


class LastChargeSensor(_BaseTripSensor):
    """Metric from the most recently logged charge session."""

    _CONFIG: dict[str, dict[str, Any]] = {
        "kwh": {
            "unit": UnitOfEnergy.KILO_WATT_HOUR,
            "device_class": SensorDeviceClass.ENERGY,
            # HA rejects measurement for device_class=energy; last-charge is a
            # one-shot value, not a running total — None is the right call.
            "state_class": None,
            "precision": 2,
            "slug": "last_charge_kwh",
        },
        "total_cost": {
            "device_class": SensorDeviceClass.MONETARY,
            "state_class": None,
            "precision": 2,
            "slug": "last_charge_cost",
        },
        "price_per_kwh": {
            "device_class": SensorDeviceClass.MONETARY,
            "state_class": None,
            "precision": 4,
            "icon": "mdi:cash",
            "slug": "last_charge_price",
        },
    }

    def __init__(self, coordinator: EvTripLoggerCoordinator, *, key: str) -> None:
        super().__init__(coordinator)
        cfg = self._CONFIG[key]
        self._key = key
        self.entity_description = SensorEntityDescription(
            key=cfg["slug"],
            translation_key=cfg["slug"],
            native_unit_of_measurement=(
                cfg.get("unit")
                if key == "kwh"
                else coordinator.currency
            ),
            device_class=cfg.get("device_class"),
            state_class=cfg.get("state_class"),
            icon=cfg.get("icon"),
            suggested_display_precision=cfg.get("precision"),
        )
        self._attr_unique_id = f"{coordinator.entry_id}_{cfg['slug']}"

    @property
    def native_value(self) -> float | None:
        charge = self._coordinator.last_charge
        if charge is None:
            return None
        return getattr(charge, self._key, None)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        charge = self._coordinator.last_charge
        if charge is None:
            return None
        return {
            "ended_at": charge.ended_at.isoformat(),
            "location": charge.location,
            "notes": charge.notes,
        }


class ChargesAggregateSensor(_BaseTripSensor):
    """Roll-up sensor over the `charges` table."""

    _CONFIG: dict[str, dict[str, Any]] = {
        "kwh": {
            "unit": UnitOfEnergy.KILO_WATT_HOUR,
            "device_class": SensorDeviceClass.ENERGY,
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "precision": 1,
            "slug": "charged_energy",
        },
        "total_cost": {
            "device_class": SensorDeviceClass.MONETARY,
            "state_class": None,
            "precision": 2,
            "slug": "spent_charging",
        },
        "count": {
            "device_class": None,
            "state_class": SensorStateClass.MEASUREMENT,
            "icon": "mdi:counter",
            "precision": 0,
            "slug": "charges",
        },
        "avg_price_per_kwh": {
            "device_class": SensorDeviceClass.MONETARY,
            "state_class": None,
            "icon": "mdi:cash",
            "precision": 4,
            "slug": "avg_charge_price",
        },
    }

    _PERIOD_SUFFIX = {
        "today": "today",
        "week": "this_week",
        "month": "this_month",
        "year": "this_year",
        "30d": "30d",
    }

    def __init__(
        self, coordinator: EvTripLoggerCoordinator, *, period: str, key: str
    ) -> None:
        super().__init__(coordinator)
        cfg = self._CONFIG[key]
        self._period = period
        self._key = key
        self._value: float | int | None = None

        slug = f"{cfg['slug']}_{self._PERIOD_SUFFIX[period]}"
        self.entity_description = SensorEntityDescription(
            key=slug,
            translation_key=slug,
            native_unit_of_measurement=(
                cfg.get("unit")
                if key == "kwh"
                else (coordinator.currency if key != "count" else None)
            ),
            device_class=cfg.get("device_class"),
            state_class=cfg.get("state_class"),
            icon=cfg.get("icon"),
            suggested_display_precision=cfg.get("precision"),
        )
        self._attr_unique_id = f"{coordinator.entry_id}_{slug}"

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_refresh, _AGGREGATE_REFRESH)
        )
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, *_: Any) -> None:
        since = period_start(dt_util.now(), self._period)
        aggregates = await self._coordinator.storage.async_charges_aggregates_since(since)
        self._value = aggregates.get(self._key)
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | int | None:
        return self._value
