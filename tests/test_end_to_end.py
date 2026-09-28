"""End to end: the whole integration on the whole recorded capture.

One entry is set up with every platform on a real ``RehomClient`` replaying the
sanitised fixture (``lib/aiorehom/tests/fixtures/20260925T102117Z``).  The
test checks the entity inventory of the reference plant, then plays every
``ws.jsonl`` frame through the replayed WebSocket up to the last one
(10:51:44.8Z) and checks this timeline:

- the whole-house setpoint mismatch: problem state from the first sync, repair
  issue once the 5-min grace has passed, both cleared by the 10:41:06.4Z
  and 10:41:10.8Z frames;
- zones 001, 009 and 010 cooling from 10:41:34Z, zone 002 at 23 °C throughout;
- the 14-s VMC 001 probe glitch at 10:37:07Z: raw alarm only, no problem
  state, no event (60-s debounce);
- no alarm event at the first sync, nor anywhere in the capture;
- nothing but reads reached the device.

A second run has "Enable control" on: the same entities, one offset change
through Home Assistant's number action (shown at once, confirmed by the
controller's echo), then the whole capture; the setpoint-mismatch issue is
fixable there (the house is at comfort, a verified level), and exactly one
write reached the device.
"""

from __future__ import annotations

import json
import logging

from aiorehom import ConnectionState
from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ATTR_PRESET_MODE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rehom.const import (
    DOMAIN,
    ISSUE_SETPOINT_MISMATCH,
    ISSUE_SETPOINT_MISMATCH_FIXABLE,
    PLATFORMS,
)
from custom_components.rehom.diagnostics import async_get_config_entry_diagnostics
from custom_components.rehom.issues import issue_id

from .conftest import CONTROL_OPTIONS
from .harness import (
    FIXTURE_DIR,
    FIXTURE_MAC,
    FIXTURE_START,
    FIXTURE_VMCS,
    FIXTURE_ZONES,
    RehomHarness,
    T,
    termo_update,
)
from .platform_helpers import StateChanges, get_state, is_available

#: The last recorded frame is at 10:51:44.8Z; the capture ends at 10:51:58Z.
AFTER_LAST_FRAME = T("10:51:45")

#: Every read method a ReplayTransport records (the harness logs writes as "post_bulk_update").
READ_CALLS = {
    "login",
    "get_alive",
    "get_config",
    "get_interface",
    "get_overrides",
    "get_plant_conf",
    "get_history",
    "close",
}

HUB_KEYS = {
    Platform.BINARY_SENSOR: ("live_updates", "bus", "watchdog", "heartbeat", "installer_session"),
    Platform.SENSOR: ("cpu_temperature", "controlled_by"),
}
PLANT_KEYS = {
    Platform.CLIMATE: ("climate",),
    Platform.SENSOR: (
        "season",
        "controller_setpoint",
        "temperature_off",
        "temperature_economy",
        "temperature_pre_comfort",
        "temperature_comfort",
        "active_alarms",
    ),
    Platform.BINARY_SENSOR: ("demand", "setpoint_mismatch", "read_only", "alarm"),
    Platform.EVENT: ("alarm",),
    Platform.SWITCH: ("predictive",),
}
ZONE_KEYS = {
    Platform.CLIMATE: ("climate",),
    Platform.SENSOR: (
        "temperature",
        "humidity",
        "level",
        "control_source",
        "temporary_comfort_end",
        "next_change",
    ),
    Platform.BINARY_SENSOR: ("calling", "probe", "setpoint_forced", "alarm"),
    Platform.EVENT: ("alarm",),
    Platform.NUMBER: ("temperature_offset",),
}
VMC_FLAGS = (
    "alarm_expansion",
    "alarm_high_pressure",
    "alarm_dirty_filter",
    "alarm_defrost",
    "alarm_antifreeze_probe",
    "alarm_condensate_level",
    "alarm_recirculation_probe",
    "alarm_fresh_air_probe",
)
VMC_KEYS = {
    Platform.FAN: ("fan",),
    Platform.SELECT: ("mode",),
    Platform.SENSOR: (
        "effective_mode",
        "vmc_state",
        "duty_cycle",
        "inlet_air_temperature",
        "renewal_position",
    ),
    Platform.BINARY_SENSOR: (
        "dehumidifying",
        "integration",
        "defrost",
        "schedule_active",
        "compressor",
        "connectivity",
        *VMC_FLAGS,
        "alarm",
    ),
    Platform.EVENT: ("alarm",),
}
#: Zone 011's probe has no humidity sensor (UMIDITA = 255 in the whole capture).
ZONES_WITHOUT_HUMIDITY = {"011"}

