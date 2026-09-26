"""Push coordinator, availability rules and the entity base.

Entities here are bare ``RehomEntity`` instances bound to the loaded entry's
coordinator (no platform is needed to evaluate ``available``).
"""

from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any, ClassVar

from aiorehom import (
    AlarmSource,
    ConnectionState,
    DeviceKind,
    RehomConnectionError,
    RehomState,
    StateUpdate,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rehom.coordinator import RehomCoordinator
from custom_components.rehom.entity import (
    EntityFactory,
    RehomEntity,
    alarm_attributes,
    alarm_device,
    async_setup_dynamic_entities,
    device_alarms,
    device_identifier,
    entity_unique_id,
)

from .conftest import ENTRY_DATA
from .harness import FIXTURE_MAC, RehomHarness, T, termo_update

UNAVAILABLE_LOG = "The Rehom controller is unavailable"
AVAILABLE_LOG = "The Rehom controller is available again"

ZONE_002_OFFLINE = "1,0,1,0,0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"
AVAILABILITY_FRAMES = {
    "extra_frames": [
        (T("10:23:00"), termo_update("REHOM...STATO_SONDE", ZONE_002_OFFLINE)),
        (T("10:25:00"), termo_update("REHOM...STATO_SONDE", "1,1,1")),  # short: 009+ unknown
        (T("10:26:00"), termo_update("REHOM...WEBSERVER", "2")),  # serial line down
        (T("10:27:00"), termo_update("REHOM...WEBSERVER", "1")),
    ]
}


@pytest.fixture
def platforms() -> list[Platform]:
    """No entity platform: the entity base is exercised directly."""
    return []


class _ExemptEntity(RehomEntity):
    """Like a unit's connectivity sensor, the only offline-exempt entity (rule 2)."""

    _offline_exempt: ClassVar[bool] = True


def _coordinator(entry: MockConfigEntry) -> RehomCoordinator:
    coordinator: RehomCoordinator = entry.runtime_data.coordinator
    return coordinator


def _count(caplog: pytest.LogCaptureFixture, message: str) -> int:
    return sum(1 for record in caplog.records if record.getMessage() == message)


# -- coordinator -----------------------------------------------------------------------


async def test_push_updates(
    hass: HomeAssistant, init_integration: MockConfigEntry, harness: RehomHarness
) -> None:
    """Library updates reach the coordinator without polling."""
    coordinator = _coordinator(init_integration)
    assert coordinator.data.zones["002"].calling is False
    notified: list[RehomState] = []
    unsub = coordinator.async_add_listener(lambda: notified.append(coordinator.data))

    await harness.advance_to(T("10:24:40"))  # zone 002 starts calling at 10:24:34.6
    assert coordinator.data is harness.client.state
    assert coordinator.data.zones["002"].calling is True
    assert coordinator.data.plant.demand is True
    assert notified
    assert coordinator.last_update_success
    assert isinstance(coordinator.last_update, StateUpdate)
    assert coordinator.last_update.state is coordinator.data
    unsub()

    # homeassistant.update_entity / a manual refresh never does I/O.
    calls = harness.transport_calls
    assert await coordinator._async_update_data() is harness.client.state
    assert harness.transport_calls == calls


async def test_unavailable_and_recovery(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Three failed /alive/ polls -> unavailable (logged once); healing -> available (once)."""
    caplog.set_level(logging.INFO, logger="custom_components.rehom")
    coordinator = _coordinator(init_integration)
    plant_entity = RehomEntity(coordinator, DeviceKind.PLANT, None, "probe")
    zone_entity = _ExemptEntity(coordinator, DeviceKind.ZONE, "002", "probe_connection")
    assert plant_entity.available
    assert zone_entity.available

    harness.errors["get_alive"] = RehomConnectionError("replay: /alive/ down")
    await harness.advance(150)  # polls at +30/+60/+90 s fail
    assert harness.client.connection_state is ConnectionState.UNAVAILABLE
    assert not coordinator.last_update_success
    assert not plant_entity.available
    assert not zone_entity.available  # rule 1 beats the offline exemption
    assert _count(caplog, UNAVAILABLE_LOG) == 1

    # Frames keep arriving on the WebSocket (10:24:34.6: zone 002 calls):
    # the data follows, the entities stay unavailable.
    assert coordinator.data.zones["002"].calling is True
    assert coordinator.data is harness.client.state
    await harness.advance(60)
    assert not coordinator.last_update_success
    assert _count(caplog, UNAVAILABLE_LOG) == 1

    harness.errors.clear()
    await harness.advance(60)
    assert harness.client.connection_state is ConnectionState.CONNECTED
    assert coordinator.last_update_success
    assert plant_entity.available
    assert zone_entity.available
    assert _count(caplog, AVAILABLE_LOG) == 1
    assert _count(caplog, UNAVAILABLE_LOG) == 1


@pytest.mark.parametrize("platforms", [[Platform.SENSOR]])
async def test_manual_refresh_while_unavailable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """homeassistant.update_entity never makes stale data look live (rule 1)."""
    caplog.set_level(logging.DEBUG, logger="custom_components.rehom")
    assert await async_setup_component(hass, "homeassistant", {})
    coordinator = _coordinator(init_integration)
    entity_id = "sensor.zona_001_temperature"
    harness.errors["get_alive"] = RehomConnectionError("replay: /alive/ down")
    await harness.advance(150)
    assert harness.client.connection_state is ConnectionState.UNAVAILABLE
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE  # type: ignore[union-attr]
    calls = harness.transport_calls

    await hass.services.async_call(
        "homeassistant", "update_entity", {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await harness.advance(15)
    assert harness.client.connection_state is ConnectionState.UNAVAILABLE
    assert not coordinator.last_update_success
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE  # type: ignore[union-attr]
    assert "recovered" not in caplog.text
    assert _count(caplog, UNAVAILABLE_LOG) == 1
    assert harness.transport_calls == calls  # a manual refresh never does I/O
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.translation_key == "unavailable"

    harness.errors.clear()
    await harness.advance(60)
    assert coordinator.last_update_success
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE  # type: ignore[union-attr]


async def test_degraded_stays_available(
    hass: HomeAssistant, init_integration: MockConfigEntry, harness: RehomHarness
) -> None:
    """WebSocket down, REST fallback working: DEGRADED is still available."""
    coordinator = _coordinator(init_integration)
    connections: list[ConnectionState] = []
    unsub = harness.client.on_connection_change(connections.append)

    harness.block_ws()
    await harness.advance(5)
    assert harness.client.connection_state is ConnectionState.DEGRADED
    assert coordinator.last_update_success
    assert harness.client.available

    await harness.advance(60)  # first REST fallback after 60 s
    assert harness.client.stats.fallbacks >= 1
    assert coordinator.last_update_success

    harness.unblock_ws()
    await harness.advance(60)  # reconnect after the current backoff (<= 37.5 s)
    assert harness.client.connection_state is ConnectionState.CONNECTED
    assert coordinator.last_update_success
    assert connections[0] is ConnectionState.DEGRADED
    assert connections[-1] is ConnectionState.CONNECTED
    unsub()


async def test_update_data_before_first_sync(
    hass: HomeAssistant, harness: RehomHarness, mock_config_entry: MockConfigEntry
) -> None:
    """Without a complete sync the first refresh fails with not_ready."""
    mock_config_entry.add_to_hass(hass)
    client = harness.create_client(hass, ENTRY_DATA)
    coordinator = RehomCoordinator(hass, mock_config_entry, client)  # type: ignore[arg-type]
    assert coordinator.hub_id == FIXTURE_MAC
    assert coordinator.last_update is None
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.translation_key == "not_ready"
    await client.close()


async def test_connection_transitions_without_data(
    hass: HomeAssistant,
    harness: RehomHarness,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-available transitions other than UNAVAILABLE are ignored; the outage logs once."""
    caplog.set_level(logging.INFO, logger="custom_components.rehom")
    mock_config_entry.add_to_hass(hass)
    client = harness.create_client(hass, ENTRY_DATA)  # never connected: not available
    coordinator = RehomCoordinator(hass, mock_config_entry, client)  # type: ignore[arg-type]
    notified: list[bool] = []
    unsub = coordinator.async_add_listener(lambda: notified.append(True))

    coordinator._async_handle_connection(ConnectionState.CONNECTING)
    assert coordinator.last_update_success  # untouched
    assert notified == []

    coordinator._async_handle_connection(ConnectionState.UNAVAILABLE)
    coordinator._async_handle_connection(ConnectionState.UNAVAILABLE)
    assert not coordinator.last_update_success
    assert len(notified) == 2
    assert _count(caplog, UNAVAILABLE_LOG) == 1
    unsub()
    await client.close()


# -- availability rules (custom_components/rehom/entity.py) --------------------------------


@pytest.mark.parametrize("device_patch", [AVAILABILITY_FRAMES])
async def test_unit_availability(
    hass: HomeAssistant, init_integration: MockConfigEntry, harness: RehomHarness
) -> None:
    """Rules 2, 3 and 5, absent units, and the offline exemption."""
    coordinator = _coordinator(init_integration)
    hub = RehomEntity(coordinator, DeviceKind.HUB, None, "probe")
    plant = RehomEntity(coordinator, DeviceKind.PLANT, None, "probe")
    zone_002 = RehomEntity(coordinator, DeviceKind.ZONE, "002", "probe")
    zone_002_probe = _ExemptEntity(coordinator, DeviceKind.ZONE, "002", "probe_connection")
    zone_009 = RehomEntity(coordinator, DeviceKind.ZONE, "009", "probe")
    vmc_001 = RehomEntity(coordinator, DeviceKind.VMC, "001", "probe")
    absent_zone = RehomEntity(coordinator, DeviceKind.ZONE, "004", "probe")
    absent_vmc = RehomEntity(coordinator, DeviceKind.VMC, "003", "probe")
    everything = (hub, plant, zone_002, zone_002_probe, zone_009, vmc_001)

    assert all(entity.available for entity in everything)
    assert not absent_zone.available
    assert not absent_vmc.available
    assert absent_zone.zone is None
    assert absent_vmc.vmc is None
    assert plant.zone is None  # not a zone entity
    assert plant.vmc is None

    # Rule 2: zone 002 offline -> unavailable, except offline-exempt entities.
    await harness.advance_to(T("10:23:01"))
    assert zone_002.zone is not None
    assert zone_002.zone.online is False
    assert not zone_002.available
    assert zone_002_probe.available
    assert zone_009.available
    assert hub.available
    assert plant.available

    # Rule 3: a short STATO vector -> online unknown -> available.
    await harness.advance_to(T("10:25:01"))
    assert zone_009.zone is not None
    assert zone_009.zone.online is None
    assert zone_009.available
    assert zone_002.available

    # Rule 5: serial line down -> every zone/VMC entity unavailable, hub/plant not.
    await harness.advance_to(T("10:26:01"))
    assert coordinator.data.plant.serial_down is True
    assert not zone_002.available
    assert not zone_002_probe.available
    assert not zone_009.available
    assert not vmc_001.available
    assert hub.available
    assert plant.available

    await harness.advance_to(T("10:27:01"))
    assert all(entity.available for entity in everything)


# -- ids and alarm helpers ------------------------------------------------------------------


def test_ids() -> None:
    """Identifiers and unique ids never contain names, serials or hosts."""
    assert device_identifier(FIXTURE_MAC, DeviceKind.HUB) == ("rehom", FIXTURE_MAC)
    assert device_identifier(FIXTURE_MAC, DeviceKind.HUB, "001") == ("rehom", FIXTURE_MAC)
    assert device_identifier(FIXTURE_MAC, DeviceKind.PLANT) == ("rehom", f"{FIXTURE_MAC}_plant")
    assert device_identifier(FIXTURE_MAC, DeviceKind.ZONE, "001") == (
        "rehom",
        f"{FIXTURE_MAC}_zone_001",
    )
    assert entity_unique_id(FIXTURE_MAC, DeviceKind.VMC, "002", "fan") == (
        f"{FIXTURE_MAC}_vmc_002_fan"
    )
    assert entity_unique_id(FIXTURE_MAC, DeviceKind.HUB, None, "bus") == f"{FIXTURE_MAC}_hub_bus"


async def test_entity_unique_id_and_accessors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The base entity derives its unique id and model accessors from kind/unit/key."""
    coordinator = _coordinator(init_integration)
    entity = RehomEntity(coordinator, DeviceKind.ZONE, "001", "temperature")
    assert entity.unique_id == f"{FIXTURE_MAC}_zone_001_temperature"
    assert entity.has_entity_name
    assert entity.rehom_state is coordinator.data
    assert entity.plant is coordinator.data.plant
    assert entity.zone is coordinator.data.zones["001"]
    assert entity.vmc is None
    vmc_entity = RehomEntity(coordinator, DeviceKind.VMC, "001", "fan")
    assert vmc_entity.vmc is coordinator.data.vmcs["001"]
    assert vmc_entity.zone is None


ALARM_FRAMES = {
    "extra_frames": [
        *AVAILABILITY_FRAMES["extra_frames"],
        (T("10:23:00"), termo_update("DEUM.001..ALLARM_PRESSOSTATO_FILTRO", "1")),
    ]
}


@pytest.mark.parametrize("device_patch", [ALARM_FRAMES])
async def test_alarm_helpers(
    hass: HomeAssistant, init_integration: MockConfigEntry, harness: RehomHarness
) -> None:
    """Unit alarms belong to their unit; hub/plant alarms to the plant; 60-s debounce."""
    coordinator = _coordinator(init_integration)
    assert device_alarms(coordinator.data, DeviceKind.VMC, "001") == ()

    await harness.advance_to(T("10:23:30"))  # zone 002 offline and the VMC flag for 30 s
    state = coordinator.data
    assert device_alarms(state, DeviceKind.VMC, "001") == ()  # not debounced yet
    raw = device_alarms(state, DeviceKind.VMC, "001", debounced=False)
    assert [alarm.id for alarm in raw] == ["vmc:001:ALLARM_PRESSOSTATO_FILTRO"]
    assert alarm_device(raw[0]) == (DeviceKind.VMC, "001")
    # A unit's own "not responding" is not one of its device alarms (rule 2): the
    # connectivity sensor shows it, and the plant's "Active alarms" counts it.
    not_responding = [alarm for alarm in state.alarms if alarm.id == "zone:002:not_responding"]
    assert not_responding
    assert not_responding[0].source is AlarmSource.UNIT_NOT_RESPONDING
    assert alarm_device(not_responding[0]) == (DeviceKind.ZONE, "002")
    assert device_alarms(state, DeviceKind.ZONE, "002", debounced=False) == ()

    await harness.advance_to(T("10:24:05"))
    alarms = device_alarms(coordinator.data, DeviceKind.VMC, "001")
    assert [alarm.id for alarm in alarms] == [alarm.id for alarm in raw]
    attributes = alarm_attributes(alarms[0])
    assert set(attributes) == {"alarm_id", "source", "unit", "code", "text", "first_seen"}
    assert attributes["alarm_id"] == alarms[0].id
    assert attributes["source"] == "vmc_flag"
    assert attributes["unit"] == "001"
    assert attributes["first_seen"] == alarms[0].first_seen.isoformat()
    assert "zone:002:not_responding" in {a.id for a in coordinator.data.alarms_debounced}
    assert device_alarms(coordinator.data, DeviceKind.ZONE, "002") == ()
    assert device_alarms(coordinator.data, DeviceKind.ZONE, "001") == ()

    # Serial line down (10:26:00-10:27:00): a hub-level alarm, shown on the plant device.
    await harness.advance_to(T("10:26:50"))
    assert device_alarms(coordinator.data, DeviceKind.PLANT, None) == ()  # < 60 s
    plant_alarms = device_alarms(coordinator.data, DeviceKind.PLANT, None, debounced=False)
    assert [alarm.id for alarm in plant_alarms] == ["hub:serial_line"]
    assert all(alarm_device(alarm) == (DeviceKind.PLANT, None) for alarm in plant_alarms)
    assert all(alarm.device is not DeviceKind.ZONE for alarm in plant_alarms)


# -- dynamic entities ---------------------------------------------------------------------------


async def test_dynamic_entities(
    hass: HomeAssistant, init_integration: MockConfigEntry, harness: RehomHarness
) -> None:
    """Entities are added once, when ``build`` first yields their key."""
    entry = init_integration
    coordinator = _coordinator(entry)
    added: list[list[Entity]] = []

    def add_entities(entities: Iterable[Entity], *args: Any, **kwargs: Any) -> None:
        added.append(list(entities))

    def build(state: RehomState) -> Iterable[tuple[str, EntityFactory]]:
        yield "plant_probe", lambda: RehomEntity(coordinator, DeviceKind.PLANT, None, "probe")
        if state.plant.demand:
            yield "plant_demand", lambda: RehomEntity(coordinator, DeviceKind.PLANT, None, "x")

    async_setup_dynamic_entities(entry, add_entities, build)
    assert [len(batch) for batch in added] == [1]

    await harness.advance_to(T("10:24:40"))  # demand on
    assert [len(batch) for batch in added] == [1, 1]
    assert added[1][0].unique_id == f"{FIXTURE_MAC}_plant_x"

    await harness.advance_to(T("10:25:40"))  # more updates: nothing new
    assert [len(batch) for batch in added] == [1, 1]
