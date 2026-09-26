"""Repair issues, none of them fixable from Home Assistant.

Home Assistant's frozen clock and the library's virtual clock read the same
instant (``tests/harness.py``), so the 5- and 15-minute grace rules run on the
replayed timeline.
"""

from __future__ import annotations

from datetime import timedelta

from aiorehom import RehomConnectionError, RehomResponseError
from aiorehom.replay import ReplayData
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rehom.const import (
    DOMAIN,
    ISSUE_INSTALLER_SESSION_ACTIVE,
    ISSUE_SEASON_MISMATCH,
    ISSUE_SETPOINT_MISMATCH,
    ISSUE_UNSUPPORTED_API,
)
from custom_components.rehom.issues import issue_id

from .harness import FIXTURE_START, RehomHarness, T, bus_update


@pytest.fixture
def platforms() -> list[Platform]:
    """Issues need no entity platform."""
    return []


def _installer_session_open(data: ReplayData) -> None:
    data.plant_conf["CONFIGURA_ON"] = "1"


def _conf_season_winter(data: ReplayData) -> None:
    data.plant_conf["stagione"] = "0"


def _issue(
    issue_registry: ir.IssueRegistry, entry: MockConfigEntry, key: str
) -> ir.IssueEntry | None:
    return issue_registry.async_get_issue(DOMAIN, issue_id(key, entry.entry_id))


def _keys(issue_registry: ir.IssueRegistry, entry: MockConfigEntry) -> set[str]:
    keys = (
        ISSUE_INSTALLER_SESSION_ACTIVE,
        ISSUE_SEASON_MISMATCH,
        ISSUE_SETPOINT_MISMATCH,
        ISSUE_UNSUPPORTED_API,
    )
    return {key for key in keys if _issue(issue_registry, entry, key) is not None}


