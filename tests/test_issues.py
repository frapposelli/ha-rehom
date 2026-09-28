"""Repair issues: when each one is raised and deleted, and which one is fixable.

Only ``setpoint_mismatch`` can be fixable: with "Enable control" on and the
house at a level Home Assistant may send (economy or comfort; pre-comfort is
never sent to the house).  The fix flow itself is tested in ``test_repairs.py``.

Home Assistant's frozen clock and the library's virtual clock read the same
instant (``tests/harness.py``), so the 5- and 15-minute grace rules run on the
replayed timeline.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

from aiorehom import MasterPreset, RehomConnectionError, RehomResponseError
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
    ISSUE_SETPOINT_MISMATCH_FIXABLE,
    ISSUE_UNSUPPORTED_API,
)
from custom_components.rehom.issues import issue_id, setpoint_fix_preset

from .conftest import CONTROL_OPTIONS
from .harness import FIXTURE_START, RehomHarness, T, bus_update, set_values, termo_update

#: The house MANUAL at economy (29 °C) while the controller regulates to 26 °C.
ECO_MISMATCH = set_values({"REHOM...SET_POINT": "1"})
#: The pre-comfort temperature 25 °C (26 °C in the capture, the regulated setpoint).
PRE_COMFORT_25 = {"REHOM...TEMP_PRE": "25"}
#: The house MANUAL at pre-comfort (25 °C) while the controller regulates to 26 °C.
PRE_COMFORT_MISMATCH = set_values({**PRE_COMFORT_25, "REHOM...SET_POINT": "2"})


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
    assert not issue.is_fixable  # "Enable control" is off
    assert issue.data is None
    assert not issue.is_persistent
    assert _keys(issue_registry, entry) == {ISSUE_SETPOINT_MISMATCH}

    await harness.advance_to(T("10:41:05"))
    assert _keys(issue_registry, entry) == {ISSUE_SETPOINT_MISMATCH}

    await harness.advance_to(T("10:41:07"))
    assert entry.runtime_data.coordinator.data.plant.setpoint_mismatch is False
    assert _keys(issue_registry, entry) == set()

    await harness.advance(120)  # stays deleted
    assert _keys(issue_registry, entry) == set()


@pytest.mark.parametrize("entry_options", [CONTROL_OPTIONS])
@pytest.mark.parametrize("device_patch", [ECO_MISMATCH])
async def test_setpoint_mismatch_fixable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Control on and the house at economy: the issue offers the fix (for this entry)."""
    entry = init_integration
    await harness.advance_to(T("10:28:00"))
    issue = _issue(issue_registry, entry, ISSUE_SETPOINT_MISMATCH)
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": entry.entry_id}
    # same issue id; the fixable texts (a fix flow instead of a description)
    assert issue.translation_key == ISSUE_SETPOINT_MISMATCH_FIXABLE
    assert issue.translation_placeholders == {"setpoint": "26", "level_temperature": "29"}
    assert harness.writes == []  # raising the issue sends nothing


@pytest.mark.parametrize("entry_options", [CONTROL_OPTIONS])
async def test_setpoint_mismatch_comfort_fixable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The capture itself: comfort (24 °C) regulating to 26 °C, and comfort may be sent."""
    entry = init_integration
    await harness.advance_to(T("10:28:00"))
    issue = _issue(issue_registry, entry, ISSUE_SETPOINT_MISMATCH)
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": entry.entry_id}
    assert issue.translation_key == ISSUE_SETPOINT_MISMATCH_FIXABLE
    assert issue.translation_placeholders == {"setpoint": "26", "level_temperature": "24"}
    assert harness.writes == []


@pytest.mark.parametrize("entry_options", [CONTROL_OPTIONS])
@pytest.mark.parametrize("device_patch", [PRE_COMFORT_MISMATCH])
async def test_setpoint_mismatch_pre_comfort_not_fixable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Control on, but pre-comfort is never sent to the house: no fix offered."""
    await harness.advance_to(T("10:28:00"))
    issue = _issue(issue_registry, init_integration, ISSUE_SETPOINT_MISMATCH)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.data is None
    assert issue.translation_key == ISSUE_SETPOINT_MISMATCH
    assert issue.translation_placeholders == {"setpoint": "26", "level_temperature": "25"}


@pytest.mark.parametrize("entry_options", [CONTROL_OPTIONS])
@pytest.mark.parametrize("device_patch", [ECO_MISMATCH])
async def test_setpoint_mismatch_runtime_flag_off(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The option is on but the running client cannot write: no fix offered."""
    init_integration.runtime_data.control_enabled = False
    await harness.advance_to(T("10:28:00"))
    issue = _issue(issue_registry, init_integration, ISSUE_SETPOINT_MISMATCH)
    assert issue is not None
    assert not issue.is_fixable


@pytest.mark.parametrize("entry_options", [CONTROL_OPTIONS])
@pytest.mark.parametrize(
    "device_patch",
    [
        set_values(
            PRE_COMFORT_25,
            extra_frames=[
                (T("10:29:00"), termo_update("REHOM...SET_POINT", "1")),
                (T("10:31:00"), termo_update("REHOM...SET_POINT", "2")),
                (T("10:33:00"), termo_update("REHOM...SET_POINT", "3")),
            ],
        )
    ],
)
async def test_setpoint_mismatch_fixable_follows_the_level(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Re-judged on every update: comfort, economy (fix), pre-comfort (no fix), comfort (fix)."""
    entry = init_integration
    fixable: list[tuple[bool, str, str | None]] = []
    for when in ("10:28:00", "10:29:05", "10:31:05", "10:33:05"):
        await harness.advance_to(T(when))
        issue = _issue(issue_registry, entry, ISSUE_SETPOINT_MISMATCH)
        assert issue is not None
        placeholders = issue.translation_placeholders or {}
        fixable.append(
            (issue.is_fixable, issue.translation_key, placeholders.get("level_temperature"))
        )
    assert fixable == [
        (True, ISSUE_SETPOINT_MISMATCH_FIXABLE, "24"),
        (True, ISSUE_SETPOINT_MISMATCH_FIXABLE, "29"),
        (False, ISSUE_SETPOINT_MISMATCH, "25"),
        (True, ISSUE_SETPOINT_MISMATCH_FIXABLE, "24"),
    ]
    assert harness.writes == []


@pytest.mark.parametrize("entry_options", [CONTROL_OPTIONS])
@pytest.mark.parametrize(
    ("preset", "fix"),
    [
        (MasterPreset.ECONOMY, MasterPreset.ECONOMY),
        (MasterPreset.COMFORT, MasterPreset.COMFORT),
        (MasterPreset.PRE_COMFORT, None),  # a level, but never sent to the house
        (MasterPreset.AUTO, None),  # AUTO has no setpoint of its own
        (MasterPreset.OFF, None),
        (None, None),
    ],
)
async def test_setpoint_fix_preset(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    preset: MasterPreset | None,
    fix: MasterPreset | None,
) -> None:
    """Only a MANUAL level that may be sent (control.VERIFIED_VALUES) can be re-sent."""
    plant = init_integration.runtime_data.coordinator.data.plant
    other: Any = dataclasses.replace(plant, preset=preset)
    assert setpoint_fix_preset(init_integration, other) is fix


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
