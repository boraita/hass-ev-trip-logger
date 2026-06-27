"""Sensors exposed by EV Trip Logger."""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
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
from homeassistant.helpers.event import async_call_later, async_track_time_interval
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
            # v0.5.28 — total regen aggregated per period.
            AggregateSensor(coordinator, period="today", key="regen_kwh"),
            AggregateSensor(coordinator, period="week", key="regen_kwh"),
            AggregateSensor(coordinator, period="month", key="regen_kwh"),
            AggregateSensor(coordinator, period="30d", key="regen_kwh"),
            AggregateSensor(coordinator, period="year", key="regen_kwh"),
            # v0.6.0 — gross discharge as a first-class metric (OVMS
            # pattern). Pairs with regen_kwh so dashboards can render
            # "energy out vs energy back" without losing the net number
            # in `energy_kwh`. Plus a derived regen_ratio so the
            # dashboard doesn't have to do the division client-side.
            AggregateSensor(coordinator, period="month", key="discharge_kwh"),
            AggregateSensor(coordinator, period="30d", key="discharge_kwh"),
            AggregateSensor(coordinator, period="year", key="discharge_kwh"),
            AggregateSensor(coordinator, period="month", key="regen_ratio"),
            AggregateSensor(coordinator, period="30d", key="regen_ratio"),
        ]
    )

    entities.append(RecentTripsSensor(coordinator))
    entities.append(RecentChargesSensor(coordinator))
    entities.append(LastTripRouteSensor(coordinator))
    if coordinator._abrp is not None:
        entities.append(AbrpNextChargeSocSensor(coordinator))
    # v0.5.38 — two rolling-average sensors per tracked entity.
    for eid in coordinator._tracked_sensors:
        entities.append(TrackedAvgSensor(coordinator, eid, days=7))
        entities.append(TrackedAvgSensor(coordinator, eid, days=30))
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
    # v0.5.54 — additional bucket dimensions + SoH.
    entities.append(ConsumptionBySeasonSensor(coordinator))
    entities.append(ConsumptionByTimeOfDaySensor(coordinator))
    entities.append(BatterySohSensor(coordinator))
    # v0.5.84 — measured-degradation proxy from per-trip power-vs-SoC
    # cross-validation. Independent signal vs the chemistry-model SoH.
    entities.append(BatteryCalibrationFactorSensor(coordinator))
    # v0.5.90 — rolling-median AC→DC efficiency from EVSE-side energy.
    entities.append(AvgChargingEfficiencySensor(coordinator))
    # v0.5.57 — expected SoH model based on age/km/chemistry/climate.
    entities.append(ExpectedBatterySohSensor(coordinator))
    entities.append(BatteryHealthVsExpectedSensor(coordinator))
    # v0.5.43 — driver identity. Stats work off the DB column (fillable
    # via set_trip even without a live sensor); the live sensor only
    # makes sense when a driver sensor is wired.
    entities.append(DriverStatsSensor(coordinator))
    if coordinator._driver_sensor:
        entities.append(CurrentDriverSensor(coordinator))

    # v0.5.0 — dashboard-driven additions
    entities.append(MonthlyHistorySensor(coordinator))
    entities.append(DailyKm60dSensor(coordinator))
    entities.append(TripPatternsSensor(coordinator))
    entities.append(TopsSensor(coordinator))
    for key in (
        "avg_distance_km",
        "avg_duration_min",
        "avg_speed_kmh",
        "avg_regen_kwh",
        "driving_time_min",
    ):
        entities.append(AvgTripMetricsSensor(coordinator, key=key))
    for key in ("avg_kwh", "avg_cost", "avg_soc_start", "avg_soc_end", "avg_soc_added"):
        entities.append(AvgChargeMetricsSensor(coordinator, key=key))

    entities.extend(
        [
            LastChargeSensor(coordinator, key="kwh"),
            LastChargeSensor(coordinator, key="total_cost"),
            LastChargeSensor(coordinator, key="price_per_kwh"),
            LastChargeSensor(coordinator, key="is_dcfc"),
            # v0.5.90 — AC-side energy + AC→DC efficiency.
            LastChargeSensor(coordinator, key="evse_energy_kwh"),
            LastChargeSensor(coordinator, key="charging_efficiency_pct"),
            # v0.6.0 — peak charging power per session, drives DCFC
            # stress accounting.
            LastChargeSensor(coordinator, key="peak_charge_power_kw"),
            CurrentChargeSensor(coordinator, key="kwh"),
            CurrentChargeSensor(coordinator, key="total_cost"),
            CurrentChargeSensor(coordinator, key="price_per_kwh"),
            CurrentChargeSensor(coordinator, key="power_kw"),
            CurrentChargeSensor(coordinator, key="duration_min"),
            CurrentChargeSensor(coordinator, key="is_dcfc"),
            # v0.5.92 — also expose current-charge EVSE energy + efficiency.
            CurrentChargeSensor(coordinator, key="evse_energy_kwh"),
            CurrentChargeSensor(coordinator, key="charging_efficiency_pct"),
            ChargesAggregateSensor(coordinator, period="month", key="kwh"),
            ChargesAggregateSensor(coordinator, period="month", key="total_cost"),
            ChargesAggregateSensor(coordinator, period="month", key="count"),
            ChargesAggregateSensor(coordinator, period="30d", key="avg_price_per_kwh"),
            ChargesAggregateSensor(coordinator, period="30d", key="avg_ac_price_per_kwh"),
            ChargesAggregateSensor(coordinator, period="30d", key="avg_dc_price_per_kwh"),
            # v0.5.101 — period-bound charging efficiency. Uses the
            # paired SUM(kwh)/SUM(evse) over rows with EVSE data, so
            # the result is always 0-100 %. Replaces the dashboard
            # template that mixed `energy_charged_this_month`
            # (all charges) with `sum(evse)` (only EVSE charges) and
            # produced 700-800 % numbers.
            ChargesAggregateSensor(
                coordinator, period="month", key="charging_efficiency_pct",
            ),
            ChargesAggregateSensor(
                coordinator, period="year", key="charging_efficiency_pct",
            ),
            # v0.6.1 — grid-side energy (OVMS-style first-class pair).
            ChargesAggregateSensor(coordinator, period="month", key="evse_kwh"),
            ChargesAggregateSensor(coordinator, period="year", key="evse_kwh"),
            ChargesAggregateSensor(coordinator, period="lifetime", key="evse_kwh"),
            # v0.6.1 — lifetime battery-side accumulator (HA Energy
            # dashboard contribution; TOTAL_INCREASING + energy
            # device_class).
            ChargesAggregateSensor(coordinator, period="lifetime", key="kwh"),
            # v0.6.1 — DCFC stress signals at period + lifetime scope.
            ChargesAggregateSensor(coordinator, period="month", key="high_power_kwh"),
            ChargesAggregateSensor(coordinator, period="year", key="high_power_kwh"),
            ChargesAggregateSensor(coordinator, period="lifetime", key="high_power_kwh"),
            ChargesAggregateSensor(coordinator, period="month", key="high_power_count"),
            ChargesAggregateSensor(coordinator, period="lifetime", key="high_power_count"),
            ChargesAggregateSensor(coordinator, period="lifetime", key="peak_power_max_kw"),
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

    # v0.5.62/74 — only the avg-of-samples ratio (`avg_temp_c`) stays
    # None in idle... almost. v0.5.74 returns the live exterior-temp
    # reading instead (see `native_value`), so even with no trip in
    # progress the user sees the current temperature, not `unknown`.
    # Speed and consumption returning 0 when no trip is in progress
    # reads cleanly on dashboards ("not driving → no consumption"),
    # avoids the `unknown` warning, and is mathematically defensible.
    _RATIO_KEYS: set[str] = set()

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
            # v0.5.74 — `current_trip_avg_temperature` in idle: instead
            # of `unknown`, show the live exterior-temp reading. The
            # tile then doubles as "outside temperature now" when the
            # car is parked, which is what the dashboard wants anyway.
            if self._meta.key == "avg_temp_c":
                return self._coordinator._read_float(
                    self._coordinator._temp
                ) if self._coordinator._temp else None
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

    # v0.5.63 — fields that ONLY get populated on `live` trips
    # (BYD's cloud-poll lag means most trips end up `reconstructed`).
    # When the most recent trip is reconstructed, these are None →
    # show 0.0 so the dashboard reads "0 kW" / "0 kWh" / "0 km/h"
    # instead of `unknown`.
    _ZERO_WHEN_MISSING_KEYS = frozenset({
        "max_power_kw", "max_speed_kmh", "regen_kwh",
    })

    @property
    def native_value(self) -> float | None:
        trip = self._coordinator.last_trip
        if trip is None:
            return None
        value = getattr(trip, self._meta.key, None)
        # v0.5.68 — legacy fallback for old trips logged with weather
        # entity (pre-v0.5.68). New trips populate `avg_temp_c` only;
        # if it's None, fall back to `ambient_temp_c` for trips that
        # already have it in storage.
        if value is None and self._meta.key == "avg_temp_c":
            return getattr(trip, "ambient_temp_c", None)
        # v0.5.63 — reconstructed-trip fallback (max_power / max_speed /
        # regen all live-path-only).
        if value is None and self._meta.key in self._ZERO_WHEN_MISSING_KEYS:
            return 0.0
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        trip = self._coordinator.last_trip
        if trip is None:
            return None
        return {
            "started_at": trip.started_at.isoformat(),
            "ended_at": trip.ended_at.isoformat(),
            "origin": _humanize_location(
                trip.origin, getattr(trip, "start_address", None)
            ),
            "destination": _humanize_location(
                trip.destination, getattr(trip, "end_address", None)
            ),
            "origin_raw": trip.origin,
            "destination_raw": trip.destination,
            "driver": getattr(trip, "driver", None),
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
        if self._key == "score":
            # v0.5.50 — anchor to the per-car baseline rather than the
            # original 14.5 default. `score` on the record is kept for
            # back-compat and uses 14.5 — we bypass it here.
            return trip.score_with_baseline(
                self._coordinator.score_baseline_kwh_100km
            )
        return getattr(trip, self._key, None)


class CurrentTripExtraSensor(_BaseTripSensor):
    """Live cost and score of the in-progress trip (mirror of LastTripExtraSensor).

    cost shows 0 €/€ when idle; score is unknown until consumption is computable.
    """

    # v0.5.78 — idle defaults: when no trip is in progress we previously
    # showed `unknown` for `score` (because it's a ratio that's only
    # meaningful with consumption data). Surface the LAST trip's score
    # instead — dashboards stay readable, and the value is the most
    # recent thing the user actually drove, which is what they want to
    # see at a glance. Resolved lazily in `native_value` so the cache
    # stays warm.
    _IDLE_DEFAULTS: dict[str, float | None] = {"cost": 0.0}

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
            # v0.5.78 — `score` idle: surface the LAST trip's score
            # instead of `unknown`. Same idea Tesla / BYD apps use:
            # the tile keeps showing the latest meaningful number when
            # the car is parked, not a blank.
            if self._key == "score":
                last = self._coordinator.last_trip
                if last is not None and last.score is not None:
                    return round(last.score, 1)
                return None
            return self._IDLE_DEFAULTS.get(self._key)
        return snapshot.get(self._key)


class AggregateSensor(_BaseTripSensor):
    """Roll-up sensor: today / week / month / year totals."""

    _PERIODIC_KEYS_UNITS: dict[str, tuple[str | None, SensorDeviceClass | None, str | None]] = {
        "distance_km": (UnitOfLength.KILOMETERS, SensorDeviceClass.DISTANCE, None),
        "energy_kwh": (UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, None),
        "regen_kwh": (UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, "mdi:battery-charging"),
        # v0.6.0 — gross discharge alongside regen, OVMS-style split.
        "discharge_kwh": (UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, "mdi:battery-arrow-down"),
        # v0.6.0 — recovered / discharged ratio, 0-1. MEASUREMENT
        # state-class (it's a ratio, not a cumulative).
        "regen_ratio": (PERCENTAGE, None, "mdi:recycle"),
        "cost": (None, SensorDeviceClass.MONETARY, "mdi:currency-eur"),
        "count": (None, None, "mdi:counter"),
        "avg_consumption_kwh_100km": ("kWh/100km", None, "mdi:car-electric"),
    }

    _SLUG_BY_KEY: dict[str, str] = {
        "distance_km": "distance",
        "energy_kwh": "energy",
        "regen_kwh": "regen",
        "discharge_kwh": "discharge",
        "regen_ratio": "regen_ratio",
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
        "regen_kwh": SensorStateClass.TOTAL_INCREASING,
        "discharge_kwh": SensorStateClass.TOTAL_INCREASING,
        "regen_ratio": SensorStateClass.MEASUREMENT,
        "cost": SensorStateClass.TOTAL,
        # v0.5.43 — TOTAL_INCREASING (was MEASUREMENT): the count climbs
        # within the period and resets at the boundary, which is exactly
        # the reset semantics LTS understands. Lets statistics-graph
        # draw monthly trip-count bars (CONTRACT.md §3b).
        "count": SensorStateClass.TOTAL_INCREASING,
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
        raw = aggregates.get(self._key)
        # v0.6.0 — regen_ratio comes back as 0-1 from storage; surface
        # as a 0-100 percentage to match PERCENTAGE unit + the way the
        # dashboard wants to render it.
        if self._key == "regen_ratio" and isinstance(raw, (int, float)):
            raw = round(float(raw) * 100.0, 1)
        self._value = raw
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | int | None:
        return self._value


def _r(value: float | None, ndigits: int) -> float | None:
    return round(value, ndigits) if value is not None else None


def _humanize_location(
    raw: str | None, address: str | None
) -> str | None:
    """Turn device_tracker state + geocoded address into a friendly label.

    Priority: a real zone name (anything that isn't `not_home`/`unknown`)
    wins, else the geocoded address, else a Spanish fallback. This means
    a trip ending in a named zone ("home", "Trabajo") shows the zone,
    while a trip ending outside any zone shows the street/town.
    """
    NONZONE = {"not_home", "unknown", "unavailable", "none", ""}
    raw_clean = (raw or "").strip()
    if raw_clean and raw_clean.casefold() not in NONZONE:
        return raw_clean
    if address:
        return address
    return "Outside known zones"


def _trip_to_attr(
    trip: Any, *, score_baseline: float = 14.5
) -> dict[str, Any]:
    """Serialise a TripRecord for sensor attributes.

    Exposes every field we capture so any Lovelace card / template can use
    them. Keeps both `id` (the new v0.4.3 short alias) and `trip_id`
    (the historical key dashboards have been using since v0.1) for
    backwards compatibility — both point at the same DB primary key.

    v0.5.50 — `score_baseline` is the kWh/100km anchor for 10/10. Pass
    `coordinator.score_baseline_kwh_100km` for the per-car-calibrated
    value; falls back to the historical 14.5 default to keep this
    function callable without a coordinator (e.g. tests).
    """
    start_addr = getattr(trip, "start_address", None)
    end_addr = getattr(trip, "end_address", None)
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
        # v0.6.0 — paired with regen_kwh: gross energy out of the
        # battery before regen recovery. Dashboards can compute
        # `regen_kwh / discharge_kwh` for a per-trip "energy recovered"
        # ratio.
        "discharge_kwh": _r(getattr(trip, "discharge_kwh", None), 2),
        "avg_temp_c": _r(trip.avg_temp_c, 1),
        "cost": _r(trip.cost, 2),
        "currency": trip.currency,
        "score": _r(trip.score_with_baseline(score_baseline), 1),
        # v0.5.19 — origin/destination now show the geocoded address
        # when the raw device_tracker state was `not_home` (= outside
        # any HA zone). The raw value is preserved as `origin_raw` /
        # `destination_raw` for backwards compatibility and journey
        # logic. Dashboards that already read `origin` get street+town
        # instead of "not_home" without any YAML changes.
        "origin": _humanize_location(trip.origin, start_addr),
        "destination": _humanize_location(trip.destination, end_addr),
        "origin_raw": trip.origin,
        "destination_raw": trip.destination,
        # GPS endpoints — populated only for trips logged after v0.5.3.
        # Lets the dashboard build a precise Google-Maps route link.
        "start_lat": _r(getattr(trip, "start_lat", None), 6),
        "start_lon": _r(getattr(trip, "start_lon", None), 6),
        "end_lat": _r(getattr(trip, "end_lat", None), 6),
        "end_lon": _r(getattr(trip, "end_lon", None), 6),
        # Reverse-geocoded human-readable labels (Nominatim, v0.5.12+).
        # NULL for older trips and for points inside a named HA zone.
        "start_address": start_addr,
        "end_address": end_addr,
        # v0.5.13+ provenance — see storage.py header. The dashboard
        # can surface these as a small badge so the user knows whether
        # consumption was anchored on charge-end SoC (most accurate),
        # a fresh pre-on sample, the legacy cached value, or the
        # independent power-integration estimator.
        "soc_start_source": getattr(trip, "soc_start_source", None),
        "energy_source": getattr(trip, "energy_source", None),
        "energy_from_power": _r(getattr(trip, "energy_from_power", None), 2),
        # v0.5.26 — distance recomputed from the GPS route via
        # haversine. May differ slightly from `distance_km` (odo-
        # derived); when both exist the dashboard can show both.
        "gps_distance_km": _r(getattr(trip, "gps_distance_km", None), 1),
        # v0.5.27 — energy added BEFORE this trip (between the previous
        # trip's end and this trip's start) and DURING this trip's
        # window. Lets dashboards show "+24 kWh entre trips" and helps
        # interpret SoC deltas when a charge happened in between.
        "kwh_charged_before": _r(getattr(trip, "kwh_charged_before", None), 2),
        "kwh_charged_during": _r(getattr(trip, "kwh_charged_during", None), 2),
        # v0.5.35 — detection-quality tag: 'live' | 'reconstructed' |
        # 'reconstructed_polling_paused'. Dashboards can color rows
        # accordingly.
        "confidence": getattr(trip, "confidence", None),
        # v0.5.43 — who drove (state of the configured driver sensor,
        # e.g. the BT-connected phone). NULL when unidentified.
        "driver": getattr(trip, "driver", None),
        # v0.5.76 — weighted-avg €/kWh after FIFO replay.
        "cost_basis_per_kwh": _r(
            getattr(trip, "cost_basis_per_kwh", None), 3
        ),
        # v0.5.84 — per-trip battery-capacity calibration factor K.
        "calibration_factor_k": _r(
            getattr(trip, "calibration_factor_k", None), 3
        ),
        # v0.5.86 — 95 % CI band on consumption + low-confidence flag.
        # Dashboards can render "16.5 ± 8" instead of just "16.5" so
        # quantization noise on short trips is visible at a glance.
        "consumption_lower_kwh_100km": _r(
            getattr(trip, "consumption_lower_kwh_100km", None), 1
        ),
        "consumption_upper_kwh_100km": _r(
            getattr(trip, "consumption_upper_kwh_100km", None), 1
        ),
        "low_confidence": getattr(trip, "low_confidence", None),
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
        # v0.5.90 — AC-side metered energy + AC→DC efficiency.
        "evse_energy_kwh": _r(
            getattr(charge, "evse_energy_kwh", None), 2
        ),
        "charging_efficiency_pct": _r(
            getattr(charge, "charging_efficiency_pct", None), 1
        ),
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
        # v0.5.78 — no journey has closed yet (fresh install or
        # journey state hasn't resolved). Show 0 instead of `unknown`;
        # it's a count, and zero is the literal truth.
        return self._summary["stages"] if self._summary else 0

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
        # v0.5.78 — without configured sensors we can't observe the
        # cable. Default to `disconnected` (the most common reality:
        # the car isn't plugged in). Better than `unknown` which
        # surfaces as a stuck warning in dashboards.
        if not plug and not charge:
            return "disconnected"
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
        # v0.5.78 — source sensor is unavailable (Tesla integration
        # asleep, BYD cloud poll paused). Show `disconnected` instead
        # of `unknown` so dashboards stop flagging it. The real plug
        # state will recover automatically when the upstream wakes.
        return "disconnected"


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
        baseline = self._coordinator.score_baseline_kwh_100km
        return {
            "trips": [_trip_to_attr(t, score_baseline=baseline) for t in self._trips],
            # v0.5.50 — expose the active calibration so dashboards can
            # show a "calibrated for THIS car" caption next to the score.
            "score_baseline_kwh_100km": round(baseline, 2),
            "score_baseline_trip_count": self._coordinator.score_baseline_trip_count,
            # v0.5.51 — visibility into the capacity calibration. The
            # `effective_battery_capacity_kwh` is what every SoC→kWh
            # conversion now uses; `n` lets the dashboard caption
            # "calibrated from N real charges" vs the declared spec.
            "effective_battery_capacity_kwh": round(
                self._coordinator.battery_capacity, 2
            ),
            "battery_capacity_calibration_charges": (
                self._coordinator._battery_capacity_calibration_n
            ),
            # v0.5.71 — expose the resolved exterior-temp sensor + its
            # current reading. Lets the user see whether the auto-detect
            # (v0.5.69+) wired anything for them without digging through
            # logs. None means: CONF_TEMP empty AND auto-detect found
            # nothing.
            "exterior_temp_sensor_entity": self._coordinator._temp,
            "exterior_temp_c_now": (
                self._coordinator._read_float(self._coordinator._temp)
                if self._coordinator._temp else None
            ),
        }


class AbrpNextChargeSocSensor(_BaseTripSensor):
    """Target SoC of the next charge stop, read from ABRP's tlm/get_next_charge.

    Only meaningful while a route is active in ABRP. State is the
    target SoC (%) the route planner expects; None when there's no
    active route. Refreshed every ABRP_NEXT_CHARGE_REFRESH_S — we
    intentionally don't piggyback on the per-metric push so this small
    GET stays cheap and predictable.
    """

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="abrp_next_charge_soc",
            translation_key="abrp_next_charge_soc",
            native_unit_of_measurement=PERCENTAGE,
            device_class=SensorDeviceClass.BATTERY,
            icon="mdi:map-marker-radius",
        )
        self._attr_unique_id = f"{coordinator.entry_id}_abrp_next_charge_soc"
        self._value: int | None = None

    async def async_added_to_hass(self) -> None:
        # Imported lazily to avoid a hard dependency at module import.
        from .const import ABRP_NEXT_CHARGE_REFRESH_S  # noqa: PLC0415
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_refresh,
                timedelta(seconds=ABRP_NEXT_CHARGE_REFRESH_S),
            )
        )

    async def _async_refresh(self, *_: Any) -> None:
        client = self._coordinator._abrp
        if client is None:
            return
        try:
            self._value = await client.refresh_next_charge()
        except Exception:  # pragma: no cover — defensive
            return
        self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        return self._value


class TrackedAvgSensor(_BaseTripSensor):
    """Rolling N-day mean of an arbitrary numeric sensor via HA recorder.

    Configured via CONF_TRACKED_SENSORS. Each tracked entity gets two
    of these — 7-day and 30-day. The state is the arithmetic mean of
    every numeric sample the recorder has in the window; non-numeric
    states (unknown/unavailable/strings) are dropped. Attributes
    expose the sample count + window-edge timestamps so the dashboard
    can show "based on N readings since …".
    """

    _unrecorded_attributes = frozenset({"samples"})

    def __init__(
        self,
        coordinator: EvTripLoggerCoordinator,
        source_entity: str,
        *,
        days: int,
    ) -> None:
        super().__init__(coordinator)
        self._source = source_entity
        self._days = days
        # Slug = "<source-suffix>_avg_<N>d" so the resulting entity_id
        # is sensor.<device>_<source-suffix>_avg_<N>d.
        # source_entity looks like "sensor.byd_sealion_7_today_s_energy_consumption"
        # → we strip the "sensor." prefix and any device prefix that
        # matches the coordinator's entry title (lowercased). For
        # foreign devices we keep the full slug.
        src_slug = source_entity.split(".", 1)[-1]
        device_prefix = (coordinator.entry.title or "").lower().replace(" ", "_")
        if device_prefix and src_slug.startswith(device_prefix + "_"):
            src_slug = src_slug[len(device_prefix) + 1:]
        slug = f"{src_slug}_avg_{days}d"
        self.entity_description = SensorEntityDescription(
            key=slug,
            translation_key=None,  # no translation file entry
            icon="mdi:chart-line-variant",
            state_class=SensorStateClass.MEASUREMENT,
        )
        self._attr_name = f"{src_slug.replace('_', ' ').title()} avg {days}d"
        self._attr_has_entity_name = False  # use _attr_name verbatim
        self._attr_unique_id = (
            f"{coordinator.entry_id}_{src_slug}_avg_{days}d"
        )
        self._mean: float | None = None
        self._samples: int = 0
        self._window_start: datetime | None = None
        # v0.5.48 — last KNOWN unit of the source. Mirroring the source's
        # live attributes made the unit flip to None while the upstream
        # integration was reloading, which the recorder registered as a
        # units-changed statistics issue (one Repair per sensor).
        self._unit_cache: str | None = None
        # v0.5.48 — startup retry budget: the first refresh often runs
        # before the recorder is ready; without a fast retry the sensor
        # sat 'unknown' until the next slow cadence tick (30 min).
        self._startup_retries_left: int = 5

    async def async_added_to_hass(self) -> None:
        from .const import TRACKED_AVG_REFRESH_S  # noqa: PLC0415
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_refresh,
                timedelta(seconds=TRACKED_AVG_REFRESH_S),
            )
        )

    @callback
    def _schedule_startup_retry(self) -> None:
        """One-shot fast retry while we still have no value (startup)."""
        if self._mean is not None or self._startup_retries_left <= 0:
            return
        self._startup_retries_left -= 1

        async def _retry(_now: Any) -> None:
            await self._async_refresh()

        self.async_on_remove(
            async_call_later(self.hass, 60, _retry)
        )

    async def _async_refresh(self, *_: Any) -> None:
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.history import (  # noqa: PLC0415
                state_changes_during_period,
            )
        except Exception:
            return
        end = dt_util.now()
        start = end - timedelta(days=self._days)
        self._window_start = start
        try:
            recorder = get_instance(self.hass)
            result = await recorder.async_add_executor_job(
                state_changes_during_period,
                self.hass, start, end, self._source,
            )
        except Exception as exc:
            _LOGGER.debug("TrackedAvg %s: recorder query failed: %s",
                          self._source, exc)
            self._schedule_startup_retry()
            return
        states = result.get(self._source, []) if isinstance(result, dict) else []
        values: list[float] = []
        for s in states:
            try:
                v = float(s.state)
            except (TypeError, ValueError):
                continue
            values.append(v)
        if values:
            self._mean = sum(values) / len(values)
            self._samples = len(values)
        else:
            self._mean = None
            self._samples = 0
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        if self._mean is None:
            return None
        return round(self._mean, 2)

    @property
    def native_unit_of_measurement(self) -> str | None:
        # v0.5.48 — sticky unit: adopt the source's unit when readable
        # and KEEP it across upstream unavailability blips. A unit that
        # flips to None and back makes the recorder open a units-changed
        # Repair for the long-term statistics.
        state = self.hass.states.get(self._source)
        if state is not None:
            unit = state.attributes.get("unit_of_measurement")
            if unit:
                self._unit_cache = unit
        return self._unit_cache

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source_entity": self._source,
            "window_days": self._days,
            "samples": self._samples,
            "window_start": self._window_start.isoformat()
                if self._window_start else None,
        }


