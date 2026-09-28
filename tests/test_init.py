"""Setup, unload, devices and entry hooks."""

from __future__ import annotations

from functools import partial
import logging
from typing import Any

from aiorehom import (
    ConnectionState,
    DeviceKind,
    RehomAuthenticationError,
    RehomClient,
    RehomConnectionError,
    RehomResponseError,
)
from aiorehom.replay import ReplayData
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.rehom import api, async_remove_config_entry_device
from custom_components.rehom.const import (
    CONF_ENABLE_CONTROL,
    CONF_TEMPORARY_COMFORT_DURATION,
    DOMAIN,
    ISSUE_INSTALLER_SESSION_ACTIVE,
    ISSUE_SEASON_MISMATCH,
    ISSUE_SETPOINT_MISMATCH,
    ISSUE_UNSUPPORTED_API,
    ISSUE_UNSUPPORTED_API_SETUP,
    PLATFORMS,
)
from custom_components.rehom.coordinator import RehomCoordinator, RehomRuntimeData
from custom_components.rehom.entity import RehomEntity, device_identifier
from custom_components.rehom.issues import (
    RehomIssueTracker,
    async_create_unsupported_api_issue,
    issue_id,
)

from .conftest import CONTROL_OPTIONS, ENTRY_DATA
from .harness import FIXTURE_MAC, FIXTURE_VMCS, FIXTURE_ZONES, RehomHarness

ALL_ISSUES = (
    ISSUE_INSTALLER_SESSION_ACTIVE,
    ISSUE_SEASON_MISMATCH,
    ISSUE_SETPOINT_MISMATCH,
    ISSUE_UNSUPPORTED_API,
)


@pytest.fixture
def platforms() -> list[Platform]:
    """Core tests need no entity platform."""
    return []


def _drop_board(data: ReplayData) -> None:
    data.config.pop("BOARD", None)
    data.config.pop("PLATFORM", None)


def _drop_mac(data: ReplayData) -> None:
    data.interface[:] = [row for row in data.interface if row.get("Key") != "MacAddress"]


def _entry_issues(issue_registry: ir.IssueRegistry, entry_id: str) -> set[str]:
    return {
        key
        for (domain, issue), _item in issue_registry.issues.items()
        if domain == DOMAIN
        for key in ALL_ISSUES
        if issue == issue_id(key, entry_id)
    }


# -- setup ----------------------------------------------------------------------------


