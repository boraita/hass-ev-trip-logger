"""EV Trip Logger integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import EvTripLoggerCoordinator
from .services import async_register_services, async_unregister_services
from .storage import TripStorage

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EV Trip Logger from a config entry."""
    storage = TripStorage(hass, entry.entry_id)
    await storage.async_init()

    coordinator = EvTripLoggerCoordinator(hass, entry, storage)
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: EvTripLoggerCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()

        if not hass.data[DOMAIN]:
            async_unregister_services(hass)

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