class LastTripRouteSensor(_BaseTripSensor):
    """Route waypoints (lat/lon timeline) of the most recent completed trip.

    State is the number of waypoints; the full list lives in the
    `points` attribute as [{ts, lat, lon, index}, …]. Downsampled to
    `_MAX_POINTS` to keep the attribute under HA's 16 KB recorder cap
    when state changes are written. Recorder exclusion below also
    prevents the heavy blob from being persisted.
    """

    _unrecorded_attributes = frozenset({"points"})
    _MAX_POINTS = 30

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="last_trip_route",
            translation_key="last_trip_route",
            icon="mdi:map-marker-path",
        )
        self._attr_unique_id = f"{coordinator.entry_id}_last_trip_route"
        self._points: list[dict[str, Any]] = []

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_refresh()
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._listener)
        )

    @callback
    def _listener(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        trip = self._coordinator.last_trip
        if trip is None or trip.trip_id is None:
            self._points = []
        else:
            raw = await self._coordinator.storage.async_trip_positions(trip.trip_id)
            # Downsample evenly to _MAX_POINTS so a long trip doesn't
            # blow up the attribute size. Keep first and last always.
            if len(raw) > self._MAX_POINTS:
                step = len(raw) / (self._MAX_POINTS - 1)
                idxs = sorted({int(i * step) for i in range(self._MAX_POINTS - 1)} | {len(raw) - 1})
                raw = [raw[i] for i in idxs if i < len(raw)]
            self._points = [
                {
                    "index": i + 1,
                    "ts": p.get("ts"),
                    "lat": round(float(p["lat"]), 6),
                    "lon": round(float(p["lon"]), 6),
                }
                for i, p in enumerate(raw)
            ]
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return len(self._points)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"points": self._points}


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

    # v0.5.16 — dict-valued attributes re-emit on every refresh; without
    # these exclusions the recorder serialises kilobytes of state per
    # tick and HA logs "State attributes exceeded maximum size" warnings.
    _unrecorded_attributes = frozenset({
        "most_efficient", "longest", "cheapest", "totals", "best_score",
    })

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
            "destination": _humanize_location(
                trip.destination, getattr(trip, "end_address", None)
            ),
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = self._records
        if not rec:
            return None
        eff = rec.get("most_efficient")
        longest = rec.get("longest")
        cheapest = rec.get("cheapest")
        # v0.5.50 — score against the per-car calibrated baseline.
        baseline = self._coordinator.score_baseline_kwh_100km
        best = eff.score_with_baseline(baseline) if eff is not None else None
        best_score = self._entry(eff, round(best, 1) if best is not None else None)
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
        # v0.5.78 — replaced `unknown` with `idle` so the enum stays
        # valid when no charge has been logged yet.
        "options": ["AC", "DC", "idle"],
        "device_class": SensorDeviceClass.ENUM,
        "icon": "mdi:ev-plug-type2",
        "slug_last": "last_charge_type",
        "slug_current": "current_charge_type",
    },
    "evse_energy_kwh": {
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "precision": 2,
        "icon": "mdi:transmission-tower",
        "slug_last": "last_charge_evse_kwh",
        "slug_current": "current_charge_evse_kwh",
    },
    "charging_efficiency_pct": {
        "unit": PERCENTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
        "icon": "mdi:percent",
        "slug_last": "last_charge_efficiency",
        "slug_current": "current_charge_efficiency",
    },
    # v0.6.0 — peak instantaneous charging power during the session.
    # Surfaces the DCFC-stress signal at the row scope; the
    # >=100 kW threshold drives the SoH model and the lifetime/period
    # high-power aggregates.
    "peak_charge_power_kw": {
        "unit": UnitOfPower.KILO_WATT,
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
        "icon": "mdi:lightning-bolt",
        "slug_last": "last_charge_peak_power",
        "slug_current": "current_charge_peak_power",
    },
}


