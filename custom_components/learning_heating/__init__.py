"""Self-learning heating control for radiator valves on zoned boilers."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .controller import HeatingController
from .schedule import validate_schedule

_LOGGER = logging.getLogger(__name__)

ROOM_SCHEMA = vol.Schema(
    {
        vol.Required("name"): cv.string,
        vol.Required("climate"): cv.entity_id,
        vol.Required("valve"): cv.entity_id,
        vol.Required("zone"): cv.string,
        vol.Required("schedule"): validate_schedule,
        vol.Optional("occupied", default=True): cv.boolean,
        vol.Optional("setback"): vol.Coerce(float),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required("weather"): cv.entity_id,
                vol.Required("zones"): {cv.string: cv.entity_id},
                vol.Optional("setback", default=14.0): vol.Coerce(float),
                vol.Optional("warm_cutoff", default=16.0): vol.Coerce(float),
                vol.Optional("max_preheat_hours", default=3.0): vol.Coerce(float),
                vol.Optional("history_days", default=10): cv.positive_int,
                vol.Optional("people", default=[]): cv.entity_ids,
                vol.Required("rooms"): {cv.slug: ROOM_SCHEMA},
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config[DOMAIN]
    unknown = {room["zone"] for room in conf["rooms"].values()} - set(conf["zones"])
    if unknown:
        _LOGGER.error("Rooms refer to undefined zones: %s", ", ".join(sorted(unknown)))
        return False

    controller = HeatingController(hass, conf)
    await controller.async_load()
    hass.data[DOMAIN] = controller
    for platform in (Platform.SENSOR, Platform.SWITCH, Platform.BUTTON):
        hass.async_create_task(discovery.async_load_platform(hass, platform, DOMAIN, {}, config))

    def room_for(call: ServiceCall):
        slug = call.data["room"]
        if slug not in controller.rooms:
            raise ServiceValidationError(
                f"Unknown room '{slug}'; choose from {', '.join(controller.rooms)}"
            )
        return controller.rooms[slug]

    async def feedback(call: ServiceCall) -> ServiceResponse:
        entry = await controller.async_feedback(
            room_for(call), call.data["feeling"], call.data.get("when"), call.data["nudge"]
        )
        return entry if call.return_response else None

    async def reset_feedback(call: ServiceCall) -> None:
        await controller.async_reset_feedback(room_for(call))

    async def resume(call: ServiceCall) -> None:
        await controller.async_resume(room_for(call))

    hass.services.async_register(
        DOMAIN, "feedback", feedback,
        schema=vol.Schema({
            vol.Required("room"): cv.string,
            vol.Required("feeling"): vol.In(["too_hot", "too_cold"]),
            vol.Optional("when"): cv.datetime,
            vol.Optional("nudge", default=0.0): vol.All(vol.Coerce(float), vol.Range(min=0, max=3)),
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    room_schema = vol.Schema({vol.Required("room"): cv.string})
    hass.services.async_register(DOMAIN, "reset_feedback", reset_feedback, schema=room_schema)
    hass.services.async_register(DOMAIN, "resume", resume, schema=room_schema)
    controller.async_start()
    return True
