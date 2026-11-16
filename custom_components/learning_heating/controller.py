"""Learns each room and drives its valve or heater setpoints."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.components.climate import HVACMode
from homeassistant.components.recorder import get_instance, history
from homeassistant.const import (
    ATTR_TEMPERATURE,
    EVENT_HOMEASSISTANT_STARTED,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_UPDATE
from .model import RoomLearner
from .quiet import calling, round_to_step, window
from .schedule import points_for_day, scheduled_temps
from .valves import shut_gap

_LOGGER = logging.getLogger(__name__)

TICK = timedelta(minutes=5)
FIRST_TICK_DELAY_S = 60
FORECAST_REFRESH = timedelta(minutes=30)
SAVE_DELAY_S = 600
SAVE_INTERVAL = timedelta(minutes=15)
WRITE_RETRY = timedelta(minutes=10)
OWN_WRITE_WINDOW = timedelta(minutes=15)
OVERRIDE_FALLBACK = timedelta(hours=4)
FALLBACK_LEAD_H = 1.0
LEAD_MARGIN = 1.2
LEAD_SLACK_H = 0.25
COAST_MAX_H = 1.0
COAST_TOLERANCE = 0.5
WARM_WINDOW_H = 3
WARM_HYSTERESIS = 1.0
DEFAULT_OUTDOOR = 8.0
MIN_TARGET, MAX_TARGET = 5.0, 30.0
COMFORT_STEP = 0.5
COMFORT_LIMIT = 3.0
MAX_FEEDBACK_JUMP = 1.5
LEAD_STEP_H = 0.5
MAX_EXTRA_LEAD_H = 3.0
BEHIND_TARGET = 0.5
REPEAT_PRESS = timedelta(minutes=10)
NORMAL_SHUT_GAP = 1.0
MIN_VALVE_CORRECTION = 1.5
MAX_VALVE_CORRECTION = 4.0
EASING_EXIT = 0.5
VALVE_LEARN_INTERVAL = timedelta(hours=24)
COLD_MARGIN = 1.0
AWAY_DELAY = timedelta(minutes=20)
LONG_AWAY = timedelta(hours=12)
AWAY_DROP = 2.0
STATE_HOME = "home"
QUIET_PREP = timedelta(minutes=20)
ROOM_AWAY = timedelta(hours=2)
TRIAL_STEP = 0.5
TRIAL_DAYS = 7
TRIAL_CALM = timedelta(days=7)
TRIAL_LIMIT = 2.0
FLOOR_HOLD = timedelta(days=30)
IDLE_REASONS = ("unoccupied", "out", "away", "warm outside")
BOOST_GAP = 2.0
DEFAULT_HEATER_W = 1000.0
FEEDBACK_LOG_SIZE = 20
FEEDBACK_MAX_AGE = timedelta(days=2)
BAD_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)
REASON_VALVE_OFF = "valve switched off by hand"
REASON_QUIET = "quiet hours"
HAND_LEARN_REASONS = ("schedule", REASON_QUIET, "manual override")


def _round_target(value: float) -> float:
    return max(MIN_TARGET, min(MAX_TARGET, round(value * 2) / 2))


def _when(at: datetime) -> str:
    at = dt_util.as_local(at)
    days = (at.date() - dt_util.now().date()).days
    if days == 0:
        return f"{at:%H:%M}"
    if days == 1:
        return f"tomorrow {at:%H:%M}"
    return f"{at:%a %H:%M}"


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Room:
    def __init__(self, slug: str, conf: dict, default_setback: float) -> None:
        self.slug = slug
        self.name: str = conf["name"]
        self.climate: str = conf["climate"]
        self.valve: str | None = conf.get("valve")
        self.zone: str | None = conf.get("zone")
        self.boost: str | None = conf.get("boost")
        self.power: float | None = conf.get("power")
        self.boost_power: float | None = conf.get("boost_power")
        quiet = conf.get("quiet")
        self.quiet = (quiet["from"], quiet["to"]) if quiet else None
        self.quiet_until: datetime | None = None
        self.boost_wanted: bool | None = None
        self.energy = 0.0
        self.energy_yesterday: float | None = None
        self.people: list[str] = conf["people"]
        self.out_since: datetime | None = None
        self.who = ""
        self.trial: dict[str, Any] | None = None
        self.floors: dict[str, str] = {}
        self.tried: dict[str, str] = {}
        self.calm_since: str | None = None
        self.schedule = conf["schedule"]
        self.setback: float = conf.get("setback", default_setback)
        self.occupied: bool = conf["occupied"]
        self.learner = RoomLearner()
        self.valve_open = 0.0
        self.target: float | None = None
        self.wanted: float | None = None
        self.setpoint: float | None = None
        self.shut_gap: float | None = None
        self.easing = False
        self.cold_hours = 0.0
        self.reason = "starting"
        self.next_change: datetime | None = None
        self.next_target: float | None = None
        self.lead_hours: float | None = None
        self.override_until: datetime | None = None
        self.last_write: tuple[float, datetime] | None = None
        self.recent_writes: list[tuple[float, datetime]] = []
        self.resume_requested = False
        self.event_at: datetime | None = None
        self.plan_hold: tuple[str, datetime] | None = None
        self.comfort: dict[str, float] = {}
        self.extra_lead = 0.0
        self.feedback_log: list[dict[str, Any]] = []

    @property
    def summary(self) -> str:
        """What the room is doing, in plain words."""
        if self.reason == REASON_VALVE_OFF:
            return "Switched off by hand"
        if self.target is None or self.reason == "starting":
            return "Starting up"
        temp = f"{self.target:g}°"
        if self.override_until is not None:
            return f"Set by hand to {temp} until {_when(self.override_until)}"
        if self.reason == "unoccupied":
            return f"Not in use, kept at {temp}"
        if self.reason == "out":
            return f"{self.who + ' out' if self.who else 'Nobody home'}, so eased off to {temp}"
        if self.reason == "away":
            return f"{self.who + ' out' if self.who else 'Nobody home'} for a while, kept at {temp}"
        if self.reason == "warm outside":
            return f"Mild outside, so eased off to {temp}"
        if self.reason == REASON_QUIET and self.quiet_until:
            return f"Quiet hours: left at {temp} until {_when(self.quiet_until)}"
        if self.reason.startswith("pre-heating") and self.event_at:
            return f"Warming up to {temp} for {_when(self.event_at)}"
        if self.reason.startswith("coasting") and self.event_at:
            return f"Easing off to {temp}; it'll stay warm until {_when(self.event_at)}"
        if self.next_change is not None:
            return f"Keeping {temp} until {_when(self.next_change)}"
        return f"Keeping {temp}"

    @property
    def valve_correction(self) -> float:
        """How much lower to set a valve that stays open well above its setpoint."""
        if self.shut_gap is None:
            return 0.0
        extra = self.shut_gap - NORMAL_SHUT_GAP
        return min(MAX_VALVE_CORRECTION, extra) if extra >= MIN_VALVE_CORRECTION else 0.0

    def quiet_window(
        self, now: datetime, lead: timedelta = timedelta()
    ) -> tuple[datetime, datetime] | None:
        """The quiet hours `now` is in, or within `lead` of. Nothing is sent during them."""
        return window(now, *self.quiet, lead) if self.quiet else None

    @property
    def held(self) -> bool:
        """Overridden by hand, or its valve switched off by hand."""
        return self.override_until is not None or self.reason == REASON_VALVE_OFF

    @property
    def trial_summary(self) -> str | None:
        if self.trial is None:
            return None
        return (
            f"Trying {TRIAL_STEP:g}° less from {self.trial['period']}: "
            f"{len(self.trial['days'])} of {TRIAL_DAYS} days without complaint"
        )

    def comfort_as_dict(self) -> dict[str, Any]:
        return {
            "offsets": self.comfort, "extra_lead": self.extra_lead, "log": self.feedback_log,
            "trial": self.trial, "floors": self.floors, "tried": self.tried,
            "calm_since": self.calm_since,
        }

    def load_comfort(self, data: dict[str, Any]) -> None:
        self.comfort = {k: float(v) for k, v in data.get("offsets", {}).items()}
        self.extra_lead = float(data.get("extra_lead", 0.0))
        self.feedback_log = list(data.get("log", []))
        self.trial = data.get("trial")
        self.floors = dict(data.get("floors", {}))
        self.tried = dict(data.get("tried", {}))
        self.calm_since = data.get("calm_since")
        # Forget what was learnt about parts of the day the schedule no longer has.
        known = scheduled_temps(self.schedule)
        self.comfort = {k: v for k, v in self.comfort.items() if k in known}
        self.floors = {k: v for k, v in self.floors.items() if k in known}
        self.tried = {k: v for k, v in self.tried.items() if k in known}
        if self.trial is not None and self.trial.get("period") not in known:
            self.trial = None


class HeatingController:
    def __init__(self, hass: HomeAssistant, conf: dict) -> None:
        self.hass = hass
        self.weather: str = conf["weather"]
        self.zones: dict[str, str] = conf["zones"]
        self.warm_cutoff: float = conf["warm_cutoff"]
        self.max_preheat: float = conf["max_preheat_hours"]
        self.history_days: int = conf["history_days"]
        self.people: list[str] = conf["people"]
        self.energy_price: float | None = conf.get("energy_price")
        self.guests = False
        self.out_since: datetime | None = None
        self.rooms = {
            slug: Room(slug, room, conf["setback"]) for slug, room in conf["rooms"].items()
        }
        self.enabled = True
        self.outdoor: float | None = None
        self.forecast: list[tuple[float, float]] = []
        self.warm = False
        self._zone_on: dict[str, bool] = {zone: False for zone in self.zones}
        self._store: Store = Store(hass, 1, f"{DOMAIN}.models")
        self._lock = asyncio.Lock()
        self._ticking = False
        self._written: dict[str, float] = {}
        self._unchecked = set(self.rooms)
        self.score_date: str | None = None
        self.cold_yesterday: float | None = None
        self._last_scored: datetime | None = None

    # ---- lifecycle -------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._written: dict[str, float] = data.get("written", {})
        for slug, gap in data.get("valve_gaps", {}).items():
            if slug in self.rooms and gap is not None:
                self.rooms[slug].shut_gap = float(gap)
        score = data.get("score", {})
        self.score_date = score.get("date")
        self.cold_yesterday = score.get("yesterday")
        for slug, hours in score.get("cold", {}).items():
            if slug in self.rooms:
                self.rooms[slug].cold_hours = float(hours)
        for slug, kwh in score.get("energy", {}).items():
            if slug in self.rooms:
                self.rooms[slug].energy = float(kwh)
        for slug, kwh in score.get("energy_yesterday", {}).items():
            if slug in self.rooms and kwh is not None:
                self.rooms[slug].energy_yesterday = float(kwh)
        for slug, saved in data.get("comfort", {}).items():
            if slug in self.rooms:
                self.rooms[slug].load_comfort(saved)
        for slug, saved in data.get("rooms", {}).items():
            if slug in self.rooms:
                try:
                    self.rooms[slug].learner = RoomLearner.from_dict(saved)
                except (KeyError, TypeError, ValueError):
                    _LOGGER.warning("Discarding unreadable saved model for %s", slug)

    @callback
    def async_start(self) -> None:
        if self.hass.state is CoreState.running:
            self._async_started()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._async_started)

    @callback
    def _async_started(self, _event: Event | None = None) -> None:
        now = dt_util.now().timestamp()
        self._seed_inputs(now)
        for room in self.rooms.values():
            state = self.hass.states.get(room.climate)
            temp = _float(state.attributes.get("current_temperature")) if state else None
            if temp is not None:
                room.learner.observe(now, temp)

        climates = {room.climate: room for room in self.rooms.values()}
        valves = {room.valve: room for room in self.rooms.values() if room.valve}
        heaters = {
            entity: room
            for room in self.rooms.values() if room.zone is None
            for entity in (room.climate, room.boost) if entity
        }
        zones = {entity: zone for zone, entity in self.zones.items()}
        track = partial(async_track_state_change_event, self.hass)
        track(list(climates), callback(lambda e: self._on_climate(climates[e.data["entity_id"]], e)))
        track(list(valves), callback(lambda e: self._on_valve(valves[e.data["entity_id"]], e)))
        track(list(heaters), callback(lambda e: self._on_heater(heaters[e.data["entity_id"]], e)))
        track(list(zones), callback(lambda e: self._on_zone(zones[e.data["entity_id"]], e)))
        track([self.weather], self._on_weather)
        people = set(self.people) | {p for room in self.rooms.values() for p in room.people}
        if people:
            track(list(people), self._on_person)

        async_track_time_interval(self.hass, self._async_tick, TICK)
        async_track_time_interval(self.hass, self._async_refresh_forecast, FORECAST_REFRESH)
        async_track_time_interval(self.hass, self._async_save, SAVE_INTERVAL)
        async_track_time_interval(self.hass, self._async_learn_valves, VALVE_LEARN_INTERVAL)
        self.hass.async_create_background_task(self._async_learn_valves(), f"{DOMAIN} valve learning")
        self.hass.async_create_task(self._async_refresh_forecast())
        async_call_later(self.hass, FIRST_TICK_DELAY_S, self._async_first_tick)

        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        untrained = [
            r for r in self.rooms.values()
            if r.zone is not None and all(m.samples == 0 for m in r.learner.models)
        ]
        if untrained:
            self.hass.async_create_background_task(
                self._async_bootstrap(untrained), f"{DOMAIN} history bootstrap"
            )

    async def _async_first_tick(self, _now: datetime) -> None:
        self._ticking = True
        await self.async_tick()

    def _adopt_change_made_while_stopped(self, room: Room, state: State, now: datetime) -> bool:
        """On the first write after startup, treat a valve that differs from what we last set
        (or, the very first time, from what we would set) as changed by hand."""
        self._unchecked.discard(room.slug)
        current = _float(state.attributes.get(ATTR_TEMPERATURE))
        expected = self._written.get(room.slug, room.setpoint)
        if state.state != HVACMode.HEAT or current is None or abs(current - expected) < 0.25:
            return False
        room.override_until = room.next_change or now + OVERRIDE_FALLBACK
        room.target = current
        room.reason = "manual override"
        _LOGGER.info(
            "%s is at %.1f, not %.1f as last set; holding until %s",
            room.name, current, expected, room.override_until.strftime("%a %H:%M"),
        )
        return True

    def _seed_inputs(self, now: float) -> None:
        weather = self.hass.states.get(self.weather)
        self.outdoor = _float(weather.attributes.get(ATTR_TEMPERATURE)) if weather else None
        for zone, entity in self.zones.items():
            state = self.hass.states.get(entity)
            self._zone_on[zone] = state is not None and state.state == STATE_ON
        for room in self.rooms.values():
            state = self.hass.states.get(room.valve) if room.valve else None
            room.valve_open = (_float(state.state) or 0.0) / 100 if state else 0.0
            if self.outdoor is not None:
                room.learner.set_outdoor(now, self.outdoor)
            room.learner.set_valve(now, self._effective_valve(room))

    # ---- inputs ----------------------------------------------------------

    def _effective_valve(self, room: Room) -> float:
        """How hard the room is being heated, from 0 to 1."""
        if room.zone is None:
            return self._heater_output(room)[0]
        return room.valve_open if self._zone_on.get(room.zone) else 0.0

    def _heater_output(self, room: Room) -> tuple[float, float]:
        """For a room of self-regulating heaters: share of full output, and estimated watts."""

        def share(entity: str | None) -> float:
            state = self.hass.states.get(entity) if entity else None
            if state is None:
                return 0.0
            return calling(
                state.state == HVACMode.HEAT,
                _float(state.attributes.get("current_temperature")),
                _float(state.attributes.get(ATTR_TEMPERATURE)),
            )

        main, boost = share(room.climate), share(room.boost)
        main_w = room.power or DEFAULT_HEATER_W
        boost_w = (room.boost_power or main_w) if room.boost else 0.0
        watts = main * (room.power or 0.0) + boost * (room.boost_power or 0.0)
        return (main * main_w + boost * boost_w) / (main_w + boost_w), watts

    def heater_watts(self, room: Room) -> float:
        return self._heater_output(room)[1]

    @callback
    def _on_heater(self, room: Room, event: Event) -> None:
        new: State | None = event.data["new_state"]
        if new is not None:
            room.learner.set_valve(new.last_updated.timestamp(), self._effective_valve(room))

    @callback
    def _on_valve(self, room: Room, event: Event) -> None:
        new: State | None = event.data["new_state"]
        value = _float(new.state) if new else None
        if value is None:
            return
        room.valve_open = max(0.0, min(1.0, value / 100))
        room.learner.set_valve(new.last_updated.timestamp(), self._effective_valve(room))

    @callback
    def _on_zone(self, zone: str, event: Event) -> None:
        new: State | None = event.data["new_state"]
        if new is None or new.state in BAD_STATES:
            return
        self._zone_on[zone] = new.state == STATE_ON
        now = new.last_updated.timestamp()
        for room in self.rooms.values():
            if room.zone == zone:
                room.learner.set_valve(now, self._effective_valve(room))

    @callback
    def _on_weather(self, event: Event) -> None:
        new: State | None = event.data["new_state"]
        value = _float(new.attributes.get(ATTR_TEMPERATURE)) if new else None
        if value is None:
            return
        self.outdoor = value
        for room in self.rooms.values():
            room.learner.set_outdoor(new.last_updated.timestamp(), value)

    @callback
    def _on_person(self, event: Event) -> None:
        """Someone came or went: re-plan straight away rather than at the next tick."""
        old: State | None = event.data["old_state"]
        new: State | None = event.data["new_state"]
        if self._ticking and (old is None or new is None or old.state != new.state):
            self.hass.async_create_task(self.async_tick())

    def _out_since(
        self, people: list[str], previous: datetime | None, now: datetime
    ) -> datetime | None:
        """Since when all of these people have been out. Unknown counts as home."""
        states = [self.hass.states.get(person) for person in people]
        everyone_out = bool(states) and all(
            state is not None and state.state not in BAD_STATES and state.state != STATE_HOME
            for state in states
        )
        if not everyone_out:
            return None
        return previous or min(now, max(state.last_changed for state in states))

    def _update_presence(self, now: datetime) -> None:
        self.out_since = self._out_since(self.people, self.out_since, now)
        for room in self.rooms.values():
            if room.people:
                room.out_since = self._out_since(room.people, room.out_since, now)
                names = [s.name for p in room.people if (s := self.hass.states.get(p)) is not None]
                room.who = " and ".join(names)

    def _room_out(self, room: Room, now: datetime, quiet: bool) -> tuple[timedelta | None, timedelta]:
        """How long the people a room follows have been out, and how long counts as away.

        A room with its own people ignores everyone else. Going into quiet hours, only a
        long absence counts: a brief one must not leave the room cold all night.
        """
        since, long_away = (room.out_since, ROOM_AWAY) if room.people else (self.out_since, LONG_AWAY)
        if self.guests or since is None or now - since < AWAY_DELAY:
            return None, long_away
        out = now - since
        return (None if quiet and out < long_away else out), long_away

    @callback
    def _on_climate(self, room: Room, event: Event) -> None:
        new: State | None = event.data["new_state"]
        old: State | None = event.data["old_state"]
        if new is None or new.state in BAD_STATES:
            return
        temp = _float(new.attributes.get("current_temperature"))
        old_temp = _float(old.attributes.get("current_temperature")) if old else None
        if temp is not None and temp != old_temp:
            room.learner.observe(new.last_updated.timestamp(), temp)
            self._store.async_delay_save(self._data, SAVE_DELAY_S)
            async_dispatcher_send(self.hass, SIGNAL_UPDATE)

        if old is None or old.state in BAD_STATES:
            return
        target = _float(new.attributes.get(ATTR_TEMPERATURE))
        if target is None or target == _float(old.attributes.get(ATTR_TEMPERATURE)):
            return
        now = dt_util.now()
        room.recent_writes = [w for w in room.recent_writes if now - w[1] < OWN_WRITE_WINDOW]
        if any(abs(value - target) < 0.25 for value, _ in room.recent_writes):
            return
        if room.target is not None and abs(target - room.target) < 0.25:
            return
        upcoming = [at for at, _ in self.timeline(room, now) if at > now]
        room.override_until = upcoming[0] if upcoming else now + OVERRIDE_FALLBACK
        self._hand_change(room, now, target, room.target)
        room.target = target
        room.reason = "manual override"
        _LOGGER.info(
            "%s set to %.1f outside the controller; holding until %s",
            room.name, target, room.override_until.strftime("%a %H:%M"),
        )
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def _async_refresh_forecast(self, _now: datetime | None = None) -> None:
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": self.weather, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not fetch forecast from %s: %s", self.weather, err)
            return
        points = []
        for item in (response or {}).get(self.weather, {}).get("forecast", []):
            when = dt_util.parse_datetime(str(item.get("datetime", "")))
            temp = _float(item.get("temperature"))
            if when is not None and temp is not None:
                points.append((when.timestamp(), temp))
        self.forecast = sorted(points)

    # ---- decisions -------------------------------------------------------

    def _outdoor_fn(self, now: datetime):
        start = now.timestamp()
        current = self.outdoor if self.outdoor is not None else DEFAULT_OUTDOOR
        points = self.forecast

        def outdoor_at(hours: float) -> float:
            t = start + hours * 3600
            if not points or t <= points[0][0]:
                return current
            for (t0, v0), (t1, v1) in zip(points, points[1:]):
                if t0 <= t <= t1:
                    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
            return points[-1][1]

        return outdoor_at

    def _update_warm(self, now: datetime) -> None:
        outdoor_at = self._outdoor_fn(now)
        mean = sum(outdoor_at(h) for h in range(WARM_WINDOW_H + 1)) / (WARM_WINDOW_H + 1)
        threshold = self.warm_cutoff - WARM_HYSTERESIS if self.warm else self.warm_cutoff
        self.warm = mean > threshold

    def timeline(self, room: Room, now: datetime) -> list[tuple[datetime, float]]:
        today = now.date()
        points: list[tuple[datetime, float]] = []
        for offset in (-1, 0, 1, 2):
            for at, temp, key in points_for_day(self.hass, today + timedelta(days=offset), room.schedule):
                points.append((at, temp + room.comfort.get(key, 0.0)))
        return sorted(points)

    def _period_at(self, room: Room, when: datetime) -> str | None:
        points: list[tuple[datetime, float, str]] = []
        for offset in (-1, 0):
            points += points_for_day(self.hass, when.date() + timedelta(days=offset), room.schedule)
        past = [key for at, _, key in sorted(points) if at <= when]
        return past[-1] if past else None

    def _scheduled_at(self, room: Room, when: datetime) -> float | None:
        """The scheduled temperature at a moment, with what feedback has already changed."""
        points: list[tuple[datetime, float, str]] = []
        for offset in (-1, 0):
            points += points_for_day(self.hass, when.date() + timedelta(days=offset), room.schedule)
        past = [(temp, key) for at, temp, key in sorted(points) if at <= when]
        return past[-1][0] + room.comfort.get(past[-1][1], 0.0) if past else None

    def _warmer_period_before(self, room: Room, when: datetime) -> str | None:
        """The most recent earlier schedule entry that was warmer than the one in force at `when`."""
        points: list[tuple[datetime, float, str]] = []
        for offset in (-1, 0):
            for at, temp, key in points_for_day(self.hass, when.date() + timedelta(days=offset), room.schedule):
                points.append((at, temp + room.comfort.get(key, 0.0), key))
        past = [p for p in sorted(points) if p[0] <= when]
        if len(past) < 2:
            return None
        current = past[-1][1]
        for _, temp, key in reversed(past[:-1]):
            if temp > current:
                return key
        return None

    def _decide(self, room: Room, now: datetime) -> None:
        points = self.timeline(room, now)
        # Nothing can be sent in quiet hours, so shortly before them the room is set for
        # the schedule as it stands when they begin, and left there until they end.
        quiet = room.quiet_window(now, QUIET_PREP)
        room.quiet_until = quiet[1] if quiet else None
        at = quiet[0] if quiet else now
        past = [p for p in points if p[0] <= at]
        upcoming = [p for p in points if p[0] > now]
        base = past[-1][1] if past else room.setback
        room.next_change, room.next_target = upcoming[0] if upcoming else (None, None)
        room.lead_hours = None

        if room.override_until is not None:
            if now < room.override_until:
                room.wanted = room.setpoint = room.target
                room.easing = False
                return
            room.override_until = None

        out, long_away = self._room_out(room, now, quiet is not None)
        if not room.occupied:
            target, reason = room.setback, "unoccupied"
        elif out is not None and out >= long_away:
            target, reason = min(base, room.setback), "away"
        elif out is not None:
            target, reason = max(min(base, room.setback), base - AWAY_DROP), "out"
        elif self.warm:
            target, reason = min(base, room.setback), "warm outside"
        elif quiet:
            target, reason = base, REASON_QUIET
        else:
            target, reason = self._plan(room, now, base, upcoming)

        room.target = _round_target(target)
        room.reason = reason
        # What the room is meant to be at right now, ignoring early warm-up or easing off.
        room.wanted = min(base, room.target) if reason.startswith("pre-heating") else (
            base if reason.startswith("coasting") else room.target
        )

    def _update_setpoint(self, room: Room, state: State) -> None:
        """What to send the valve: the target, or lower for a valve that won't shut by itself."""
        room.setpoint = room.target
        if room.target is not None:
            step = _float(state.attributes.get("target_temp_step"))
            room.setpoint = round_to_step(room.target, step)
        correction = room.valve_correction
        temp = _float(state.attributes.get("current_temperature"))
        if room.target is None or not correction or temp is None:
            room.easing = False
            return
        if temp >= room.target:
            room.easing = True
        elif temp < room.target - EASING_EXIT:
            room.easing = False
        if room.easing:
            room.setpoint = _round_target(room.target - correction)

    # ---- trying less heat ------------------------------------------------

    def _trials(self, room: Room, now: datetime) -> None:
        """Slowly find the lowest comfortable temperatures.

        After a week with no complaint or hand change, one part of the room's day is set
        half a degree lower. Seven days in use without a complaint keeps it. A "too cold"
        or a hand turn-up puts it back and leaves that part of the day alone for a month.
        """
        if room.trial is not None:
            today = now.date().isoformat()
            if (
                room.occupied and room.override_until is None
                and room.reason not in IDLE_REASONS and room.reason != REASON_VALVE_OFF
                and today not in room.trial["days"]
                and self._period_at(room, now) == room.trial["period"]
            ):
                room.trial["days"].append(today)
            if len(room.trial["days"]) >= TRIAL_DAYS:
                self._end_trial(room, now, failed=False, revert=False)
            return
        if not room.occupied:
            return
        if room.calm_since is None:
            room.calm_since = now.isoformat()
            return
        if now - datetime.fromisoformat(room.calm_since) < TRIAL_CALM:
            return
        options = []
        for key, temp in scheduled_temps(room.schedule).items():
            offset = room.comfort.get(key, 0.0)
            floor = room.floors.get(key)
            if (
                temp + offset - TRIAL_STEP <= room.setback
                or offset - TRIAL_STEP < -TRIAL_LIMIT
                or (floor is not None and now < datetime.fromisoformat(floor))
            ):
                continue
            options.append((room.tried.get(key, ""), -(temp + offset), key))
        if not options:
            return
        period = min(options)[2]
        room.comfort[period] = room.comfort.get(period, 0.0) - TRIAL_STEP
        room.trial = {"period": period, "started": now.isoformat(timespec="minutes"), "days": []}
        _LOGGER.info("%s: trying %.1f less from %s", room.name, TRIAL_STEP, period)

    def _end_trial(self, room: Room, now: datetime, failed: bool, revert: bool) -> None:
        period = room.trial["period"]
        if revert:
            room.comfort[period] = room.comfort.get(period, 0.0) + TRIAL_STEP
            if room.comfort[period] == 0.0:
                room.comfort.pop(period)
        room.tried[period] = now.isoformat(timespec="minutes")
        if failed:
            room.floors[period] = (now + FLOOR_HOLD).isoformat(timespec="minutes")
        room.trial = None
        _LOGGER.info(
            "%s: less heat from %s %s", room.name, period,
            "was too cold; left alone for a month" if failed else "went unnoticed; kept",
        )

    def _hand_change(
        self, room: Room, now: datetime, target: float, before: float | None
    ) -> None:
        """Someone changed the room by hand: a sign the temperature was not right.

        The part of the day it happened in moves toward what they chose, by up to 1.5C.
        A change of more than 3C from the plan is a different need, not a correction,
        and is not learnt. Changes while pre-heating, coasting, or when the room is
        not in use say nothing about the schedule.
        """
        room.calm_since = now.isoformat()
        period = self._period_at(room, now)
        if before is not None and target > before and room.trial is not None and period == room.trial["period"]:
            self._end_trial(room, now, failed=True, revert=True)
        scheduled = self._scheduled_at(room, now)
        if period is None or scheduled is None or room.reason not in HAND_LEARN_REASONS:
            return
        gap = target - scheduled
        if abs(gap) < COMFORT_STEP or abs(gap) > COMFORT_LIMIT:
            return
        feeling = "too_cold" if gap > 0 else "too_hot"
        if any(
            item["feeling"] == feeling and item.get("period") == period
            and abs(now - datetime.fromisoformat(item["time"])) < REPEAT_PRESS
            for item in room.feedback_log
        ):
            return
        step = math.copysign(min(MAX_FEEDBACK_JUMP, round(abs(gap) * 2) / 2), gap)
        offset = room.comfort.get(period, 0.0)
        new = max(-COMFORT_LIMIT, min(COMFORT_LIMIT, offset + step))
        if new == offset:
            return
        room.comfort[period] = new
        if new == 0.0:
            room.comfort.pop(period)
        change = new - offset
        word = "up" if gap > 0 else "down"
        entry = {
            "pressed": now.isoformat(timespec="seconds"),
            "reply": (
                f"Noted: you turned it {word} by hand, so this room will be {abs(change):g}° "
                f"{'warmer' if change > 0 else 'cooler'} at this time of day from now on"
            ),
            "time": now.isoformat(timespec="minutes"),
            "feeling": feeling,
            "temperature": room.learner.last_temp,
            "target": scheduled,
            "period": period,
            "action": (
                f"turned {word} by hand to {target:g}C: "
                f"{'raised' if change > 0 else 'lowered'} the {period} target by {change:+.1f}C"
            ),
        }
        room.feedback_log = (room.feedback_log + [entry])[-FEEDBACK_LOG_SIZE:]
        self._store.async_delay_save(self._data, SAVE_DELAY_S)
        _LOGGER.info("%s: %s", room.name, entry["action"])

    def _score(self, now: datetime) -> None:
        """Count the hours each room in use spent more than a degree colder than wanted."""
        today = now.date().isoformat()
        if self.score_date != today:
            if self.score_date is not None:
                self.cold_yesterday = round(sum(r.cold_hours for r in self.rooms.values()), 2)
            for room in self.rooms.values():
                if self.score_date is not None:
                    room.energy_yesterday = round(room.energy, 3)
                room.cold_hours = room.energy = 0.0
            self.score_date = today
        elapsed = min(now - self._last_scored, 2 * TICK) if self._last_scored else timedelta()
        self._last_scored = now
        for room in self.rooms.values():
            temp = room.learner.last_temp
            if (
                room.occupied and room.wanted is not None and temp is not None
                and room.reason != REASON_VALVE_OFF and temp < room.wanted - COLD_MARGIN
            ):
                room.cold_hours += elapsed.total_seconds() / 3600
            if room.zone is None and room.power:
                room.energy += self._heater_output(room)[1] / 1000 * elapsed.total_seconds() / 3600

    def _plan(
        self, room: Room, now: datetime, base: float, upcoming: list[tuple[datetime, float]]
    ) -> tuple[float, str]:
        learner = room.learner
        outdoor_at = self._outdoor_fn(now)
        horizon = now + timedelta(hours=self.max_preheat + room.extra_lead)
        target, reason = base, "schedule"
        room.event_at = None

        for at, temp in upcoming:
            if at > horizon or temp < base:
                break
            if temp <= target:
                continue
            if learner.reliable:
                need = learner.hours_to_reach(temp, outdoor_at, self.max_preheat)
                lead = self.max_preheat if need is None else min(
                    self.max_preheat, need * LEAD_MARGIN + LEAD_SLACK_H
                )
            else:
                lead = FALLBACK_LEAD_H
            lead += room.extra_lead
            if room.lead_hours is None:
                room.lead_hours = lead
            if now >= at - timedelta(hours=lead):
                target, reason = temp, f"pre-heating for {at:%H:%M}"
                room.event_at = at

        if reason == "schedule" and learner.reliable and upcoming:
            at, temp = upcoming[0]
            hours = (at - now).total_seconds() / 3600
            if temp < base and hours <= COAST_MAX_H:
                predicted = learner.predict(hours, 0.0, outdoor_at)
                if predicted is not None and predicted >= base - COAST_TOLERANCE:
                    target, reason = temp, f"coasting to {at:%H:%M}"
                    room.event_at = at

        # Pre-heating and coasting change the room's temperature, and so the estimate they
        # were chosen by. Once started they carry on to the schedule change, or the
        # target would flip back and forth on every reading.
        if reason != "schedule":
            room.plan_hold = (reason, room.event_at)
        elif room.plan_hold is not None:
            held_reason, held_at = room.plan_hold
            held_temp = next((t for a, t in upcoming if a == held_at), None)
            warming = held_reason.startswith("pre-heating")
            last = learner.last_temp
            if (
                held_temp is not None
                and (held_temp > base if warming else held_temp < base)
                and (warming or last is None or last >= base - COAST_TOLERANCE - 0.5)
            ):
                return held_temp, held_reason
            room.plan_hold = None
        return target, reason

    async def _async_tick(self, _now: datetime | None = None) -> None:
        if self._ticking:
            await self.async_tick()

    async def async_tick(self) -> None:
        async with self._lock:
            now = dt_util.now()
            for room in self.rooms.values():
                room.learner.advance(now.timestamp())
            self._update_warm(now)
            self._update_presence(now)
            for room in self.rooms.values():
                self._decide(room, now)
                if self.enabled:
                    await self._async_apply(room, now)
                    await self._async_boost(room, now)
                    self._trials(room, now)
                if room.zone is None:
                    room.learner.set_valve(now.timestamp(), self._effective_valve(room))
            self._score(now)
            async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def _async_apply(self, room: Room, now: datetime) -> None:
        if room.override_until is not None or room.target is None or room.quiet_window(now):
            return
        state = self.hass.states.get(room.climate)
        if state is None or state.state in BAD_STATES:
            return
        resume = room.resume_requested
        room.resume_requested = False
        if state.state == HVACMode.OFF and not resume:
            room.reason = REASON_VALVE_OFF
            return
        self._update_setpoint(room, state)
        if resume:
            self._unchecked.discard(room.slug)
        elif room.slug in self._unchecked and self._adopt_change_made_while_stopped(room, state, now):
            return
        current = _float(state.attributes.get(ATTR_TEMPERATURE))
        if state.state == HVACMode.HEAT and current is not None and abs(current - room.setpoint) < 0.25:
            return
        if not resume and room.last_write and room.last_write[0] == room.setpoint and now - room.last_write[1] < WRITE_RETRY:
            return
        await self._async_write(room, state, now)

    async def _async_boost(self, room: Room, now: datetime) -> None:
        """Run a room's second heater only to help it warm up, and never into quiet hours."""
        if room.boost is None or room.quiet_window(now):
            return
        state = self.hass.states.get(room.boost)
        main = self.hass.states.get(room.climate)
        if state is None or state.state in BAD_STATES or main is None:
            return
        temp = _float(main.attributes.get("current_temperature"))
        preparing = room.quiet_window(now, QUIET_PREP) is not None
        want = False
        if (
            not preparing and room.override_until is None and room.occupied
            and main.state == HVACMode.HEAT and temp is not None
            and room.target is not None and room.target > room.setback
        ):
            if room.boost_wanted or room.reason.startswith("pre-heating"):
                want = temp < room.target
            else:
                want = temp <= room.target - BOOST_GAP
        changed = want != room.boost_wanted
        room.boost_wanted = want
        if not (changed or preparing) or (state.state == HVACMode.HEAT) == want:
            return
        try:
            await self.hass.services.async_call(
                "climate", "set_hvac_mode",
                {"entity_id": room.boost, "hvac_mode": HVACMode.HEAT if want else HVACMode.OFF},
                blocking=True,
            )
            if want:
                step = _float(state.attributes.get("target_temp_step"))
                await self.hass.services.async_call(
                    "climate", "set_temperature",
                    {"entity_id": room.boost, ATTR_TEMPERATURE: round_to_step(room.target, step)},
                    blocking=True,
                )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not switch %s boost heater: %s", room.name, err)
            return
        _LOGGER.info("%s boost heater %s (%s)", room.name, "on" if want else "off", room.reason)

    async def _async_write(self, room: Room, state: State, now: datetime) -> None:
        if room.quiet_window(now):
            return
        current = _float(state.attributes.get(ATTR_TEMPERATURE))
        value = room.setpoint if room.setpoint is not None else room.target
        room.last_write = (value, now)
        room.recent_writes.append(room.last_write)
        self._written[room.slug] = value
        self._store.async_delay_save(self._data, 30)
        try:
            if state.state != HVACMode.HEAT:
                await self.hass.services.async_call(
                    "climate", "set_hvac_mode",
                    {"entity_id": room.climate, "hvac_mode": HVACMode.HEAT}, blocking=True,
                )
            await self.hass.services.async_call(
                "climate", "set_temperature",
                {"entity_id": room.climate, ATTR_TEMPERATURE: value}, blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not set %s to %.1f: %s", room.name, value, err)
            return
        _LOGGER.info("%s: %s -> %.1f (%s)", room.name, current, value, room.reason)

    # ---- comfort feedback -----------------------------------------------

    async def async_feedback(
        self, room: Room, feeling: str, when: datetime | None = None, nudge: float = 0.0
    ) -> dict[str, Any]:
        """Learn from 'too hot' / 'too cold' at a moment, now or in the last two days.

        Too cold while the room was still short of its target means heating started too
        late, so the room's warm-up lead grows. Too hot while the room was already well
        above its target means the heat was left over from an earlier, warmer part of the
        day, so that part is lowered. Otherwise the target for that part of the day was
        wrong and moves by 0.5C, or by up to 1.5C where that is what it takes to put the
        target on the right side of the temperature the room was at: a room that feels
        cold at 17.3C is not helped by a target of 17. A second press within a few minutes
        of the first is not learnt twice. A non-zero `nudge` also moves the room by that much straight
        away, held until its next schedule change.
        """
        now = dt_util.now()
        if when is None:
            when = now
        elif when.tzinfo is None:
            when = when.replace(tzinfo=dt_util.get_default_time_zone())
        if when > now + timedelta(minutes=1) or now - when > FEEDBACK_MAX_AGE:
            raise HomeAssistantError("Feedback must be about the last two days")
        temp, target = await self._async_conditions_at(room, when)
        period = self._period_at(room, when)
        if period is None:
            raise HomeAssistantError(f"{room.name} has no schedule entry before {when}")

        repeat = any(
            item["feeling"] == feeling
            and abs(when - datetime.fromisoformat(item["time"])) < REPEAT_PRESS
            for item in room.feedback_log
        )
        behind = temp is not None and target is not None and temp < target - BEHIND_TARGET
        overshot = temp is not None and target is not None and temp > target + BEHIND_TARGET
        leftover = self._warmer_period_before(room, when) if feeling == "too_hot" and overshot else None
        if leftover is not None:
            period = leftover
        offset = room.comfort.get(period, 0.0)
        lead_before = room.extra_lead
        step = COMFORT_STEP
        scheduled = self._scheduled_at(room, when)
        if leftover is None and temp is not None and scheduled is not None:
            gap = temp - scheduled if feeling == "too_cold" else scheduled - temp
            step = max(step, min(MAX_FEEDBACK_JUMP, math.ceil((gap + COMFORT_STEP) * 2) / 2))
        if repeat:
            action = "already noted"
        elif feeling == "too_cold" and behind and room.extra_lead < MAX_EXTRA_LEAD_H:
            room.extra_lead = min(MAX_EXTRA_LEAD_H, room.extra_lead + LEAD_STEP_H)
            action = (
                f"room was {temp:.1f}C, short of its {target:.1f}C target: "
                f"now starts heating {round(room.extra_lead * 60)} min earlier"
            )
        elif feeling == "too_cold":
            room.comfort[period] = min(COMFORT_LIMIT, offset + step)
            action = f"raised the {period} target by {room.comfort[period] - offset:+.1f}C"
            if step > COMFORT_STEP:
                action += f", to above the {temp:.1f}C the room was at"
        else:
            room.comfort[period] = max(-COMFORT_LIMIT, offset - step)
            action = f"lowered the {period} target by {room.comfort[period] - offset:+.1f}C"
            if step > COMFORT_STEP:
                action += f", to below the {temp:.1f}C the room was at"
            if leftover is not None:
                action = (
                    f"room was {temp:.1f}C, above its {target:.1f}C target, so the heat was left "
                    f"over from earlier: {action}"
                )
            elif overshot and room.extra_lead > 0:
                room.extra_lead = max(0.0, room.extra_lead - LEAD_STEP_H)
                action += f"; warm-up lead cut to {round(room.extra_lead * 60)} min"
        if not repeat:
            room.calm_since = now.isoformat()
            if feeling == "too_cold" and room.trial is not None and room.trial["period"] == period:
                raised = room.comfort.get(period, 0.0) > offset
                self._end_trial(room, now, failed=True, revert=not raised)
                action += "; stopped trying less heat here for a month"
        if room.comfort.get(period) == 0.0:
            room.comfort.pop(period)

        # The same thing in plain words, for whoever pressed the button.
        change = room.comfort.get(period, 0.0) - offset
        then = "at this time of day" if now - when < REPEAT_PRESS else "at that time of day"
        if repeat:
            reply = "Already noted a few minutes ago"
        elif change and leftover is not None:
            reply = f"Noted: the earlier, warmer part of the day will be {-change:g}° cooler from now on"
        elif change:
            reply = (
                f"Noted: this room will be {abs(change):g}° "
                f"{'warmer' if change > 0 else 'cooler'} {then} from now on"
            )
        elif room.extra_lead > lead_before:
            reply = "Noted: this room will start warming up earlier from now on"
        else:
            reply = "Noted"

        entry = {
            "pressed": now.isoformat(timespec="seconds"),
            "reply": reply,
            "time": when.isoformat(timespec="minutes"),
            "feeling": feeling,
            "temperature": temp,
            "target": target,
            "period": period,
            "action": action,
        }
        await self.async_tick()
        if nudge and room.quiet_window(now):
            entry["action"] = action = f"{action}; nothing changed now, as it is quiet hours"
            entry["reply"] = f"{reply}. Nothing changes right now, as the heaters are left alone at night"
        elif nudge and room.target is not None:
            held_at = room.target + (nudge if feeling == "too_cold" else -nudge)
            await self._async_hold(room, held_at)
            entry["action"] = action = (
                f"{action}; set to {room.target:g}°C until {_when(room.override_until)}"
            )
            entry["reply"] = f"{reply}. Set to {room.target:g}° until {_when(room.override_until)}"
        room.feedback_log = (room.feedback_log + [entry])[-FEEDBACK_LOG_SIZE:]
        _LOGGER.info("%s feedback %s at %s: %s", room.name, feeling, entry["time"], action)
        await self._async_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        return entry

    async def _async_hold(self, room: Room, temp: float) -> None:
        """Hold a room at a temperature until its next schedule change, as a manual change would."""
        async with self._lock:
            now = dt_util.now()
            upcoming = [at for at, _ in self.timeline(room, now) if at > now]
            room.override_until = upcoming[0] if upcoming else now + OVERRIDE_FALLBACK
            room.target = room.setpoint = _round_target(temp)
            room.easing = False
            room.reason = "manual override"
            state = self.hass.states.get(room.climate)
            if self.enabled and state is not None and state.state not in BAD_STATES:
                self._unchecked.discard(room.slug)
                await self._async_write(room, state, now)

    async def async_resume(self, room: Room) -> None:
        """Drop a manual override (or hand-set Off) and go back to the plan now."""
        room.override_until = None
        room.resume_requested = True
        _LOGGER.info("%s resumed its schedule", room.name)
        await self.async_tick()

    async def async_reset_feedback(self, room: Room) -> None:
        room.comfort = {}
        room.extra_lead = 0.0
        room.feedback_log = []
        room.trial = None
        room.floors = {}
        room.tried = {}
        await self._async_save()
        await self.async_tick()

    async def _async_conditions_at(self, room: Room, when: datetime) -> tuple[float | None, float | None]:
        """Valve-reported temperature and setpoint at a moment."""
        if dt_util.now() - when < timedelta(minutes=10):
            state = self.hass.states.get(room.climate)
            estimate = room.learner.estimate
            temp = estimate if estimate is not None else room.learner.last_temp
            return temp, _float(state.attributes.get(ATTR_TEMPERATURE)) if state else None
        states = await get_instance(self.hass).async_add_executor_job(
            partial(
                history.get_significant_states, self.hass,
                when - timedelta(hours=6), when + timedelta(seconds=1),
                entity_ids=[room.climate], significant_changes_only=True,
            )
        )
        temp = target = None
        for state in states.get(room.climate, []):
            if state.last_updated > when or state.state in BAD_STATES:
                continue
            value = _float(state.attributes.get("current_temperature"))
            temp = value if value is not None else temp
            value = _float(state.attributes.get(ATTR_TEMPERATURE))
            target = value if value is not None else target
        return temp, target

    # ---- learning from history ------------------------------------------

    async def _async_bootstrap(self, rooms: list[Room]) -> None:
        end = dt_util.utcnow()
        start = end - timedelta(days=self.history_days)
        recorder = get_instance(self.hass)
        rooms = [r for r in rooms if r.zone is not None]
        zone_entities = {self.zones[r.zone]: r.zone for r in rooms}
        ids = [r.climate for r in rooms] + list(zone_entities)
        _LOGGER.info("Learning %d rooms from %d days of history", len(rooms), self.history_days)
        try:
            states = await recorder.async_add_executor_job(
                partial(
                    history.get_significant_states, self.hass, start, end,
                    entity_ids=ids, significant_changes_only=True,
                )
            )
            weather = await recorder.async_add_executor_job(
                partial(
                    history.get_significant_states, self.hass, start, end,
                    entity_ids=[self.weather], significant_changes_only=False,
                )
            )
        except Exception:  # noqa: BLE001 - history is optional, live learning carries on
            _LOGGER.exception("Could not read heating history; learning from live data only")
            return

        events: list[tuple[float, str, Any, float]] = []
        for state in weather.get(self.weather, []):
            value = _float(state.attributes.get(ATTR_TEMPERATURE))
            if value is not None:
                events.append((state.last_updated.timestamp(), "outdoor", None, value))
        for entity, zone in zone_entities.items():
            for state in states.get(entity, []):
                if state.state not in BAD_STATES:
                    events.append((state.last_updated.timestamp(), "zone", zone, state.state == STATE_ON))
        for room in rooms:
            for state in states.get(room.climate, []):
                if state.state in BAD_STATES:
                    continue
                when = state.last_updated.timestamp()
                opening = _float(state.attributes.get("valve"))
                if opening is not None:
                    events.append((when, "valve", room, opening / 100))
                value = _float(state.attributes.get("current_temperature"))
                if value is not None:
                    events.append((when, "temp", room, value))
        events.sort(key=lambda e: e[0])

        learners = {room.slug: RoomLearner() for room in rooms}
        zone_on = dict.fromkeys(self.zones, False)
        valve = {room.slug: 0.0 for room in rooms}
        last_temp: dict[str, float] = {}
        for when, kind, key, value in events:
            if kind == "outdoor":
                for learner in learners.values():
                    learner.set_outdoor(when, value)
            elif kind == "zone":
                zone_on[key] = value
                for room in rooms:
                    if room.zone == key:
                        learners[room.slug].set_valve(when, valve[room.slug] if value else 0.0)
            elif kind == "valve":
                valve[key.slug] = value
                learners[key.slug].set_valve(when, value if zone_on[key.zone] else 0.0)
            elif last_temp.get(key.slug) != value:
                last_temp[key.slug] = value
                learners[key.slug].observe(when, value)

        now = dt_util.now().timestamp()
        for room in rooms:
            learner = learners[room.slug]
            if self.outdoor is not None:
                learner.set_outdoor(now, self.outdoor)
            learner.set_valve(now, self._effective_valve(room))
            room.learner = learner
            best = learner.model
            _LOGGER.info(
                "%s learnt from %d days of history: warm-up %.2f C/h, loss %.3f/h, "
                "gains %.2f C/h, radiator lag %.1fh, %d samples, reliable=%s",
                room.name, self.history_days, best.a, best.k, best.gains, best.lag,
                best.samples, learner.reliable,
            )
        await self._async_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def _async_learn_valves(self, _now: datetime | None = None) -> None:
        """Learn from history how far above its setpoint each valve stops calling for heat."""
        end = dt_util.utcnow()
        start = end - timedelta(days=self.history_days)
        try:
            states = await get_instance(self.hass).async_add_executor_job(
                partial(
                    history.get_significant_states, self.hass, start, end,
                    entity_ids=[room.climate for room in self.rooms.values()],
                    significant_changes_only=True,
                )
            )
        except Exception:  # noqa: BLE001 - optional; valves are simply left uncorrected
            _LOGGER.exception("Could not read valve history")
            return
        for room in self.rooms.values():
            if room.valve is None:
                continue
            samples = [
                (
                    state.last_updated.timestamp(),
                    _float(state.attributes.get("current_temperature")),
                    _float(state.attributes.get(ATTR_TEMPERATURE)),
                    _float(state.attributes.get("valve")),
                )
                for state in states.get(room.climate, [])
                if state.state not in BAD_STATES
            ]
            gap = shut_gap(samples)
            if gap is not None:
                room.shut_gap = gap
        await self._async_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def _async_save(self, _now: datetime | None = None) -> None:
        await self._store.async_save(self._data())

    @callback
    def _data(self) -> dict:
        return {
            "rooms": {slug: room.learner.as_dict() for slug, room in self.rooms.items()},
            "comfort": {slug: room.comfort_as_dict() for slug, room in self.rooms.items()},
            "written": self._written,
            "valve_gaps": {slug: room.shut_gap for slug, room in self.rooms.items()},
            "score": {
                "date": self.score_date,
                "yesterday": self.cold_yesterday,
                "cold": {slug: round(room.cold_hours, 3) for slug, room in self.rooms.items()},
                "energy": {s: round(r.energy, 4) for s, r in self.rooms.items() if r.power},
                "energy_yesterday": {s: r.energy_yesterday for s, r in self.rooms.items() if r.power},
            },
        }
