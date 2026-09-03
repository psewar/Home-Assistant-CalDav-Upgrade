"""Recurrence-aware CalDAV event access built on the ``ical`` library.

These helpers have **no Home Assistant imports** so they can be exercised standalone
against any CalDAV server (see ``tests/e2e_radicale.py``). The HA calendar entity and
coordinator are thin wrappers around them.

Design
------
A CalDAV server stores one *resource* (``<uid>.ics``) per event series: the master
VEVENT plus any ``RECURRENCE-ID`` overrides. We

* fetch resources **unexpanded** and let ``ical`` expand them (``timeline_tz``), which
  yields instances carrying ``uid``, ``recurrence_id`` and ``rrule`` – exactly what the
  Home Assistant calendar API needs to address single occurrences;
* apply edits/deletes with ``ical.store.EventStore`` – the same RFC 5545 logic Home
  Assistant's *Local Calendar* uses (single instance → override / EXDATE,
  this-and-future → UNTIL on the old series + a new series, no recurrence id → whole
  series);
* write the resulting component(s) back: the original resource is overwritten (or
  deleted when nothing of that UID is left), a split-off series becomes a new resource.

Datetimes are kept timezone-aware; the store adds the matching ``VTIMEZONE`` so a
weekly 17:00 stays 17:00 local across DST changes.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import logging
from typing import Any
import uuid

import caldav
from ical.calendar import Calendar
from ical.calendar_stream import IcsCalendarStream
from ical.event import Event
from ical.exceptions import CalendarParseError
from ical.store import EventStore, EventStoreError
from ical.types import Range, Recur
from ical.types.recur import RecurrenceId

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "EventInstance",
    "CalDavStoreError",
    "list_events",
    "create_event",
    "update_event",
    "delete_event",
    "parse_range",
]


class CalDavStoreError(Exception):
    """Raised when an event cannot be found or the edit is invalid."""


@dataclass(slots=True)
class EventInstance:
    """One (possibly expanded) event occurrence, backend-neutral."""

    uid: str | None
    summary: str
    start: dt.datetime | dt.date
    end: dt.datetime | dt.date
    description: str | None = None
    location: str | None = None
    rrule: str | None = None
    recurrence_id: str | None = None


def parse_range(value: str | None) -> Range:
    """Map the Home Assistant ``recurrence_range`` string to ``ical.types.Range``."""
    if value and value.upper() == Range.THIS_AND_FUTURE.value:
        return Range.THIS_AND_FUTURE
    return Range.NONE


# --------------------------------------------------------------------------- read


def _calendar_from_resource(obj: caldav.CalendarObjectResource) -> Calendar:
    obj.load(only_if_unloaded=True)
    return IcsCalendarStream.calendar_from_ics(obj.data)


def _instance_from_ical(event: Event) -> EventInstance:
    start = event.dtstart
    end = event.dtend if event.dtend is not None else event.end
    if isinstance(start, dt.datetime) and isinstance(end, dt.datetime):
        if end <= start:
            end = start + dt.timedelta(minutes=30)
    elif not isinstance(start, dt.datetime) and not isinstance(end, dt.datetime):
        if end <= start:
            end = start + dt.timedelta(days=1)
    return EventInstance(
        uid=event.uid,
        summary=event.summary or "",
        start=start,
        end=end,
        description=event.description,
        location=event.location,
        rrule=event.rrule.as_rrule_str() if event.rrule else None,
        recurrence_id=event.recurrence_id,
    )


def _instance_from_vobject(obj: caldav.CalendarObjectResource) -> EventInstance | None:
    """Fallback for resources ``ical`` refuses to parse: plain, non-expanded."""
    inst = obj.vobject_instance
    if inst is None or not hasattr(inst, "vevent"):
        return None
    vevent = inst.vevent

    def val(name: str) -> Any:
        return getattr(vevent, name).value if hasattr(vevent, name) else None

    start = vevent.dtstart.value
    if hasattr(vevent, "dtend"):
        end = vevent.dtend.value
    elif hasattr(vevent, "duration"):
        end = start + vevent.duration.value
    else:
        end = start + dt.timedelta(days=1)
    return EventInstance(
        uid=val("uid"),
        summary=val("summary") or "",
        start=start,
        end=end,
        description=val("description"),
        location=val("location"),
        rrule=val("rrule"),
        recurrence_id=None,
    )


def list_events(
    calendar: caldav.Calendar, start: dt.datetime, end: dt.datetime
) -> list[EventInstance]:
    """Return all occurrences overlapping ``[start, end)`` with recurrence metadata."""
    objects = calendar.search(start=start, end=end, event=True, expand=False)
    out: list[EventInstance] = []
    tzinfo = start.tzinfo or dt.timezone.utc
    for obj in objects:
        try:
            cal = _calendar_from_resource(obj)
        except (CalendarParseError, ValueError) as err:  # pydantic errors are ValueErrors
            _LOGGER.warning(
                "CalDAV resource %s could not be parsed as iCalendar (%s); "
                "showing it without recurrence support",
                getattr(obj, "url", "?"),
                err,
            )
            if (fallback := _instance_from_vobject(obj)) is not None:
                out.append(fallback)
            continue
        for event in cal.timeline_tz(tzinfo).overlapping(start, end):
            out.append(_instance_from_ical(event))
    return out


# -------------------------------------------------------------------------- write


def _build_event(params: dict[str, Any]) -> Event:
    """Turn the Home Assistant event dict (summary/dtstart/dtend/…/rrule) into an Event."""
    data = dict(params)
    rrule = data.get("rrule")
    if isinstance(rrule, str):
        data["rrule"] = Recur.from_rrule(rrule) if rrule else None
    elif rrule is None and "rrule" in data:
        data["rrule"] = None
    recur = data.get("rrule")
    if isinstance(recur, Recur) and isinstance(recur.until, dt.datetime):
        # RFC 5545: UNTIL must be UTC when DTSTART carries a TZID.
        if recur.until.tzinfo is not None:
            recur.until = recur.until.astimezone(dt.timezone.utc)
    try:
        return Event(**data)
    except (CalendarParseError, ValueError) as err:
        raise CalDavStoreError(f"Invalid event data: {err}") from err


def _ics_for(cal: Calendar, events: list[Event]) -> str:
    """Serialize ``events`` together with the calendar's timezones/other components."""
    subset = cal.model_copy(update={"events": events})
    return IcsCalendarStream.calendar_to_ics(subset)