def _is_dcfc_label(value: bool | None) -> str:
    if value is True:
        return "DC"
    if value is False:
        return "AC"
    # v0.5.78 — idle (no active charge, no last charge): `idle` reads
    # cleaner on dashboards than `unknown`, and it's the literal truth.
    return "idle"


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

    # v0.5.62 — numeric idle defaults. kWh / cost / power / duration all
    # start at 0 in an idle state so the dashboard tile reads "0 kWh"
    # instead of "unknown". price_per_kwh keeps the home tariff (so
    # "if I charged now, this is what it would cost"), is_dcfc maps
    # via _is_dcfc_label.
    _IDLE_NUMERIC_KEYS = frozenset({
        "kwh", "total_cost", "power_kw", "duration_min",
        # v0.5.92 — show 0 in idle for these too so the dashboard
        # reads "0 kWh / 0 %" cleanly instead of "unknown" between
        # sessions.
        "evse_energy_kwh", "charging_efficiency_pct",
    })

    @property
    def native_value(self) -> float | str | None:
        snap = self._coordinator.current_charge_snapshot()
        if snap is None:
            if self._key == "is_dcfc":
                return _is_dcfc_label(None)
            if self._key in self._IDLE_NUMERIC_KEYS:
                return 0.0
            # v0.5.63 — `price_per_kwh` in idle = the home tariff. Lets
            # the dashboard show "current tariff: 0.07 €/kWh" instead
            # of `unknown` when no session is active.
            if self._key == "price_per_kwh":
                return self._coordinator._energy_price
            return None
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
            # v0.5.43 — same period-reset semantics as the trip count;
            # TOTAL_INCREASING gives LTS for monthly charge-count bars.
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "icon": "mdi:counter",
            "precision": 0,
            "slug": "charges",
        },
        # v0.5.43 — avg prices switched from MONETARY/no-state-class to
        # plain MEASUREMENT with a <currency>/kWh unit (CONTRACT.md §3b):
        # MONETARY forbids MEASUREMENT in HA, and without a state_class
        # the recorder kept no long-term statistics, so price-trend
        # graphs were impossible. A price-per-kWh isn't a monetary total
        # anyway.
        "avg_price_per_kwh": {
            "device_class": None,
            "state_class": SensorStateClass.MEASUREMENT,
            "per_kwh_unit": True,
            "icon": "mdi:cash",
            "precision": 4,
            "slug": "avg_charge_price",
        },
        "avg_ac_price_per_kwh": {
            "device_class": None,
            "state_class": SensorStateClass.MEASUREMENT,
            "per_kwh_unit": True,
            "icon": "mdi:home-lightning-bolt",
            "precision": 4,
            "slug": "avg_ac_charge_price",
        },
        "avg_dc_price_per_kwh": {
            "device_class": None,
            "state_class": SensorStateClass.MEASUREMENT,
            "per_kwh_unit": True,
            "icon": "mdi:ev-station",
            "precision": 4,
            "slug": "avg_dc_charge_price",
        },
        # v0.5.101 — kWh-weighted AC→DC efficiency over the period:
        # SUM(kwh) / SUM(evse_energy_kwh) × 100 across the charges
        # that actually had EVSE data. State is None when no charge
        # in the period had EVSE, so the UI reads "unknown" instead
        # of 0 (which would look like total loss).
        "charging_efficiency_pct": {
            "device_class": None,
            "state_class": SensorStateClass.MEASUREMENT,
            "unit": PERCENTAGE,
            "icon": "mdi:battery-charging-high",
            "precision": 1,
            "slug": "avg_charging_efficiency",
        },
        # v0.6.1 — grid-side energy delivered by the EVSE / wallbox
        # over the period. `kwh` (battery-side) tracks what landed in
        # the pack; `evse_kwh` tracks what came out of the wall. The
        # paired exposure mirrors OVMS' v.c.kwh vs v.c.kwh.grid and
        # lets the HA Energy dashboard track grid consumption (the
        # battery-side value would under-count by the AC→DC losses).
        "evse_kwh": {
            "unit": UnitOfEnergy.KILO_WATT_HOUR,
            "device_class": SensorDeviceClass.ENERGY,
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "icon": "mdi:transmission-tower-export",
            "precision": 2,
            "slug": "grid_energy_charged",
        },
        # v0.6.1 — DCFC-stress signals at period scope, sourced from
        # the v0.6.0 high-power cohort (peak_charge_power_kw >= 100).
        # `high_power_kwh` totals the kWh delivered in high-stress
        # sessions; `high_power_count` is just the session count;
        # `peak_power_max_kw` is informational ("best ever peak").
        "high_power_kwh": {
            "unit": UnitOfEnergy.KILO_WATT_HOUR,
            "device_class": SensorDeviceClass.ENERGY,
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "icon": "mdi:flash-alert",
            "precision": 2,
            "slug": "high_power_charged",
        },
        "high_power_count": {
            "device_class": None,
            "state_class": SensorStateClass.TOTAL_INCREASING,
            "icon": "mdi:flash-alert-outline",
            "precision": 0,
            "slug": "high_power_sessions",
        },
        "peak_power_max_kw": {
            "unit": UnitOfPower.KILO_WATT,
            "device_class": SensorDeviceClass.POWER,
            "state_class": SensorStateClass.MEASUREMENT,
            "icon": "mdi:lightning-bolt",
            "precision": 1,
            "slug": "peak_charge_power_max",
        },
    }

    _PERIOD_SUFFIX = {
        "today": "today",
        "week": "this_week",
        "month": "this_month",
        "year": "this_year",
        "30d": "30d",
        # v0.6.1 — monotonically-growing accumulator since first-ever
        # logged row. Pairs with the TOTAL_INCREASING state-class so HA
        # picks the right LTS aggregation (sum-over-cycles, not mean).
        "lifetime": "lifetime",
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
        # v0.5.101 — unit precedence: explicit cfg["unit"] (kwh,
        # percentage, etc) > per-kWh derived (price sensors) > raw
        # currency (totals) > no unit (counts).
        if cfg.get("unit"):
            unit = cfg["unit"]
        elif cfg.get("per_kwh_unit"):
            unit = f"{coordinator.currency}/kWh"
        elif key == "count":
            unit = None
        else:
            unit = coordinator.currency
        self.entity_description = SensorEntityDescription(
            key=slug,
            translation_key=slug,
            native_unit_of_measurement=unit,
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

    _unrecorded_attributes = frozenset({"by_bucket"})
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
        """The kWh/100km of the bucket the current outside temp falls into.

        v0.5.78 — fall back to the integration's score-baseline (the
        user's own historical median consumption) when there's not
        enough data yet or the outside temp sensor is asleep. Way more
        useful than `unknown` for fresh installs and Tesla-asleep
        scenarios.
        """
        if not self._buckets:
            return getattr(self._coordinator, "score_baseline_kwh_100km", None)
        temp = self._coordinator.exterior_temp
        if temp is None:
            # No live temp → return the overall average across buckets
            # so the tile shows "typical consumption" instead of going
            # blank when the temp source is asleep.
            vals = [v for v in self._buckets.values() if v is not None]
            return sum(vals) / len(vals) if vals else None
        bucket = int((temp // self._BUCKET_SIZE_C) * self._BUCKET_SIZE_C)
        val = self._buckets.get(str(bucket))
        if val is None:
            # Bucket has no samples — fall back to the overall mean.
            vals = [v for v in self._buckets.values() if v is not None]
            return sum(vals) / len(vals) if vals else None
        return val

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

    _unrecorded_attributes = frozenset({"months"})

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

    _unrecorded_attributes = frozenset({"days"})
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

    _unrecorded_attributes = frozenset({"by_hour", "by_weekday", "km_by_weekday"})
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
        # v0.5.43 — mean regen recovered per trip over the window. No
        # ENERGY device_class: HA forbids MEASUREMENT on ENERGY, and an
        # average is a measurement, not an accumulating total.
        "avg_regen_kwh": {
            "unit": UnitOfEnergy.KILO_WATT_HOUR,
            "icon": "mdi:battery-charging",
            "slug": "avg_trip_regen",
            "precision": 2,
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
        # v0.5.64 — show 0.0 instead of `unknown` when the 30d window
        # has no data for this metric. Reconstructed trips never
        # capture regen, so on cloud-polled cars `avg_trip_regen_30d`
        # was stuck at `unknown` for weeks. 0.0 reads cleanly.
        return self._value if self._value is not None else 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"sample_count": self._count, "window_days": self._WINDOW_DAYS}


class TopsSensor(_BaseTripSensor):
    """Top-N trips per criterion (powers the Rankings screen)."""

    # v0.5.16 — six lists × nine dicts is the heaviest blob the integration
    # emits. Without exclusion, the recorder serialises ~30 KB per refresh
    # and HA drops the attributes silently with a warning.
    _unrecorded_attributes = frozenset({
        "longest", "longest_duration", "top_consumption",
        "top_efficiency", "top_speed", "cheapest",
    })
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
        # v0.5.39 — per-session SoC averages so the user knows their
        # typical charging behaviour: at what % do they plug in, to
        # what % do they top up, and how many points they add per
        # session.
        "avg_soc_start": {
            "unit": PERCENTAGE,
            "device_class": SensorDeviceClass.BATTERY,
            "icon": "mdi:battery-low",
            "slug": "avg_charge_soc_start_30d",
            "precision": 1,
        },
        "avg_soc_end": {
            "unit": PERCENTAGE,
            "device_class": SensorDeviceClass.BATTERY,
            "icon": "mdi:battery-high",
            "slug": "avg_charge_soc_end_30d",
            "precision": 1,
        },
        "avg_soc_added": {
            "unit": PERCENTAGE,
            "icon": "mdi:battery-plus",
            "slug": "avg_charge_soc_added_30d",
            "precision": 1,
        },
    }

    _WINDOW_DAYS = 30

    def __init__(self, coordinator: EvTripLoggerCoordinator, *, key: str) -> None:
        super().__init__(coordinator)
        cfg = self._CONFIG[key]
        self._key = key
        slug = cfg["slug"]
        # v0.5.55 — `energy` / `monetary` device classes reject
        # `measurement` (HA expects `total`/`total_increasing`). These are
        # averages, not instantaneous readings, so the right answer here
        # is no state_class at all (= None). Avoids the warning spam
        # `'measurement' is impossible considering device class 'energy'`.
        is_energy_or_money = cfg.get("device_class") in (
            SensorDeviceClass.ENERGY, SensorDeviceClass.MONETARY,
        )
        self.entity_description = SensorEntityDescription(
            key=slug,
            translation_key=slug,
            native_unit_of_measurement=(
                coordinator.currency if cfg.get("use_currency") else cfg.get("unit")
            ),
            device_class=cfg.get("device_class"),
            state_class=(
                None if is_energy_or_money else SensorStateClass.MEASUREMENT
            ),
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


class CurrentDriverSensor(_BaseTripSensor):
    """Who is driving right now (v0.5.43).

    State mirrors the active trip's captured driver — i.e. the person
    whose phone the car's bluetooth picked up. Unknown while idle or
    when nobody was identified. The last completed trip's driver is
    exposed as an attribute for "who parked it" questions.
    """

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="current_driver",
            translation_key="current_driver",
            icon="mdi:account-tie-hat",
        )
        self._attr_unique_id = f"{coordinator.entry_id}_current_driver"

    @property
    def native_value(self) -> str | None:
        active = self._coordinator.current
        if active is not None:
            return active.driver
        # v0.5.78 — idle: surface the LAST trip's driver instead of
        # `unknown`. Tile reads "who parked it" when stopped, which is
        # what dashboards actually want. Stays None only if no trip
        # has ever been logged.
        last = self._coordinator.last_trip
        if last is not None and getattr(last, "driver", None):
            return last.driver
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self._coordinator.last_trip
        return {
            "trip_active": self._coordinator.current is not None,
            "last_trip_driver": getattr(last, "driver", None) if last else None,
        }


class DriverStatsSensor(_BaseTripSensor):
    """Per-driver usage over the last 30 days (v0.5.43).

    State = number of identified drivers in the window. The heavy
    payload lives in attributes: one row per driver with trips, km,
    driving hours, energy and mean consumption, plus an 'unknown'
    bucket so totals always add up. Powers the dashboard's
    "quién usa el coche" panel.
    """

    _unrecorded_attributes = frozenset({"drivers"})
    _WINDOW_DAYS = 30

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="driver_stats",
            translation_key="driver_stats",
            icon="mdi:account-group",
        )
        self._attr_unique_id = f"{coordinator.entry_id}_driver_stats"
        self._rows: list[dict[str, Any]] = []

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
        self._rows = await self._coordinator.storage.async_driver_stats(since)
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return sum(1 for r in self._rows if r.get("driver") != "unknown")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"drivers": self._rows, "window_days": self._WINDOW_DAYS}


# ---------------------------------------------------------------------------
# v0.5.54 — season / time-of-day / battery-soh sensors.
# ---------------------------------------------------------------------------


class ConsumptionBySeasonSensor(_BaseTripSensor):
    """v0.5.54 — lifetime consumption bucketed by season.

    State = the season we're CURRENTLY in (so the displayed value
    shifts naturally month-to-month). Attributes carry every season's
    aggregate.
    """

    _unrecorded_attributes = frozenset({"by_season"})

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="consumption_by_season",
            translation_key="consumption_by_season",
            native_unit_of_measurement="kWh/100km",
            icon="mdi:weather-partly-snowy-rainy",
            suggested_display_precision=1,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_consumption_by_season"
        self._by_season: dict[str, dict[str, Any]] = {}

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_refresh, _AGGREGATE_REFRESH
            )
        )
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, *_: Any) -> None:
        self._by_season = await self._coordinator.storage.async_aggregates_by_season()
        self.async_write_ha_state()

    @staticmethod
    def _current_season(now: datetime) -> str:
        m = now.month
        if m in (12, 1, 2):
            return "winter"
        if m in (3, 4, 5):
            return "spring"
        if m in (6, 7, 8):
            return "summer"
        return "autumn"

    @property
    def native_value(self) -> float | None:
        season = self._current_season(dt_util.now())
        return (self._by_season.get(season) or {}).get(
            "avg_consumption_kwh_100km"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "current_season": self._current_season(dt_util.now()),
            "by_season": self._by_season,
        }