async def test_setpoint_mismatch(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """SET_POINT_TEMP 26 vs TEMP_COM 24 from the start; the 10:41:06 frames fix it."""
    entry = init_integration
    plant = entry.runtime_data.coordinator.data.plant
    assert plant.setpoint_mismatch is True
    assert plant.setpoint_mismatch_since == FIXTURE_START
    assert _keys(issue_registry, entry) == set()

    await harness.advance_to(T("10:26:30"))
    assert _keys(issue_registry, entry) == set()

    await harness.advance_to(T("10:28:00"))
    issue = _issue(issue_registry, entry, ISSUE_SETPOINT_MISMATCH)
    assert issue is not None
    assert issue.domain == DOMAIN
    assert issue.translation_key == ISSUE_SETPOINT_MISMATCH
    assert issue.translation_placeholders == {"setpoint": "26", "level_temperature": "24"}
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable
    assert not issue.is_persistent
    assert _keys(issue_registry, entry) == {ISSUE_SETPOINT_MISMATCH}

    await harness.advance_to(T("10:41:05"))
    assert _keys(issue_registry, entry) == {ISSUE_SETPOINT_MISMATCH}

    await harness.advance_to(T("10:41:07"))
    assert entry.runtime_data.coordinator.data.plant.setpoint_mismatch is False
    assert _keys(issue_registry, entry) == set()

    await harness.advance(120)  # stays deleted
    assert _keys(issue_registry, entry) == set()


@pytest.mark.parametrize(
    "device_patch",
    [
        {
            "patch": _installer_session_open,
            "extra_frames": [
                (FIXTURE_START + timedelta(minutes=17), bus_update("CONFIGURA_ON", "0"))
            ],
        }
    ],
)
async def test_installer_session_active(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """An installer session open for 15 minutes raises the issue; closing it deletes it."""
    entry = init_integration
    assert entry.runtime_data.coordinator.data.plant.installer_session is True

    await harness.advance_to(FIXTURE_START + timedelta(minutes=14))
    assert _issue(issue_registry, entry, ISSUE_INSTALLER_SESSION_ACTIVE) is None

    await harness.advance_to(FIXTURE_START + timedelta(minutes=16))
    issue = _issue(issue_registry, entry, ISSUE_INSTALLER_SESSION_ACTIVE)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable
    assert issue.translation_placeholders is None

    await harness.advance_to(FIXTURE_START + timedelta(minutes=17, seconds=5))
    assert entry.runtime_data.coordinator.data.plant.installer_session is False
    assert _issue(issue_registry, entry, ISSUE_INSTALLER_SESSION_ACTIVE) is None


@pytest.mark.parametrize("device_patch", [{"patch": _conf_season_winter}])
async def test_season_mismatch(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Running season (summer) vs configured season (winter) for 15 minutes."""
    entry = init_integration
    plant = entry.runtime_data.coordinator.data.plant
    assert plant.season is not None
    assert plant.conf_season is not None
    assert plant.season != plant.conf_season

    await harness.advance_to(FIXTURE_START + timedelta(minutes=14))
    assert _issue(issue_registry, entry, ISSUE_SEASON_MISMATCH) is None

    await harness.advance_to(FIXTURE_START + timedelta(minutes=16))
    issue = _issue(issue_registry, entry, ISSUE_SEASON_MISMATCH)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable

    # Unloading deletes the state-derived issues.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _keys(issue_registry, entry) == set()


async def test_no_season_or_installer_issue_on_fixture(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The reference plant: seasons agree and no installer session is open."""
    plant = init_integration.runtime_data.coordinator.data.plant
    assert plant.installer_session is False
    assert plant.season == plant.conf_season
    await harness.advance_to(FIXTURE_START + timedelta(minutes=16))  # past both 15-min graces
    assert _issue(issue_registry, init_integration, ISSUE_SEASON_MISMATCH) is None
    assert _issue(issue_registry, init_integration, ISSUE_INSTALLER_SESSION_ACTIVE) is None


async def test_unsupported_api_at_runtime(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Two consecutive resyncs failing to parse raise the issue; a good resync deletes it."""
    entry = init_integration
    client = harness.client
    harness.errors["get_interface"] = RehomResponseError(200, "bad")

    await harness.advance_to(FIXTURE_START + timedelta(seconds=600 + 5))  # periodic resync
    assert client.stats.sync_failure_streak == 1
    assert client.stats.last_sync_error == "RehomResponseError"
    await harness.advance_to(FIXTURE_START + timedelta(seconds=600 + 55))
    assert client.stats.sync_failure_streak == 1
    assert _issue(issue_registry, entry, ISSUE_UNSUPPORTED_API) is None  # one is not enough

    await harness.advance_to(FIXTURE_START + timedelta(seconds=600 + 60 + 5))  # 60-s retry
    assert client.stats.sync_failure_streak == 2
    await harness.advance(60)  # the next 60-s check
    issue = _issue(issue_registry, entry, ISSUE_UNSUPPORTED_API)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert not issue.is_fixable
    assert issue.translation_placeholders == {"version": "3.16.3"}
    # The library keeps serving the last good state.
    assert client.available
    assert entry.runtime_data.coordinator.last_update_success

    harness.errors.clear()
    await harness.advance_to(FIXTURE_START + timedelta(seconds=600 + 60 + 120 + 5))  # retry
    assert client.stats.sync_failure_streak == 0
    await harness.advance(60)
    assert _issue(issue_registry, entry, ISSUE_UNSUPPORTED_API) is None


async def test_resync_connection_errors_are_not_unsupported_api(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Failing resyncs for another reason never raise unsupported_api."""
    harness.errors["get_interface"] = RehomConnectionError("replay: timeout")
    await harness.advance_to(FIXTURE_START + timedelta(seconds=600 + 60 + 120 + 30))
    assert harness.client.stats.sync_failure_streak >= 2
    assert harness.client.stats.last_sync_error == "RehomConnectionError"
    assert _issue(issue_registry, init_integration, ISSUE_UNSUPPORTED_API) is None


async def _recover(harness: RehomHarness, entry: MockConfigEntry) -> None:
    """Heal the transport and advance until the coordinator is available again."""
    harness.errors.clear()
    for _ in range(12):
        await harness.advance(5)
        if entry.runtime_data.coordinator.last_update_success:
            return
    raise AssertionError("the controller did not recover")


async def test_graces_do_not_mature_during_an_outage(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Stale data never raises an issue; after the outage the 5-min grace starts again."""
    entry = init_integration
    coordinator = entry.runtime_data.coordinator
    harness.errors["get_alive"] = RehomConnectionError("replay: /alive/ down")
    await harness.advance_to(T("10:25:00"))
    assert not coordinator.last_update_success

    # 10:27:06.8 would be 5 min after the mismatch started, but nobody saw it since 10:24
    await harness.advance_to(T("10:29:00"))
    assert not coordinator.last_update_success
    assert coordinator.data.plant.setpoint_mismatch is True
    assert _keys(issue_registry, entry) == set()

    await _recover(harness, entry)
    recovered = harness.now
    assert coordinator.data.plant.setpoint_mismatch is True
    await harness.advance_to(recovered + timedelta(minutes=4, seconds=50))
    assert _keys(issue_registry, entry) == set()  # only 4m50s observed so far
    await harness.advance_to(recovered + timedelta(minutes=6, seconds=5))  # + the 60-s tick
    assert _keys(issue_registry, entry) == {ISSUE_SETPOINT_MISMATCH}


async def test_issues_are_kept_through_an_outage(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """An issue raised before an outage is neither deleted nor re-judged on stale data."""
    entry = init_integration
    await harness.advance_to(T("10:28:00"))
    assert _keys(issue_registry, entry) == {ISSUE_SETPOINT_MISMATCH}
    harness.errors["get_alive"] = RehomConnectionError("replay: /alive/ down")
    await harness.advance_to(T("10:33:00"))
    assert not entry.runtime_data.coordinator.last_update_success
    assert _keys(issue_registry, entry) == {ISSUE_SETPOINT_MISMATCH}
    await _recover(harness, entry)
    await harness.advance(60)
    assert _keys(issue_registry, entry) == {ISSUE_SETPOINT_MISMATCH}  # still a mismatch
    await harness.advance_to(T("10:41:12"))  # the 10:41:06 frames fix it
    assert _keys(issue_registry, entry) == set()


@pytest.mark.parametrize("device_patch", [{"patch": _conf_season_winter}])
async def test_own_graces_restart_after_an_outage(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The tracker's own "since" (season, installer) restarts after an outage too."""
    entry = init_integration
    await harness.advance_to(FIXTURE_START + timedelta(minutes=2))
    harness.errors["get_alive"] = RehomConnectionError("replay: /alive/ down")
    await harness.advance_to(FIXTURE_START + timedelta(minutes=5))
    assert not entry.runtime_data.coordinator.last_update_success
    await _recover(harness, entry)
    recovered = harness.now
    # past 15 min since the start, but only about 10 min observed since the outage
    await harness.advance_to(FIXTURE_START + timedelta(minutes=16))
    assert _issue(issue_registry, entry, ISSUE_SEASON_MISMATCH) is None
    await harness.advance_to(recovered + timedelta(minutes=16))  # still inside the capture
    assert _issue(issue_registry, entry, ISSUE_SEASON_MISMATCH) is not None
