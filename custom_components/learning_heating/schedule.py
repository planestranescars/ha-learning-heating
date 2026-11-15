"""Room schedules keyed by 'HH:MM', 'sunrise' or 'sunset' with an optional +/-HH:MM offset.

A schedule is either one mapping used every day, or two under `weekdays` and `weekends`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

_SPEC = re.compile(
    r"^(?:(?P<hm>\d{1,2}:\d{2})|(?P<sun>sunrise|sunset)(?:\s*(?P<sign>[+-])\s*(?P<off>\d{1,2}:\d{2}))?)$"
)

Entry = tuple[str, int, float, str]
DAY_GROUPS = {"weekdays": range(0, 5), "weekends": range(5, 7)}


def _minutes(text: str) -> int:
    hours, minutes = (int(x) for x in text.split(":"))
    if hours > 23 or minutes > 59:
        raise vol.Invalid(f"invalid time '{text}'")
    return hours * 60 + minutes


def parse_spec(spec: str) -> tuple[str, int]:
    match = _SPEC.match(spec.strip().lower())
    if not match:
        raise vol.Invalid(f"invalid schedule time '{spec}'")
    if match["hm"]:
        return "time", _minutes(match["hm"])
    offset = _minutes(match["off"]) if match["off"] else 0
    return match["sun"], -offset if match["sign"] == "-" else offset


def _entries(value, days: str = "") -> list[Entry]:
    if not isinstance(value, dict) or not value:
        raise vol.Invalid("schedule must map times to temperatures")
    entries = []
    for spec, temp in value.items():
        if not isinstance(spec, str):
            raise vol.Invalid(f"schedule time {spec!r} must be quoted, e.g. \"22:00\"")
        kind, minutes = parse_spec(spec)
        entries.append((kind, minutes, vol.Coerce(float)(temp), days))
    return entries


def validate_schedule(value) -> list[Entry]:
    if isinstance(value, dict) and value and set(value) <= set(DAY_GROUPS):
        if set(value) != set(DAY_GROUPS):
            raise vol.Invalid("a schedule split by day needs both 'weekdays' and 'weekends'")
        return [entry for days, part in value.items() for entry in _entries(part, days)]
    return _entries(value)


def entry_key(kind: str, minutes: int) -> str:
    if kind == "time":
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
    if minutes == 0:
        return kind
    sign = "-" if minutes < 0 else "+"
    return f"{kind}{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def scheduled_temps(entries: list[Entry]) -> dict[str, float]:
    """Each schedule entry's temperature, by the key that feedback adjustments use."""
    temps = {}
    for kind, minutes, temp, days in entries:
        key = entry_key(kind, minutes)
        temps[f"{days} {key}" if days else key] = temp
    return temps


def points_for_day(
    hass: HomeAssistant, day: date, entries: list[Entry]
) -> list[tuple[datetime, float, str]]:
    points = []
    for kind, minutes, temp, days in entries:
        if days and day.weekday() not in DAY_GROUPS[days]:
            continue
        if kind == "time":
            at = datetime.combine(day, time(minutes // 60, minutes % 60), dt_util.get_default_time_zone())
        else:
            event = get_astral_event_date(hass, kind, day)
            if event is None:
                continue
            at = dt_util.as_local(event) + timedelta(minutes=minutes)
        key = entry_key(kind, minutes)
        points.append((at, temp, f"{days} {key}" if days else key))
    return sorted(points)
