from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .controller import HeatingController
from .nights import GOOD_NIGHTS, NightCheck


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if discovery_info is None:
        return
    controller: HeatingController = hass.data[DOMAIN]
    nights = controller.nights
    if nights is None or not nights.bedrooms:
        return
    async_add_entities([SleepsIn(nights, person) for person in nights.sleepers])


class SleepsIn(SelectEntity):
    """The bedroom someone sleeps in, which their morning question is about."""

    _attr_should_poll = False
    _attr_icon = "mdi:bed"

    def __init__(self, nights: NightCheck, person: str) -> None:
        self.nights = nights
        self.person = person
        self._attr_unique_id = f"{DOMAIN}_{person.replace('.', '_')}_sleeps_in"
        self._attr_options = [nights.controller.rooms[s].name for s in nights.bedrooms]

    @property
    def name(self) -> str:
        return f"{self.nights.name(self.person)} sleeps in"

    @property
    def current_option(self) -> str | None:
        slug = self.nights.sleepers[self.person].room
        return self.nights.controller.rooms[slug].name if slug else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sleeper = self.nights.sleepers[self.person]
        return {
            "asked_each_morning": not sleeper.settled,
            "good_nights_in_a_row": sleeper.good,
            "good_nights_to_stop": GOOD_NIGHTS,
            "last_asked": sleeper.asked,
            "last_answered": sleeper.answered,
        }

    async def async_select_option(self, option: str) -> None:
        for slug in self.nights.bedrooms:
            if self.nights.controller.rooms[slug].name == option:
                self.nights.set_room(self.person, slug)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self.async_write_ha_state)
        )
