from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .controller import HeatingController, Room

FEELINGS = {"too_hot": ("too hot", "mdi:thermometer-chevron-up"), "too_cold": ("too cold", "mdi:snowflake")}


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if discovery_info is None:
        return
    controller: HeatingController = hass.data[DOMAIN]
    entities: list[ButtonEntity] = [
        FeedbackButton(controller, room, feeling)
        for room in controller.rooms.values()
        for feeling in FEELINGS
    ]
    entities += [ResumeButton(controller, room) for room in controller.rooms.values()]
    async_add_entities(entities)


class FeedbackButton(ButtonEntity):
    _attr_should_poll = False

    def __init__(self, controller: HeatingController, room: Room, feeling: str) -> None:
        self.controller = controller
        self.room = room
        self.feeling = feeling
        label, icon = FEELINGS[feeling]
        self._attr_name = f"{room.name} {label}"
        self._attr_icon = icon
        self._attr_unique_id = f"{DOMAIN}_{room.slug}_{feeling}"

    async def async_press(self) -> None:
        await self.controller.async_feedback(self.room, self.feeling)


class ResumeButton(ButtonEntity):
    """Only available while the room is held by a manual change."""

    _attr_should_poll = False
    _attr_icon = "mdi:calendar-sync"

    def __init__(self, controller: HeatingController, room: Room) -> None:
        self.controller = controller
        self.room = room
        self._attr_name = f"{room.name} resume schedule"
        self._attr_unique_id = f"{DOMAIN}_{room.slug}_resume"

    @property
    def available(self) -> bool:
        return self.room.held

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self.async_write_ha_state)
        )

    async def async_press(self) -> None:
        await self.controller.async_resume(self.room)
