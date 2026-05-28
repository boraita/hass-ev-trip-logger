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
from homeassistant.core import HomeAssistant
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
    """Live metric while a trip is in progress."""

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
            return None
        return snapshot.get(self._meta.key)

    @property
    def available(self) -> bool:
        return self._coordinator.current is not None


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


class AggregateSensor(_BaseTripSensor):
    """Roll-up sensor: today / week / month / year totals."""

    _PERIODIC_KEYS_UNITS: dict[str, tuple[str | None, SensorDeviceClass | None, str | None]] = {
        "distance_km": (UnitOfLength.KILOMETERS, SensorDeviceClass.DISTANCE, None),
        "energy_kwh": (UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, None),
        "cost": (None, SensorDeviceClass.MONETARY, "mdi:currency-eur"),
        "count": (None, None, "mdi:counter"),
        "avg_consumption_kwh_100km": ("kWh/100km", None, "mdi:car-electric"),
    }

    def __init__(
        self, coordinator: EvTripLoggerCoordinator, *, period: str, key: str
    ) -> None:
        super().__init__(coordinator)
        self._period = period
        self._key = key
        self._value: float | int | None = None

        unit, device_class, icon = self._PERIODIC_KEYS_UNITS[key]
        slug = f"{period}_{key.replace('_kwh_100km', '_consumption')}".replace(
            "_km", ""
        )
        self.entity_description = SensorEntityDescription(
            key=f"total_{slug}",
            translation_key=f"total_{slug}",
            native_unit_of_measurement=(
                unit if key != "cost" else coordinator.currency
            ),
            device_class=device_class,
            state_class=SensorStateClass.TOTAL_INCREASING if key in ("distance_km", "energy_kwh") else SensorStateClass.MEASUREMENT,
            icon=icon,
            suggested_display_precision=0 if key == "count" else 1,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_total_{slug}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_refresh, _AGGREGATE_REFRESH)
        )

    async def _async_refresh(self, *_: Any) -> None:
        since = period_start(dt_util.now(), self._period)
        aggregates = await self._coordinator.storage.async_aggregates_since(since)
        self._value = aggregates.get(self._key)
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | int | None:
        return self._value
