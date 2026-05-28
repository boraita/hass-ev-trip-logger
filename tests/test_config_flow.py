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
