# Learning Heating

A Home Assistant custom integration that runs room schedules on radiator valves (TRVs)
and learns how each room behaves, so rooms are warm when you need them and the boiler
runs less when you don't.

Built for eQ-3 MAX! valves on a zoned boiler, but it works with any `climate` entity
that takes a target temperature plus a sensor reporting the valve opening.

## What it does

- **Learns each room** from its temperature reports: how fast it warms with the valve
  open, how fast it loses heat to outside, steady gains (sun, people, cooking) and how
  long the radiator takes to respond. Works with sparse, irregular reports.
- **Starts heating early** so each room reaches its next target on time, using the
  hourly weather forecast. Learnt timing is only used for a room once its predictions
  beat "the temperature won't change"; until then a fixed one-hour lead is used.
- **Coasts** into lower targets when the room will stay warm enough without heat.
- **Warm-weather cut-off**: rooms drop to setback when the outdoor forecast is mild.
- **Respects manual changes** made at the valve or in Home Assistant until the room's
  next schedule change, including changes made while Home Assistant was restarting.
- **Corrects valves that won't shut**: learns from history how far above its setpoint
  each valve stops calling for heat (needs a `valve` attribute on the climate entity).
  A valve that stays open several degrees above its setpoint is sent a lower number
  once the room is warm enough, so it stops holding its zone and the boiler on.
- **Keeps score**: `sensor.heating_cold_room_hours_today` counts the hours rooms in use
  spent more than 1 °C colder than wanted, to weigh against heating run time.
- **Presence**: list `people` and, once all of them have been out for 20 minutes, rooms
  ease off by 2 °C (to setback after 12 hours). Normal service resumes the moment anyone
  is home. `switch.heating_guests_staying` turns this off while untracked people stay.
- **Comfort feedback**: "too hot" / "too cold" buttons and a service. Too cold while the
  room was still short of its target makes it start earlier. Too hot while the room was
  already well above its target (heat left over from earlier, e.g. a warm evening at
  bedtime) lowers that earlier, warmer part of the day. Otherwise the target for that
  part of the day moves by 0.5 °C, or by up to 1.5 °C where that is needed to put the
  target on the right side of the temperature the room was at (too cold at 17.3 °C
  means the target must be above 17.3 °C to bring any heat). A second press within
  10 minutes isn't counted twice.
- **Heat pumps whose own sensor can't be trusted** (`cycle`): some units read the
  temperature of their own output, and keep reading high for an hour after they stop.
  Such a room heats flat out for a spell, runs the fan alone for a few minutes so the
  reading becomes the room's, and heats again only if the room is then more than 0.5 °C
  below target. A cold room warms up in repeated spells; a warm one is left off. A
  target at or below `off_at_or_below` keeps the unit off. Turning the unit off by hand
  stops it until resumed; changing its mode by hand leaves it alone until the next
  schedule change. There is no cooling yet.
- **Hand changes are feedback**: turning a room up or down by hand (on the valve, the
  panel or the thermostat) says the temperature was not right for that part of the day,
  so that part moves toward what was chosen, by up to 1.5 °C. A change of more than 3 °C,
  or one made while pre-heating, coasting or with the room not in use, is not learnt.
- **Steady pre-heating**: once a room starts pre-heating or coasting it carries on until
  the schedule change, instead of switching back and forth as the readings move.
- **A reply to every press**: each room's target sensor carries `last_feedback_reply`
  (what the press changed, in plain words) and `last_feedback_at`, so a dashboard can
  confirm a press the moment it happens.
- **Rooms that follow a person**: give a room its own `people` and it ignores everyone
  else: it eases off by 2 °C when they have all been out for 20 minutes, and drops to
  its setback after 2 hours.
- **Trying less heat**: after a week with no complaint or hand change in a room, one
  part of its day is set 0.5 °C lower. Seven days in use without a complaint keeps the
  saving and the next part of the day gets its turn. A "too cold" or a hand turn-up
  puts it back and leaves that part of the day alone for a month. It never goes more
  than 2 °C below the schedule.
- **Electric heaters**: a room without a `valve` and `zone` is heated by heaters that
  have their own thermostats, such as panel heaters. Its warm-up is learnt in the same
  way. A second heater given as `boost` only runs to help the room warm up, or when it
  has fallen 2 °C behind. With `power` set, the room gets an estimated energy sensor.
- **Quiet hours**: for heaters that beep or click when sent a command. Between `from`
  and `to` nothing at all is sent to the room. Twenty minutes beforehand the room is
  set for the schedule as it stands when quiet hours begin, the boost heater is
  switched off, and both are left alone until quiet hours end. Schedule changes that
  fall inside quiet hours wait until they end, so put the bedtime entry before `from`.
- **Weekday and weekend schedules**: give a room's schedule as two mappings under
  `weekdays` and `weekends`.

## Install

1. In HACS, add this repository as a custom repository (category: Integration) and
   download **Learning Heating**.
2. Copy [`examples/learning_heating.yaml`](examples/learning_heating.yaml) to your
   config folder, edit it for your rooms, and add to `configuration.yaml`:
   ```yaml
   learning_heating: !include learning_heating.yaml
   ```
3. Restart Home Assistant. Valves are switched to manual (`heat`) mode so their own
   built-in schedules don't interfere.

## Entities

Per room:

| Entity | Purpose |
|---|---|
| `sensor.<room>_heating_target` | Target being applied, with a plain-English `summary` (e.g. "Warming up to 21° for 06:30"), the reason, next change, pre-heat lead and feedback adjustments as attributes |
| `sensor.<room>_temperature` | Last reported temperature (kept in long-term statistics) |
| `sensor.<room>_warm_up_rate` | Learnt warm-up rate in °C/h, with heat loss, gains, radiator lag and prediction error as attributes |
| `switch.<room>_occupied` | Off holds the room at its setback temperature |
| `button.<room>_too_hot` / `button.<room>_too_cold` | Comfort feedback for right now |
| `button.<room>_resume_schedule` | Ends a manual change; only available while the room is held by one |
| `sensor.<room>_heating_energy_today` | Electric rooms with `power` set: estimated kWh today, from rated power and whether each heater is below its setting. Not a meter reading. Yesterday's total and, with `energy_price`, the cost are attributes |

Plus `switch.learning_heating_control`: off stops all valve writes (valves keep their
last setting).

## Services

- `learning_heating.feedback`: `room`, `feeling` (`too_hot` / `too_cold`), optional
  `when` (any time in the last two days) and optional `nudge` (°C) to also warm or cool
  the room straight away until its next schedule change. Returns what was changed.
- `learning_heating.resume`: `room`. Ends a manual change, or a valve switched off by
  hand, and returns the room to its plan straight away.
- `learning_heating.reset_feedback`: `room`. Forgets that room's feedback adjustments.

## Manual changes

Any change to a valve's target that the controller didn't make (the knob, the app,
another automation) holds that room until its next schedule change, or 4 hours if it
has none. A valve switched to Off by hand stays off. While held, the room keeps
learning but its valve isn't touched, ahead of occupancy and warm-weather rules.
Press the room's **resume schedule** button (or call `learning_heating.resume`) to
end it early.

## The model

```
dR/dt = (u - R) / tau                 radiator warmth, lagging the valve
dT/dt = a * R - k * (T - T_out) + g   room temperature
```

`u` is the valve opening (zero while the zone isn't circulating). Integrating between
two temperature reports gives one linear sample in `a`, `k` and `g`, fitted by
recursive least squares; three radiator lags are learnt side by side and the
best-predicting one is used. Models are saved in `.storage/learning_heating.models`.
