"""Config and options flows (100 % of config_flow.py).

Only documentation addresses (192.0.2.0/24) and the pseudonymised fixture MAC
are used.  Every probe runs a real ``RehomClient`` on the replayed fixture.
"""

from __future__ import annotations

from collections.abc import Generator
from ipaddress import ip_address
from typing import Any
from unittest.mock import AsyncMock, patch

from aiorehom import (
    ConnectionState,
    RehomAuthenticationError,
    RehomConnectionError,
    RehomResponseError,
)
from aiorehom.replay import ReplayData
from homeassistant.config_entries import (
    SOURCE_DHCP,
    SOURCE_IGNORE,
    SOURCE_USER,
    SOURCE_ZEROCONF,
    ConfigEntryState,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator
import voluptuous as vol

from custom_components.rehom.const import CONF_TEMPORARY_COMFORT_DURATION, DOMAIN

from .conftest import ENTRY_DATA
from .harness import FIXTURE_MAC, RehomHarness

DISCOVERED_IP = "192.0.2.10"
OTHER_IP = "192.0.2.99"
OTHER_MAC = "02:00:00:00:00:01"
CREDENTIALS = {CONF_USERNAME: "user", CONF_PASSWORD: "test-password"}

ZEROCONF_INFO = ZeroconfServiceInfo(
    ip_address=ip_address(DISCOVERED_IP),
    ip_addresses=[ip_address(DISCOVERED_IP)],
    hostname="rehomserver.local.",
    name="rehom._http._tcp.local.",
    port=8000,
    type="_http._tcp.local.",
    properties={},
)
DHCP_INFO = DhcpServiceInfo(ip=DISCOVERED_IP, hostname="rehomserver", macaddress="020000317a5a")


@pytest.fixture
def platforms() -> list[Platform]:
    """Reloads during these tests set up the entry without entity platforms."""
    return []


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Entries created by a flow are not set up for real."""
    with patch("custom_components.rehom.async_setup_entry", return_value=True) as mock:
        yield mock


def _drop_mac(data: ReplayData) -> None:
    data.interface[:] = [row for row in data.interface if row.get("Key") != "MacAddress"]


def _mac_row() -> dict[str, Any]:
    return {
        "Gruppo": "WEBSERVER",
        "Unita": "",
        "SubUni": "",
        "Key": "MacAddress",
        "Valore": FIXTURE_MAC,
        "path": "WEBSERVER...MacAddress",
    }


def _defaults(result: Any) -> dict[str, Any]:
    """Form field -> default value (``None`` when the field has no default)."""
    schema: vol.Schema = result["data_schema"]
    return {
        str(key): (None if key.default is vol.UNDEFINED else key.default()) for key in schema.schema
    }


def _assert_all_closed(harness: RehomHarness) -> None:
    assert harness.clients
    assert all(client.connection_state is ConnectionState.CLOSED for client in harness.clients)


def _add_entry(
    hass: HomeAssistant, *, unique_id: str = FIXTURE_MAC, **data: Any
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Rehom", unique_id=unique_id, data={**ENTRY_DATA, **data}
    )
    entry.add_to_hass(hass)
    return entry


# -- user ------------------------------------------------------------------------------


async def test_user_flow(
    hass: HomeAssistant, harness: RehomHarness, mock_setup_entry: AsyncMock
) -> None:
    """The user step creates an entry keyed by the controller MAC."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    assert _defaults(result) == {
        CONF_HOST: "rehomserver.local",
        CONF_PORT: 8000,
        CONF_USERNAME: None,
        CONF_PASSWORD: None,
    }

    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(ENTRY_DATA))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Rehom"
    assert result["data"] == ENTRY_DATA
    assert isinstance(result["data"][CONF_PORT], int)
    assert result["result"].unique_id == FIXTURE_MAC
    assert result["result"].options == {}
    _assert_all_closed(harness)
    assert harness.client_data[-1] == ENTRY_DATA
    await hass.async_block_till_done()
    assert len(mock_setup_entry.mock_calls) == 1


@pytest.mark.parametrize(
    ("method", "error", "user_input", "errors"),
    [
        ("login", RehomAuthenticationError(401), {}, {"base": "invalid_auth"}),
        ("get_alive", RehomConnectionError("down"), {}, {"base": "cannot_connect"}),
        ("get_interface", RuntimeError("boom"), {}, {"base": "unknown"}),
        (None, None, {CONF_HOST: "bad host!"}, {CONF_HOST: "invalid_host"}),
    ],
)
async def test_user_flow_errors(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    mock_setup_entry: AsyncMock,
    method: str | None,
    error: BaseException | None,
    user_input: dict[str, Any],
    errors: dict[str, str],
) -> None:
    """Each validation error re-shows the form with the input; then the user recovers."""
    if method is not None and error is not None:
        harness.errors[method] = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    bad_input = {**ENTRY_DATA, CONF_PORT: 8001, **user_input}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], bad_input)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == errors
    # The input is kept (the password is never echoed).
    assert _defaults(result) == {
        CONF_HOST: bad_input[CONF_HOST],
        CONF_PORT: 8001,
        CONF_USERNAME: "user",
        CONF_PASSWORD: None,
    }
    # (an invalid host fails in the RehomClient constructor: no client exists)
    assert all(client.connection_state is ConnectionState.CLOSED for client in harness.clients)

    harness.errors.clear()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(ENTRY_DATA))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == FIXTURE_MAC
    _assert_all_closed(harness)


