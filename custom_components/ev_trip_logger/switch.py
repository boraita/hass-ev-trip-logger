"""Switch platform — runtime kill-switch for ABRP telemetry push.

Mirrors the legacy `abrp_telemetry` plugin's UX: a single switch the
user (or automations) can toggle to gate the outbound push. The
underlying client + config stay in place; flipping this off just
short-circuits `_async_maybe_send_abrp`.

The switch is RestoreEntity-backed so its state survives HA restarts
exactly like the old plugin's switch did.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import EvTripLoggerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EvTripLoggerCoordinator = hass.data[DOMAIN][entry.entry_id]
    # Only expose the switch when ABRP is actually configured; without
    # credentials the switch would be a useless orphan.
    if coordinator._abrp is None:
        return
    async_add_entities([AbrpPushSwitch(coordinator)])


class AbrpPushSwitch(SwitchEntity, RestoreEntity):
    """ON ⇒ telemetry is forwarded to ABRP on each metric tick (throttled);
    OFF ⇒ pushes are short-circuited, the next-charge sensor still polls.
    """

    _attr_has_entity_name = True
    entity_description = SwitchEntityDescription(
        key="abrp_push",
        translation_key="abrp_push",
        icon="mdi:cloud-upload-outline",
    )

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry_id}_abrp_push"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore previous state across restarts (default ON when never set).
        last = await self.async_get_last_state()
        if last is not None and last.state == "off":
            self._coordinator.abrp_push_enabled = False
        else:
            self._coordinator.abrp_push_enabled = True

    @property
    def is_on(self) -> bool:
        return self._coordinator.abrp_push_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._coordinator.abrp_push_enabled = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._coordinator.abrp_push_enabled = False
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        client = self._coordinator._abrp
        return {
            "interval_s": self._coordinator._abrp_interval_s,
            "last_sent_at": getattr(client, "last_sent_at", None),
            "car_model": self._coordinator._abrp_car_model,
        }
