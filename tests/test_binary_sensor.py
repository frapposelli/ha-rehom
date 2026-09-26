"""Binary sensor platform on the replay."""

from __future__ import annotations

from typing import Any

from aiorehom.replay import ReplayData
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.rehom.binary_sensor import (
    RehomAlarmBinarySensor,
    RehomVmcAlarmFlagBinarySensor,
    RehomVmcBinarySensor,
    RehomZoneBinarySensor,
)

from .harness import FIXTURE_START, RehomHarness, T, bus_update, termo_update
from .platform_helpers import StateChanges, get_entity, get_state, is_available

DIRTY_FILTER = "binary_sensor.vmc_001_dirty_filter"
RECIRCULATION = "binary_sensor.vmc_001_recirculation_air_probe_alarm"
VMC_001_ALARM = "binary_sensor.vmc_001_alarm"
ZONE_002_OFFLINE = "1,0,1,0,0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"
ZONE_002_ONLINE = "1,1,1,0,0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"
#: Binary sensor keys of a zone.
ZONE_002_KEYS = ("calling", "probe", "setpoint_forced", "alarm")


def _frames(*frames: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """``device_patch`` with synthetic frames ``(time, frame)``."""
    return {"extra_frames": [(T(at), frame) for at, frame in frames]}


def _remote_access(data: ReplayData) -> None:
    """Remote access is configured (synthetic record)."""
    data.interface.append(
        {
            "Gruppo": "CONFIG",
            "Unita": "",
            "SubUni": "",
            "Key": "REMOTE_TOKEN",
            "Valore": "<redacted len=32>",
            "path": "CONFIG...REMOTE_TOKEN",
        }
    )


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the binary_sensor platform."""
    return [Platform.BINARY_SENSOR]


async def test_snapshot(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """Every binary sensor at the replay start."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_timeline(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Recorded timeline: demand, heartbeat, the 14-s VMC glitch, mismatch, compressor."""
    recirculation = StateChanges(hass, RECIRCULATION)
    vmc_alarm = StateChanges(hass, VMC_001_ALARM)
    mismatch = get_state(hass, "binary_sensor.rehom_plant_setpoint_mismatch")
    assert mismatch.state == STATE_ON
    assert mismatch.attributes["controller_setpoint"] == 26.0
    assert mismatch.attributes["level_temperature"] == 24.0
    assert mismatch.attributes["since"] == FIXTURE_START.isoformat()
    assert get_state(hass, "binary_sensor.rehom_plant_demand").state == STATE_OFF
    assert get_state(hass, "binary_sensor.rehom_server_live_updates").state == STATE_ON
    assert get_state(hass, "binary_sensor.rehom_server_controller_heartbeat").state == STATE_UNKNOWN
    assert get_state(hass, "binary_sensor.rehom_server_bus").state == STATE_OFF
    assert get_state(hass, "binary_sensor.rehom_server_watchdog").state == STATE_OFF

    # 10:24:34.6 zone 002 calls -> demand; the controller heartbeat has started by then
    await harness.advance_to(T("10:24:40"))
    assert get_state(hass, "binary_sensor.rehom_plant_demand").state == STATE_ON
    assert get_state(hass, "binary_sensor.zona_002_calling").state == STATE_ON
    assert get_state(hass, "binary_sensor.zona_001_calling").state == STATE_OFF
    heartbeat = get_state(hass, "binary_sensor.rehom_server_controller_heartbeat")
    assert heartbeat.state == STATE_ON
    assert heartbeat.attributes["last_heartbeat"] is not None

    # 10:37:07.134-10:37:21.135 VMC 001 ALLARM_SONDA_RICIRCOLO: raw only, never debounced
    await harness.advance_to(T("10:37:10"))
    raw_ids = {alarm.id for alarm in harness.client.state.alarms}
    assert "vmc:001:ALLARM_SONDA_RICIRCOLO" in raw_ids
    assert get_state(hass, RECIRCULATION).state == STATE_OFF
    await harness.advance_to(T("10:37:25"))
    assert get_state(hass, RECIRCULATION).state == STATE_OFF
    assert get_state(hass, VMC_001_ALARM).state == STATE_OFF

    # 10:41:06.4 / 10:41:10.8 the setpoint mismatch ends
    await harness.advance_to(T("10:41:15"))
    mismatch = get_state(hass, "binary_sensor.rehom_plant_setpoint_mismatch")
    assert mismatch.state == STATE_OFF
    assert mismatch.attributes["controller_setpoint"] == 24.0

    # 10:41:47.2 VMC 002 compressor on, dehumidifying
    assert get_state(hass, "binary_sensor.vmc_002_compressor").state == STATE_OFF
    await harness.advance_to(T("10:41:50"))
    assert get_state(hass, "binary_sensor.vmc_002_compressor").state == STATE_ON
    assert get_state(hass, "binary_sensor.vmc_002_dehumidifying").state == STATE_ON

    assert {state.state for state in recirculation.states} <= {STATE_OFF}
    assert {state.state for state in vmc_alarm.states} <= {STATE_OFF}
    recirculation.stop()
    vmc_alarm.stop()


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:23:00", termo_update("DEUM.001..ALLARM_PRESSOSTATO_FILTRO", "1")),
            ("10:25:00", termo_update("DEUM.001..ALLARM_PRESSOSTATO_FILTRO", "0")),
        )
    ],
)
async def test_vmc_flag_debounced(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A VMC flag and the VMC alarm turn on 60 s after the flag, and off 60 s after it ends."""
    await harness.advance_to(T("10:23:59"))
    assert get_state(hass, DIRTY_FILTER).state == STATE_OFF
    assert get_state(hass, VMC_001_ALARM).state == STATE_OFF
    await harness.advance_to(T("10:24:01"))
    assert get_state(hass, DIRTY_FILTER).state == STATE_ON
    alarm = get_state(hass, VMC_001_ALARM)
    assert alarm.state == STATE_ON
    assert [item["alarm_id"] for item in alarm.attributes["alarms"]] == [
        "vmc:001:ALLARM_PRESSOSTATO_FILTRO"
    ]
    assert isinstance(alarm.attributes["alarm_bitmask"], int)
    assert get_state(hass, "binary_sensor.vmc_002_dirty_filter").state == STATE_OFF
    await harness.advance_to(T("10:25:59"))  # the flag dropped at 10:25:00: not cleared yet
    assert get_state(hass, DIRTY_FILTER).state == STATE_ON
    assert get_state(hass, VMC_001_ALARM).state == STATE_ON
    await harness.advance_to(T("10:26:01"))
    assert get_state(hass, DIRTY_FILTER).state == STATE_OFF
    assert get_state(hass, VMC_001_ALARM).state == STATE_OFF


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:23:00", termo_update("DEUM.001..ALLARM_PRESSOSTATO_FILTRO", "1")),
            ("10:25:00", termo_update("DEUM.001..ALLARM_PRESSOSTATO_FILTRO", "0")),
            ("10:25:05", termo_update("DEUM.001..ALLARM_PRESSOSTATO_FILTRO", "1")),
        )
    ],
)
async def test_vmc_flag_short_dropout_does_not_flicker(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A 5-s dropout of a debounced flag keeps the flag and the alarm on (clear debounce)."""
    await harness.advance_to(T("10:24:01"))
    flag = StateChanges(hass, DIRTY_FILTER)
    alarm = StateChanges(hass, VMC_001_ALARM)
    await harness.advance_to(T("10:27:30"))
    assert get_state(hass, DIRTY_FILTER).state == STATE_ON
    assert {state.state for state in flag.states} <= {STATE_ON}
    assert {state.state for state in alarm.states} <= {STATE_ON}
    flag.stop()
    alarm.stop()


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:23:00", termo_update("REHOM...STATO_SONDE", ZONE_002_OFFLINE)),
            ("10:25:00", termo_update("REHOM...STATO_SONDE", ZONE_002_ONLINE)),
        )
    ],
)
async def test_zone_offline(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Availability rule 2: zone 002 offline -> only its probe connection stays available."""
    await harness.advance_to(T("10:23:05"))
    zone_002 = [
        entry.entity_id
        for entry in er.async_entries_for_config_entry(entity_registry, init_integration.entry_id)
        if entry.unique_id.endswith(tuple(f"_zone_002_{key}" for key in ZONE_002_KEYS))
    ]
    assert len(zone_002) == len(ZONE_002_KEYS)
    available = [entity_id for entity_id in zone_002 if is_available(hass, entity_id)]
    assert available == ["binary_sensor.zona_002_probe_connection"]
    assert get_state(hass, "binary_sensor.zona_002_probe_connection").state == STATE_OFF
    assert get_state(hass, "binary_sensor.zona_001_calling").state == STATE_OFF
    await harness.advance_to(T("10:24:05"))  # "not responding" is debounced now
    assert not is_available(hass, "binary_sensor.zona_002_alarm")
    assert "zone:002:not_responding" in {
        alarm.id for alarm in harness.client.state.alarms_debounced
    }
    # Back online: the library still holds the ended "not responding" for 60 s,
    # but it is not an alarm of the (live) zone.
    await harness.advance_to(T("10:25:05"))
    assert "zone:002:not_responding" in {
        alarm.id for alarm in harness.client.state.alarms_debounced
    }
    alarm = get_state(hass, "binary_sensor.zona_002_alarm")
    assert alarm.state == STATE_OFF
    assert alarm.attributes["alarms"] == []
    assert get_state(hass, "binary_sensor.zona_002_probe_connection").state == STATE_ON


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:25:00", termo_update("REHOM...STATO_SONDE", ZONE_002_OFFLINE)),
            ("10:26:00", termo_update("REHOM...WEBSERVER", "2")),
            ("10:27:00", termo_update("REHOM...WEBSERVER", "1")),
        )
    ],
)
async def test_demand_follows_the_unit_rules(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Demand ignores an offline zone's stale call and is unavailable with the bus down."""
    demand = "binary_sensor.rehom_plant_demand"
    await harness.advance_to(T("10:24:40"))  # zone 002 calls from 10:24:34.6
    assert get_state(hass, demand).state == STATE_ON
    await harness.advance_to(T("10:25:05"))  # zone 002 offline, its ATTIVA=1 is stale
    assert harness.client.state.zones["002"].calling is True
    assert harness.client.state.plant.demand is True  # the library's raw aggregate
    assert get_state(hass, demand).state == STATE_OFF
    await harness.advance_to(T("10:26:05"))  # serial line down: zone data is stale
    assert get_state(hass, demand).state == STATE_UNAVAILABLE
    assert is_available(hass, "binary_sensor.rehom_plant_setpoint_mismatch")
    await harness.advance_to(T("10:27:05"))
    assert get_state(hass, demand).state == STATE_OFF  # zone 002 still offline


@pytest.mark.parametrize(
    "device_patch", [_frames(("10:23:00", termo_update("REHOM...WEBSERVER", "2")))]
)
async def test_serial_line_down(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Serial line down: zone and VMC entities unavailable, hub and plant available."""
    await harness.advance_to(T("10:23:05"))
    for entity_id in (
        "binary_sensor.zona_001_calling",
        "binary_sensor.zona_001_probe_connection",
        "binary_sensor.zona_001_alarm",
        "binary_sensor.vmc_001_connectivity",
        "binary_sensor.vmc_001_alarm",
        DIRTY_FILTER,
    ):
        assert not is_available(hass, entity_id), entity_id
    for entity_id in (
        "binary_sensor.rehom_server_live_updates",
        "binary_sensor.rehom_server_bus",
        "binary_sensor.rehom_plant_setpoint_mismatch",
        "binary_sensor.rehom_plant_alarm",
    ):
        assert is_available(hass, entity_id), entity_id
    # Demand is made only of zone data: unavailable like the zones (rule 5)
    assert not is_available(hass, "binary_sensor.rehom_plant_demand")
    # the hub's serial-line alarm belongs to the plant device
    await harness.advance_to(T("10:24:05"))
    plant_alarm = get_state(hass, "binary_sensor.rehom_plant_alarm")
    assert plant_alarm.state == STATE_ON
    assert [item["alarm_id"] for item in plant_alarm.attributes["alarms"]] == ["hub:serial_line"]


async def test_live_updates(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Live updates: off while the WebSocket is down (REST fallback), on after reconnecting."""
    entity_id = "binary_sensor.rehom_server_live_updates"
    assert get_state(hass, entity_id).state == STATE_ON
    harness.block_ws()
    await harness.advance(5)
    assert get_state(hass, entity_id).state == STATE_OFF
    assert is_available(hass, "binary_sensor.zona_001_calling")  # DEGRADED keeps entities
    harness.unblock_ws()
    await harness.advance(40)
    assert get_state(hass, entity_id).state == STATE_ON


@pytest.mark.parametrize("device_patch", [_frames(("10:23:00", bus_update("CONFIGURA_ON", "1")))])
async def test_installer_session(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """An open installer session."""
    entity_id = "binary_sensor.rehom_server_installer_session"
    assert get_state(hass, entity_id).state == STATE_OFF
    assert get_state(hass, entity_id).attributes["last_activity"] is None
    await harness.advance_to(T("10:23:05"))
    assert get_state(hass, entity_id).state == STATE_ON


@pytest.mark.parametrize("device_patch", [{"patch": _remote_access}])
async def test_internet_access(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """The internet access sensor exists only with a remote-access token."""
    assert get_state(hass, "binary_sensor.rehom_server_internet_access").state == STATE_ON


async def test_no_internet_sensor_without_token(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The reference plant has no remote token: no internet access sensor."""
    assert hass.states.get("binary_sensor.rehom_server_internet_access") is None


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:23:00", termo_update("DEUM...ABILITA_F_COOLING", "1")),
            ("10:23:30", termo_update("DEUM.001..FREE_COOLING", "1")),
        )
    ],
)
async def test_read_only_free_cooling(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Free cooling shown read-only appears as a binary sensor (no switch)."""
    entity_id = "binary_sensor.vmc_001_free_cooling"
    assert hass.states.get(entity_id) is None
    await harness.advance_to(T("10:23:05"))
    assert get_state(hass, entity_id).state == STATE_OFF
    await harness.advance_to(T("10:23:35"))
    assert get_state(hass, entity_id).state == STATE_ON


@pytest.mark.parametrize(
    "device_patch", [_frames(("10:23:00", termo_update("REHOM...STATO_DEUM", "1,0,0")))]
)
async def test_vmc_offline(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """VMC 002 offline: connectivity off, everything else unavailable (rule 2)."""
    await harness.advance_to(T("10:23:05"))
    assert get_state(hass, "binary_sensor.vmc_002_connectivity").state == STATE_OFF
    assert not is_available(hass, "binary_sensor.vmc_002_alarm")
    assert not is_available(hass, "binary_sensor.vmc_002_dehumidifying")
    assert not is_available(hass, "binary_sensor.vmc_002_dirty_filter")
    assert get_state(hass, "binary_sensor.vmc_001_connectivity").state == STATE_ON


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:23:00", termo_update("REHOM...PRESENZA_DEUM", "1,0,0")),
            (
                "10:23:00",
                termo_update(
                    "REHOM...PRESENZA_SONDE", "1,1,1,0,0,0,0,0,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0"
                ),
            ),
        )
    ],
)
async def test_absent_units(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Absent units: every entity unavailable (exempt ones too); values read as unknown."""
    await harness.advance_to(T("10:23:05"))
    for entity_id in (
        "binary_sensor.vmc_002_connectivity",
        "binary_sensor.vmc_002_alarm",
        "binary_sensor.vmc_002_dirty_filter",
        "binary_sensor.zona_011_probe_connection",
        "binary_sensor.zona_011_alarm",
    ):
        assert get_state(hass, entity_id).state == STATE_UNAVAILABLE, entity_id
    vmc_sensor = get_entity(hass, "binary_sensor.vmc_002_dehumidifying")
    assert isinstance(vmc_sensor, RehomVmcBinarySensor)
    assert vmc_sensor.is_on is None
    assert vmc_sensor.extra_state_attributes is None
    flag = get_entity(hass, "binary_sensor.vmc_002_dirty_filter")
    assert isinstance(flag, RehomVmcAlarmFlagBinarySensor)
    assert flag.is_on is None
    alarm = get_entity(hass, "binary_sensor.vmc_002_alarm")
    assert isinstance(alarm, RehomAlarmBinarySensor)
    assert alarm.extra_state_attributes == {"alarms": [], "alarm_bitmask": None}
    zone_sensor = get_entity(hass, "binary_sensor.zona_011_setpoint_forced")
    assert isinstance(zone_sensor, RehomZoneBinarySensor)
    assert zone_sensor.is_on is None
    assert zone_sensor.extra_state_attributes is None
