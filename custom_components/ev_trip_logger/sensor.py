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
from homeassistant.helpers.entity import EntityCategory
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
    diagnostic: bool = False,
) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=unit,
        device_class=device_class,
        state_class=state_class,
        icon=icon,
        suggested_display_precision=suggested_precision,
        entity_category=EntityCategory.DIAGNOSTIC if diagnostic else None,
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
            diagnostic=True,
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
            diagnostic=True,
        ),
    ),
    TripSensorMeta(
        key="max_power_kw",
        description=_desc(
            "max_power",
            unit=UnitOfPower.KILO_WATT,
            device_class=SensorDeviceClass.POWER,
            suggested_precision=1,
            diagnostic=True,
        ),
    ),
    TripSensorMeta(
        key="max_speed_kmh",
        description=_desc(
            "max_speed",
            unit=UnitOfSpeed.KILOMETERS_PER_HOUR,
            device_class=SensorDeviceClass.SPEED,
            suggested_precision=0,
            diagnostic=True,
        ),
    ),
    TripSensorMeta(
        key="regen_kwh",
        description=_desc(
            "regen_energy",
            unit=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
            suggested_precision=2,
            diagnostic=True,
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
        entities.append(CurrentTripExtraSensor(coordinator, key=key, cfg=cfg))

    entities.extend(
        [
            AggregateSensor(coordinator, period="today", key="distance_km"),
            AggregateSensor(coordinator, period="week", key="distance_km"),
            AggregateSensor(coordinator, period="month", key="distance_km"),
            AggregateSensor(coordinator, period="year", key="distance_km"),
            AggregateSensor(coordinator, period="today", key="count"),
            AggregateSensor(coordinator, period="month", key="energy_kwh"),
            AggregateSensor(coordinator, period="month", key="cost"),
            AggregateSensor(coordinator, period="month", key="count"),
            AggregateSensor(coordinator, period="30d", key="avg_consumption_kwh_100km"),
        ]
    )

    entities.append(RecentTripsSensor(coordinator))
    entities.append(RecentChargesSensor(coordinator))
    entities.append(TripRecordsSensor(coordinator))
    entities.append(ChargeInProgressSensor(coordinator))
    entities.append(PlugStateSensor(coordinator))
    entities.append(LastJourneySensor(coordinator))
    entities.append(CurrentJourneySensor(coordinator))
    entities.append(RecentJourneysSensor(coordinator))
    entities.append(BatteryEnergySensor(coordinator))
    entities.append(EnergyToFullSensor(coordinator))
    entities.append(BatteryPercentSensor(coordinator))
    entities.append(RangeAtRecentEfficiencySensor(coordinator))
    entities.append(ConsumptionByTempBucketSensor(coordinator))

    # v0.5.0 — dashboard-driven additions
    entities.append(MonthlyHistorySensor(coordinator))
    entities.append(DailyKm60dSensor(coordinator))
    entities.append(TripPatternsSensor(coordinator))
    entities.append(TopsSensor(coordinator))
    for key in ("avg_distance_km", "avg_duration_min", "avg_speed_kmh", "driving_time_min"):
        entities.append(AvgTripMetricsSensor(coordinator, key=key))
    for key in ("avg_kwh", "avg_cost"):
        entities.append(AvgChargeMetricsSensor(coordinator, key=key))

    entities.extend(
        [
            LastChargeSensor(coordinator, key="kwh"),
            LastChargeSensor(coordinator, key="total_cost"),
            LastChargeSensor(coordinator, key="price_per_kwh"),
            LastChargeSensor(coordinator, key="is_dcfc"),
            CurrentChargeSensor(coordinator, key="kwh"),
            CurrentChargeSensor(coordinator, key="total_cost"),
            CurrentChargeSensor(coordinator, key="price_per_kwh"),
            CurrentChargeSensor(coordinator, key="power_kw"),
            CurrentChargeSensor(coordinator, key="duration_min"),
            CurrentChargeSensor(coordinator, key="is_dcfc"),
            ChargesAggregateSensor(coordinator, period="month", key="kwh"),
            ChargesAggregateSensor(coordinator, period="month", key="total_cost"),
            ChargesAggregateSensor(coordinator, period="month", key="count"),
            ChargesAggregateSensor(coordinator, period="30d", key="avg_price_per_kwh"),
            ChargesAggregateSensor(coordinator, period="30d", key="avg_ac_price_per_kwh"),
            ChargesAggregateSensor(coordinator, period="30d", key="avg_dc_price_per_kwh"),
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


class CurrentTripExtraSensor(_BaseTripSensor):
    """Live cost and score of the in-progress trip (mirror of LastTripExtraSensor).

    cost shows 0 €/€ when idle; score is unknown until consumption is computable.
    """

    _IDLE_DEFAULTS: dict[str, float | None] = {"cost": 0.0, "score": None}

    def __init__(
        self, coordinator: EvTripLoggerCoordinator, *, key: str, cfg: dict[str, Any]
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        slug = f"current_trip_{cfg['slug']}"
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
        snapshot = self._coordinator.current_snapshot()
        if snapshot is None:
            return self._IDLE_DEFAULTS.get(self._key)
        return snapshot.get(self._key)


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

    # monetary device_class accepts `total` (with optional last_reset). Using
    # total_increasing on monetary is rejected by HA, but plain `total` works
    # and lets statistics-graph plot monthly bars over time.
    _STATE_CLASS_BY_KEY: dict[str, SensorStateClass | None] = {
        "distance_km": SensorStateClass.TOTAL_INCREASING,
        "energy_kwh": SensorStateClass.TOTAL_INCREASING,
        "cost": SensorStateClass.TOTAL,
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
        # Secondary periods are diagnostic so the device card stays uncluttered.
        is_diagnostic = (period, key) in {
            ("today", "distance_km"),
            ("week", "distance_km"),
            ("year", "distance_km"),
        }
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
            entity_category=EntityCategory.DIAGNOSTIC if is_diagnostic else None,
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


def _r(value: float | None, ndigits: int) -> float | None:
    return round(value, ndigits) if value is not None else None


def _trip_to_attr(trip: Any) -> dict[str, Any]:
    """Serialise a TripRecord for sensor attributes.

    Exposes every field we capture so any Lovelace card / template can use
    them. Keeps both `id` (the new v0.4.3 short alias) and `trip_id`
    (the historical key dashboards have been using since v0.1) for
    backwards compatibility — both point at the same DB primary key.
    """
    return {
        "id": trip.trip_id,
        "trip_id": trip.trip_id,
        "journey_id": trip.journey_id,
        "started_at": trip.started_at.isoformat(),
        "ended_at": trip.ended_at.isoformat(),
        "distance_km": _r(trip.distance_km, 1),
        "duration_min": _r(trip.duration_min, 1),
        "odometer_start": _r(trip.odometer_start, 1),
        "odometer_end": _r(trip.odometer_end, 1),
        "soc_start": _r(trip.soc_start, 1),
        "soc_end": _r(trip.soc_end, 1),
        "soc_used_pct": _r(trip.soc_used_pct, 1),
        "energy_kwh": _r(trip.energy_kwh, 2),
        "consumption_kwh_100km": _r(trip.consumption_kwh_100km, 1),
        "avg_speed_kmh": _r(trip.avg_speed_kmh, 1),
        "max_speed_kmh": _r(trip.max_speed_kmh, 0),
        "max_power_kw": _r(trip.max_power_kw, 1),
        "regen_kwh": _r(trip.regen_kwh, 2),
        "avg_temp_c": _r(trip.avg_temp_c, 1),
        "cost": _r(trip.cost, 2),
        "currency": trip.currency,
        "score": _r(trip.score, 1),
        "origin": trip.origin,
        "destination": trip.destination,
        # GPS endpoints — populated only for trips logged after v0.5.3.
        # Lets the dashboard build a precise Google-Maps route link.
        "start_lat": _r(getattr(trip, "start_lat", None), 6),
        "start_lon": _r(getattr(trip, "start_lon", None), 6),
        "end_lat": _r(getattr(trip, "end_lat", None), 6),
        "end_lon": _r(getattr(trip, "end_lon", None), 6),
        # Reverse-geocoded human-readable labels (Nominatim, v0.5.12+).
        # NULL for older trips and for points inside a named HA zone.
        "start_address": getattr(trip, "start_address", None),
        "end_address": getattr(trip, "end_address", None),
    }


def _charge_to_attr(charge: Any) -> dict[str, Any]:
    """Serialise a ChargeRecord. Same `id`/`charge_id` dual-alias as trips."""
    return {
        "id": charge.charge_id,
        "charge_id": charge.charge_id,
        "started_at": charge.started_at.isoformat() if charge.started_at else None,
        "ended_at": charge.ended_at.isoformat(),
        "kwh": _r(charge.kwh, 2),
        "price_per_kwh": _r(charge.price_per_kwh, 4),
        "total_cost": _r(charge.total_cost, 2),
        "currency": charge.currency,
        "soc_start": _r(charge.soc_start, 1),
        "soc_end": _r(charge.soc_end, 1),
        "location": charge.location,
        "notes": charge.notes,
        "is_dcfc": charge.is_dcfc,
        "price_locked": getattr(charge, "price_locked", False),
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
    """Running journey including the active stage's live mileage.

    Closed stages come from storage (refreshed when a trip ends/deletes).
    The active stage's running km / kWh are overlaid live whenever the
    coordinator notifies of an odometer or battery move, so the card
    keeps climbing during the drive instead of freezing between stops.
    """

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self._closed: dict[str, Any] | None = None
        self.entity_description = SensorEntityDescription(
            key="current_journey",
            translation_key="current_journey",
            icon="mdi:map-marker-distance",
            state_class=SensorStateClass.MEASUREMENT,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_current_journey"

    async def async_added_to_hass(self) -> None:
        await self._async_refresh_closed()
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh_closed)
        )
        # Live overlay updates on every coordinator notify (odo / battery / power).
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )

    @callback
    def _schedule_refresh_closed(self) -> None:
        self.hass.async_create_task(self._async_refresh_closed())

    async def _async_refresh_closed(self) -> None:
        jid = self._coordinator.current_journey_id
        self._closed = (
            await self._coordinator.storage.async_journey_summary(jid)
            if jid is not None
            else None
        )
        self.async_write_ha_state()

    def _compute(self) -> dict[str, Any] | None:
        """Merge closed stages + active stage running data."""
        base = self._closed
        snap = (
            self._coordinator.current_snapshot()
            if self._coordinator.current is not None
            else None
        )
        if base is None and snap is None:
            return None

        distance = (base["distance_km"] if base else 0.0) + (
            snap.get("distance_km") or 0.0 if snap else 0.0
        )
        energy = (base["energy_kwh"] if base else 0.0) + (
            snap.get("energy_kwh") or 0.0 if snap else 0.0
        )
        stages = (base["stages"] if base else 0) + (1 if snap is not None else 0)
        started_at = (
            base["started_at"]
            if base
            else (self._coordinator.current.started_at if snap else None)
        )
        return {
            "journey_id": base["journey_id"] if base else None,
            "started_at": started_at,
            "distance_km": distance,
            "energy_kwh": energy,
            "cost": base["cost"] if base else 0.0,
            "stages": stages,
            "stage_active": snap is not None,
        }

    @property
    def native_value(self) -> int:
        s = self._compute()
        return s["stages"] if s else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        s = self._compute()
        if not s:
            return None
        return {
            "journey_id": s["journey_id"],
            "started_at": s["started_at"].isoformat() if s["started_at"] else None,
            "distance_km": round(s["distance_km"], 1),
            "energy_kwh": round(s["energy_kwh"], 2),
            "cost": round(s["cost"], 2),
            "stage_active": s["stage_active"],
        }


class RecentJourneysSensor(_BaseTripSensor):
    """List of the last N completed journeys for Lovelace cards."""

    # Big JSON blob read live by dashboards; never store it in the recorder
    # (with a large recent window it exceeds the 16 KB per-state attr limit).
    _unrecorded_attributes = frozenset({"journeys"})

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self._journeys: list[dict[str, Any]] = []
        # No state_class: the state is a row count, not a measurement — keeping
        # it off avoids pushing the large JSON attribute into LTS/the recorder.
        self.entity_description = SensorEntityDescription(
            key="recent_journeys",
            translation_key="recent_journeys",
            icon="mdi:map-marker-multiple",
        )
        self._attr_unique_id = f"{coordinator.entry_id}_recent_journeys"

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        self._journeys = await self._coordinator.storage.async_recent_completed_journeys(
            self._coordinator.current_journey_id, self._coordinator.recent_limit
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return len(self._journeys)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "journeys": [
                {
                    "journey_id": j["journey_id"],
                    "started_at": j["started_at"].isoformat() if j["started_at"] else None,
                    "ended_at": j["ended_at"].isoformat() if j["ended_at"] else None,
                    "distance_km": round(j["distance_km"], 1),
                    "energy_kwh": round(j["energy_kwh"], 2),
                    "cost": round(j["cost"], 2),
                    "stages": j["stages"],
                }
                for j in self._journeys
            ],
        }


class PlugStateSensor(_BaseTripSensor):
    """Enum sensor exposing the real cable+charging state.

    Combines the configured plug_sensor with the charge_sensor to produce:
    - `disconnected` — cable not connected
    - `charging`     — cable connected and current flowing
    - `paused`       — cable connected, no current (charge complete / target SoC reached)
    - `unknown`     — sensors unavailable

    Lets the dashboard show "cable still plugged in, paused" rather than the
    misleading "idle" the integration showed before v0.5.10.
    """

    _attr_options = ["charging", "paused", "disconnected", "unknown"]

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="plug_state",
            translation_key="plug_state",
            device_class=SensorDeviceClass.ENUM,
            icon="mdi:ev-plug-type2",
            options=["charging", "paused", "disconnected", "unknown"],
        )
        self._attr_unique_id = f"{coordinator.entry_id}_plug_state"

    async def async_added_to_hass(self) -> None:
        # Refresh whenever the source sensors update.
        from homeassistant.helpers.event import async_track_state_change_event

        @callback
        def _refresh(_event: Any) -> None:
            self.async_write_ha_state()

        sources = [
            s for s in (
                getattr(self._coordinator, "_plug_sensor", None),
                self._coordinator._charge_sensor,
            ) if s
        ]
        if sources:
            self.async_on_remove(
                async_track_state_change_event(self.hass, sources, _refresh)
            )

    @property
    def native_value(self) -> str:
        plug = getattr(self._coordinator, "_plug_sensor", None)
        charge = self._coordinator._charge_sensor
        plug_state = self.hass.states.get(plug) if plug else None
        charge_state = self.hass.states.get(charge) if charge else None
        # Without configured sensors we can't distinguish — say unknown.
        if not plug and not charge:
            return "unknown"
        # Treat the plug as the source of truth for "is the cable connected?"
        # If the user only configured charge_sensor (no plug), fall back to
        # using charge as a proxy: charging=on → charging, off → disconnected.
        if plug_state is not None and plug_state.state in ("on", "off"):
            if plug_state.state == "off":
                return "disconnected"
            # Plug is on — distinguish charging vs paused via charge_sensor
            if charge_state is not None and charge_state.state in ("on", "off"):
                return "charging" if charge_state.state == "on" else "paused"
            return "paused"  # plug on but no charge_sensor → conservative
        # Only charge_sensor configured
        if charge_state is not None and charge_state.state in ("on", "off"):
            return "charging" if charge_state.state == "on" else "disconnected"
        return "unknown"


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

    # Big JSON blob read live by dashboards; never store it in the recorder
    # (with a large recent window it exceeds the 16 KB per-state attr limit).
    _unrecorded_attributes = frozenset({"trips"})

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self._trips: list[Any] = []
        # No state_class: the state is a row count, not a measurement.
        self.entity_description = SensorEntityDescription(
            key="recent_trips",
            translation_key="recent_trips",
            icon="mdi:format-list-bulleted",
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
        self._trips = await self._coordinator.storage.async_recent_trips(
            self._coordinator.recent_limit
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return len(self._trips)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"trips": [_trip_to_attr(t) for t in self._trips]}


class RecentChargesSensor(_BaseTripSensor):
    """List of the most recent charges, exposed via attributes."""

    # Big JSON blob read live by dashboards; never store it in the recorder
    # (with a large recent window it exceeds the 16 KB per-state attr limit).
    _unrecorded_attributes = frozenset({"charges"})

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self._charges: list[Any] = []
        # No state_class: the state is a row count, not a measurement.
        self.entity_description = SensorEntityDescription(
            key="recent_charges",
            translation_key="recent_charges",
            icon="mdi:ev-station",
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
        self._charges = await self._coordinator.storage.async_recent_charges(
            self._coordinator.recent_limit
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return len(self._charges)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"charges": [_charge_to_attr(c) for c in self._charges]}


class TripRecordsSensor(_BaseTripSensor):
    """All-time trip records, computed over the full history in the DB.

    State is the lifetime trip count. Attributes expose the record-holding
    trips so dashboards can show 'best ever' without iterating the recent
    window (which they can't see past). 'best_score' and 'most_efficient'
    point at the same trip because score is a decreasing function of
    consumption — exposed separately so cards can label them independently.
    """

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self._records: dict[str, Any] | None = None
        self.entity_description = SensorEntityDescription(
            key="trip_records",
            translation_key="trip_records",
            icon="mdi:trophy",
        )
        self._attr_unique_id = f"{coordinator.entry_id}_trip_records"

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        self._records = await self._coordinator.storage.async_records()
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return int(self._records["count"]) if self._records else 0

    @staticmethod
    def _entry(trip: Any, value: float | None) -> dict[str, Any] | None:
        if trip is None or value is None:
            return None
        return {
            "value": value,
            "trip_id": trip.trip_id,
            "ended_at": trip.ended_at.isoformat(),
            "distance_km": round(trip.distance_km, 1),
            "destination": trip.destination,
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = self._records
        if not rec:
            return None
        eff = rec.get("most_efficient")
        longest = rec.get("longest")
        cheapest = rec.get("cheapest")
        best_score = self._entry(
            eff, round(eff.score, 1) if eff and eff.score is not None else None
        )
        most_efficient = self._entry(
            eff,
            round(eff.consumption_kwh_100km, 1)
            if eff and eff.consumption_kwh_100km is not None
            else None,
        )
        longest_e = self._entry(
            longest, round(longest.distance_km, 1) if longest else None
        )
        cheapest_e = self._entry(
            cheapest, round(cheapest.cost, 2) if cheapest and cheapest.cost is not None else None
        )
        if cheapest_e is not None:
            cheapest_e["currency"] = cheapest.currency
        return {
            "best_score": best_score,
            "most_efficient": most_efficient,
            "longest": longest_e,
            "cheapest": cheapest_e,
            "totals": rec.get("totals"),
        }


class BatteryPercentSensor(_BaseTripSensor):
    """Mirror of the source battery sensor — gives the device card a battery %."""

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="battery_state",
            translation_key="battery_state",
            native_unit_of_measurement=PERCENTAGE,
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_battery_state"

    @property
    def native_value(self) -> float | None:
        return self._coordinator.battery_level


class BatteryEnergySensor(_BaseTripSensor):
    """kWh currently in the battery: SoC% × battery_capacity / 100."""

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="battery_energy",
            translation_key="battery_energy",
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            # ENERGY_STORAGE (not ENERGY) is the right device_class for "kWh
            # currently held at a point in time" — and it accepts MEASUREMENT.
            device_class=SensorDeviceClass.ENERGY_STORAGE,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:battery-charging",
            suggested_display_precision=1,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_battery_energy"

    @property
    def native_value(self) -> float | None:
        soc = self._coordinator.battery_level
        if soc is None:
            return None
        return round(soc / 100.0 * self._coordinator.battery_capacity, 2)


class EnergyToFullSensor(_BaseTripSensor):
    """kWh needed to reach 100% from current battery level."""

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="energy_to_full",
            translation_key="energy_to_full",
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            # Same rationale as BatteryEnergySensor: this is a point-in-time
            # "capacity headroom", not consumed energy.
            device_class=SensorDeviceClass.ENERGY_STORAGE,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:battery-plus",
            suggested_display_precision=1,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_energy_to_full"

    @property
    def native_value(self) -> float | None:
        soc = self._coordinator.battery_level
        if soc is None:
            return None
        return round(max(0.0, (100.0 - soc) / 100.0 * self._coordinator.battery_capacity), 2)


_CHARGE_FIELD_CONFIG: dict[str, dict[str, Any]] = {
    "kwh": {
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        # state_class=total: HA accepts it with device_class=energy AND stops
        # warning about "no state class" after a prior measurement. Auto-reset
        # detection treats every value replacement as a fresh period — the
        # "change" stat equals each charge's kWh, which is what we want.
        "state_class": SensorStateClass.TOTAL,
        "precision": 2,
        "slug_last": "last_charge_kwh",
        "slug_current": "current_charge_kwh",
    },
    "total_cost": {
        "use_currency": True,
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": None,
        "precision": 2,
        "slug_last": "last_charge_cost",
        "slug_current": "current_charge_cost",
    },
    "price_per_kwh": {
        "use_currency": True,
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": None,
        "precision": 4,
        "icon": "mdi:cash",
        "slug_last": "last_charge_price",
        "slug_current": "current_charge_price",
    },
    "power_kw": {  # current-only — last completed charges don't carry power
        "unit": UnitOfPower.KILO_WATT,
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
        "slug_current": "current_charge_power",
    },
    "duration_min": {  # current-only
        "unit": UnitOfTime.MINUTES,
        "device_class": SensorDeviceClass.DURATION,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 0,
        "slug_current": "current_charge_duration",
    },
    "is_dcfc": {  # enum label so it's readable in cards
        "options": ["AC", "DC", "unknown"],
        "device_class": SensorDeviceClass.ENUM,
        "icon": "mdi:ev-plug-type2",
        "slug_last": "last_charge_type",
        "slug_current": "current_charge_type",
    },
}


def _is_dcfc_label(value: bool | None) -> str:
    if value is True:
        return "DC"
    if value is False:
        return "AC"
    return "unknown"


class LastChargeSensor(_BaseTripSensor):
    """Metric from the most recently logged charge session."""

    def __init__(self, coordinator: EvTripLoggerCoordinator, *, key: str) -> None:
        super().__init__(coordinator)
        cfg = _CHARGE_FIELD_CONFIG[key]
        self._key = key
        slug = cfg["slug_last"]
        self.entity_description = SensorEntityDescription(
            key=slug,
            translation_key=slug,
            native_unit_of_measurement=(
                coordinator.currency if cfg.get("use_currency") else cfg.get("unit")
            ),
            device_class=cfg.get("device_class"),
            state_class=cfg.get("state_class"),
            icon=cfg.get("icon"),
            suggested_display_precision=cfg.get("precision"),
            options=cfg.get("options"),
        )
        self._attr_unique_id = f"{coordinator.entry_id}_{slug}"

    @property
    def native_value(self) -> float | str | None:
        charge = self._coordinator.last_charge
        if charge is None:
            return None
        if self._key == "is_dcfc":
            return _is_dcfc_label(charge.is_dcfc)
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


class CurrentChargeSensor(_BaseTripSensor):
    """Live metrics for an in-progress charging session (mirror of LastChargeSensor)."""

    def __init__(self, coordinator: EvTripLoggerCoordinator, *, key: str) -> None:
        super().__init__(coordinator)
        cfg = _CHARGE_FIELD_CONFIG[key]
        self._key = key
        slug = cfg["slug_current"]
        self.entity_description = SensorEntityDescription(
            key=slug,
            translation_key=slug,
            native_unit_of_measurement=(
                coordinator.currency if cfg.get("use_currency") else cfg.get("unit")
            ),
            device_class=cfg.get("device_class"),
            state_class=cfg.get("state_class"),
            icon=cfg.get("icon"),
            suggested_display_precision=cfg.get("precision"),
            options=cfg.get("options"),
        )
        self._attr_unique_id = f"{coordinator.entry_id}_{slug}"

    @property
    def native_value(self) -> float | str | None:
        snap = self._coordinator.current_charge_snapshot()
        if snap is None:
            # No active charge → show "unknown" for the enum, None for numerics
            # so HA renders "unknown" instead of stale data.
            return _is_dcfc_label(None) if self._key == "is_dcfc" else None
        value = snap.get(self._key)
        if self._key == "is_dcfc":
            return _is_dcfc_label(value)
        return value


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
            "state_class": SensorStateClass.TOTAL,
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
        "avg_ac_price_per_kwh": {
            "device_class": SensorDeviceClass.MONETARY,
            "state_class": None,
            "icon": "mdi:home-lightning-bolt",
            "precision": 4,
            "slug": "avg_ac_charge_price",
        },
        "avg_dc_price_per_kwh": {
            "device_class": SensorDeviceClass.MONETARY,
            "state_class": None,
            "icon": "mdi:ev-station",
            "precision": 4,
            "slug": "avg_dc_charge_price",
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
        # avg_*_price_per_kwh are stats curiosities, not daily checks.
        is_diagnostic = key in (
            "avg_price_per_kwh",
            "avg_ac_price_per_kwh",
            "avg_dc_price_per_kwh",
        )
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
            entity_category=EntityCategory.DIAGNOSTIC if is_diagnostic else None,
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


class RangeAtRecentEfficiencySensor(_BaseTripSensor):
    """Estimated remaining range from current battery energy and 30-day consumption.

    Formula:  range_km = battery_energy_kwh / (avg_consumption_30d / 100)

    More honest than the car dash's WLTP/EPA estimate because it uses *your*
    actual recent driving efficiency, including HVAC and temperature.
    """

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="range_at_recent_efficiency",
            translation_key="range_at_recent_efficiency",
            native_unit_of_measurement=UnitOfLength.KILOMETERS,
            device_class=SensorDeviceClass.DISTANCE,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:map-marker-distance",
            suggested_display_precision=0,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_range_at_recent_efficiency"
        self._value: float | None = None
        self._based_on_kwh_per_100km: float | None = None
        self._sample_count: int = 0

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_refresh, _AGGREGATE_REFRESH)
        )
        self.async_on_remove(
            self._coordinator.async_add_listener(self._on_listener)
        )
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _on_listener(self) -> None:
        # Battery moves → re-derive without hitting storage.
        soc = self._coordinator.battery_level
        if soc is None or self._based_on_kwh_per_100km is None:
            return
        battery_energy = soc / 100.0 * self._coordinator.battery_capacity
        if self._based_on_kwh_per_100km > 0:
            self._value = round(
                battery_energy / (self._based_on_kwh_per_100km / 100.0), 1
            )
            self.async_write_ha_state()

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, *_: Any) -> None:
        since = period_start(dt_util.now(), "30d")
        agg = await self._coordinator.storage.async_aggregates_since(since)
        cons = agg.get("avg_consumption_kwh_100km") or 0.0
        self._sample_count = int(agg.get("count") or 0)
        self._based_on_kwh_per_100km = cons if cons > 0 else None
        self._on_listener()  # write_ha_state via the same path
        if self._value is None:
            self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "based_on_kwh_per_100km": self._based_on_kwh_per_100km,
            "sample_count": self._sample_count,
        }


class ConsumptionByTempBucketSensor(_BaseTripSensor):
    """Consumption (kWh/100km) bucketed by outdoor temperature over the last 90 days.

    State = the consumption for the bucket the *current* outside temperature
    falls into, so the value flips between e.g. "winter consumption" and
    "summer consumption" automatically. The full bucket map is exposed in
    attributes for dashboard charts.
    """

    _BUCKET_SIZE_C = 5.0

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="consumption_by_temp_bucket",
            translation_key="consumption_by_temp_bucket",
            native_unit_of_measurement="kWh/100km",
            icon="mdi:thermometer-lines",
            suggested_display_precision=1,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_consumption_by_temp_bucket"
        self._buckets: dict[str, float] = {}
        self._sample_count: int = 0

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
        since = dt_util.now() - timedelta(days=90)
        result = await self._coordinator.storage.async_consumption_by_temp_bucket(
            since, self._BUCKET_SIZE_C
        )
        self._buckets = result.get("by_bucket", {})
        self._sample_count = int(result.get("sample_count", 0))
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """The kWh/100km of the bucket the current outside temp falls into."""
        if not self._buckets:
            return None
        temp = self._coordinator.exterior_temp
        if temp is None:
            return None
        bucket = int((temp // self._BUCKET_SIZE_C) * self._BUCKET_SIZE_C)
        return self._buckets.get(str(bucket))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "by_bucket": self._buckets,
            "bucket_size_c": self._BUCKET_SIZE_C,
            "sample_count": self._sample_count,
        }


# === v0.5.0: Pantallas 3 (Trends), 4 (Patterns), 6 (Tops), 8 (Trip-list KPIs), 9 (Charges KPIs) ===


class MonthlyHistorySensor(_BaseTripSensor):
    """Per-month rollups for the last 12 months (powers dual-axis bar chart)."""

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="monthly_history",
            translation_key="monthly_history",
            icon="mdi:chart-bar-stacked",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_monthly_history"
        self._months: list[dict[str, Any]] = []

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
        self._months = await self._coordinator.storage.async_monthly_history(12)
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return len(self._months)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"months": self._months}


class DailyKm60dSensor(_BaseTripSensor):
    """Per-day km totals for the last 60 days (powers the line chart)."""

    _WINDOW_DAYS = 60

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="daily_km_60d",
            translation_key="daily_km_60d",
            native_unit_of_measurement=UnitOfLength.KILOMETERS,
            device_class=SensorDeviceClass.DISTANCE,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:chart-line",
            suggested_display_precision=0,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_daily_km_60d"
        self._days: list[dict[str, Any]] = []

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
        self._days = await self._coordinator.storage.async_daily_km_window(
            self._WINDOW_DAYS
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(sum(d.get("distance_km", 0) for d in self._days), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"days": self._days, "window_days": self._WINDOW_DAYS}


class TripPatternsSensor(_BaseTripSensor):
    """Trip distribution by hour-of-day and weekday (powers radar / bar charts)."""

    _WINDOW_DAYS = 90

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="trip_patterns",
            translation_key="trip_patterns",
            icon="mdi:chart-timeline-variant",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_trip_patterns"
        self._patterns: dict[str, Any] = {}

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
        self._patterns = await self._coordinator.storage.async_trip_patterns(
            self._WINDOW_DAYS
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return int(self._patterns.get("sample_count", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "by_hour": self._patterns.get("by_hour", {}),
            "by_weekday": self._patterns.get("by_weekday", {}),
            "km_by_weekday": self._patterns.get("km_by_weekday", {}),
            "window_days": self._WINDOW_DAYS,
        }


class AvgTripMetricsSensor(_BaseTripSensor):
    """Per-trip averages over a window — one instance per metric key."""

    _CONFIG: dict[str, dict[str, Any]] = {
        "avg_distance_km": {
            "unit": UnitOfLength.KILOMETERS,
            "device_class": SensorDeviceClass.DISTANCE,
            "icon": "mdi:map-marker-distance",
            "slug": "avg_trip_distance",
            "precision": 1,
        },
        "avg_duration_min": {
            "unit": UnitOfTime.MINUTES,
            "device_class": SensorDeviceClass.DURATION,
            "icon": "mdi:timer-outline",
            "slug": "avg_trip_duration",
            "precision": 0,
        },
        "avg_speed_kmh": {
            "unit": UnitOfSpeed.KILOMETERS_PER_HOUR,
            "device_class": SensorDeviceClass.SPEED,
            "icon": "mdi:speedometer",
            "slug": "avg_trip_speed",
            "precision": 1,
        },
        "driving_time_min": {
            "unit": UnitOfTime.MINUTES,
            "device_class": SensorDeviceClass.DURATION,
            "icon": "mdi:steering",
            "slug": "driving_time_30d",
            "precision": 0,
        },
    }

    _WINDOW_DAYS = 30

    def __init__(self, coordinator: EvTripLoggerCoordinator, *, key: str) -> None:
        super().__init__(coordinator)
        cfg = self._CONFIG[key]
        self._key = key
        slug = cfg["slug"]
        self.entity_description = SensorEntityDescription(
            key=slug,
            translation_key=slug,
            native_unit_of_measurement=cfg.get("unit"),
            device_class=cfg.get("device_class"),
            state_class=SensorStateClass.MEASUREMENT,
            icon=cfg.get("icon"),
            suggested_display_precision=cfg.get("precision"),
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_{slug}"
        self._value: float | None = None
        self._count: int = 0

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
        since = dt_util.now() - timedelta(days=self._WINDOW_DAYS)
        m = await self._coordinator.storage.async_avg_trip_metrics(since)
        self._value = m.get(self._key)
        self._count = int(m.get("count", 0))
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"sample_count": self._count, "window_days": self._WINDOW_DAYS}


class TopsSensor(_BaseTripSensor):
    """Top-N trips per criterion (powers the Rankings screen)."""

    _LIMIT = 9

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="tops",
            translation_key="tops",
            icon="mdi:trophy-variant",
        )
        self._attr_unique_id = f"{coordinator.entry_id}_tops"
        self._tops: dict[str, list[dict[str, Any]]] = {}

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
        self._tops = await self._coordinator.storage.async_tops_lists(self._LIMIT)
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return sum(len(v) for v in self._tops.values())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**self._tops, "limit": self._LIMIT}


class AvgChargeMetricsSensor(_BaseTripSensor):
    """Average per-session charge metric (kWh or cost) for the last 30 days."""

    _CONFIG: dict[str, dict[str, Any]] = {
        "avg_kwh": {
            "unit": UnitOfEnergy.KILO_WATT_HOUR,
            "device_class": SensorDeviceClass.ENERGY,
            "icon": "mdi:battery-charging",
            "slug": "avg_charge_kwh_30d",
            "precision": 2,
        },
        "avg_cost": {
            "use_currency": True,
            "device_class": SensorDeviceClass.MONETARY,
            "icon": "mdi:cash-multiple",
            "slug": "avg_charge_cost_30d",
            "precision": 2,
        },
    }

    _WINDOW_DAYS = 30

    def __init__(self, coordinator: EvTripLoggerCoordinator, *, key: str) -> None:
        super().__init__(coordinator)
        cfg = self._CONFIG[key]
        self._key = key
        slug = cfg["slug"]
        self.entity_description = SensorEntityDescription(
            key=slug,
            translation_key=slug,
            native_unit_of_measurement=(
                coordinator.currency if cfg.get("use_currency") else cfg.get("unit")
            ),
            device_class=cfg.get("device_class"),
            state_class=SensorStateClass.MEASUREMENT,
            icon=cfg.get("icon"),
            suggested_display_precision=cfg.get("precision"),
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_{slug}"
        self._value: float | None = None
        self._count: int = 0

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
        since = dt_util.now() - timedelta(days=self._WINDOW_DAYS)
        m = await self._coordinator.storage.async_avg_charge_metrics(since)
        self._value = m.get(self._key)
        self._count = int(m.get("count", 0))
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"sample_count": self._count, "window_days": self._WINDOW_DAYS}