@pytest.mark.parametrize("device_patch", [{"patch": _drop_mac}])
async def test_user_flow_missing_mac(
    hass: HomeAssistant, harness: RehomHarness, mock_setup_entry: AsyncMock
) -> None:
    """A controller without WEBSERVER.MacAddress cannot be identified."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(ENTRY_DATA))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "missing_mac"}
    _assert_all_closed(harness)

    harness.device.data.interface.append(_mac_row())  # the controller reports it again
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(ENTRY_DATA))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == FIXTURE_MAC


async def test_user_flow_already_configured(
    hass: HomeAssistant, harness: RehomHarness, mock_setup_entry: AsyncMock
) -> None:
    """The same controller at a new address updates the existing entry."""
    entry = _add_entry(hass, host=OTHER_IP, port=8001)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(ENTRY_DATA))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "rehomserver.local"
    assert entry.data[CONF_PORT] == 8000
    assert entry.data[CONF_PASSWORD] == "test-password"
    _assert_all_closed(harness)


# -- zeroconf --------------------------------------------------------------------------


async def test_zeroconf_flow(
    hass: HomeAssistant, harness: RehomHarness, mock_setup_entry: AsyncMock
) -> None:
    """Zeroconf asks for credentials and creates an entry at the discovered IP."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"] == {"host": DISCOVERED_IP}
    assert _defaults(result) == {CONF_USERNAME: None, CONF_PASSWORD: None}
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows[0]["context"]["title_placeholders"] == {"name": f"Rehom ({DISCOVERED_IP})"}
    # provisional unique id (the MAC needs a login): the discovery can be ignored
    assert flows[0]["context"]["unique_id"] == "rehomserver.local"
    assert harness.clients == []  # nothing is contacted before the credentials

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Rehom"
    assert result["data"] == {CONF_HOST: DISCOVERED_IP, CONF_PORT: 8000, **CREDENTIALS}
    assert result["result"].unique_id == FIXTURE_MAC
    _assert_all_closed(harness)


async def test_zeroconf_discovery_can_be_ignored(
    hass: HomeAssistant, harness: RehomHarness, hass_ws_client: WebSocketGenerator
) -> None:
    """A zeroconf discovery can be ignored, and the next announcement stays ignored."""
    assert await async_setup_component(hass, "config", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )
    assert result["type"] is FlowResultType.FORM
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "config_entries/ignore_flow", "flow_id": result["flow_id"], "title": "Rehom"}
    )
    response = await client.receive_json()
    assert response["success"], response
    await hass.async_block_till_done()
    entries = hass.config_entries.async_entries(DOMAIN, include_ignore=True)
    assert [(entry.source, entry.unique_id) for entry in entries] == [
        (SOURCE_IGNORE, "rehomserver.local")
    ]
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert harness.clients == []


