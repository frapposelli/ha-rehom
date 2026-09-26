"""Push coordinator for one Rehom controller.

The library publishes an immutable ``RehomState`` after every batch of
WebSocket frames, resync and time-driven rebuild (slot boundaries, override
expiry, alarm debounce, heartbeat deadline); this coordinator only forwards
it.  There is no polling (``update_interval=None``) and no I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from aiorehom import (
    ConnectionState,
    RehomClient,
    RehomNotReadyError,
    RehomState,
    StateUpdate,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, EXC_NOT_READY, EXC_UNAVAILABLE

if TYPE_CHECKING:
    from .issues import RehomIssueTracker

_LOGGER = logging.getLogger(__package__)


@dataclass(slots=True, kw_only=True)
class RehomRuntimeData:
    """``entry.runtime_data`` of a loaded Rehom config entry."""

    client: RehomClient
    coordinator: RehomCoordinator
    hub_device_id: str  # device registry id of the hub device (for via_device_id)
    issues: RehomIssueTracker


type RehomConfigEntry = ConfigEntry[RehomRuntimeData]


class RehomCoordinator(DataUpdateCoordinator[RehomState]):
    """Forwards library state updates to Home Assistant listeners.

    ``data`` is the latest ``RehomState``.  ``last_update`` is the latest
    ``StateUpdate`` (``None`` until the first one after setup), for listeners
    that need ``previous``/``reason``.  Availability (``last_update_success``)
    follows ``client.available`` (CONNECTED or DEGRADED).
    """

    config_entry: RehomConfigEntry

    def __init__(self, hass: HomeAssistant, entry: RehomConfigEntry, client: RehomClient) -> None:
        """Create the coordinator; call :meth:`async_start` after ``client.connect()``."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,
            always_update=True,
        )
        self.client = client
        #: The hub id: the entry unique_id (``format_mac`` of ``WEBSERVER.MacAddress``).
        self.hub_id: str = entry.unique_id or ""
        self.last_update: StateUpdate | None = None
        self._unavailable_logged = False

    async def _async_update_data(self) -> RehomState:
        """Return the library's current state (no I/O).

        Used by the first refresh and by ``homeassistant.update_entity``.  While
        the client is not available the refresh fails: returning the (stale)
        state would mark every entity available again until the next
        connection change (availability rule 1).  Right after ``connect()`` the
        client is always available, so the first refresh is unaffected.
        """
        try:
            state = self.client.state
        except RehomNotReadyError as err:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key=EXC_NOT_READY) from err
        if not self.client.available:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key=EXC_UNAVAILABLE)
        return state

    @callback
    def async_start(self) -> None:
        """Subscribe to the client; the subscriptions end when the entry unloads."""
        entry = self.config_entry
        entry.async_on_unload(self.client.subscribe(self._async_handle_update))
        entry.async_on_unload(self.client.on_connection_change(self._async_handle_connection))

    @callback
    def _async_handle_update(self, update: StateUpdate) -> None:
        self.last_update = update
        if self.client.available:
            self.async_set_updated_data(update.state)
        else:
            self.data = update.state  # keep the latest; entities stay unavailable

    @callback
    def _async_handle_connection(self, connection: ConnectionState) -> None:
        if connection is ConnectionState.CLOSED:
            return  # unload or HA stop: entities are going away
        if self.client.available:
            if self._unavailable_logged:
                self.logger.info("The Rehom controller is available again")
                self._unavailable_logged = False
            self.async_set_updated_data(self.client.state)
        elif connection is ConnectionState.UNAVAILABLE:
            if not self._unavailable_logged:
                self.logger.info("The Rehom controller is unavailable")
                self._unavailable_logged = True
            self.last_update_success = False
            self.async_update_listeners()
