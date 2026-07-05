"""Config flow for EV Trip Logger."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    DateSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_BATTERY,
    CONF_BATTERY_CAPACITY,
    CONF_CHARGE_SENSOR,
    CONF_CURRENCY,
    CONF_DRIVER_SENSOR,
    CONF_ENERGY_PRICE,
    CONF_HOME_ZONE,
    CONF_IDLE_TIMEOUT,
    CONF_IDLE_TRIP_TIMEOUT_MIN,
    CONF_LOCATION,
    CONF_MIN_TRIP_DISTANCE,
    CONF_NAME,
    CONF_ODOMETER,
    CONF_ABRP_API_KEY,
    CONF_ABRP_CAR_MODEL,
    CONF_ABRP_PUSH_INTERVAL_S,
    CONF_ABRP_TOKEN,
    CONF_PLUG_SENSOR,
    CONF_POLLING_PAUSED_SENSOR,
    CONF_LAST_TRIP_ENERGY_SENSOR,
    CONF_LAST_TRIP_DISTANCE_SENSOR,
    CONF_POWER_SIGN_INVERTED,
    CONF_EVSE_POWER_SENSOR,
    CONF_TRACKED_SENSORS,
    DEFAULT_ABRP_PUSH_INTERVAL_S,
    CONF_POWER,
    CONF_RECENT_LIMIT,
    CONF_SPEED,
    CONF_RANGE_SENSOR,
    CONF_HEADING_SENSOR,
    CONF_DCFC_THRESHOLD_KW,
    DEFAULT_DCFC_THRESHOLD_KW,
    ABRP_MIN_SEND_INTERVAL_S,
    CONF_BATTERY_CHEMISTRY,
    CONF_TEMP,
    CONF_ELEVATION_PROVIDER,
    CONF_ELEVATION_PROVIDER_URL,
    CONF_IDLE_POWER_ESTIMATE_KW,
    CONF_VEHICLE_FIRST_REGISTERED,
    CONF_VEHICLE_MODEL,
    CONF_VEHICLE_ON,
    DEFAULT_BATTERY_CHEMISTRY,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_ELEVATION_PROVIDER,
    DEFAULT_IDLE_POWER_ESTIMATE_KW,
    ELEVATION_PROVIDER_OPTIONS,
    DEFAULT_CURRENCY,
    DEFAULT_ENERGY_PRICE,
    DEFAULT_HOME_ZONE,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_IDLE_TRIP_TIMEOUT_MIN,
    DEFAULT_MIN_TRIP_DISTANCE,
    DEFAULT_RECENT_LIMIT,
    DOMAIN,
)


def _required_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "My EV")): TextSelector(),
            vol.Required(
                CONF_ODOMETER, default=defaults.get(CONF_ODOMETER)
            ): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="distance")
            ),
            vol.Required(
                CONF_BATTERY, default=defaults.get(CONF_BATTERY)
            ): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="battery")
            ),
            vol.Required(
                CONF_VEHICLE_ON, default=defaults.get(CONF_VEHICLE_ON)
            ): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
        }
    )


def _cohort_options() -> list[tuple[str, str]]:
    """Lazy import so the config_flow module load doesn't pull in
    coordinator + its dependencies for trivial cases (HA imports
    config_flow eagerly during entry restore)."""
    from .coordinator import cohort_baseline_options  # noqa: PLC0415

    return cohort_baseline_options()


def _optional_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}

    def _optional(key: str, selector: Any) -> Any:
        # HA's options-flow UI hides vol.Optional fields that have no
        # default value, so we always pass description.suggested_value
        # (None when unset) to force the field to render. The user can
        # then pick or clear a sensor on demand.
        current = defaults.get(key)
        return vol.Optional(key, description={"suggested_value": current})

    return vol.Schema(
        {
            _optional(
                CONF_POWER,
                EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power")),
            ): EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power")),
            # v0.5.61 — accept `sensor.*` as well: Tesla exposes
            # `sensor.<vehicle>_charging_state` (enum: Charging /
            # Disconnected / Complete / Stopped / NoPower / Starting),
            # not a binary_sensor. The integration recognises the
            # textual states (see _CHARGING_STATES in coordinator).
            _optional(
                CONF_CHARGE_SENSOR,
                EntitySelector(
                    EntitySelectorConfig(domain=["binary_sensor", "sensor"])
                ),
            ): EntitySelector(
                EntitySelectorConfig(domain=["binary_sensor", "sensor"])
            ),
            _optional(
                CONF_PLUG_SENSOR,
                EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
            ): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
            _optional(
                CONF_POLLING_PAUSED_SENSOR,
                EntitySelector(
                    EntitySelectorConfig(domain=["switch", "binary_sensor"])
                ),
            ): EntitySelector(
                EntitySelectorConfig(domain=["switch", "binary_sensor"])
            ),
            _optional(
                CONF_TRACKED_SENSORS,
                EntitySelector(
                    EntitySelectorConfig(domain="sensor", multiple=True)
                ),
            ): EntitySelector(
                EntitySelectorConfig(domain="sensor", multiple=True)
            ),
            _optional(
                CONF_LOCATION,
                EntitySelector(
                    EntitySelectorConfig(
                        domain=["device_tracker", "person", "input_select", "sensor"]
                    )
                ),
            ): EntitySelector(
                EntitySelectorConfig(
                    domain=["device_tracker", "person", "input_select", "sensor"]
                )
            ),
            _optional(
                CONF_TEMP,
                EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
            ): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            # v0.5.68 — weather entity removed from the config flow.
            # The car's own exterior temperature sensor (CONF_TEMP)
            # supplies temperature in real time with better
            # granularity; the other weather fields were never used.
            # v0.5.43 — driver identity. Any entity whose state names the
            # person using the car: the manufacturer's "connected
            # bluetooth device" sensor, an input_select, or a template
            # sensor mapping BT MAC → person.
            _optional(
                CONF_DRIVER_SENSOR,
                EntitySelector(
                    EntitySelectorConfig(
                        domain=["sensor", "input_select", "select", "input_text"]
                    )
                ),
            ): EntitySelector(
                EntitySelectorConfig(
                    domain=["sensor", "input_select", "select", "input_text"]
                )
            ),
            _optional(
                CONF_SPEED,
                EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="speed")
                ),
            ): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="speed")
            ),
            # v0.8.0 — ABRP-only extras: estimated range (km) and GPS heading (°).
            _optional(
                CONF_RANGE_SENSOR,
                EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="distance")
                ),
            ): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="distance")
            ),
            _optional(
                CONF_HEADING_SENSOR,
                EntitySelector(EntitySelectorConfig(domain="sensor")),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            # v0.5.77 — vehicle-native per-trip energy + distance sensors.
            # When set, the logger uses these as ground truth (avoids
            # SoC quantization + regen-trapezoid noise). Generic by
            # design: BYD `last_trip_energy`, Tesla `Trip A` etc. all
            # satisfy this shape.
            _optional(
                CONF_LAST_TRIP_ENERGY_SENSOR,
                EntitySelector(EntitySelectorConfig(domain="sensor")),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            _optional(
                CONF_LAST_TRIP_DISTANCE_SENSOR,
                EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="distance")
                ),
            ): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="distance")
            ),
            # v0.5.85 — power-sensor polarity. Default off (positive =
            # discharge). Flip ON for BYD-cloud-style sensors that
            # report discharge as negative.
            vol.Optional(
                CONF_POWER_SIGN_INVERTED,
                default=defaults.get(CONF_POWER_SIGN_INVERTED, False),
            ): bool,
            # v0.5.89 — EVSE / wallbox power sensor for AC-side energy
            # accounting during charges. Optional; when wired,
            # comparing battery-input vs EVSE-output exposes real
            # charging efficiency.
            _optional(
                CONF_EVSE_POWER_SENSOR,
                EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="power")
                ),
            ): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="power")
            ),
            vol.Required(
                CONF_BATTERY_CAPACITY,
                default=defaults.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=300, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="kWh"
                )
            ),
            # v0.5.57 — battery chemistry drives the expected-SoH model.
            # Optional; default 'lfp' covers BYD Blade and most large-pack
            # 2022+ EVs.
            vol.Optional(
                CONF_BATTERY_CHEMISTRY,
                default=defaults.get(CONF_BATTERY_CHEMISTRY, DEFAULT_BATTERY_CHEMISTRY),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=["lfp", "nmc", "nca"],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="battery_chemistry",
                )
            ),
            # v0.5.57 — first-registered date. Feeds calendar-aging
            # component of the expected SoH. Optional — we proxy from
            # km/15 000 when missing.
            _optional(
                CONF_VEHICLE_FIRST_REGISTERED,
                DateSelector(),
            ): DateSelector(),
            # v0.6.3 — optional cohort baseline pick. Lookup keys come
            # from `cohort_baselines.json`; selecting one anchors the
            # SoH 100 % point against the observed-new capacity for
            # that model (Tessie pattern). Leave blank to keep the
            # nameplate behaviour. Local import keeps the JSON load
            # off the module-import path of config_flow at HA boot.
            _optional(
                CONF_VEHICLE_MODEL,
                SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=k, label=lbl)
                            for k, lbl in _cohort_options()
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="vehicle_model_key",
                        custom_value=True,
                    )
                ),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=k, label=lbl)
                        for k, lbl in _cohort_options()
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="vehicle_model_key",
                    custom_value=True,
                )
            ),
            # v0.6.6 — kW the vehicle draws while parked with ignition
            # on (HVAC + electronics). Drives the moving-consumption
            # estimate so the dashboard can split "energy moving" vs
            # "energy waiting". 2.5 kW is a reasonable mid-size SUV
            # summer default; range 0.5-5 covers small EVs to large
            # luxury cabins.
            vol.Required(
                CONF_IDLE_POWER_ESTIMATE_KW,
                default=defaults.get(
                    CONF_IDLE_POWER_ESTIMATE_KW,
                    DEFAULT_IDLE_POWER_ESTIMATE_KW,
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0.5, max=5.0, step=0.1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="kW",
                )
            ),
            # v0.7.5 — optional elevation provider. Default "none"
            # keeps the trip's GPS route on-host; users opt in per
            # deployment. "custom" pairs with the URL field below to
            # point at a self-hosted OpenTopoData instance.
            vol.Optional(
                CONF_ELEVATION_PROVIDER,
                default=defaults.get(
                    CONF_ELEVATION_PROVIDER, DEFAULT_ELEVATION_PROVIDER,
                ),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(ELEVATION_PROVIDER_OPTIONS),
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="elevation_provider",
                )
            ),
            _optional(
                CONF_ELEVATION_PROVIDER_URL,
                TextSelector(),
            ): TextSelector(),
            vol.Required(
                CONF_MIN_TRIP_DISTANCE,
                default=defaults.get(CONF_MIN_TRIP_DISTANCE, DEFAULT_MIN_TRIP_DISTANCE),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=10, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="km"
                )
            ),
            vol.Required(
                CONF_IDLE_TIMEOUT,
                default=defaults.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=30, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min"
                )
            ),
            vol.Required(
                CONF_IDLE_TRIP_TIMEOUT_MIN,
                default=defaults.get(
                    CONF_IDLE_TRIP_TIMEOUT_MIN, DEFAULT_IDLE_TRIP_TIMEOUT_MIN
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=2, max=60, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min"
                )
            ),
            # v0.8.0 — DC fast-charge threshold, previously internal-only.
            # Charges above this average power are tagged DCFC (and flagged
            # is_dcfc to ABRP). Default 11 kW sits above 3-phase AC.
            vol.Required(
                CONF_DCFC_THRESHOLD_KW,
                default=defaults.get(
                    CONF_DCFC_THRESHOLD_KW, DEFAULT_DCFC_THRESHOLD_KW
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=5, max=50, step=1, mode=NumberSelectorMode.BOX,
                    unit_of_measurement="kW",
                )
            ),
            vol.Required(
                CONF_ENERGY_PRICE,
                default=defaults.get(CONF_ENERGY_PRICE, DEFAULT_ENERGY_PRICE),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=5, step=0.001, mode=NumberSelectorMode.BOX,
                    unit_of_measurement="/kWh",
                )
            ),
            # ISO-4217 code — dropdown of common currencies, custom entry
            # allowed for anything not listed (keeps it free-form but guides
            # away from typos like "EURO").
            vol.Required(
                CONF_CURRENCY,
                default=defaults.get(CONF_CURRENCY, DEFAULT_CURRENCY),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=c, label=c)
                        for c in ("EUR", "USD", "GBP", "CHF", "SEK", "NOK",
                                  "DKK", "PLN", "CZK", "AUD", "CAD", "JPY")
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            ),
            vol.Required(
                CONF_HOME_ZONE,
                default=defaults.get(CONF_HOME_ZONE, DEFAULT_HOME_ZONE),
            ): EntitySelector(EntitySelectorConfig(domain="zone")),
            vol.Required(
                CONF_RECENT_LIMIT,
                default=defaults.get(CONF_RECENT_LIMIT, DEFAULT_RECENT_LIMIT),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=5, max=200, step=5, mode=NumberSelectorMode.BOX
                )
            ),
            # v0.5.31 — ABRP integration. All three optional; leave
            # token blank to disable. The pair (token, api_key) comes
            # from ABRP's Generic OEM linker; car_model is e.g.
            # "byd:sealion:25:82:rwd".
            _optional(CONF_ABRP_TOKEN, TextSelector()): TextSelector(),
            _optional(CONF_ABRP_API_KEY, TextSelector()): TextSelector(),
            _optional(CONF_ABRP_CAR_MODEL, TextSelector()): TextSelector(),
            vol.Required(
                CONF_ABRP_PUSH_INTERVAL_S,
                default=defaults.get(
                    CONF_ABRP_PUSH_INTERVAL_S, DEFAULT_ABRP_PUSH_INTERVAL_S
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=ABRP_MIN_SEND_INTERVAL_S, max=600, step=5,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
        }
    )


class EvTripLoggerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_NAME].lower())
            self._abort_if_unique_id_configured()
            self._data.update(user_input)
            return await self.async_step_optional()

        return self.async_show_form(step_id="user", data_schema=_required_schema())

    async def async_step_optional(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title=self._data[CONF_NAME], data=self._data
            )

        return self.async_show_form(
            step_id="optional", data_schema=_optional_schema()
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            merged = {**entry.data, **entry.options, **user_input}
            return self.async_update_reload_and_abort(entry, data=merged)

        defaults = {**entry.data, **entry.options}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_required_schema(defaults).extend(
                _optional_schema(defaults).schema
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return EvTripLoggerOptionsFlow()


class EvTripLoggerOptionsFlow(OptionsFlow):
    """Options flow — edit any sensor or parameter without losing history.

    v0.5.67 — the required entities (odometer, battery, vehicle_on) are
    also editable from here: a user who swaps the manufacturer
    integration (e.g. moves from `byd_vehicle` to a forked variant
    with different entity names) shouldn't have to reinstall and lose
    history. The coordinator reads via `{**entry.data, **entry.options}`,
    so overriding from options Just Works.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        defaults = {**self.config_entry.data, **self.config_entry.options}
        # v0.5.67 — combine required + optional schemas so odometer /
        # battery / vehicle_on appear at the top of the dialog
        # (matching the order of the first-install wizard).
        combined = vol.Schema(
            {
                **_required_schema(defaults).schema,
                **_optional_schema(defaults).schema,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=combined,
        )
