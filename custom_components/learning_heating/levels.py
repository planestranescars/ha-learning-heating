"""Learning how hard a heat pump needs to run.

A heat pump uses less energy when it is asked for no more than the room needs. How much
that is depends on the weather: on a mild day a low setting is plenty, in a cold snap
only full output keeps up.

So each spell starts at the output learnt for weather like today's. If the room is not
getting there, the output is stepped up during the spell, and next time in such weather
starts higher; that weather then does not go back to the output that failed for a
fortnight. If spells at an output keep ending quickly, it was more than needed, and next
time starts lower. A room far below its target, as at the start of a morning warm-up,
gets full output.
"""
from __future__ import annotations

import math
from typing import Any

BIN_C = 3.0            # outdoor temperatures within 3 degC count as the same weather
FAR_GAP = 1.5          # this far below target: full output
STEP_UP_MIN = 20       # minutes at an output without getting there before stepping up
SHORT_SPELL_MIN = 12   # a spell this short at an output means it was more than needed
SHORT_NEEDED = 2       # short spells in a row before starting lower
DEFAULT_LEVEL = 1
VERSION = 2            # what the outputs are; a new set of outputs starts learning afresh
FLOOR_DAYS = 14        # how long an output that failed stays ruled out for that weather


def weather_bin(outdoor: float | None) -> str:
    if outdoor is None:
        return "unknown"
    return str(int(math.floor(outdoor / BIN_C) * BIN_C))


class LevelLearner:
    """Which output each kind of weather needs, learnt from how spells went."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.table: dict[str, int] = {}
        self.short: dict[str, int] = {}
        self.floor: dict[str, tuple[int, str]] = {}

    @property
    def top(self) -> int:
        return self.count - 1

    def learnt(self, outdoor: float | None) -> int:
        return min(self.top, self.table.get(weather_bin(outdoor), DEFAULT_LEVEL))

    def start(self, outdoor: float | None, gap: float) -> tuple[int, bool]:
        """Output to start a spell at, and whether it is a warm-up from well below target."""
        if gap >= FAR_GAP:
            return self.top, True
        return self.learnt(outdoor), False

    def step_up(self, level: int, minutes_at_level: float, rising_short: bool) -> bool:
        """Raise the output during a spell that is not getting the room there."""
        return level < self.top and minutes_at_level >= STEP_UP_MIN and rising_short

    def _floor(self, key: str, today: str | None) -> int:
        level, since = self.floor.get(key, (0, ""))
        if today and since:
            from datetime import date
            try:
                if (date.fromisoformat(today) - date.fromisoformat(since)).days > FLOOR_DAYS:
                    return 0
            except ValueError:
                return 0
        return level

    def spell_done(
        self, outdoor: float | None, start_level: int, end_level: int, minutes: float,
        warm_up: bool, today: str | None = None,
    ) -> str | None:
        """Learn from a finished spell; say what changed, if anything."""
        if warm_up:
            return None
        key = weather_bin(outdoor)
        before = self.table.get(key, DEFAULT_LEVEL)
        if end_level > start_level:
            self.table[key] = min(self.top, max(before, end_level))
            self.floor[key] = (start_level + 1, today or "")
            self.short[key] = 0
        elif minutes < SHORT_SPELL_MIN and start_level > self._floor(key, today):
            self.short[key] = self.short.get(key, 0) + 1
            if self.short[key] >= SHORT_NEEDED:
                self.table[key] = start_level - 1
                self.short[key] = 0
        else:
            self.short[key] = 0
        after = self.table.get(key, DEFAULT_LEVEL)
        if after == before:
            return None
        way = "more" if after > before else "less"
        return f"with {key}° to {int(key) + int(BIN_C)}° outside, starts with {way} output (level {after + 1} of {self.count})" if key != "unknown" else f"starts with {way} output (level {after + 1} of {self.count})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION, "table": self.table, "short": self.short,
            "floor": {k: list(v) for k, v in self.floor.items()},
        }

    def load(self, data: dict[str, Any] | None) -> None:
        data = data or {}
        if data.get("version") != VERSION:
            return
        self.floor = {
            str(k): (max(0, min(self.top, int(v[0]))), str(v[1]))
            for k, v in data.get("floor", {}).items()
        }
        self.table = {str(k): max(0, min(self.top, int(v))) for k, v in data.get("table", {}).items()}
        self.short = {str(k): int(v) for k, v in data.get("short", {}).items()}