async def test_zeroconf_repeated_announcement(hass: HomeAssistant, harness: RehomHarness) -> None:
    """The same controller announced at a second address does not stack a second card."""
    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )
    assert first["type"] is FlowResultType.FORM
    other = ZeroconfServiceInfo(
        ip_address=ip_address(OTHER_IP),
        ip_addresses=[ip_address(OTHER_IP)],
        hostname="RehomServer.local.",
        name="rehom._http._tcp.local.",
        port=8000,
        type="_http._tcp.local.",
        properties={},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=other
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


@pytest.mark.parametrize("host", [DISCOVERED_IP, "rehomserver.local"])
async def test_zeroconf_already_configured(
    hass: HomeAssistant, harness: RehomHarness, host: str
) -> None:
    """A configured IP or mDNS host name aborts without contacting the device."""
    entry = _add_entry(hass, host=host)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == host
    assert harness.clients == []


async def test_zeroconf_confirm_errors(
    hass: HomeAssistant, harness: RehomHarness, mock_setup_entry: AsyncMock
) -> None:
    """Errors in the confirm step re-show it; the user then recovers."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )
    harness.errors["login"] = RehomAuthenticationError(403)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["errors"] == {"base": "invalid_auth"}

    harness.errors["login"] = RehomConnectionError("down")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["errors"] == {"base": "cannot_connect"}

    harness.errors.clear()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    _assert_all_closed(harness)


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        # configured by IP: the probed discovered address replaces the old one
        (OTHER_IP, 8080, {CONF_HOST: DISCOVERED_IP, CONF_PORT: 8000}),
        # configured by host name: never replaced by a discovered address
        ("rehom.home.arpa", 8080, {CONF_HOST: "rehom.home.arpa", CONF_PORT: 8080}),
    ],
)
async def test_zeroconf_confirm_already_configured(
    hass: HomeAssistant,
    harness: RehomHarness,
    host: str,
    port: int,
    expected: dict[str, Any],
) -> None:
    """A controller configured under another address aborts after the probe (MAC).

    discovery-update-info: an entry configured by IP follows the discovered
    address (the probe proved it reaches the same controller).
    """
    entry = _add_entry(hass, host=host, port=port)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert {key: entry.data[key] for key in (CONF_HOST, CONF_PORT)} == expected
    assert entry.data[CONF_USERNAME] == ENTRY_DATA[CONF_USERNAME]  # credentials untouched
    _assert_all_closed(harness)


async def test_zeroconf_recovers_an_entry_stuck_on_an_old_ip(
    hass: HomeAssistant, harness: RehomHarness
) -> None:
    """An IP entry in SETUP_RETRY (controller moved) is updated and set up again."""
    entry = _add_entry(hass, host=OTHER_IP)
    harness.errors["get_alive"] = RehomConnectionError("replay: old address")
    with patch("custom_components.rehom.PLATFORMS", []):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        harness.errors.clear()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF_INFO
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        await hass.async_block_till_done()
        assert entry.data[CONF_HOST] == DISCOVERED_IP
        assert entry.state is ConfigEntryState.LOADED
        assert harness.client_data[-1][CONF_HOST] == DISCOVERED_IP
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_discovery_in_progress_zeroconf_then_dhcp(
    hass: HomeAssistant, harness: RehomHarness
) -> None:
    """A DHCP discovery of a host that zeroconf already offers is dropped."""
    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )
    assert first["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_discovery_in_progress_dhcp_then_zeroconf(
    hass: HomeAssistant, harness: RehomHarness
) -> None:
    """A zeroconf discovery of a host that DHCP already offers is dropped."""
    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    assert first["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"


async def test_user_flow_not_matching_discovery(
    hass: HomeAssistant, harness: RehomHarness, mock_setup_entry: AsyncMock
) -> None:
    """A user flow (no discovered host) never matches a discovery flow."""
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=ZEROCONF_INFO
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 2


# -- dhcp ------------------------------------------------------------------------------


async def test_dhcp_flow(
    hass: HomeAssistant, harness: RehomHarness, mock_setup_entry: AsyncMock
) -> None:
    """A new controller found by DHCP: confirm, then an entry at the lease IP."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"] == {"host": DISCOVERED_IP}
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows[0]["context"]["title_placeholders"] == {"name": f"Rehom ({DISCOVERED_IP})"}
    assert flows[0]["context"]["unique_id"] == FIXTURE_MAC

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: DISCOVERED_IP, CONF_PORT: 8000, **CREDENTIALS}
    assert result["result"].unique_id == FIXTURE_MAC
    _assert_all_closed(harness)


async def test_dhcp_lease_mac_differs(
    hass: HomeAssistant, harness: RehomHarness, mock_setup_entry: AsyncMock
) -> None:
    """The entry is keyed by the MAC the controller reports, not the lease MAC."""
    info = DhcpServiceInfo(ip=DISCOVERED_IP, hostname="rehomserver", macaddress="020000aabbcc")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=info
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == FIXTURE_MAC


async def test_dhcp_updates_ip_host(hass: HomeAssistant, harness: RehomHarness) -> None:
    """A configured IP address follows the new lease."""
    entry = _add_entry(hass, host=OTHER_IP)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == DISCOVERED_IP
    assert harness.clients == []


async def test_dhcp_keeps_host_name(hass: HomeAssistant, harness: RehomHarness) -> None:
    """A configured host name (rehomserver.local) is never replaced by a lease IP."""
    entry = _add_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "rehomserver.local"
    assert harness.clients == []


async def test_dhcp_known_ip_other_entry(hass: HomeAssistant, harness: RehomHarness) -> None:
    """The lease IP is already configured for an entry with another id."""
    _add_entry(hass, unique_id=OTHER_MAC, host=DISCOVERED_IP)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_dhcp_confirm_already_configured(hass: HomeAssistant, harness: RehomHarness) -> None:
    """A lease MAC unknown to HA, but the controller is configured under its own MAC."""
    entry = _add_entry(hass)
    info = DhcpServiceInfo(ip=DISCOVERED_IP, hostname="rehomserver", macaddress="020000aabbcc")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=info
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "rehomserver.local"


