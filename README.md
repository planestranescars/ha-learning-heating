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
  A valve that stays open more than 1.5 °C above its setpoint is sent a lower number
  once the room is warm enough, so it stops holding its zone and the boiler on, and a
  bedroom is not kept warmer than wanted all night.
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
  schedule change. Each heating spell lasts only as long as the gap to the target needs,
  at the warm-up rate learnt so far, so a room close to target is not overshot.
  There is no cooling yet.
- **A room thermometer of its own**: give a room `sensor:` (any temperature sensor) and
  that reading is used as the room's temperature and for learning, instead of the
  heating unit's own. A heat-pump room with a sensor needs no fan-only checks: it heats
  until the room, plus the rise still to come after the unit stops, reaches the target.
  That rise is learnt by watching the room after each spell, and shown as
  `rise_after_heat_c`. If the sensor stops reporting for 15 minutes the room falls back
  to fan-only checks.
- **How hard a heat pump runs is learnt**: a heat pump is most efficient, and a room most
  even, when asked for no more than the room needs. A heat-pump room with a sensor has
  four outputs, the configured `heat_fan` at 24°, 26°, 28° and the configured
  `heat_target` (measured in a garden office: a slower fan left the heat round the unit,
  and the setting is what limits how hard the unit works). Each spell starts at
  the output learnt for weather like today's (outdoor temperatures in 3 °C bands). If the
  room is not there after 20 minutes the output steps up, and next time in that weather
  starts higher, not going back to the output that failed for a fortnight; if spells keep
  ending within 12 minutes, next time starts lower. A room
  1.5 °C or more below target, as at the start of a warm-up, gets full output. It heats
  again 0.3 °C below target, and a spell lasts at least 5 minutes. The current and learnt outputs are shown as `output_level`
  and `output_levels_learnt`.
- **Warm-ups tune themselves**: a warm-up aims to arrive on time rather than early (a
  bedroom warming while people are still asleep wakes them; a little short at the change
  does less harm). Each one is checked against what happened. A room that reached its
  temperature 15 minutes or more before it was wanted starts that much later next time;
  one still more than 0.7 °C short starts 30 minutes earlier. Each change
  is reported on the target sensor as `last_auto_change`.
- **Schedule times that move**: if a room is turned up, or reported too cold, shortly before
  a warmer step (or turned down shortly before a cooler one) three times running, that
  step moves earlier; the opposite just after a step moves it later. Steps move 15 minutes
  at a time, by at most an hour, and the shift is shown as `schedule_shifts`.
- **Sun and wind**: the thermal model also learns how much each room warms in sunshine
  and how much extra heat a breeze takes from it, using the weather entity's condition,
  cloud cover and wind, and the forecast of them when planning a warm-up.
- **Warm-up that can start later**: too hot during the run-up to a warmer part of the day
  moves that room's warm-up start 30 minutes later (too cold while still short of target
  moves it earlier), instead of changing the temperature wanted.
- **A thermostat per room** (`thermostat: true`): a climate entity that shows and sets the
  temperature wanted in the room, apart from whatever is sent to the valve or heater
  (which may be lower while the room eases off). Setting it holds the room until its
  next schedule change and counts as a hand change.
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
- **How the night was**: mark rooms `bedroom: true` and each gets three buttons, last
  night too cold / fine / too warm, which can be pressed at any time that day. Too warm
  after heating that came on while people were asleep (more than 30 minutes before the
  morning step) makes it come on 30 minutes later; otherwise the night's temperature
  comes down 0.5 °C. Too cold with the room still short at the morning step makes it
  warm up 30 minutes earlier; otherwise the night's temperature goes up 0.5 °C. With
  `night_check`, each person listed is asked on their phone each morning (at `at`, while
  they are not away) about the bedroom they sleep in, shown and changed with
  `select.<person>_sleeps_in`, and can answer from the notification. After 4 good nights
  in a row, or a week of unanswered questions, they are not asked again until a bad
  night is reported for their room. The same is available as the `night_feedback`
  service.
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
- `learning_heating.night_feedback`: `room`, `feeling` (`too_cold` / `fine` / `too_hot`),
  optional `person`. How last night was in a bedroom; see "How the night was".
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