class ConsumptionByTimeOfDaySensor(_BaseTripSensor):
    """v0.5.54 — lifetime consumption bucketed by start-hour of the trip."""

    _unrecorded_attributes = frozenset({"by_time"})

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="consumption_by_time_of_day",
            translation_key="consumption_by_time_of_day",
            native_unit_of_measurement="kWh/100km",
            icon="mdi:clock-time-eight-outline",
            suggested_display_precision=1,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_consumption_by_time_of_day"
        self._by_time: dict[str, dict[str, Any]] = {}

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_refresh, _AGGREGATE_REFRESH
            )
        )
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, *_: Any) -> None:
        self._by_time = await self._coordinator.storage.async_aggregates_by_time_of_day()
        self.async_write_ha_state()

    @staticmethod
    def _current_bucket(now: datetime) -> str:
        h = now.hour
        if h >= 22 or h < 6:
            return "night"
        if h < 12:
            return "morning"
        if h < 15:
            return "midday"
        if h < 19:
            return "afternoon"
        return "evening"

    @property
    def native_value(self) -> float | None:
        bucket = self._current_bucket(dt_util.now())
        return (self._by_time.get(bucket) or {}).get(
            "avg_consumption_kwh_100km"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "current_bucket": self._current_bucket(dt_util.now()),
            "by_time": self._by_time,
        }


