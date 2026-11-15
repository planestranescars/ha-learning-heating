from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SIGNAL_UPDATE
from .controller import HeatingController, Room


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if discovery_info is None:
        return
    controller: HeatingController = hass.data[DOMAIN]
    entities: list[SwitchEntity] = [ControlSwitch(controller)]
    if controller.people:
        entities.append(GuestsSwitch(controller))
    entities += [OccupiedSwitch(controller, room) for room in controller.rooms.values()]
    async_add_entities(entities)


class _Switch(SwitchEntity, RestoreEntity):
    _attr_should_poll = False

    def __init__(self, controller: HeatingController) -> None:
        self.controller = controller

    def _get(self) -> bool:
        raise NotImplementedError

    def _set(self, value: bool) -> None:
        raise NotImplementedError

    @property
    def is_on(self) -> bool:
        return self._get()

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._set(last.state == STATE_ON)
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self.async_write_ha_state)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_change(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_change(False)

    async def _async_change(self, value: bool) -> None:
        self._set(value)
        self.async_write_ha_state()
        await self.controller.async_tick()


class ControlSwitch(_Switch):
    _attr_name = "Learning heating control"
    _attr_unique_id = f"{DOMAIN}_control"
    _attr_icon = "mdi:home-thermometer"

    def _get(self) -> bool:
        return self.controller.enabled

    def _set(self, value: bool) -> None:
        self.controller.enabled = value


class GuestsSwitch(_Switch):
    """On: people the home can't track are staying, so don't ease off when everyone tracked is out."""

    _attr_name = "Heating guests staying"
    _attr_unique_id = f"{DOMAIN}_guests"
    _attr_icon = "mdi:account-group"

    def _get(self) -> bool:
        return self.controller.guests

    def _set(self, value: bool) -> None:
        self.controller.guests = value


class OccupiedSwitch(_Switch):
    _attr_icon = "mdi:bed"

    def __init__(self, controller: HeatingController, room: Room) -> None:
        super().__init__(controller)
        self.room = room
        self._attr_name = f"{room.name} occupied"
        self._attr_unique_id = f"{DOMAIN}_{room.slug}_occupied"

    def _get(self) -> bool:
        return self.room.occupied

    def _set(self, value: bool) -> None:
        self.room.occupied = value
        if not value:
            self.room.override_until = None
