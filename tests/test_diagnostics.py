"""Config entry diagnostics."""

from __future__ import annotations

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_dumps
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion

from custom_components.rehom.diagnostics import async_get_config_entry_diagnostics

from .conftest import CONTROL_OPTIONS
from .harness import FIXTURE_MAC, RehomHarness, T


@pytest.fixture
def platforms() -> list[Platform]:
    """Diagnostics need no entity platform."""
    return []


async def test_diagnostics(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    snapshot: SnapshotAssertion,
) -> None:
    """The redacted output: state, client stats, versions, lock and heartbeat."""
    await harness.advance_to(T("10:24:40"))  # a heartbeat has been seen (~10:23)
    result = await async_get_config_entry_diagnostics(hass, init_integration)
    assert result == snapshot

    assert result["entry"] == {
        "title": REDACTED,
        "unique_id": REDACTED,
        "data": {"host": REDACTED, "port": 8000, "username": REDACTED, "password": REDACTED},
        "options": {"temporary_comfort_duration": 2.0},
    }
    assert result["client"]["connection_state"] == "connected"
    assert result["client"]["available"] is True
    assert result["client"]["last_synced_at"] == "2026-09-25T10:22:06.806000+00:00"
    assert result["client"]["stats"]["syncs"] == 1
    assert result["versions"]["web_services"] == "3.16.3"
    assert result["versions"]["controller"] == "4.04 R"
    assert result["lock"] == {
        "state": "normal",
        "is_crono": False,
        "serial_down": False,
        "read_only": False,
    }
    assert result["heartbeat"]["ok"] is True
    assert 0 <= result["heartbeat"]["age_seconds"] < 180
    assert result["state"]["hub"]["mac"] == REDACTED
    assert {zone["name"] for zone in result["state"]["zones"].values()} == {REDACTED}
    assert {vmc["name"] for vmc in result["state"]["vmcs"].values()} == {REDACTED}


@pytest.mark.parametrize("entry_options", [CONTROL_OPTIONS])
async def test_diagnostics_show_control_enabled(
    hass: HomeAssistant, init_integration: MockConfigEntry, harness: RehomHarness
) -> None:
    """With "Enable control" on, the options say so (checked before the first control)."""
    result = await async_get_config_entry_diagnostics(hass, init_integration)
    assert result["entry"]["options"] == {
        "enable_control": True,
        "temporary_comfort_duration": 2.0,
    }
    assert harness.allow_writes == [True]
    assert harness.writes == []  # reading diagnostics sends nothing


async def test_diagnostics_before_heartbeat(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """At the replay start no heartbeat has been seen yet."""
    result = await async_get_config_entry_diagnostics(hass, init_integration)
    assert result["heartbeat"] == {"ok": None, "age_seconds": None}


async def test_diagnostics_has_no_personal_data(
    hass: HomeAssistant, init_integration: MockConfigEntry, harness: RehomHarness
) -> None:
    """No MAC, host, credentials, unit names, serials or custom ids survive."""
    state = init_integration.runtime_data.coordinator.data
    text = json_dumps(await async_get_config_entry_diagnostics(hass, init_integration))

    forbidden = {
        FIXTURE_MAC,
        FIXTURE_MAC.replace(":", ""),
        FIXTURE_MAC.upper(),
        "rehomserver.local",
        "test-password",
        '"user"',
    }
    for unit in (*state.zones.values(), *state.vmcs.values()):
        forbidden.add(unit.name)
    for identity in (
        *(zone.identity for zone in state.zones.values()),
        *(vmc.identity for vmc in state.vmcs.values()),
        *(actuator.identity for actuator in state.actuators.values()),
    ):
        forbidden.update(value for value in (identity.serial, identity.custom_id) if value)
    assert "Zona 001" in forbidden
    assert "713381" in forbidden
    for needle in sorted(forbidden):
        assert needle not in text, needle

    # The raw record dump (names, user names, GPS, installation id) is not included.
    assert "45.0000000" not in text
    assert "INSTALLATION_ID" not in text
    assert "UTEN" not in text
