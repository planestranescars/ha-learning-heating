from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .controller import HeatingController, Room


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if discovery_info is None:
        return
    controller: HeatingController = hass.data[DOMAIN]
    async_add_entities(
        RoomThermostat(controller, room) for room in controller.rooms.values() if room.thermostat
    )


class RoomThermostat(ClimateEntity):
    """The temperature wanted in a room, apart from whatever is sent to its valve or heater.

    Setting it holds the room there until its next schedule change and teaches the
    schedule, exactly as turning the valve by hand does.
    """

    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 5.0
    _attr_max_temp = 30.0

    def __init__(self, controller: HeatingController, room: Room) -> None:
        self.controller = controller
        self.room = room
        self._attr_name = f"{room.name} thermostat"
        self._attr_unique_id = f"{DOMAIN}_{room.slug}_thermostat"
        self.entity_id = f"climate.{room.slug}_thermostat"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self.async_write_ha_state)
        )

    @property
    def current_temperature(self) -> float | None:
        if self.room.cycle:
            return self.room.true_temp
        return self.room.learner.last_temp

    @property
    def target_temperature(self) -> float | None:
        return self.room.target

    @property
    def hvac_action(self) -> HVACAction | None:
        state = self.hass.states.get(self.room.climate)
        if state is None:
            return None
        action = state.attributes.get("hvac_action")
        if action in (HVACAction.HEATING, HVACAction.IDLE, HVACAction.OFF):
            return HVACAction(action)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"summary": self.room.summary, "valve_setpoint": self.room.setpoint}

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.controller.async_set_target(self.room, float(temp))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        return
