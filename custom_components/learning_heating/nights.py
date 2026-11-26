"""Asking people each morning how the night was in their bedroom.

Each person listed gets a notification on their phone with three answers: too cold,
fine, too warm. The answer is learnt from (see HeatingController.async_night_feedback)
and remembered against the room they said they slept in. Once someone has had enough
good nights in a row, or has left the question unanswered for a week, they are not
asked again until something is reported wrong in their room. The same answers can be
given at any time with each bedroom's buttons.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_UPDATE

if TYPE_CHECKING:
    from .controller import HeatingController

_LOGGER = logging.getLogger(__name__)

ACTION = "LEARNING_HEATING_NIGHT"
GOOD_NIGHTS = 4       # nights in a row reported fine before the question stops
IGNORED_NIGHTS = 7    # questions in a row left unanswered before it stops
ANSWERS = {"too_cold": "Too cold", "fine": "Fine", "too_hot": "Too warm"}
# Android fixes a channel's importance when it is first used, so a channel that began
# quiet stays quiet: the question has a channel of its own that pops up. Moving to a new
# channel forgives the questions that went unseen on the old one.
CHANNEL = "Heating questions"
ASK_UNTIL_HOUR = 13   # someone out when the question is due is asked on getting home, until then


class Sleeper:
    """Someone who is asked about their nights."""

    def __init__(self, person: str, notify: str, room: str | None) -> None:
        self.person = person
        self.notify = notify
        self.room = room
        self.good = 0
        self.ignored = 0
        self.settled = False
        self.asked: str | None = None
        self.answered: str | None = None
        self.last_answer: str | None = None
        self.channel: str | None = None
        self.waiting: str | None = None   # the day they were out when the question was due

    def as_dict(self) -> dict[str, Any]:
        return {
            "room": self.room, "good": self.good, "ignored": self.ignored,
            "settled": self.settled, "asked": self.asked, "answered": self.answered,
            "last_answer": self.last_answer, "channel": self.channel,
        }

    def load(self, data: dict[str, Any], bedrooms: list[str]) -> None:
        if data.get("room") in bedrooms:
            self.room = data["room"]
        self.good = int(data.get("good", 0))
        self.ignored = int(data.get("ignored", 0))
        self.settled = bool(data.get("settled", False))
        self.asked = data.get("asked")
        self.answered = data.get("answered")
        self.last_answer = data.get("last_answer")
        self.channel = data.get("channel")
        if self.channel != CHANNEL:
            # Asked where it could not be seen: none of that counts as ignored.
            self.ignored = 0
            if self.good < GOOD_NIGHTS:
                self.settled = False

    def unsettle(self) -> None:
        self.settled = False
        self.good = 0
        self.ignored = 0


class NightCheck:
    def __init__(self, hass: HomeAssistant, controller: HeatingController, conf: dict) -> None:
        self.hass = hass
        self.controller = controller
        self.at = conf["at"]
        self.open: str | None = conf.get("open")
        self.bedrooms = [slug for slug, room in controller.rooms.items() if room.bedroom]
        self.sleepers: dict[str, Sleeper] = {}
        for person, settings in conf["people"].items():
            room = settings.get("room")
            if room not in self.bedrooms:
                room = self.bedrooms[0] if self.bedrooms else None
            self.sleepers[person] = Sleeper(person, settings["notify"], room)
        self._store: Store = Store(hass, 1, f"{DOMAIN}.nights")

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        for person, saved in data.get("people", {}).items():
            if person in self.sleepers:
                self.sleepers[person].load(saved, self.bedrooms)

    @callback
    def async_start(self) -> None:
        async_track_time_change(
            self.hass, self._async_ask, hour=self.at.hour, minute=self.at.minute, second=0
        )
        self.hass.bus.async_listen("mobile_app_notification_action", self._on_action)
        async_track_state_change_event(self.hass, list(self.sleepers), self._on_person)

    @callback
    def _on_person(self, event: Event) -> None:
        """Someone who was out when the question was due has come home: ask now."""
        new = event.data.get("new_state")
        sleeper = self.sleepers.get(event.data.get("entity_id"))
        if sleeper is None or new is None or new.state != "home":
            return
        if sleeper.waiting != self._today() or dt_util.as_local(dt_util.now()).hour >= ASK_UNTIL_HOUR:
            return
        sleeper.waiting = None
        self.hass.async_create_task(self._async_ask_one(sleeper, self._today()))

    def name(self, person: str) -> str:
        state = self.hass.states.get(person)
        if state is not None and state.attributes.get("friendly_name"):
            return str(state.attributes["friendly_name"])
        return person.split(".", 1)[-1].replace("_", " ").title()

    def _today(self) -> str:
        return dt_util.as_local(dt_util.now()).date().isoformat()

    async def _async_ask(self, _now: datetime | None = None) -> None:
        today = self._today()
        for sleeper in self.sleepers.values():
            state = self.hass.states.get(sleeper.person)
            if state is not None and state.state == "not_home":
                sleeper.waiting = today
                continue
            await self._async_ask_one(sleeper, today, save=False)
        self._save()

    async def _async_ask_one(self, sleeper: Sleeper, today: str, save: bool = True) -> None:
        if sleeper.settled or sleeper.room is None or today in (sleeper.asked, sleeper.answered):
            return
        if sleeper.asked is not None and sleeper.answered != sleeper.asked:
            sleeper.ignored += 1
            if sleeper.ignored >= IGNORED_NIGHTS:
                sleeper.settled = True
                if save:
                    self._save()
                return
        room = self.controller.rooms[sleeper.room]
        await self._async_send(sleeper, f"How was last night in {room.name}?", ask=True)
        sleeper.asked = today
        sleeper.channel = CHANNEL
        if save:
            self._save()

    async def _async_send(self, sleeper: Sleeper, message: str, ask: bool) -> None:
        domain, _, service = sleeper.notify.partition(".")
        if not service:
            domain, service = "notify", domain
        data: dict[str, Any] = {
            "tag": f"{DOMAIN}_night_{sleeper.person.replace('.', '_')}",
            "channel": CHANNEL, "importance": "high", "priority": "high", "ttl": 0,
        }
        if ask:
            data["actions"] = [
                {"action": f"{ACTION}|{sleeper.person}|{sleeper.room}|{answer}", "title": title}
                for answer, title in ANSWERS.items()
            ]
        if self.open:
            data["clickAction"] = data["url"] = self.open
        try:
            await self.hass.services.async_call(
                domain, service, {"title": "Heating", "message": message, "data": data},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not ask %s about last night: %s", sleeper.person, err)

    @callback
    def _on_action(self, event: Event) -> None:
        parts = str(event.data.get("action", "")).split("|")
        if len(parts) != 4 or parts[0] != ACTION or parts[3] not in ANSWERS:
            return
        _, person, room, answer = parts
        if room in self.controller.rooms:
            self.hass.async_create_task(self.async_answer(room, answer, person=person, reply=True))

    def person_for_user(self, user_id: str | None) -> str | None:
        if user_id is None:
            return None
        for person in self.sleepers:
            state = self.hass.states.get(person)
            if state is not None and state.attributes.get("user_id") == user_id:
                return person
        return None

    async def async_answer(
        self, slug: str, answer: str, person: str | None = None, reply: bool = False
    ) -> dict[str, Any]:
        """Learn from an answer about last night in a room, from a notification or a button."""
        entry = await self.controller.async_night_feedback(self.controller.rooms[slug], answer)
        message = entry["reply"]
        sleeper = self.sleepers.get(person) if person else None
        if answer != "fine":
            for other in self.sleepers.values():
                if other.room == slug:
                    other.unsettle()
        if sleeper is not None:
            if sleeper.room != slug:
                sleeper.room = slug
                sleeper.unsettle()
            counted = sleeper.answered == self._today() and sleeper.last_answer == "fine"
            sleeper.answered = self._today()
            sleeper.last_answer = answer
            sleeper.ignored = 0
            if answer != "fine" and counted:
                sleeper.good = max(0, sleeper.good - 1)
            if answer == "fine" and not counted:
                sleeper.good += 1
                if sleeper.good >= GOOD_NIGHTS and not sleeper.settled:
                    sleeper.settled = True
                    message += (
                        f". That's {GOOD_NIGHTS} good nights in a row, so I'll stop asking; "
                        "you can still say how a night was at any time"
                    )
            if reply:
                await self._async_send(sleeper, message, ask=False)
        self._save()
        return {**entry, "reply": message}

    def set_room(self, person: str, slug: str) -> None:
        sleeper = self.sleepers[person]
        if slug in self.bedrooms and slug != sleeper.room:
            sleeper.room = slug
            sleeper.unsettle()
            self._save()

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {"people": {p: s.as_dict() for p, s in self.sleepers.items()}}, 10
        )
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
