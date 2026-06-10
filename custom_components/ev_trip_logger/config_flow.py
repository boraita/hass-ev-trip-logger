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
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
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
    CONF_TRACKED_SENSORS,
    DEFAULT_ABRP_PUSH_INTERVAL_S,
    CONF_POWER,
    CONF_RECENT_LIMIT,
    CONF_SPEED,
    CONF_TEMP,
    CONF_VEHICLE_ON,
    DEFAULT_BATTERY_CAPACITY,
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
            _optional(
                CONF_CHARGE_SENSOR,
                EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
            ): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
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
            vol.Required(
                CONF_BATTERY_CAPACITY,
                default=defaults.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=300, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="kWh"
                )
            ),
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
            vol.Required(
                CONF_ENERGY_PRICE,
                default=defaults.get(CONF_ENERGY_PRICE, DEFAULT_ENERGY_PRICE),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=5, step=0.001, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_CURRENCY,
                default=defaults.get(CONF_CURRENCY, DEFAULT_CURRENCY),
            ): TextSelector(),
            vol.Required(
                CONF_HOME_ZONE,
                default=defaults.get(CONF_HOME_ZONE, "zone.home"),
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
                    min=5, max=600, step=5,
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
    """Options flow — edit any optional sensor or parameter without losing history.

    Includes the same fields as the initial config-flow's `optional` step so
    users who skipped a sensor at first install can wire it up later (e.g.
    add a power sensor to start capturing regen / max_power, add a speed
    sensor for max_speed). Required entities (odometer, battery,
    vehicle_on) stay in entry.data and can only be changed via a full
    reconfigure.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_optional_schema(defaults),
        )