class BatterySohSensor(_BaseTripSensor):
    """v0.5.54 — State of Health = calibrated / declared × 100.

    State stays at 100 until enough charges accumulate to calibrate
    (then the SoH naturally drops with degradation). The capacity
    history is exposed as attributes for dashboard line charts.
    """

    _unrecorded_attributes = frozenset({"history"})

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="battery_soh",
            translation_key="battery_soh",
            native_unit_of_measurement="%",
            icon="mdi:battery-heart-variant",
            suggested_display_precision=1,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_battery_soh"
        self._history: list[dict[str, Any]] = []

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_refresh, _AGGREGATE_REFRESH
            )
        )
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, *_: Any) -> None:
        self._history = await self._coordinator.storage.async_capacity_history(
            limit=24
        )
        # v0.5.66 — pre-compute logger_km here (executor query) so the
        # sync extra_state_attributes can read it from a cache without
        # blocking the event loop.
        self._logger_km_cache = (
            await self._coordinator.storage.async_logger_total_km()
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        declared = self._coordinator._battery_capacity_declared
        calibrated = self._coordinator._battery_capacity_calibrated
        if calibrated is None or declared <= 0:
            return 100.0  # no data yet → assume healthy
        return round(calibrated / declared * 100.0, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coord = self._coordinator
        declared = coord._battery_capacity_declared
        calibrated = coord._battery_capacity_calibrated
        # Degradation rate: slope between oldest and newest snapshot.
        rate_kwh_per_year: float | None = None
        if len(self._history) >= 2:
            oldest = self._history[0]
            newest = self._history[-1]
            try:
                t0 = datetime.fromisoformat(oldest["observed_at"])
                t1 = datetime.fromisoformat(newest["observed_at"])
                years = (t1 - t0).total_seconds() / (365.25 * 86400)
                if years > 0:
                    delta = newest["calibrated_kwh"] - oldest["calibrated_kwh"]
                    rate_kwh_per_year = round(delta / years, 3)
            except Exception:  # pragma: no cover — defensive
                rate_kwh_per_year = None
        # v0.5.65 — surface the car's current km and age in the SoH
        # sensor attributes. Pairs nicely with the history (which now
        # carries odometer_km on every snapshot): the dashboard can
        # render "100 % at 26 471 km" and plot SoH vs km.
        odo = coord._read_float(coord._odometer)
        first_reg = coord._vehicle_first_registered
        # v0.5.66 — logger_km: kilometres witnessed under the
        # integration's monitoring (SUM of distance_km across all
        # trips). This is the figure the SoH model uses, since model
        # penalties only know about the period the logger has been
        # active. Exposed alongside `odometer_km` for transparency.
        try:
            logger_km = self._logger_km_cache
        except AttributeError:
            logger_km = None
        age_years: float | None = None
        if first_reg is not None:
            try:
                age_years = round(
                    (dt_util.now() - first_reg).total_seconds() / (365.25 * 86400),
                    2,
                )
            except Exception:  # pragma: no cover — defensive
                age_years = None
        return {
            "declared_capacity_kwh": declared,
            "calibrated_capacity_kwh": (
                round(calibrated, 2) if calibrated is not None else None
            ),
            "calibration_charges": coord._battery_capacity_calibration_n,
            "degradation_kwh_per_year": rate_kwh_per_year,
            # v0.5.66 — TWO km figures. `logger_km` drives the model
            # (what the integration has actually observed). `odometer_km`
            # is the car's lifetime mileage; useful for the dashboard
            # banner ("100 % @ 26 471 km") but NOT used in the SoH model.
            "logger_km": (
                round(logger_km, 1) if logger_km is not None else None
            ),
            "odometer_km": round(odo, 1) if odo is not None else None,
            "age_years": age_years,
            "battery_chemistry": coord._battery_chemistry,
            "history": self._history,
        }


class AvgChargingEfficiencySensor(_BaseTripSensor):
    """v0.5.90 — rolling-median AC→DC charging efficiency across the
    last 30 charge sessions where both EVSE energy and battery kWh are
    recorded. State is the median %; attributes show sample count.

    Useful for catching: lossy cables (drops below 85 %), EVSE
    derating issues, or DCFC sessions interleaved with AC (DCFC is
    typically 92-97 %, AC home 88-94 %).
    """

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="avg_charging_efficiency_30d",
            translation_key="avg_charging_efficiency_30d",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:percent-circle",
            suggested_display_precision=1,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = (
            f"{coordinator.entry_id}_avg_charging_efficiency_30d"
        )
        self._median: float | None = None
        self._n: int = 0

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_refresh, _AGGREGATE_REFRESH
            )
        )
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, *_: Any) -> None:
        self._median, self._n = (
            await self._coordinator.storage.async_avg_charging_efficiency_pct(
                window=30, min_charges=3,
            )
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._median

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "sample_count": self._n,
            "window_charges": 30,
            "interpretation": (
                "AC home charger typical 88-94 %, DCFC 92-97 %. "
                "Below 85 % signals lossy cable / wallbox derating "
                "/ onboard charger inefficiency. Only charges with "
                "an EVSE power sensor wired contribute."
            ),
        }


