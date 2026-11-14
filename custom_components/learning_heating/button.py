from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .controller import HeatingController, Room

FEELINGS = {"too_hot": ("too hot", "mdi:thermometer-chevron-up"), "too_cold": ("too cold", "mdi:snowflake")}


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if discovery_info is None:
        return
    controller: HeatingController = hass.data[DOMAIN]
    async_add_entities(
        FeedbackButton(controller, room, feeling)
        for room in controller.rooms.values()
        for feeling in FEELINGS
    )


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