#: Words that must not appear in any unique id: features absent or out of scope in this version.
EXCLUDED_FEATURES = (
    "fancoil",
    "domotica",
    "energy",
    "water",
    "meter",
    "meteo",
    "weather",
    "forecast",
    "free_cooling",
    "internet",
)
EXCLUDED_DEVICE_CLASSES = {
    SensorDeviceClass.ENERGY,
    SensorDeviceClass.GAS,
    SensorDeviceClass.POWER,
    SensorDeviceClass.VOLUME,
    SensorDeviceClass.WATER,
}

HOUSE = "climate.rehom_plant"
MISMATCH = "binary_sensor.rehom_plant_setpoint_mismatch"
ACTIVE_ALARMS = "sensor.rehom_plant_active_alarms"
RECIRCULATION_FLAG = "binary_sensor.vmc_001_recirculation_air_probe_alarm"
VMC_001_ALARM = "binary_sensor.vmc_001_alarm"
GLITCH_ALARM_ID = "vmc:001:ALLARM_SONDA_RICIRCOLO"


def _expected_inventory() -> set[tuple[str, str]]:
    """(domain, unique_id) of every entity the integration creates on this plant."""
    expected: set[tuple[str, str]] = set()

    def add(prefix: str, keys: dict[Platform, tuple[str, ...]]) -> None:
        for platform, platform_keys in keys.items():
            expected.update((str(platform), f"{prefix}_{key}") for key in platform_keys)

    add(f"{FIXTURE_MAC}_hub", HUB_KEYS)
    add(f"{FIXTURE_MAC}_plant", PLANT_KEYS)
    for zone in FIXTURE_ZONES:
        add(f"{FIXTURE_MAC}_zone_{zone}", ZONE_KEYS)
        if zone in ZONES_WITHOUT_HUMIDITY:
            expected.discard(("sensor", f"{FIXTURE_MAC}_zone_{zone}_humidity"))
    for vmc in FIXTURE_VMCS:
        add(f"{FIXTURE_MAC}_vmc_{vmc}", VMC_KEYS)
    return expected


def _recorded_frames() -> int:
    """Number of WebSocket frames in the fixture's ``ws.jsonl``."""
    lines = (FIXTURE_DIR / "ws.jsonl").read_text("utf-8").splitlines()
    return sum(1 for line in lines if line.strip() and "frame" in json.loads(line))


def _issues(issue_registry: ir.IssueRegistry) -> set[str]:
    return {issue for (domain, issue) in issue_registry.issues if domain == DOMAIN}


def _zone_actions(hass: HomeAssistant) -> dict[str, str]:
    return {
        zone: get_state(hass, f"climate.zona_{zone}").attributes[ATTR_HVAC_ACTION]
        for zone in FIXTURE_ZONES
    }


@pytest.fixture
def platforms() -> list[Platform]:
    """The whole integration."""
    return list(PLATFORMS)