async def test_setup_entry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A healthy setup: LOADED, runtime data, one hub device with its fields."""
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED
    runtime = entry.runtime_data
    assert isinstance(runtime, RehomRuntimeData)
    assert runtime.client is harness.client
    assert harness.client_data == [ENTRY_DATA]
    # "Enable control" is off: the client is read-only
    assert harness.allow_writes == [False]
    assert runtime.client.allow_writes is False
    assert runtime.control_enabled is False
    assert runtime.client.connection_state is ConnectionState.CONNECTED
    assert isinstance(runtime.coordinator, RehomCoordinator)
    assert runtime.coordinator.hub_id == FIXTURE_MAC
    assert runtime.coordinator.data is runtime.client.state
    assert runtime.coordinator.last_update_success
    assert runtime.coordinator.update_interval is None  # push only
    assert isinstance(runtime.issues, RehomIssueTracker)

    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert len(devices) == 1
    hub = devices[0]
    assert hub.id == runtime.hub_device_id
    assert hub.identifiers == {(DOMAIN, FIXTURE_MAC)}
    assert hub.connections == {(dr.CONNECTION_NETWORK_MAC, FIXTURE_MAC)}
    assert hub.name == "Rehom server"  # from translation_key "hub"
    assert hub.manufacturer == "Rehom"
    assert hub.model == "Rehom Server"
    assert hub.sw_version == "3.16.3"
    assert hub.hw_version == "pi / arm32"
    assert hub.configuration_url == "http://rehomserver.local:8000/www/"
    assert hub.via_device_id is None
    assert hub.area_id is None


@pytest.mark.parametrize("entry_options", [CONTROL_OPTIONS])
async def test_setup_entry_control_enabled(
    hass: HomeAssistant,
    harness: RehomHarness,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ "Enable control" on: the client is built with writes, and that is logged once."""
    caplog.set_level(logging.INFO, logger="custom_components.rehom")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await harness.settle()
    runtime = mock_config_entry.runtime_data
    assert harness.allow_writes == [True]
    assert runtime.client.allow_writes is True
    assert runtime.control_enabled is True
    assert caplog.text.count("Control is enabled") == 1
    assert "post_bulk_update" not in harness.transport_calls  # setting up writes nothing
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "entry_options",
    [
        {},
        {CONF_TEMPORARY_COMFORT_DURATION: 2.0, CONF_ENABLE_CONTROL: False},
        {CONF_ENABLE_CONTROL: "true"},
        {CONF_ENABLE_CONTROL: 1},
    ],
    ids=["missing", "false", "string", "int"],
)
async def test_setup_entry_control_off(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only an exact True enables control; anything else builds a read-only client."""
    assert harness.allow_writes == [False]
    assert init_integration.runtime_data.control_enabled is False
    assert "Control is enabled" not in caplog.text


@pytest.mark.parametrize("platforms", [list(PLATFORMS)])
async def test_full_setup_inventory(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    platforms: list[Platform],
) -> None:
    """All platforms: 144 entities on 10 devices, 10 disabled by default."""
    entry = init_integration
    entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert len(entities) == 144
    assert len(devices) == 10
    disabled = [e for e in entities if e.disabled_by is er.RegistryEntryDisabler.INTEGRATION]
    assert len(disabled) == 10
    assert all(entity.unique_id.startswith(f"{FIXTURE_MAC}_") for entity in entities)
    # unique ids are unique per entity domain ("alarm" is a binary_sensor and an event)
    assert len({(entity.domain, entity.unique_id) for entity in entities}) == len(entities)

    per_device: dict[str, int] = {}
    for entity in entities:
        assert entity.device_id is not None
        per_device[entity.device_id] = per_device.get(entity.device_id, 0) + 1
    hub_id = entry.runtime_data.hub_device_id
    by_identifier = {
        next(iter(device.identifiers))[1].removeprefix(f"{FIXTURE_MAC}_"): device
        for device in devices
    }
    assert set(by_identifier) == {
        FIXTURE_MAC,
        "plant",
        *(f"zone_{zone}" for zone in FIXTURE_ZONES),
        *(f"vmc_{vmc}" for vmc in FIXTURE_VMCS),
    }
    counts = {name: per_device.get(device.id, 0) for name, device in by_identifier.items()}
    assert counts == {
        FIXTURE_MAC: 7,
        "plant": 14,
        **{f"zone_{zone}": 13 for zone in FIXTURE_ZONES if zone != "011"},
        "zone_011": 12,  # no humidity sensor
        **{f"vmc_{vmc}": 23 for vmc in FIXTURE_VMCS},
    }
    for name, device in by_identifier.items():
        assert device.via_device_id == (None if name == FIXTURE_MAC else hub_id), name
        assert device.area_id is None, name


@pytest.mark.parametrize("device_patch", [{"patch": _drop_board}])
async def test_setup_without_board(
    hass: HomeAssistant, init_integration: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    """No BOARD/PLATFORM in /config/: the hub has no hardware version."""
    hub = device_registry.async_get_device_by_identifier(
        (DOMAIN, FIXTURE_MAC), init_integration.entry_id
    )
    assert hub is not None
    assert hub.hw_version is None


@pytest.mark.parametrize("device_patch", [{"patch": _drop_mac}])
async def test_setup_without_mac(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """A controller that stops reporting its MAC keeps the entry's identity."""
    assert init_integration.state is ConfigEntryState.LOADED
    assert init_integration.runtime_data.coordinator.hub_id == FIXTURE_MAC


async def test_unload_entry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Unloading closes every client and deletes the state-derived issues."""
    entry = init_integration
    await harness.advance(6 * 60)  # setpoint_mismatch matures after 5 min
    assert _entry_issues(issue_registry, entry.entry_id) == {ISSUE_SETPOINT_MISMATCH}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert harness.clients
    assert all(client.connection_state is ConnectionState.CLOSED for client in harness.clients)
    assert _entry_issues(issue_registry, entry.entry_id) == set()


async def test_stop_closes_client(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home Assistant stopping closes the client; a later unload stays clean."""
    entry = init_integration
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert harness.client.connection_state is ConnectionState.CLOSED
    # CLOSED is not an outage: the coordinator does not flip to unavailable.
    assert entry.runtime_data.coordinator.last_update_success

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "Unable to remove unknown job listener" not in caplog.text


async def test_setup_cannot_connect(
    hass: HomeAssistant, harness: RehomHarness, mock_config_entry: MockConfigEntry
) -> None:
    """A connection error at connect() -> SETUP_RETRY, client closed."""
    harness.errors["get_alive"] = RehomConnectionError("replay: unreachable")
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert harness.client.connection_state is ConnectionState.CLOSED
    assert "Cannot connect to the Rehom server at rehomserver.local" in (
        mock_config_entry.reason or ""
    )


async def test_setup_auth_failed(
    hass: HomeAssistant, harness: RehomHarness, mock_config_entry: MockConfigEntry
) -> None:
    """A refused login -> SETUP_ERROR and a reauth flow."""
    harness.errors["login"] = RehomAuthenticationError(401)
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert harness.client.connection_state is ConnectionState.CLOSED
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert flows[0]["context"]["entry_id"] == mock_config_entry.entry_id
    assert flows[0]["step_id"] == "reauth_confirm"


async def test_setup_unsupported_api(
    hass: HomeAssistant,
    harness: RehomHarness,
    mock_config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """An unparseable snapshot -> SETUP_ERROR + unsupported_api; a good setup deletes it."""
    harness.errors["get_interface"] = RehomResponseError(200, "bad")
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert harness.client.connection_state is ConnectionState.CLOSED
    issue = issue_registry.async_get_issue(
        DOMAIN, issue_id(ISSUE_UNSUPPORTED_API, mock_config_entry.entry_id)
    )
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert not issue.is_fixable
    # the setup text: no state, no automatic retry, no version (not the runtime text)
    assert issue.translation_key == ISSUE_UNSUPPORTED_API_SETUP
    assert not issue.translation_placeholders

    harness.errors.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("custom_components.rehom.PLATFORMS", [])
        assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert mock_config_entry.state is ConfigEntryState.LOADED
        assert ISSUE_UNSUPPORTED_API not in _entry_issues(
            issue_registry, mock_config_entry.entry_id
        )
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_wrong_device(hass: HomeAssistant, harness: RehomHarness) -> None:
    """The address now answers with another controller -> SETUP_RETRY (wrong_device)."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Rehom", unique_id="02:00:00:00:00:01", data=dict(ENTRY_DATA)
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.reason == "The configured address answers with a different Rehom server"
    assert harness.client.connection_state is ConnectionState.CLOSED


# -- devices ----------------------------------------------------------------------------


async def test_unit_device_info(
    hass: HomeAssistant, init_integration: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    """Plant, zone and VMC devices from RehomEntity.device_info."""
    entry = init_integration
    coordinator = entry.runtime_data.coordinator
    hub_device_id = entry.runtime_data.hub_device_id

    def register(kind: DeviceKind, unit: str | None) -> dr.DeviceEntry:
        info = RehomEntity(coordinator, kind, unit, "probe").device_info
        assert info is not None
        return device_registry.async_get_or_create(config_entry_id=entry.entry_id, **info)

    plant = register(DeviceKind.PLANT, None)
    assert plant.identifiers == {(DOMAIN, f"{FIXTURE_MAC}_plant")}
    assert plant.name == "Rehom plant"
    assert plant.manufacturer == "Rehom"
    assert plant.model == "Multizona"
    assert plant.sw_version == "4.04 R"
    assert plant.via_device_id == hub_device_id

    zone = register(DeviceKind.ZONE, "001")
    assert zone.identifiers == {(DOMAIN, f"{FIXTURE_MAC}_zone_001")}
    assert zone.name == "Zona 001"
    assert zone.model == "Zone probe"
    assert zone.sw_version == "80000154"
    assert zone.serial_number == "713381"
    assert zone.via_device_id == hub_device_id
    assert zone.area_id is None  # no suggested_area

    vmc = register(DeviceKind.VMC, "002")
    assert vmc.identifiers == {(DOMAIN, f"{FIXTURE_MAC}_vmc_002")}
    assert vmc.name == "VMC 002"
    assert vmc.model == "VMC"
    assert vmc.sw_version == "18000000"
    assert vmc.serial_number == "293782"
    assert vmc.via_device_id == hub_device_id

    # Units absent from the state still get a stable device.
    absent_zone = register(DeviceKind.ZONE, "004")
    assert absent_zone.name == "Zona 004"
    assert absent_zone.sw_version is None
    assert absent_zone.serial_number is None
    absent_vmc = register(DeviceKind.VMC, "009")
    assert absent_vmc.name == "VMC 009"
    assert absent_vmc.serial_number is None

    # The hub's DeviceInfo only links by identifier (registered in setup).
    hub_info = RehomEntity(coordinator, DeviceKind.HUB, None, "probe").device_info
    assert hub_info == {"identifiers": {(DOMAIN, FIXTURE_MAC)}}


@pytest.mark.parametrize(
    ("kind", "unit"),
    [
        (DeviceKind.ACTUATOR, "001"),
        (DeviceKind.ZONE, None),
        (DeviceKind.PLANT, "001"),
    ],
)
async def test_entity_rejects_bad_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, kind: DeviceKind, unit: str | None
) -> None:
    """Only hub, plant, zone and VMC entities exist; units need a unit id."""
    with pytest.raises(ValueError):  # noqa: PT011
        RehomEntity(init_integration.runtime_data.coordinator, kind, unit, "probe")


async def test_remove_config_entry_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    """Only devices of absent units can be removed by hand."""
    entry = init_integration

    def device(identifier: tuple[str, str]) -> dr.DeviceEntry:
        return device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={identifier}
        )

    hub = device_registry.async_get_device_by_identifier((DOMAIN, FIXTURE_MAC), entry.entry_id)
    assert hub is not None
    assert not await async_remove_config_entry_device(hass, entry, hub)
    for kind, unit in (
        (DeviceKind.PLANT, None),
        *((DeviceKind.ZONE, zone) for zone in FIXTURE_ZONES),
        *((DeviceKind.VMC, vmc) for vmc in FIXTURE_VMCS),
    ):
        present = device(device_identifier(FIXTURE_MAC, kind, unit))
        assert not await async_remove_config_entry_device(hass, entry, present)

    absent_zone = device(device_identifier(FIXTURE_MAC, DeviceKind.ZONE, "004"))
    assert await async_remove_config_entry_device(hass, entry, absent_zone)
    absent_vmc = device(device_identifier(FIXTURE_MAC, DeviceKind.VMC, "003"))
    assert await async_remove_config_entry_device(hass, entry, absent_vmc)


async def test_remove_device_while_not_loaded(
    hass: HomeAssistant,
    harness: RehomHarness,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """In SETUP_RETRY the present units are unknown: removal is refused, not an error."""
    assert await async_setup_component(hass, "config", {})
    harness.errors["get_alive"] = RehomConnectionError("replay: unreachable")
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_config_entry.supports_remove_device
    absent_zone = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={device_identifier(FIXTURE_MAC, DeviceKind.ZONE, "004")},
    )
    assert not await async_remove_config_entry_device(hass, mock_config_entry, absent_zone)

    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": absent_zone.id}
    )
    response = await client.receive_json()
    assert not response["success"]
    assert response["error"]["code"] == "home_assistant_error"  # a refusal, not unknown_error
    assert "rejected by integration" in response["error"]["message"]
    assert device_registry.async_get(absent_zone.id) is not None


async def test_remove_entry_deletes_issues(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Removing the entry deletes every issue, unsupported_api included."""
    entry = init_integration
    await harness.advance(6 * 60)
    async_create_unsupported_api_issue(hass, entry.entry_id, "3.16.3")
    assert _entry_issues(issue_registry, entry.entry_id) == {
        ISSUE_SETPOINT_MISMATCH,
        ISSUE_UNSUPPORTED_API,
    }
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _entry_issues(issue_registry, entry.entry_id) == set()
    assert all(client.connection_state is ConnectionState.CLOSED for client in harness.clients)


async def test_remove_unloaded_entry_deletes_setup_issue(
    hass: HomeAssistant,
    harness: RehomHarness,
    mock_config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The setup-time unsupported_api issue goes away with the entry."""
    harness.errors["get_interface"] = RehomResponseError(200, "bad")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _entry_issues(issue_registry, mock_config_entry.entry_id) == {ISSUE_UNSUPPORTED_API}
    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _entry_issues(issue_registry, mock_config_entry.entry_id) == set()


async def test_create_client(hass: HomeAssistant) -> None:
    """The production factory builds an unconnected client (nothing is contacted)."""
    client = api.create_client(hass, {**ENTRY_DATA, CONF_PORT: "8000"})
    assert isinstance(client, RehomClient)
    assert client.host == "rehomserver.local"
    assert client.allow_writes is False  # read-only unless asked
    assert client.connection_state is ConnectionState.DISCONNECTED
    # inject-websession: REST uses HA's shared session, and the WebSocket's private
    # session borrows its connector, i.e. HA's (mDNS-capable) resolver for *.local.
    ha_session = async_get_clientsession(hass)
    assert client._transport._session is ha_session  # type: ignore[attr-defined]
    ws_connector = client._ws_connector
    assert isinstance(ws_connector, partial)
    assert ws_connector.keywords["connector"] is ha_session.connector
    await client.close()
    assert client.connection_state is ConnectionState.CLOSED
    assert not ha_session.closed

    with pytest.raises(ValueError):  # noqa: PT011
        api.create_client(hass, {**ENTRY_DATA, CONF_HOST: "bad host!"})
    with pytest.raises(ValueError):  # noqa: PT011
        api.create_client(hass, {**ENTRY_DATA, CONF_PORT: 70000})


async def test_create_client_allow_writes(hass: HomeAssistant) -> None:
    """``allow_writes=True`` builds a writable client; anything but a bool is refused."""
    client = api.create_client(hass, ENTRY_DATA, allow_writes=True)
    assert client.allow_writes is True
    assert client.connection_state is ConnectionState.DISCONNECTED
    await client.close()
    client = api.create_client(hass, ENTRY_DATA, allow_writes=False)
    assert client.allow_writes is False
    await client.close()
    with pytest.raises(TypeError):
        api.create_client(hass, ENTRY_DATA, allow_writes="true")  # type: ignore[arg-type]


async def test_services_registered_without_entries(hass: HomeAssistant) -> None:
    """The actions exist as soon as the integration is set up (action-setup)."""
    assert await async_setup_component(hass, DOMAIN, {})
    services: dict[str, Any] = hass.services.async_services_for_domain(DOMAIN)
    assert set(services) == {"get_schedule", "set_temporary_comfort", "clear_temporary_comfort"}