class BatteryCalibrationFactorSensor(_BaseTripSensor):
    """v0.5.84 — rolling-median per-trip battery-capacity calibration K.

    K = net_power_kwh / (soc_delta × nominal_capacity). Aggregated as
    the median over the last 30 trips with both signals available.
    Persistent K < 1.0 indicates real degradation (real capacity is
    K × nominal). Per-trip K is noisy due to power-integration
    sampling gaps; the rolling median is the actionable number.
    """

    _unrecorded_attributes = frozenset({"history"})

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="battery_calibration_factor",
            translation_key="battery_calibration_factor",
            icon="mdi:scale-balance",
            suggested_display_precision=3,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = (
            f"{coordinator.entry_id}_battery_calibration_factor"
        )
        self._median: float | None = None
        self._n: int = 0

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_refresh, _AGGREGATE_REFRESH
            )
        )
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, *_: Any) -> None:
        self._median, self._n = (
            await self._coordinator.storage.async_calibration_factor_k_median(
                window=30, min_trips=5,
            )
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._median

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "sample_count": self._n,
            "window_trips": 30,
            "min_trips_required": 5,
            "nominal_capacity_kwh": self._coordinator.battery_capacity,
            "effective_capacity_kwh": (
                round(self._median * self._coordinator.battery_capacity, 2)
                if self._median is not None else None
            ),
            "interpretation": (
                "K ≈ 1.0 → capacity matches nominal. K < 1.0 → real "
                "capacity is K × nominal (degradation). Per-trip K is "
                "noisy; this median over 30 trips is the trustworthy "
                "signal. Only trips with SoC delta ≥ 2% AND non-zero "
                "power integration AND positive net energy contribute."
            ),
        }


