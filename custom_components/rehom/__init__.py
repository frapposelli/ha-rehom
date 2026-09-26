"""The Rehom integration (read-only in this version)."""

from __future__ import annotations

from aiorehom import (
    DeviceKind,
    RehomAuthenticationError,
    RehomError,
    RehomResponseError,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType

from . import api
from .const import (
    DOMAIN,
    EXC_CANNOT_CONNECT,
    EXC_INVALID_AUTH,
    EXC_UNSUPPORTED_API,
    EXC_WRONG_DEVICE,
    ISSUE_UNSUPPORTED_API,
    MANUFACTURER,
    PLATFORMS,
)
from .coordinator import RehomConfigEntry, RehomCoordinator, RehomRuntimeData
from .entity import device_identifier
from .issues import (
    STATE_ISSUES,
    RehomIssueTracker,
    async_create_unsupported_api_setup_issue,
    async_delete_issues,
)
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions (action-setup rule)."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RehomConfigEntry) -> bool:
    """Connect, register the hub device and forward the platforms."""
    client = api.create_client(hass, entry.data)
    host = entry.data[CONF_HOST]
    try:
        await client.connect()
    except RehomAuthenticationError as err:
        await client.close()
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key=EXC_INVALID_AUTH
        ) from err
    except RehomResponseError as err:
        await client.close()
        async_create_unsupported_api_setup_issue(hass, entry.entry_id)
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key=EXC_UNSUPPORTED_API
        ) from err
    except RehomError as err:
        await client.close()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key=EXC_CANNOT_CONNECT,
            translation_placeholders={"host": host},
        ) from err

    state = client.state
    if state.hub.mac is not None and dr.format_mac(state.hub.mac) != entry.unique_id:
        await client.close()
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key=EXC_WRONG_DEVICE)
    async_delete_issues(hass, entry.entry_id, (ISSUE_UNSUPPORTED_API,))

    entry.async_on_unload(client.close)

    async def _async_close(_event: Event) -> None:
        await client.close()  # idempotent

    # async_listen, not async_listen_once: a once-listener that already fired
    # cannot be removed again, and unloading after a stop would log an error.
    entry.async_on_unload(hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _async_close))

    coordinator = RehomCoordinator(hass, entry, client)
    coordinator.async_start()  # subscribe before the next await: no update is missed
    await coordinator.async_config_entry_first_refresh()

    hub_id = coordinator.hub_id
    hub = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={device_identifier(hub_id, DeviceKind.HUB)},
        connections={(dr.CONNECTION_NETWORK_MAC, hub_id)},
        translation_key="hub",
        manufacturer=MANUFACTURER,
        model="Rehom Server",
        sw_version=state.hub.web_version,
        hw_version=_hw_version(state.hub.board, state.hub.platform),
        configuration_url=f"http://{host}:{entry.data[CONF_PORT]}/www/",
    )
    issues = RehomIssueTracker(hass, entry, coordinator)
    entry.runtime_data = RehomRuntimeData(
        client=client, coordinator=coordinator, hub_device_id=hub.id, issues=issues
    )
    issues.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _hw_version(board: str | None, platform: str | None) -> str | None:
    parts = [part for part in (board, platform) if part]
    return " / ".join(parts) or None


async def async_unload_entry(hass: HomeAssistant, entry: RehomConfigEntry) -> bool:
    """Unload the platforms; the client is closed by the on_unload callbacks."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: RehomConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow removing a zone or VMC device only while its unit is absent.

    Which units are present is known only while the entry is loaded (for
    example not in SETUP_RETRY, where ``runtime_data`` does not exist).
    """
    if entry.state is not ConfigEntryState.LOADED:
        return False
    coordinator = entry.runtime_data.coordinator
    state = coordinator.data
    hub_id = coordinator.hub_id
    present = {
        device_identifier(hub_id, DeviceKind.HUB),
        device_identifier(hub_id, DeviceKind.PLANT),
    }
    present |= {device_identifier(hub_id, DeviceKind.ZONE, unit) for unit in state.zones}
    present |= {device_identifier(hub_id, DeviceKind.VMC, unit) for unit in state.vmcs}
    return not any(identifier in present for identifier in device_entry.identifiers)


async def async_remove_entry(hass: HomeAssistant, entry: RehomConfigEntry) -> None:
    """Delete every issue of a removed entry (unsupported_api survives unloads)."""
    async_delete_issues(hass, entry.entry_id, STATE_ISSUES)
