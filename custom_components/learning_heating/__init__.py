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
from .nights import ANSWERS, NightCheck
from .schedule import validate_schedule

_LOGGER = logging.getLogger(__name__)

ROOM_SCHEMA = vol.Schema(
    {
        vol.Required("name"): cv.string,
        vol.Required("climate"): cv.entity_id,
        vol.Optional("sensor"): cv.entity_id,
        vol.Optional("valve"): cv.entity_id,
        vol.Optional("zone"): cv.string,
        vol.Optional("boost"): cv.entity_id,
        vol.Optional("people", default=[]): cv.entity_ids,
        vol.Optional("power"): vol.Coerce(float),
        vol.Optional("boost_power"): vol.Coerce(float),
        vol.Optional("quiet"): vol.Schema(
            {vol.Required("from"): cv.time, vol.Required("to"): cv.time}
        ),
        vol.Optional("cycle"): vol.Schema(
            {
                vol.Optional("heat_target", default=30.0): vol.Coerce(float),
                vol.Optional("heat_fan", default="high"): cv.string,
                vol.Optional("purge_fan", default="low"): cv.string,
                vol.Optional("heat_minutes", default=25): cv.positive_int,
                vol.Optional("purge_minutes", default=10): cv.positive_int,
                vol.Optional("lead_minutes", default=180): cv.positive_int,
                vol.Optional("off_at_or_below", default=12.0): vol.Coerce(float),
            }
        ),
        vol.Required("schedule"): validate_schedule,
        vol.Optional("occupied", default=True): cv.boolean,
        vol.Optional("setback"): vol.Coerce(float),
        vol.Optional("thermostat", default=False): cv.boolean,
        vol.Optional("bedroom", default=False): cv.boolean,
    }
)

NIGHT_SCHEMA = vol.Schema(
    {
        vol.Optional("at", default="09:00"): cv.time,
        vol.Optional("open"): cv.string,
        vol.Required("people"): {
            cv.entity_id: vol.Schema(
                {vol.Required("notify"): cv.string, vol.Optional("room"): cv.slug}
            )
        },
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
                vol.Optional("energy_price"): vol.Coerce(float),
                vol.Required("rooms"): {cv.slug: ROOM_SCHEMA},
                vol.Optional("night_check"): NIGHT_SCHEMA,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config[DOMAIN]
    rooms = conf["rooms"].values()
    lopsided = [room["name"] for room in rooms if ("valve" in room) != ("zone" in room)]
    if lopsided:
        _LOGGER.error("Rooms need both a valve and a zone, or neither: %s", ", ".join(lopsided))
        return False
    unknown = {room["zone"] for room in rooms if "zone" in room} - set(conf["zones"])
    if unknown:
        _LOGGER.error("Rooms refer to undefined zones: %s", ", ".join(sorted(unknown)))
        return False

    controller = HeatingController(hass, conf)
    await controller.async_load()
    hass.data[DOMAIN] = controller
    if "night_check" in conf:
        controller.nights = NightCheck(hass, controller, conf["night_check"])
        await controller.nights.async_load()
    for platform in (
        Platform.SENSOR, Platform.SWITCH, Platform.BUTTON, Platform.CLIMATE, Platform.SELECT
    ):
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

    async def night_feedback(call: ServiceCall) -> ServiceResponse:
        room = room_for(call)
        if controller.nights is not None:
            person = call.data.get("person") or controller.nights.person_for_user(call.context.user_id)
            entry = await controller.nights.async_answer(room.slug, call.data["feeling"], person=person)
        else:
            entry = await controller.async_night_feedback(room, call.data["feeling"])
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
    hass.services.async_register(
        DOMAIN, "night_feedback", night_feedback,
        schema=vol.Schema({
            vol.Required("room"): cv.string,
            vol.Required("feeling"): vol.In(list(ANSWERS)),
            vol.Optional("person"): cv.entity_id,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    room_schema = vol.Schema({vol.Required("room"): cv.string})
    hass.services.async_register(DOMAIN, "reset_feedback", reset_feedback, schema=room_schema)
    hass.services.async_register(DOMAIN, "resume", resume, schema=room_schema)
    controller.async_start()
    if controller.nights is not None:
        controller.nights.async_start()
    return True
