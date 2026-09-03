# Home Assistant CalDAV Integration with Event Management

An enhanced version of the Home Assistant CalDAV integration (domain `n0tcaldav`,
shown as **UpgradedCalDav**) that adds full event management from the Home Assistant UI
and from custom cards – **including recurring events**.

This repository is a fork of
[TheOddPirate/Home-Assistant-CalDav-Upgrade](https://github.com/TheOddPirate/Home-Assistant-CalDav-Upgrade)
with the recurrence handling rebuilt on the [`ical`](https://github.com/allenporter/ical)
library (the one Home Assistant's own *Local Calendar* uses). Config entries created with
the original are compatible – same domain, same settings.

## Features

- All original CalDAV integration features (read-only calendar entities, next-event state)
- **Create** events, optionally with a recurrence rule (`rrule`, e.g. `FREQ=WEEKLY;BYDAY=TU`)
- **Edit** and **delete** events – for a series either
  - a **single occurrence** (written as an exception / `EXDATE`, the series stays intact),
  - **this and all future** occurrences (the series ends before this one via `UNTIL` and a
    new series with its own UID starts here – the way Apple/Google clients split a series,
    so Horde, Thunderbird, phones … all agree), or
  - the **whole series**
- Occurrences returned to Home Assistant carry `uid`, `recurrence_id` and `rrule`, so the
  built-in calendar dialogs and custom cards can address one occurrence
- Events are stored with `TZID` + `VTIMEZONE`, so a weekly 17:00 stays 17:00 across DST
- Unknown properties of foreign events (`X-…`, `VALARM`, attendees) survive edits

### What changed compared to the original fork

| Operation | Original | This fork |
| --- | --- | --- |
| Create with `rrule` | crashed without `COUNT`, dropped `INTERVAL`/`BYDAY` | full RRULE |
| Create (plain) | wrote the event but raised *Type 'str' cannot be serialized* | clean |
| Update one occurrence | wrote a `RECURRENCE-ID` into the series **master** | exception instance |
| Update this-and-future | ignored | `UNTIL` + new series |
| Delete one occurrence | deleted the **whole series** | `EXDATE` |
| Listing | no `recurrence_id` / `rrule` | both present |

## Installation

### HACS (recommended)

1. HACS → three-dot menu → **Custom repositories** → add this repository, category *Integration*
2. Install **UpgradedCalDav** and restart Home Assistant
3. Settings → Devices & services → *Add integration* → **UpgradedCalDav**

If you come from the original fork: uninstall it in HACS, add this repository, install, restart.
The existing config entry keeps working (same domain `n0tcaldav`).

### Manual

Copy `custom_components/UpgradedCalDav` into your `custom_components` folder and restart.

## Configuration

Through the UI: server URL, username, password, optional SSL verification.

## Requirements

- Home Assistant 2026.8 or newer (pins `ical==14.1.1`; the manifest requires `ical>=14.1.1`)
- Python packages: `caldav==1.3.9`, `icalendar==6.1.0`, `ical>=14.1.1`

## Tested with

- Horde / Kronolith (via `rpc.php` CalDAV)
- Radicale (the end-to-end test suite runs against it)
- Baikal / Nextcloud were reported working with the original fork

## Development

`caldav_store.py` has no Home Assistant imports and can be exercised against any CalDAV
server. The end-to-end test (`tests/e2e_radicale.py` in the development workspace) covers:
single events, weekly series, DST stability, single-occurrence edit, this-and-future split,
`EXDATE`, `UNTIL`, whole-series delete, all-day Mo–Fr series, a Horde-style resource round
trip (X-properties and alarms preserved) and the error paths.

## Credits

- The Home Assistant team for the original CalDAV integration
- [TheOddPirate](https://github.com/TheOddPirate) for the editable fork this builds on
- [allenporter/ical](https://github.com/allenporter/ical) for the RFC 5545 heavy lifting
- The `caldav` library maintainers

## Contributing

Issues and pull requests welcome.