# -- reauth ----------------------------------------------------------------------------


async def test_reauth_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry, harness: RehomHarness
) -> None:
    """New credentials are stored and the entry reloads with them."""
    entry = init_integration
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["host"] == "rehomserver.local"
    assert _defaults(result) == {CONF_USERNAME: "user", CONF_PASSWORD: None}

    new = {CONF_USERNAME: "user2", CONF_PASSWORD: "new-password"}
    clients = len(harness.clients)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data == {**ENTRY_DATA, **new}
    assert entry.state is ConfigEntryState.LOADED
    # probe + the reloaded entry's client, both with the new credentials
    assert len(harness.clients) == clients + 2
    assert harness.client_data[-1] == {**ENTRY_DATA, **new}
    assert all(client.connection_state is ConnectionState.CLOSED for client in harness.clients[:-1])
    assert harness.client.connection_state is ConnectionState.CONNECTED


async def test_reauth_flow_invalid_auth(
    hass: HomeAssistant, harness: RehomHarness, mock_config_entry: MockConfigEntry
) -> None:
    """Refused credentials re-show the form; the entry is unchanged."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    harness.errors["login"] = RehomAuthenticationError(401)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "user", CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    assert mock_config_entry.data == ENTRY_DATA
    _assert_all_closed(harness)


async def test_reauth_flow_wrong_device(hass: HomeAssistant, harness: RehomHarness) -> None:
    """The entry's address now answers with another controller."""
    entry = _add_entry(hass, unique_id=OTHER_MAC)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "user", CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data == ENTRY_DATA
    _assert_all_closed(harness)


# -- reconfigure -----------------------------------------------------------------------


async def test_reconfigure_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry, harness: RehomHarness
) -> None:
    """A new address of the same controller is stored and the entry reloads."""
    entry = init_integration
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert _defaults(result) == {CONF_HOST: "rehomserver.local", CONF_PORT: 8000}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: DISCOVERED_IP, CONF_PORT: 8000}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data == {**ENTRY_DATA, CONF_HOST: DISCOVERED_IP}
    assert entry.state is ConfigEntryState.LOADED
    assert harness.client_data[-1] == {**ENTRY_DATA, CONF_HOST: DISCOVERED_IP}
    assert harness.client.connection_state is ConnectionState.CONNECTED


async def test_reconfigure_flow_cannot_connect(
    hass: HomeAssistant, harness: RehomHarness, mock_config_entry: MockConfigEntry
) -> None:
    """An unreachable address re-shows the form with the input."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    harness.errors["get_alive"] = RehomConnectionError("down")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: DISCOVERED_IP, CONF_PORT: 8080}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert _defaults(result) == {CONF_HOST: DISCOVERED_IP, CONF_PORT: 8080}
    assert mock_config_entry.data == ENTRY_DATA
    # the entry's credentials were used for the probe
    assert harness.client_data[-1] == {**ENTRY_DATA, CONF_HOST: DISCOVERED_IP, CONF_PORT: 8080}
    _assert_all_closed(harness)


async def test_reconfigure_flow_wrong_device(hass: HomeAssistant, harness: RehomHarness) -> None:
    """The new address answers with another controller."""
    entry = _add_entry(hass, unique_id=OTHER_MAC)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: DISCOVERED_IP, CONF_PORT: 8000}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data == ENTRY_DATA


async def test_reconfigure_unsupported_api(
    hass: HomeAssistant, harness: RehomHarness, mock_config_entry: MockConfigEntry
) -> None:
    """Any other aiorehom error maps to cannot_connect."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    harness.errors["get_interface"] = RehomResponseError(200, "bad")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: DISCOVERED_IP, CONF_PORT: 8000}
    )
    assert result["errors"] == {"base": "cannot_connect"}


# -- options ---------------------------------------------------------------------------


async def test_options_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry, harness: RehomHarness
) -> None:
    """The form shows the current duration; saving a change reloads the entry."""
    entry = init_integration
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert _defaults(result) == {CONF_TEMPORARY_COMFORT_DURATION: 2.0}

    clients = len(harness.clients)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TEMPORARY_COMFORT_DURATION: 3.5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_TEMPORARY_COMFORT_DURATION: 3.5}
    assert entry.state is ConfigEntryState.LOADED
    assert len(harness.clients) == clients + 1  # reloaded


async def test_options_flow_default(hass: HomeAssistant, harness: RehomHarness) -> None:
    """An entry without options shows the default duration."""
    entry = _add_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _defaults(result) == {CONF_TEMPORARY_COMFORT_DURATION: 2.0}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TEMPORARY_COMFORT_DURATION: 24}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_TEMPORARY_COMFORT_DURATION: 24.0}
    await hass.async_block_till_done()
