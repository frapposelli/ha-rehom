"""Diagnostics for a Rehom config entry.

The redacted state model, the client's counters, versions, the lock state and
the heartbeat age.  The raw record dump (``client.dump()``) holds personal
data and secrets, and is deliberately not included.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import aiorehom
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import RehomConfigEntry

#: Keys redacted anywhere in the output (recursive by key).
TO_REDACT = {
    CONF_HOST,
    CONF_USERNAME,
    CONF_PASSWORD,
    "title",
    "unique_id",
    "mac",
    "name",
    "serial",
    "custom_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RehomConfigEntry
) -> dict[str, Any]:
    """Return the redacted diagnostics of one loaded entry."""
    runtime = entry.runtime_data
    client = runtime.client
    state = runtime.coordinator.data
    plant = state.plant
    health = state.health
    last_synced_at = client.last_synced_at
    heartbeat_at = health.heartbeat_at
    return {
        "entry": async_redact_data(
            {
                "title": entry.title,
                "unique_id": entry.unique_id,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            TO_REDACT,
        ),
        "client": {
            "connection_state": client.connection_state.value,
            "available": client.available,
            "last_synced_at": last_synced_at.isoformat() if last_synced_at else None,
            "stats": dataclasses.asdict(client.stats),
        },
        "versions": {
            "aiorehom": aiorehom.__version__,
            "web_services": state.hub.web_version,
            "controller": state.hub.controller_version,
            "board": state.hub.board,
            "platform": state.hub.platform,
        },
        "lock": {
            "state": plant.lock.value,
            "is_crono": plant.is_crono,
            "serial_down": plant.serial_down,
            "read_only": plant.read_only,
        },
        "heartbeat": {
            "ok": health.heartbeat_ok,
            "age_seconds": (
                (dt_util.utcnow() - heartbeat_at).total_seconds() if heartbeat_at else None
            ),
        },
        "state": async_redact_data(state.as_dict(), TO_REDACT),
    }