def _check_inventory(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """144 entities on 10 devices, exactly the expected set, and nothing out of scope."""
    entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    inventory = {(entity.domain, entity.unique_id) for entity in entities}
    assert len(inventory) == len(entities) == 144
    assert inventory == _expected_inventory()
    disabled = {
        entity.unique_id.removeprefix(f"{FIXTURE_MAC}_")
        for entity in entities
        if entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    }
    assert disabled == {
        "hub_heartbeat",
        "hub_cpu_temperature",
        *(
            f"vmc_{vmc}_{key}"
            for vmc in FIXTURE_VMCS
            for key in (
                "duty_cycle",
                "inlet_air_temperature",
                "renewal_position",
                "compressor",
            )
        ),
    }

    # climates: the whole house and the six zones
    climates = sorted(state.entity_id for state in hass.states.async_all("climate"))
    assert climates == [HOUSE, *(f"climate.zona_{zone}" for zone in FIXTURE_ZONES)]
    # zone sensors: temperature everywhere, humidity where the probe has one
    for zone in FIXTURE_ZONES:
        assert get_state(hass, f"sensor.zona_{zone}_temperature").state not in (
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        )
        humidity = hass.states.get(f"sensor.zona_{zone}_humidity")
        assert (humidity is None) is (zone in ZONES_WITHOUT_HUMIDITY), zone
    # VMCs: operating-mode select, fan and binary sensors
    for vmc in FIXTURE_VMCS:
        assert get_state(hass, f"select.vmc_{vmc}_operating_mode").state == "dehumidify"
        assert get_state(hass, f"fan.vmc_{vmc}").state == STATE_ON
        vmc_binary = [
            entity
            for entity in entities
            if entity.domain == "binary_sensor"
            and entity.unique_id.startswith(f"{FIXTURE_MAC}_vmc_{vmc}_")
        ]
        assert len(vmc_binary) == 15
    # hub: diagnostic entities only
    hub_entities = [
        entity for entity in entities if entity.unique_id.startswith(f"{FIXTURE_MAC}_hub_")
    ]
    assert len(hub_entities) == 7
    assert {entity.entity_category for entity in hub_entities} == {EntityCategory.DIAGNOSTIC}

    # nothing for fancoils, domotica, energy/water meters, weather, Tm/Te (out of scope)
    for entity in entities:
        suffix = entity.unique_id.removeprefix(f"{FIXTURE_MAC}_")
        assert suffix.split("_", 1)[0] in {"hub", "plant", "zone", "vmc"}, entity.unique_id
        assert not any(word in suffix for word in EXCLUDED_FEATURES), entity.unique_id
        assert entity.original_device_class not in EXCLUDED_DEVICE_CLASSES, entity.entity_id
    assert not hass.states.async_all("weather")

    # devices: hub, plant, 6 zones, 2 VMCs; no fancoil/actuator device
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    identifiers = {
        next(iter(device.identifiers))[1].removeprefix(f"{FIXTURE_MAC}_") for device in devices
    }
    assert identifiers == {
        FIXTURE_MAC,
        "plant",
        *(f"zone_{zone}" for zone in FIXTURE_ZONES),
        *(f"vmc_{vmc}" for vmc in FIXTURE_VMCS),
    }


@pytest.mark.usefixtures("init_integration")
async def test_end_to_end_replay(
    hass: HomeAssistant,
    harness: RehomHarness,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Set up on the capture, check the inventory, replay every frame, check the timeline."""
    caplog.set_level(logging.INFO, logger="custom_components.rehom")
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    issue_registry = ir.async_get(hass)
    entry = mock_config_entry
    client = harness.client
    mismatch_issue = issue_id(ISSUE_SETPOINT_MISMATCH, entry.entry_id)
    assert harness.now == FIXTURE_START
    assert client.connection_state is ConnectionState.CONNECTED

    # -- inventory at the first sync -------------------------------------------------
    _check_inventory(hass, entry, entity_registry, device_registry)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["client"]["connection_state"] == "connected"
    assert diagnostics["versions"]["web_services"] == "3.16.3"
    assert FIXTURE_MAC not in json.dumps(diagnostics)

    # -- first sync: no event, the mismatch is visible, its issue waits for the grace ----
    event_ids = sorted(state.entity_id for state in hass.states.async_all("event"))
    assert len(event_ids) == 9
    assert {get_state(hass, entity_id).state for entity_id in event_ids} == {STATE_UNKNOWN}
    events = {entity_id: StateChanges(hass, entity_id) for entity_id in event_ids}
    problems = {
        entity_id: StateChanges(hass, entity_id)
        for entity_id in (RECIRCULATION_FLAG, VMC_001_ALARM, ACTIVE_ALARMS)
    }

    house = get_state(hass, HOUSE)
    assert house.state == HVACMode.COOL
    assert house.attributes[ATTR_PRESET_MODE] == "comfort"
    assert house.attributes[ATTR_TEMPERATURE] == 24.0
    assert house.attributes["controller_setpoint"] == 26.0
    assert house.attributes["setpoint_mismatch"] is True
    assert get_state(hass, MISMATCH).state == STATE_ON
    assert get_state(hass, "sensor.rehom_plant_controller_setpoint").state == "26.0"
    assert _issues(issue_registry) == set()  # raised only after 5 min

    assert get_state(hass, "climate.zona_002").attributes[ATTR_TEMPERATURE] == 23.0
    assert get_state(hass, "number.zona_002_temperature_offset").state == "-1.0"
    assert set(_zone_actions(hass).values()) == {HVACAction.IDLE}
    assert get_state(hass, RECIRCULATION_FLAG).state == STATE_OFF
    assert get_state(hass, VMC_001_ALARM).state == STATE_OFF
    assert get_state(hass, ACTIVE_ALARMS).state == "0"

    # -- the setpoint-mismatch repair after the 5-min grace -------------------------------
    await harness.advance_to(T("10:27:00"))
    assert _issues(issue_registry) == set()
    await harness.advance_to(T("10:28:00"))
    assert _issues(issue_registry) == {mismatch_issue}
    issue = issue_registry.async_get_issue(DOMAIN, mismatch_issue)
    assert issue is not None
    assert issue.translation_key == ISSUE_SETPOINT_MISMATCH
    assert issue.translation_placeholders == {"setpoint": "26", "level_temperature": "24"}
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable

    # -- 10:37:07.134-10:37:21.135: the 14-s VMC 001 probe glitch ---------------------
    await harness.advance_to(T("10:37:10"))
    assert GLITCH_ALARM_ID in {alarm.id for alarm in client.state.alarms}
    assert GLITCH_ALARM_ID not in {alarm.id for alarm in client.state.alarms_debounced}
    await harness.advance_to(T("10:38:30"))  # well past 60 s after the flag rose
    assert GLITCH_ALARM_ID not in {alarm.id for alarm in client.state.alarms}
    assert not client.state.alarms_debounced
    assert get_state(hass, RECIRCULATION_FLAG).state == STATE_OFF
    assert get_state(hass, VMC_001_ALARM).state == STATE_OFF
    assert get_state(hass, ACTIVE_ALARMS).state == "0"
    assert get_state(hass, "event.vmc_001_alarm_events").state == STATE_UNKNOWN

    # -- 10:41:06.4 / 10:41:10.8: TEMP_COM and SET_POINT_TEMP agree, the issue clears --
    await harness.advance_to(T("10:41:05"))
    assert _issues(issue_registry) == {mismatch_issue}
    assert get_state(hass, MISMATCH).state == STATE_ON
    await harness.advance_to(T("10:41:12"))
    assert _issues(issue_registry) == set()
    assert get_state(hass, MISMATCH).state == STATE_OFF
    house = get_state(hass, HOUSE)
    assert house.attributes["controller_setpoint"] == 24.0
    assert house.attributes["setpoint_mismatch"] is False

    # -- 10:41:33.7-10:41:34.0: zones 001, 009 and 010 start cooling ------------------
    await harness.advance_to(T("10:41:30"))
    actions = _zone_actions(hass)
    assert {zone for zone, action in actions.items() if action == HVACAction.COOLING} == {"002"}
    await harness.advance_to(T("10:41:35"))
    actions = _zone_actions(hass)
    assert {zone for zone, action in actions.items() if action == HVACAction.COOLING} == {
        "001",
        "002",
        "009",
        "010",
    }
    assert get_state(hass, "climate.zona_002").attributes[ATTR_TEMPERATURE] == 23.0

    # -- the rest of the capture: every recorded frame, over the one WebSocket ---------
    await harness.advance_to(AFTER_LAST_FRAME)
    device = harness.device
    assert len(device.frames) == _recorded_frames()
    assert harness.driver.delivered == len(device.frames)
    assert device.frames_dropped == 0
    assert device.connections == 1
    assert client.connection_state is ConnectionState.CONNECTED
    assert harness.clients == [client]

    # zone 002 stays at 23 °C (base 24, offset -1) for the whole capture
    assert get_state(hass, "climate.zona_002").attributes[ATTR_TEMPERATURE] == 23.0
    # no alarm event anywhere in the capture, the glitch never became a problem state
    assert all(not recorder.events() for recorder in events.values())
    assert {get_state(hass, entity_id).state for entity_id in event_ids} == {STATE_UNKNOWN}
    assert {state.state for state in problems[RECIRCULATION_FLAG].states} <= {STATE_OFF}
    assert {state.state for state in problems[VMC_001_ALARM].states} <= {STATE_OFF}
    assert {state.state for state in problems[ACTIVE_ALARMS].states} <= {"0"}
    assert _issues(issue_registry) == set()
    # every entity stayed available; the controller was never reported unavailable
    enabled = [
        entity.entity_id
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if entity.disabled_by is None
    ]
    assert len(enabled) == 134
    assert all(get_state(hass, entity_id).state != STATE_UNAVAILABLE for entity_id in enabled)
    assert "is unavailable" not in caplog.text

    # read-only: only GETs (and the login) reached the device
    assert harness.allow_writes == [False]
    assert set(harness.transport_calls) <= READ_CALLS
    assert harness.writes == []
    for recorder in (*events.values(), *problems.values()):
        recorder.stop()


@pytest.mark.parametrize("entry_options", [CONTROL_OPTIONS])
@pytest.mark.usefixtures("init_integration")
async def test_end_to_end_with_control(
    hass: HomeAssistant, harness: RehomHarness, mock_config_entry: MockConfigEntry
) -> None:
    """Control on: the same entities, one confirmed write, then the whole capture."""
    entry = mock_config_entry
    client = harness.client
    assert harness.allow_writes == [True]
    _check_inventory(hass, entry, er.async_get(hass), dr.async_get(hass))  # same as control off

    offset = "number.zona_001_temperature_offset"
    assert get_state(hass, offset).state == "0.0"
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: offset, ATTR_VALUE: 1},
        blocking=True,
    )
    # confirmed by the controller's echo before the action returned
    assert get_state(hass, offset).state == "1.0"
    assert harness.written_values == [{"ZONA.001..DELTA_SETP_CORRENTE": "1.0"}]

    # the mismatch at comfort can be fixed from Home Assistant (comfort is verified)
    issue_registry = ir.async_get(hass)
    mismatch_issue = issue_id(ISSUE_SETPOINT_MISMATCH, entry.entry_id)
    await harness.advance_to(T("10:28:00"))
    assert _issues(issue_registry) == {mismatch_issue}
    issue = issue_registry.async_get_issue(DOMAIN, mismatch_issue)
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_key == ISSUE_SETPOINT_MISMATCH_FIXABLE
    await harness.advance_to(T("10:41:12"))
    assert _issues(issue_registry) == set()

    await harness.advance_to(AFTER_LAST_FRAME)
    assert harness.driver.delivered == len(harness.device.frames)
    assert client.connection_state is ConnectionState.CONNECTED
    assert harness.clients == [client]
    # zone 001 keeps the offset across the capture: base 24 °C from 10:41:18Z, +1
    assert get_state(hass, offset).state == "1.0"
    assert get_state(hass, "climate.zona_001").attributes[ATTR_TEMPERATURE] == 25.0
    # reads, plus exactly one write
    calls = harness.transport_calls
    assert calls.count("post_bulk_update") == 1
    assert set(calls) - {"post_bulk_update"} <= READ_CALLS
    assert len(harness.writes) == 1


@pytest.mark.parametrize(
    "device_patch",
    [
        {
            "extra_frames": [
                (
                    T("10:23:00"),
                    termo_update(
                        "REHOM...STATO_SONDE", "1,0,1,0,0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"
                    ),
                ),
                (T("10:23:00"), termo_update("REHOM...STATO_DEUM", "1,0,0")),
            ]
        }
    ],
)
async def test_offline_units_keep_only_their_connectivity_sensor(
    hass: HomeAssistant,
    harness: RehomHarness,
    entity_registry_enabled_by_default: None,
    entity_registry: er.EntityRegistry,
    init_integration: MockConfigEntry,
) -> None:
    """Availability rule 2 on every platform: an offline unit keeps only its connectivity sensor."""
    await harness.advance_to(T("10:24:05"))  # "not responding" is debounced by now
    entities = er.async_entries_for_config_entry(entity_registry, init_integration.entry_id)
    for prefix, connectivity in (
        ("zone_002_", "binary_sensor.zona_002_probe_connection"),
        ("vmc_002_", "binary_sensor.vmc_002_connectivity"),
    ):
        unit = [
            entity.entity_id
            for entity in entities
            if entity.unique_id.removeprefix(f"{FIXTURE_MAC}_").startswith(prefix)
        ]
        assert len(unit) > 10, prefix
        available = [entity_id for entity_id in unit if is_available(hass, entity_id)]
        assert available == [connectivity], prefix
        assert get_state(hass, connectivity).state == STATE_OFF
    # the outage stays visible on the plant device
    active = get_state(hass, ACTIVE_ALARMS)
    assert {item["alarm_id"] for item in active.attributes["alarms"]} == {
        "zone:002:not_responding",
        "vmc:002:not_responding",
    }
