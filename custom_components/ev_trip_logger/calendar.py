"""Calendar entity exposing trip + charge activity as all-day events.

Powers the monthly calendar view in the dashboard (Pantalla 2 of the BYD app).
Each day with at least one trip or charge becomes one all-day CalendarEvent
with a summary like "2 trips · 1 charge · 32 km".
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EvTripLoggerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EvTripLoggerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EvActivityCalendar(coordinator)])


class EvActivityCalendar(CalendarEntity):
    """All-day calendar events summarising each day's trips + charges."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EvTripLoggerCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_translation_key = "activity"
        self._attr_unique_id = f"{coordinator.entry_id}_activity_calendar"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry_id)},
        )
        self._next_event: CalendarEvent | None = None

    @property
    def event(self) -> CalendarEvent | None:
        """Today's event if any, used by HA's calendar dashboard widget."""
        return self._next_event

    async def async_update(self) -> None:
        """Refresh the 'current event' shown on the dashboard."""
        today = dt_util.now().date()
        events = await self._async_build_events(today, today + timedelta(days=1))
        self._next_event = events[0] if events else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return events in a date range — called when the user opens a month."""
        return await self._async_build_events(
            start_date.date() if isinstance(start_date, datetime) else start_date,
            end_date.date() if isinstance(end_date, datetime) else end_date,
        )

    async def _async_build_events(
        self, start_day: date, end_day: date
    ) -> list[CalendarEvent]:
        """Build one all-day event per day with activity in [start, end)."""
        storage = self._coordinator.storage
        # Pull a wide window once — calendars typically query month-by-month.
        # The recent_* sensors already cap at 50, so we read directly from
        # storage to bypass the buffer for the calendar.
        trips = await storage.async_recent_trips(limit=500)
        charges = await storage.async_recent_charges(limit=500)

        by_day: dict[date, dict[str, Any]] = {}

        for t in trips:
            d = t.started_at.date()
            if d < start_day or d >= end_day:
                continue
            slot = by_day.setdefault(d, {"trips": 0, "charges": 0, "km": 0.0})
            slot["trips"] += 1
            slot["km"] += float(t.distance_km or 0)

        for c in charges:
            d = (c.started_at or c.ended_at).date()
            if d < start_day or d >= end_day:
                continue
            slot = by_day.setdefault(d, {"trips": 0, "charges": 0, "km": 0.0})
            slot["charges"] += 1

        events: list[CalendarEvent] = []
        for d in sorted(by_day):
            slot = by_day[d]
            parts: list[str] = []
            if slot["trips"]:
                parts.append(
                    f"{slot['trips']} viaje{'s' if slot['trips'] != 1 else ''}"
                )
            if slot["km"]:
                parts.append(f"{slot['km']:.1f} km")
            if slot["charges"]:
                parts.append(
                    f"{slot['charges']} carga{'s' if slot['charges'] != 1 else ''}"
                )
            summary = " · ".join(parts) if parts else "Actividad EV"
            events.append(
                CalendarEvent(
                    summary=summary,
                    start=d,
                    end=d + timedelta(days=1),
                    description=(
                        f"trips={slot['trips']} "
                        f"charges={slot['charges']} "
                        f"km={slot['km']:.1f}"
                    ),
                )
            )
        return events
