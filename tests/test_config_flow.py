"""Smoke tests for the config flow."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ev_trip_logger.const import (
    CONF_BATTERY,
    CONF_NAME,
    CONF_ODOMETER,
    CONF_VEHICLE_ON,
    DOMAIN,
)


async def test_user_flow_required_and_optional_steps(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Test EV",
            CONF_ODOMETER: "sensor.odometer",
            CONF_BATTERY: "sensor.battery",
            CONF_VEHICLE_ON: "binary_sensor.vehicle_on",
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "optional"


async def test_options_flow_preserves_untouched_optional(hass: HomeAssistant) -> None:
    """An optional value the frontend drops on submit must survive.

    HA's options-flow frontend omits `vol.Optional` fields the user did
    not touch (notably EntitySelector), so `user_input` arrives without
    them. The flow must merge onto the stored options rather than
    replace them wholesale, or every untouched optional is silently
    blanked — the reason the flow was previously unsafe to submit.
    Mirrors the merge the reconfigure flow already performs.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ev_trip_logger.const import CONF_CHARGE_SENSOR

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test EV",
        data={
            CONF_NAME: "Test EV",
            CONF_ODOMETER: "sensor.odometer",
            CONF_BATTERY: "sensor.battery",
            CONF_VEHICLE_ON: "binary_sensor.vehicle_on",
        },
        options={CONF_CHARGE_SENSOR: "sensor.old_charge"},
        unique_id="test-ev-options-merge",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    # Submit without CONF_CHARGE_SENSOR — exactly what the frontend sends
    # when the user leaves that field untouched.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Test EV",
            CONF_ODOMETER: "sensor.odometer",
            CONF_BATTERY: "sensor.battery",
            CONF_VEHICLE_ON: "binary_sensor.vehicle_on",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    effective = {**entry.data, **entry.options}
    assert effective.get(CONF_CHARGE_SENSOR) == "sensor.old_charge"


async def test_options_flow_clears_text_option_when_emptied(hass: HomeAssistant) -> None:
    """Clearing a text option to "" must still take effect.

    The merge preserves untouched optionals, but an option the user
    deliberately empties arrives as "" in user_input and must overwrite
    the stored value — otherwise a value could never be removed.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ev_trip_logger.const import CONF_ABRP_CAR_MODEL

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test EV",
        data={
            CONF_NAME: "Test EV",
            CONF_ODOMETER: "sensor.odometer",
            CONF_BATTERY: "sensor.battery",
            CONF_VEHICLE_ON: "binary_sensor.vehicle_on",
        },
        options={CONF_ABRP_CAR_MODEL: "byd:sealion:25:82:rwd"},
        unique_id="test-ev-options-clear",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Test EV",
            CONF_ODOMETER: "sensor.odometer",
            CONF_BATTERY: "sensor.battery",
            CONF_VEHICLE_ON: "binary_sensor.vehicle_on",
            CONF_ABRP_CAR_MODEL: "",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options.get(CONF_ABRP_CAR_MODEL) == ""


def test_optional_schema_contains_battery_energy_sensor() -> None:
    """v0.8.52 — the pack-energy sensor must be offered in the options UI."""
    from custom_components.ev_trip_logger.config_flow import _optional_schema
    from custom_components.ev_trip_logger.const import CONF_BATTERY_ENERGY_SENSOR

    schema = _optional_schema()
    keys = {str(marker) for marker in schema.schema}
    assert CONF_BATTERY_ENERGY_SENSOR in keys


async def test_options_flow_preserves_battery_energy_sensor(
    hass: HomeAssistant,
) -> None:
    """v0.8.52 — clone of the untouched-optional merge test for the
    pack-energy sensor: a stored value must survive a submit that omits
    the field (frontend drops untouched EntitySelectors)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ev_trip_logger.const import CONF_BATTERY_ENERGY_SENSOR

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test EV",
        data={
            CONF_NAME: "Test EV",
            CONF_ODOMETER: "sensor.odometer",
            CONF_BATTERY: "sensor.battery",
            CONF_VEHICLE_ON: "binary_sensor.vehicle_on",
        },
        options={CONF_BATTERY_ENERGY_SENSOR: "sensor.pack_energy"},
        unique_id="test-ev-pack-energy-merge",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Test EV",
            CONF_ODOMETER: "sensor.odometer",
            CONF_BATTERY: "sensor.battery",
            CONF_VEHICLE_ON: "binary_sensor.vehicle_on",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    effective = {**entry.data, **entry.options}
    assert effective.get(CONF_BATTERY_ENERGY_SENSOR) == "sensor.pack_energy"