class ExpectedBatterySohSensor(_BaseTripSensor):
    """v0.5.57 — predicted SoH given km, chemistry, climate, habits.

    Curve constants live in `coordinator._DEGRADATION_PROFILES` and
    are documented in the coordinator module header. The sensor is
    pure read-side: every refresh re-runs `async_compute_expected_soh`.
    """

    _unrecorded_attributes = frozenset({"factors", "inputs"})

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="expected_battery_soh",
            translation_key="expected_battery_soh",
            native_unit_of_measurement="%",
            icon="mdi:heart-pulse",
            suggested_display_precision=1,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{coordinator.entry_id}_expected_battery_soh"
        self._cache: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_refresh, _AGGREGATE_REFRESH
            )
        )
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, *_: Any) -> None:
        try:
            self._cache = await self._coordinator.async_compute_expected_soh()
        except Exception:  # pragma: no cover — defensive
            # v0.5.58 — was silent debug, but the sensor was stuck in
            # `unknown` for hours and we had no traceback. Log the full
            # exception so the next failure surfaces in system_log.
            logging.getLogger(__name__).exception(
                "expected_soh compute failed"
            )
            self._cache = {}
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._cache.get("expected_soh_pct")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "factors": self._cache.get("factors", {}),
            "inputs": self._cache.get("inputs", {}),
            "confidence": self._cache.get("confidence", "low"),
        }


