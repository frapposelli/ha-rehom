# Rehom for Home Assistant

An **unofficial** [HACS](https://hacs.xyz/) integration for **Rehom / Radiax** radiant floor heating and cooling controllers: the "Rehom server" that runs the RadiaxWeb web app and manages the zones, the ventilation and dehumidification units (VMCs) and the house-level settings. It talks to the controller over your local network and receives changes as they happen (local push).

This project is not affiliated with, endorsed by or supported by Rehom S.r.l. See the [disclaimer](#disclaimer).

## Status: preview, control is opt-in

Home Assistant shows the house, its zones and its VMCs. It changes settings on the controller **only if you turn on Enable control** in the integration options. Control is off by default: until you turn it on, every control action (thermostat mode, preset or temperature, fan speed, VMC mode, zone offset, switches) is refused with the error "Control from Home Assistant is off", and no change is ever sent to the controller.

With control on, Home Assistant sends only the settings that have been tested on a real controller, and refuses the others. See [Control](#control).

## Requirements

- Home Assistant **2026.9.0** or newer.
- [HACS](https://hacs.xyz/), for the recommended installation.
- A Rehom server reachable from Home Assistant over HTTP (port 8000 by default).
- The user name and password of an **end-user** account of the official Rehom app. Do not use the installer's account.

Home Assistant installs the Python library [`aiorehom`](https://github.com/frapposelli/aiorehom) automatically.

## Installation

### HACS (custom repository)

1. In Home Assistant, open **HACS**, then the **⋮** menu, then **Custom repositories**.
2. Add `https://github.com/frapposelli/ha-rehom` with the type **Integration**.
3. Search HACS for **Rehom**, open it and select **Download**.
4. Restart Home Assistant.
5. Add the integration (see [Configuration](#configuration)).

### Manual

1. Download the latest release of this repository.
2. Copy `custom_components/rehom/` into the `custom_components/` folder of your Home Assistant configuration directory.
3. Restart Home Assistant and add the integration.

## Configuration

Go to **Settings → Devices & services**. Home Assistant may already show a discovered **Rehom server**, found by zeroconf or DHCP. If it does, select **Add** and enter your credentials. Otherwise select **Add integration → Rehom** and fill in:

| Field | Meaning |
|---|---|
| Host | Host name or IP address of the Rehom server. The default is `rehomserver.local`. |
| Port | HTTP port of the Rehom server. The default is `8000`. |
| Username | User name of your end-user account in the Rehom app. |
| Password | Password of that account. |

The integration connects once to check the address and the credentials, then identifies the controller by its MAC address. Each controller can be added once.

- **Reconfigure** changes the host or the port. The new address must reach the same controller.
- **Re-authenticate** starts automatically if the controller refuses the stored credentials.
- If DHCP later reports a new address for a controller that you added **by IP address**, the entry follows it. So does a zeroconf discovery of that controller at a new address, once you confirm it with your credentials. An entry added by host name is never changed.
- A discovered controller that you do not want can be **ignored**. It stays ignored after a restart.

### Options

| Option | Meaning |
|---|---|
| Enable control | Off by default. When on, Home Assistant can change the settings listed under [Control](#control). Take a Home Assistant backup before you turn it on. |
| Default temporary comfort duration | Hours of comfort (0.5 to 24, in steps of 0.5) that **Set temporary comfort** uses when no duration is given. Temporary comfort is not available yet, so this option has no effect for now. |

Changing the options reloads the integration.

## Entities

The integration creates one device for the controller, one for the plant (the whole house), one per zone and one per VMC. What exists depends on your installation: for example, a zone gets a humidity sensor only if its probe measures humidity.

| Device | Entities |
|---|---|
| **Rehom server** (the controller) | Live updates (WebSocket connected), bus and watchdog problems, controller heartbeat\*, installer session, CPU temperature\*, "Controlled by" (web server, crono panel, or serial line down) |
| **Rehom plant** (the whole house) | House thermostat (mode, preset, level temperature, current temperature), season, controller setpoint, the four level temperatures, active alarms, demand, setpoint mismatch, read-only mode, alarm, alarm events, predictive algorithm |
| **Zone** | Zone thermostat, temperature, humidity, active level, control source, temporary comfort end, next change, calling, probe connection, setpoint forced, alarm, alarm events, temperature offset |
| **VMC** | Ventilation (fan), operating mode, effective mode, state, duty cycle\*, inlet air temperature\*, renewal damper position\*, dehumidifying, heating/cooling integration, defrost, schedule active, compressor\*, connection, alarm flags (dirty filter, probes, pressure, ...), alarm, alarm events, free cooling (if enabled by the installer) |

\* Disabled by default. Enable it from the entity's settings.

This version creates no entities for fancoils, home automation (domotica) devices, energy or water meters, or weather data.

Notes:

- The house thermostat's target is the temperature of the selected level (economy, pre-comfort or comfort). Its `controller_setpoint` attribute and the **Controller setpoint** sensor show what the plant really regulates to.
- The house thermostat's current temperature is the temperature of zone 001 (the web app's "T IN"). It is unknown while that zone's probe is offline. The house's heating/cooling action and the **Demand** sensor ignore zones whose probe is offline.
- When a zone probe or a VMC stops responding, all of its entities become unavailable except its connection sensor, which turns off. While the controller's serial line to the plant is down, every zone and VMC entity is unavailable.
- A zone's target is its base temperature plus its whole-degree offset (see [Zone temperatures](#zone-temperatures)). Zone thermostats use steps of 1 °C. The **Temperature offset** is a temperature difference: if you choose °F in its entity settings, +1 °C shows as +1.8 °F.
- Zone and VMC devices are not assigned to areas automatically. Assign them yourself.
- **Alarm events** have the event types `raised` and `cleared`. Their data holds `alarm_id`, `source`, `unit`, `code`, `text` and `first_seen`. They fire only for changes after Home Assistant started, never for alarms that were already active. `cleared` fires 60 seconds after the alarm ends (see [Known limitations](#known-limitations)).
- The integration is translated into English and Italian.

## Control

Control is **off by default**. While it is off, Home Assistant only reads the controller: every control action, whether it comes from a dashboard, an automation, a script or a voice assistant, is refused with "Control from Home Assistant is off", and nothing is sent.

### Turning control on

1. Take a Home Assistant backup.
2. Go to **Settings → Devices & services → Rehom** and select **Configure** on the entry.
3. Turn on **Enable control** and select **Submit**. The integration reloads.

Turn it off the same way. The [diagnostics](#diagnostics) show whether it is on.

When control is on, anyone and anything that can use the Rehom entities in Home Assistant (users, automations, scripts, voice assistants) can change the plant. Decide which entities you expose to voice assistants before you turn it on.

### What you can change

Home Assistant sends only settings that have been tested on a real Rehom controller:

| Entity | What you can do |
|---|---|
| House thermostat | Mode **Auto**, and turning it on when it is off (it goes to Auto). Presets **Economy** and **Comfort**. **Heat** or **Cool** (the mode of the current season) selects the last manual level again: Comfort if none has been seen. Preset **None** goes back to Auto; while the thermostat is off, it does nothing. While it is off, choosing Economy, Comfort, Heat or Cool switches it on at that level. |
| Zone thermostat | The target temperature (see [Zone temperatures](#zone-temperatures)). Back to the schedule (mode **Auto**, preset **None**, or turn on), presets **Economy**, **Pre-comfort** and **Comfort**, and **Heat** or **Cool** (the last manual level again): all of them need the house in Auto. Preset **None** does nothing while the zone is off (a zone switched off at its room probe refuses it, see [What is refused](#what-is-refused)). |
| Temperature offset (zone) | −3 to +3 °C, in whole degrees. A half degree rounds up, as on the zone thermostat. |
| Ventilation (fan) | Speeds 50 %, 75 % and 100 %. Turning it on when it is stopped or in standby selects the operating mode it last ran in since Home Assistant started (or the integration was reloaded). Otherwise it selects Dehumidification, or Ventilation if Dehumidification is not available. If the mode it last ran in is one that Home Assistant does not send, turning it on is refused. |
| Operating mode (VMC) | Dehumidification and Ventilation. |
| Predictive algorithm | On and off. Needs the house in Auto. |

### What is refused

- **Not tested yet**, refused with "Home Assistant does not send this setting yet":
  - the house's **Pre-comfort** preset. **Heat** and **Cool** on the house select the last manual level again, so they are refused when that level is Pre-comfort;
  - the house thermostat's target temperature (the comfort temperature);
  - the fan speed 25 %, turning the ventilation off, and VMCs whose fan speed is continuous;
  - the VMC operating modes other than Dehumidification and Ventilation;
  - any change to the mode or preset of a **zone that was switched off at its room probe** (its **Control source** sensor shows Probe). Home Assistant does not turn such a zone back on: turn on, toggle, mode Auto, Heat or Cool, and every preset (None included) are refused, and nothing is sent. Turn it back on at the probe or in the official app. This applies only to those zones: turning on the house thermostat when it is off, a zone that was switched off in the official app, or the ventilation when it is stopped or in standby still works.
- **Not possible from Home Assistant**, refused with "Home Assistant cannot do this on the Rehom controller": turning the house or a zone **off**, and the **Free cooling** switch. The rapid VMC modes are not offered.
- **Not available yet**: the **Temporary comfort** preset and the two temporary comfort [actions](#actions).
- **Never changed** from Home Assistant: the season, the schedules, fancoils and home automation devices. Use the official Rehom app for them.

The controller has its own rules, and Home Assistant reports them as errors. Nothing can be changed while the crono panel controls the plant, while the controller is in read-only mode or while its serial line is down. Zone modes and the predictive algorithm need the house in Auto. A zone's mode cannot change while its setpoint is forced or a temporary comfort runs. The fan speed is fixed in the Stop, Standby and rapid modes. A VMC operating mode, and fan speed control, must have been enabled by the installer.

Home Assistant reports a refusal caused by the request or by the plant's settings as an invalid action, and a refusal caused by the device as a device error: the controller's serial line to the plant is down, a zone probe or a VMC is not responding, or a VMC is in an error state or a forced mode. In both cases nothing is sent.

### How changes are applied

- Home Assistant sends each change once and waits until the controller reports it. The entity shows the new value only then: it never shows a value the controller has not confirmed.
- A change usually takes a second or two. While **Live updates** is off, Home Assistant has to read the whole state to confirm a change, so an action can take from about 20 seconds to about 3 minutes.
- Changes are sent one at a time, in the order they were made; a change waits for the ones before it. Stopping the automation or script that made a change does not undo it: once sent, it may still be applied.
- If the controller does not confirm a change, the action fails with "the controller did not confirm it". The change may still have been applied: the entity shows what the controller reports. If sending fails, for example because of a connection error, the action fails with "It may or may not have been applied".
- Setting a value that the controller already reports sends nothing.

### Zone temperatures

A zone's target is its base temperature (the temperature of its current level) plus its **temperature offset**, in whole degrees from −3 to +3 °C. Setting a zone thermostat's target changes the offset: Home Assistant rounds the difference from the base temperature to whole degrees, and a half degree rounds up (base + 0.5 °C gives +1). You can also set the offset directly with the zone's **Temperature offset** entity, in any house mode. It rounds the same way.

An action that sets both the mode and the target of a zone (`climate.set_temperature` with `hvac_mode`) applies the mode first, and only if it differs from the current one. A new mode can change the base temperature. If the target is then more than 3 °C from the new base, it is refused with "The target temperature of this zone must be between … °C and … °C": the target is not changed, but the mode change stays applied. A target that is not a number is refused with "The requested value is not a valid number", and nothing is sent.

The offset **stays** when the schedule moves to another slot or level. For example, +1 °C set during a comfort slot also raises the next economy slot by 1 °C. Set it back to 0 to follow the schedule's temperatures again. A zone without a target (its level or that level's temperature is unknown) cannot be set.

## Actions

All three actions target Rehom climate entities.

### `rehom.get_schedule`

Returns the weekly schedules of a zone for winter and summer: the preset bound to each weekday and its 48 half-hour levels, in controller time. Calling it on the house thermostat fails with "This action is only available for zone thermostats".

```yaml
action: rehom.get_schedule
target:
  entity_id: climate.living_room
response_variable: schedule
```

Response, one entry per targeted entity (shortened):

```yaml
climate.living_room:
  zone: "001"
  season: winter            # the active season, null if unknown
  controller_time: "2026-01-15T07:30:00"   # the controller's local time
  timezone: Europe/Rome     # the controller's time zone
  winter:
    source: prog
    crono: [pre_comfort, ...]      # the crono-panel program (48 levels), or null
    days:
      - weekday: 0                 # 0 = Sunday, as on the controller
        day: sunday
        preset: "1"
        levels: [economy, economy, ..., comfort]   # 48 half-hour slots from 00:00
      # ... 7 days
  summer: { ... }                  # same shape
```

A level is `off`, `economy`, `pre_comfort`, `comfort` or `null` for an invalid slot. `levels` is `null` when a day has no valid program.

### `rehom.set_temporary_comfort` and `rehom.clear_temporary_comfort`

These keep a zone at comfort for a number of hours, and end that. The optional `duration` must be 0.5 to 24 hours in steps of 0.5. **They are not available yet, and both are always refused**: with control off, with "Control from Home Assistant is off"; with control on, with "Temporary comfort is not available from Home Assistant yet" (on the house thermostat, with "This action is only available for zone thermostats"). They exist so that automations can be written now.

## How data is updated

The integration uses **local push**. The controller streams changes over a WebSocket, and the library adds a full resync every 10 minutes and a liveness check every 30 seconds. If the WebSocket drops, the library falls back to reading the full state over HTTP every minute. During that time "Live updates" is off and everything else stays available. When the controller stops answering, every entity becomes unavailable, and the log records the outage once and the recovery once. Values that change only with time, such as schedule slots, the end of a temporary comfort and alarm debouncing, are updated at the exact instant without polling.

## Repairs

The integration may raise these repair issues. Only the first can be fixed from Home Assistant, and only when control is on. The minutes count only while the controller answers.

| Issue | When |
|---|---|
| The Rehom plant regulates to a different temperature | The house is in manual mode, and for more than 5 minutes the controller setpoint has differed from the selected level's temperature. With **Enable control** on, and when the selected level can be set from Home Assistant (Economy or Comfort, see [What you can change](#what-you-can-change)), open the issue and confirm: Home Assistant selects the same level again, which rewrites the controller setpoint. If the controller does not confirm it, the fix may still have been applied, and the issue then closes by itself. Otherwise, raise the level's temperature by 0.1 °C in the official app, then lower it again. |
| An installer session is open | The controller has reported an open installer session for more than 15 minutes. |
| The Rehom season settings disagree | For more than 15 minutes, the running season has differed from the installer configuration. |
| Unsupported Rehom server version | The controller answers in a format this integration does not understand, for example after a firmware update. If this happens at startup, the integration is not set up: reload it after updating the integration. |

## Diagnostics

Open **Settings → Devices & services → Rehom**, then the entry's **⋮** menu, and select **Download diagnostics**. The file contains the integration options (including whether control is on), the redacted state model, the connection counters and the software versions. The MAC address, names, serial numbers, the host and the credentials are removed. Check the file before you share it.

## Troubleshooting

- Enable debug logging:

  ```yaml
  logger:
    logs:
      custom_components.rehom: debug
      aiorehom: debug
  ```

  Or use **Enable debug logging** on the integration's page, reproduce the problem, then disable it to download the log.
- **Failed to connect**: check that Home Assistant can reach `http://<host>:8000/` and that no firewall rule blocks it. If `rehomserver.local` does not resolve on your network, use the controller's IP address, preferably a DHCP reservation.
- **Invalid username or password**: use an end-user account of the Rehom app, not the installer's.
- **Entities unavailable**: the controller is not answering, or a zone probe or VMC is offline (its connection sensor is off).
- **"Control from Home Assistant is off"**: turn on **Enable control** in the integration options (see [Control](#control)).
- **"Home Assistant does not send this setting yet"**: that value has not been tested on a Rehom controller. Use the official app for it. A zone that was switched off at its room probe gets this error too (see [What is refused](#what-is-refused)).
- **"The target temperature of this zone must be between …"** or **"The requested value is not a valid number"**: see [Zone temperatures](#zone-temperatures).
- **"The controller did not confirm it"** or **"It may or may not have been applied"**: check the entity, which shows what the controller reports, and the **Live updates** sensor before you try again.
- Other refusals name their reason, for example the crono panel or a house that is not in Auto.
- When you open an issue, attach the diagnostics file and the debug log, after checking that they contain nothing you do not want to share.

## Known limitations

- **Control is opt-in and limited** to the settings listed under [What you can change](#what-you-can-change). The schedules, the season, temporary comfort, turning the house or a zone off, and the house's comfort temperature cannot be changed from Home Assistant, and a zone that was switched off at its room probe is not turned back on.
- **A zone switched off at its room probe** is recognised from the last state Home Assistant received. If the probe switches a zone off at the very moment a mode change for it is sent, that change can still put the zone back on its schedule.
- **The ventilation remembers its last operating mode only until Home Assistant restarts** or the integration reloads (see [What you can change](#what-you-can-change)).
- **Changes can be slow while Live updates is off**: an action can take up to about 3 minutes, and later changes wait for it.
- **A zone's offset stays** across schedule slots and levels until you change it (see [Zone temperatures](#zone-temperatures)).
- A zone's heating/cooling action comes from the zone's call signal. It can lag a setpoint change by about 30 seconds, and a zone above its target does not always call.
- **Alarms are debounced for 60 seconds, in both directions.** A flag that clears within a minute never turns on an alarm sensor and never fires an event. Once an alarm is on, it turns off (and fires `cleared`) only after it has been gone for 60 seconds.
- If a zone or VMC disappears from the controller, its entities become unavailable. They are not removed automatically, but you can delete the device by hand.
- Use one integration entry per controller: every full read keeps the controller busy for a moment.

## Removal

1. Go to **Settings → Devices & services → Rehom**, open the **⋮** menu of the entry and select **Delete**.
2. To remove the code, uninstall **Rehom** in HACS (or delete `custom_components/rehom/`) and restart Home Assistant.

Removing the entry deletes its devices, entities and repair issues. Nothing on the controller changes.

## Security

The controller's local interface uses plain HTTP and WebSocket, without encryption, so treat it as a device that must stay on a trusted network:

- Keep the controller on a trusted, preferably isolated network segment (for example an IoT VLAN), and allow access to it only from Home Assistant and the devices that need it.
- **Never expose** the controller's ports (8000 and 1337) to the internet, for example with router port forwarding. For remote access, use Home Assistant's own remote access or a VPN.
- Use a dedicated end-user account for Home Assistant where your installation allows it, with a long, unique password. Never use the installer's account.

The integration stores the credentials in Home Assistant's configuration, uses them only to log in to the controller, and removes them from diagnostics. With **Enable control** on, it sends changes with the same account.

## Development

Development uses Python 3.14 and [uv](https://docs.astral.sh/uv/). The library is a separate repository, checked out into `lib/aiorehom` (the path `pyproject.toml` uses):

```sh
git clone https://github.com/frapposelli/ha-rehom.git
cd ha-rehom
git clone https://github.com/frapposelli/aiorehom.git lib/aiorehom
uv sync
uv run pytest --cov --cov-report=term-missing
uv run mypy
uv run ruff check custom_components tests
uv run ruff format --check custom_components tests
```

The tests run a real `aiorehom` client against a sanitised recording of a controller that ships with the library, replayed on a virtual clock (`tests/harness.py`). They never contact a device.

## Disclaimer

This is an unofficial, community-made integration. It is not made, endorsed or supported by Rehom S.r.l. or by the maker of the controller's software, and it is provided as is, without any warranty. It relies on the controller's local interface, which is not publicly documented and may change with a firmware update. Use it at your own risk. For problems with your heating, cooling or ventilation plant, contact your installer or Rehom, not this project.

"Rehom", "Radiax" and "RadiaxWeb" are trademarks of their respective owners. They are used here only to identify the products this integration works with. The icons in `custom_components/rehom/brand/` are original artwork of this project, not the manufacturer's logo.

## License

[MIT](LICENSE) © 2026 Fabio Rapposelli
