"""Quiet hours and heaters that regulate themselves. No Home Assistant imports."""

from __future__ import annotations

from datetime import datetime, time, timedelta


def window(
    now: datetime, start: time, end: time, lead: timedelta = timedelta()
) -> tuple[datetime, datetime] | None:
    """The daily quiet window that `now` is inside, or within `lead` of entering."""
    for offset in (-1, 0, 1):
        day = now.date() + timedelta(days=offset)
        begins = datetime.combine(day, start, now.tzinfo)
        ends = datetime.combine(day, end, now.tzinfo)
        if ends <= begins:
            ends += timedelta(days=1)
        if begins - lead <= now < ends:
            return begins, ends
    return None


def calling(heating: bool, reading: float | None, setting: float | None) -> float:
    """Estimated share of the time a heater with its own thermostat is drawing power.

    Such heaters report no heating status. Below its setting it is taken to be on, above
    it off, and exactly at it (where the thermostat cycles) on half the time.
    """
    if not heating or reading is None or setting is None:
        return 0.0
    if reading < setting:
        return 1.0
    return 0.5 if reading == setting else 0.0


def round_to_step(value: float, step: float | None) -> float:
    """Round to the step a heater accepts, halves going up. Steps of 0.5 or finer are left alone."""
    if not step or step <= 0.5:
        return value
    return int(value / step + 0.5) * step
