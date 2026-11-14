from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfTemperature
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
    entities: list[SensorEntity] = []
    for room in controller.rooms.values():
        entities += [
            TargetSensor(controller, room),
            TemperatureSensor(controller, room),
            WarmUpSensor(controller, room),
        ]
    async_add_entities(entities)


class _RoomSensor(SensorEntity):
    _attr_should_poll = False
    key = ""

    def __init__(self, controller: HeatingController, room: Room) -> None:
        self.controller = controller
        self.room = room
        self._attr_unique_id = f"{DOMAIN}_{room.slug}_{self.key}"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self.async_write_ha_state)
        )


class TargetSensor(_RoomSensor):
    key = "target"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_icon = "mdi:thermostat-auto"

    def __init__(self, controller: HeatingController, room: Room) -> None:
        super().__init__(controller, room)
        self._attr_name = f"{room.name} heating target"

    @property
    def native_value(self) -> float | None:
        return self.room.target

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room = self.room
        estimate = room.learner.estimate
        return {
            "reason": room.reason,
            "occupied": room.occupied,
            "next_change": room.next_change.isoformat() if room.next_change else None,
            "next_target": room.next_target,
            "preheat_lead_minutes": round(room.lead_hours * 60) if room.lead_hours is not None else None,
            "override_until": room.override_until.isoformat() if room.override_until else None,
            "estimated_temperature": round(estimate, 1) if estimate is not None else None,
            "learnt_timing": room.learner.reliable,
            "comfort_adjustments": dict(room.comfort),
            "extra_preheat_minutes": round(room.extra_lead * 60),
            "last_feedback": room.feedback_log[-1]["action"] if room.feedback_log else None,
            "feedback_count": len(room.feedback_log),
        }


class TemperatureSensor(_RoomSensor):
    key = "temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, controller: HeatingController, room: Room) -> None:
        super().__init__(controller, room)
        self._attr_name = f"{room.name} temperature"

    @property
    def native_value(self) -> float | None:
        return self.room.learner.last_temp


class WarmUpSensor(_RoomSensor):
    key = "warm_up_rate"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "°C/h"
    _attr_icon = "mdi:radiator"

    def __init__(self, controller: HeatingController, room: Room) -> None:
        super().__init__(controller, room)
        self._attr_name = f"{room.name} warm-up rate"

    @property
    def native_value(self) -> float:
        return round(self.room.learner.model.a, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        learner = self.room.learner
        model = learner.model
        naive = learner.naive_mse
        return {
            "heat_loss_per_hour": round(model.k, 3),
            "cooling_time_constant_hours": round(1 / model.k, 1),
            "steady_gains_c_per_hour": round(model.gains, 2),
            "radiator_lag_hours": model.lag,
            "samples": model.samples,
            "prediction_error_c": round(model.rms_error, 2) if model.rms_error is not None else None,
            "no_change_error_c": round(naive**0.5, 2) if naive is not None else None,
            "reliable": learner.reliable,
        }