def _load_resource(
    calendar: caldav.Calendar, uid: str
) -> tuple[caldav.CalendarObjectResource, Calendar]:
    objects = calendar.search(event=True, uid=uid)
    if not objects:
        raise CalDavStoreError(f"No event found with UID {uid}")
    obj = objects[0]
    try:
        return obj, _calendar_from_resource(obj)
    except (CalendarParseError, ValueError) as err:
        raise CalDavStoreError(f"Event {uid} could not be parsed: {err}") from err


def _naive(value: dt.datetime | dt.date) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    return dt.datetime.combine(value, dt.time.min)


def _split_forked_series(cal: Calendar, uid: str) -> None:
    """Turn a "this and future" fork into a separate series with its own UID.

    ``ical`` models "this and future" RFC 5545-style: a component with the *same* UID,
    a ``RECURRENCE-ID`` (range THISANDFUTURE) and its own ``RRULE``. Home Assistant's
    Local Calendar can read that back, but many CalDAV servers and clients (Horde,
    Apple, Thunderbird, …) do not implement RANGE=THISANDFUTURE and would show the
    future occurrences as a single event or drop them. So we do what those clients do
    themselves: the fork becomes a new series (fresh UID, no RECURRENCE-ID) and the old
    series keeps its UNTIL. Overrides of the old series that lie past the split point
    would be orphans and are dropped.
    """
    forks = [e for e in cal.events if e.uid == uid and e.recurrence_id and e.rrule]
    if not forks:
        return
    for fork in forks:
        split_at = _naive(RecurrenceId.to_value(fork.recurrence_id))
        cal.events = [
            e
            for e in cal.events
            if not (
                e.uid == uid
                and e is not fork
                and e.recurrence_id
                and not e.rrule
                and _naive(RecurrenceId.to_value(e.recurrence_id)) >= split_at
            )
        ]
        idx = cal.events.index(fork)
        cal.events[idx] = fork.model_copy(
            update={"uid": str(uuid.uuid4()), "recurrence_id": None, "sequence": 0}
        )


def _write_back(
    calendar: caldav.Calendar,
    obj: caldav.CalendarObjectResource,
    cal: Calendar,
    uid: str,
) -> None:
    _split_forked_series(cal, uid)
    keep = [e for e in cal.events if e.uid == uid]
    others = [e for e in cal.events if e.uid != uid]
    if keep:
        obj.data = _ics_for(cal, keep)
        obj.save()
    else:
        obj.delete()
    # A this-and-future edit forks a new series with its own UID → new resource.
    for new_uid in sorted({e.uid for e in others if e.uid}):
        calendar.save_event(ical=_ics_for(cal, [e for e in others if e.uid == new_uid]))


def create_event(calendar: caldav.Calendar, params: dict[str, Any]) -> str:
    """Create a new event (optionally recurring). Returns the new UID."""
    event = _build_event(params)
    cal = Calendar()
    EventStore(cal).add(event)  # also adds the VTIMEZONE components needed
    calendar.save_event(ical=IcsCalendarStream.calendar_to_ics(cal))
    return event.uid


def update_event(
    calendar: caldav.Calendar,
    uid: str,
    params: dict[str, Any],
    recurrence_id: str | None = None,
    recurrence_range: str | None = None,
) -> None:
    """Update a whole series (no recurrence_id), one instance or this-and-future."""
    obj, cal = _load_resource(calendar, uid)
    event = _build_event(params)
    try:
        EventStore(cal).edit(
            uid,
            event,
            recurrence_id=recurrence_id or None,
            recurrence_range=parse_range(recurrence_range),
        )
    except EventStoreError as err:
        raise CalDavStoreError(str(err)) from err
    _write_back(calendar, obj, cal, uid)


def delete_event(
    calendar: caldav.Calendar,
    uid: str,
    recurrence_id: str | None = None,
    recurrence_range: str | None = None,
) -> None:
    """Delete a whole series (no recurrence_id), one instance or this-and-future."""
    obj, cal = _load_resource(calendar, uid)
    try:
        EventStore(cal).delete(
            uid,
            recurrence_id=recurrence_id or None,
            recurrence_range=parse_range(recurrence_range),
        )
    except EventStoreError as err:
        raise CalDavStoreError(str(err)) from err
    _write_back(calendar, obj, cal, uid)