class BatteryHealthVsExpectedSensor(_BaseTripSensor):
    """v0.5.57 — compares observed SoH (calibrated / declared × 100)
    against the predicted SoH for this car's age/km/habits.

    State enum:
      * `calibrating`  — no calibrated capacity yet (n_charges < 5)
      * `ahead`        — observed ≥ expected + 2 pp
      * `on_track`     — within ± 2 pp of the expected curve
      * `behind`       — observed ≤ expected − 2 pp
    """

    _attr_options = ["calibrating", "ahead", "on_track", "behind"]
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_description = SensorEntityDescription(
            key="battery_health_vs_expected",
            translation_key="battery_health_vs_expected",
            icon="mdi:scale-balance",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = (
            f"{coordinator.entry_id}_battery_health_vs_expected"
        )
        self._observed: float | None = None
        self._expected: float | None = None

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_refresh, _AGGREGATE_REFRESH
            )
        )
        self.async_on_remove(
            self._coordinator.async_add_trip_log_listener(self._schedule_refresh)
        )

    @callback
    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, *_: Any) -> None:
        coord = self._coordinator
        # Observed SoH — needs calibrated capacity. Without it we stay
        # in `calibrating` state and the verdict is unreliable.
        if coord._battery_capacity_calibrated is None:
            self._observed = None
        else:
            decl = coord._battery_capacity_declared
            cal = coord._battery_capacity_calibrated
            self._observed = (cal / decl * 100.0) if decl > 0 else None
        try:
            result = await coord.async_compute_expected_soh()
            self._expected = result.get("expected_soh_pct")
        except Exception:  # pragma: no cover — defensive
            self._expected = None
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        if self._observed is None or self._expected is None:
            return "calibrating"
        delta = self._observed - self._expected
        if delta >= 2.0:
            return "ahead"
        if delta <= -2.0:
            return "behind"
        return "on_track"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "observed_soh_pct": (
                round(self._observed, 2) if self._observed is not None else None
            ),
            "expected_soh_pct": (
                round(self._expected, 2) if self._expected is not None else None
            ),
            "delta_pp": (
                round(self._observed - self._expected, 2)
                if (self._observed is not None and self._expected is not None)
                else None
            ),
        }
