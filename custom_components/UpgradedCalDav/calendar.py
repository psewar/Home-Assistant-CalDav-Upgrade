"""Support for WebDav Calendar."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

import caldav
from caldav.lib.error import DAVError
import requests
import voluptuous as vol

from homeassistant.components.calendar import (
    ENTITY_ID_FORMAT,
    PLATFORM_SCHEMA as CALENDAR_PLATFORM_SCHEMA,
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
    EVENT_RRULE,
    EVENT_SUMMARY,
    is_offset_reached,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    AddEntitiesCallback,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CalDavConfigEntry, caldav_store
from .api import async_get_calendars
from .const import (
    CONF_NAME,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from .coordinator import CalDavUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

CONF_CALENDARS = "calendars"
CONF_CUSTOM_CALENDARS = "custom_calendars"
CONF_CALENDAR = "calendar"
CONF_SEARCH = "search"
CONF_DAYS = "days"


# Number of days to look ahead for next event when configured by ConfigEntry
CONFIG_ENTRY_DEFAULT_DAYS = 7

# Only allow VCALENDARs that support this component type
SUPPORTED_COMPONENT = "VEVENT"

# Errors the CalDAV transport / store raise that should surface as a clean HA error
_WRITE_ERRORS = (caldav_store.CalDavStoreError, DAVError, requests.RequestException)

PLATFORM_SCHEMA = CALENDAR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_URL): vol.Url(),
        vol.Optional(CONF_CALENDARS, default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Inclusive(CONF_USERNAME, "authentication"): cv.string,
        vol.Inclusive(CONF_PASSWORD, "authentication"): cv.string,
        vol.Optional(CONF_CUSTOM_CALENDARS, default=[]): vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required(CONF_CALENDAR): cv.string,
                        vol.Required(CONF_NAME): cv.string,
                        vol.Required(CONF_SEARCH): cv.string,
                    }
                )
            ],
        ),
        vol.Optional(CONF_VERIFY_SSL, default=True): cv.boolean,
        vol.Optional(CONF_DAYS, default=1): cv.positive_int,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    disc_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the WebDav Calendar platform."""
    url = config[CONF_URL]
    username = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)
    days = config[CONF_DAYS]

    client = caldav.DAVClient(
        url, None, username, password, ssl_verify_cert=config[CONF_VERIFY_SSL]
    )

    calendars = await async_get_calendars(hass, client, SUPPORTED_COMPONENT)

    entities = []
    device_id: str | None
    for calendar in list(calendars):
        # If a calendar name was given in the configuration,
        # ignore all the others
        if config[CONF_CALENDARS] and calendar.name not in config[CONF_CALENDARS]:
            _LOGGER.debug("Ignoring calendar '%s'", calendar.name)
            continue

        # Create additional calendars based on custom filtering rules
        for cust_calendar in config[CONF_CUSTOM_CALENDARS]:
            # Check that the base calendar matches
            if cust_calendar[CONF_CALENDAR] != calendar.name:
                continue

            name = cust_calendar[CONF_NAME]
            device_id = f"{cust_calendar[CONF_CALENDAR]} {cust_calendar[CONF_NAME]}"
            entity_id = async_generate_entity_id(ENTITY_ID_FORMAT, device_id, hass=hass)
            coordinator = CalDavUpdateCoordinator(
                hass,
                None,
                calendar=calendar,
                days=days,
                include_all_day=True,
                search=cust_calendar[CONF_SEARCH],
            )
            entities.append(
                WebDavCalendarEntity(name, entity_id, coordinator, supports_offset=True)
            )

        # Create a default calendar if there was no custom one for all calendars
        # that support events.
        if not config[CONF_CUSTOM_CALENDARS]:
            name = calendar.name
            device_id = calendar.name
            entity_id = async_generate_entity_id(ENTITY_ID_FORMAT, device_id, hass=hass)
            coordinator = CalDavUpdateCoordinator(
                hass,
                None,
                calendar=calendar,
                days=days,
                include_all_day=False,
                search=None,
            )
            entities.append(
                WebDavCalendarEntity(name, entity_id, coordinator, supports_offset=True)
            )

    async_add_entities(entities, True)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalDavConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the CalDav calendar platform for a config entry."""
    calendars = await async_get_calendars(hass, entry.runtime_data, SUPPORTED_COMPONENT)
    async_add_entities(
        (
            WebDavCalendarEntity(
                calendar.name,
                async_generate_entity_id(ENTITY_ID_FORMAT, calendar.name, hass=hass),
                CalDavUpdateCoordinator(
                    hass,
                    entry,
                    calendar=calendar,
                    days=CONFIG_ENTRY_DEFAULT_DAYS,
                    include_all_day=True,
                    search=None,
                ),
                unique_id=f"{entry.entry_id}-{calendar.id}",
            )
            for calendar in calendars
            if calendar.name
        ),
        True,
    )


class WebDavCalendarEntity(CoordinatorEntity[CalDavUpdateCoordinator], CalendarEntity):
    """A device for getting the next Task from a WebDav Calendar."""

    def __init__(
        self,
        name: str | None,
        entity_id: str,
        coordinator: CalDavUpdateCoordinator,
        unique_id: str | None = None,
        supports_offset: bool = False,
    ) -> None:
        """Create the WebDav Calendar Event Device."""
        super().__init__(coordinator)
        self.entity_id = entity_id
        self._event: CalendarEvent | None = None
        self._attr_name = name
        if unique_id is not None:
            self._attr_unique_id = unique_id
        self._supports_offset = supports_offset
        self._attr_supported_features = (
            CalendarEntityFeature.CREATE_EVENT
            | CalendarEntityFeature.UPDATE_EVENT
            | CalendarEntityFeature.DELETE_EVENT
        )

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        return self._event

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Get all events in a specific time frame."""
        return await self.coordinator.async_get_events(hass, start_date, end_date)

    # ------------------------------------------------------------------ CRUD
    # All three go through caldav_store (ical-based) so recurring series behave like
    # Home Assistant's Local Calendar: a single occurrence, "this and future" or the
    # whole series can be changed or deleted.

    async def async_create_event(self, **kwargs: Any) -> None:
        """Add a new (optionally recurring) event to the calendar."""
        try:
            uid = await self.hass.async_add_executor_job(
                caldav_store.create_event, self.coordinator.calendar, dict(kwargs)
            )
        except _WRITE_ERRORS as err:
            _LOGGER.error("Error creating calendar event: %s", err)
            raise HomeAssistantError(f"Error creating calendar event: {err}") from err
        _LOGGER.debug(
            "Created event %s (%s%s)",
            uid,
            kwargs.get(EVENT_SUMMARY),
            f", rrule={kwargs[EVENT_RRULE]}" if kwargs.get(EVENT_RRULE) else "",
        )
        await self.coordinator.async_refresh()

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete an event: whole series, one occurrence or this-and-future."""
        try:
            await self.hass.async_add_executor_job(
                caldav_store.delete_event,
                self.coordinator.calendar,
                uid,
                recurrence_id,
                recurrence_range,
            )
        except _WRITE_ERRORS as err:
            _LOGGER.error("Error deleting calendar event: %s", err)
            raise HomeAssistantError(f"Error deleting calendar event: {err}") from err
        await self.coordinator.async_refresh()

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Update an event: whole series, one occurrence or this-and-future."""
        try:
            await self.hass.async_add_executor_job(
                caldav_store.update_event,
                self.coordinator.calendar,
                uid,
                dict(event),
                recurrence_id,
                recurrence_range,
            )
        except _WRITE_ERRORS as err:
            _LOGGER.error("Error updating calendar event: %s", err)
            raise HomeAssistantError(f"Error updating calendar event: {err}") from err
        await self.coordinator.async_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update event data."""
        self._event = self.coordinator.data
        if self._supports_offset:
            self._attr_extra_state_attributes = {
                "offset_reached": is_offset_reached(
                    self._event.start_datetime_local,
                    self.coordinator.offset,  # type: ignore[arg-type]
                )
                if self._event
                else False
            }
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass update state from existing coordinator data."""
        await super().async_added_to_hass()
        self._handle_coordinator_update()
