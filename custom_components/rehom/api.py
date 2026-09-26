"""The only place that constructs an aiorehom client.

Tests replace :func:`create_client` (``custom_components.rehom.api.create_client``)
with a factory that returns a real ``RehomClient`` on a replay transport.
Callers MUST call it as ``api.create_client(...)`` (module attribute lookup),
never ``from .api import create_client``, so the patch applies everywhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiorehom import RehomClient
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac


class MissingMacError(Exception):
    """The controller did not report ``WEBSERVER.MacAddress`` (no stable id)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ControllerInfo:
    """Identity read by :func:`async_probe`."""

    mac: str  # format_mac() form; the config entry unique_id
    web_version: str | None
    controller_version: str | None


def create_client(hass: HomeAssistant, data: Mapping[str, Any]) -> RehomClient:
    """A new, unconnected, read-only client for ``data`` (entry data or form input).

    Raises ``ValueError`` for an invalid host or port (aiorehom validates them).
    The shared HA session is used for REST and is never modified by the library.
    The library's private WebSocket session borrows that session's connector,
    so the WebSocket resolves the host with Home Assistant's resolver too
    (mDNS for ``rehomserver.local`` on installs whose DNS does not answer it).
    """
    port: int = int(data[CONF_PORT])
    return RehomClient(
        data[CONF_HOST],
        port=port,
        ws_port=port,
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        session=async_get_clientsession(hass),
    )


async def async_probe(hass: HomeAssistant, data: Mapping[str, Any]) -> ControllerInfo:
    """Connect once (login, WebSocket, full snapshot), read the identity, close.

    Raises ``ValueError`` (invalid host/port), any ``aiorehom`` error from
    ``connect()``, or :class:`MissingMacError`.  The client is always closed.
    """
    client = create_client(hass, data)
    try:
        await client.connect()
        hub = client.state.hub
    finally:
        await client.close()
    if hub.mac is None:
        raise MissingMacError
    return ControllerInfo(
        mac=format_mac(hub.mac),
        web_version=hub.web_version,
        controller_version=hub.controller_version,
    )
