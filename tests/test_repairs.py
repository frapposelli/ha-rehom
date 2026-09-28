"""The setpoint_mismatch fix flow (repairs.py).

The fix re-sends the house's level, which rewrites the controller's setpoint to
that level's temperature.  It is offered only with "Enable control" on and a
level Home Assistant may send (economy or comfort; pre-comfort is never sent
to the house).  The flow runs through Home Assistant's repairs flow manager,
on the replayed controller.

A refusal (nothing sent) aborts with ``not_fixed``; a level sent but not
confirmed, or a request that failed on the way, aborts with ``not_confirmed``
(it may have been applied).  The reason is logged by its translation key:
at debug level for a refusal caused by use or state, as a warning otherwise.

The capture's house is MANUAL/COMFORT regulating to 26 °C instead of 24 °C;
:data:`ECO_MISMATCH` selects economy instead (26 °C instead of 29 °C).
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

from aiorehom import RehomTimeoutError
from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlowResult,
    repairs_flow_manager,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownStep
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rehom import control, repairs
from custom_components.rehom.const import (
    CONF_ENABLE_CONTROL,
    CONF_TEMPORARY_COMFORT_DURATION,
    DOMAIN,
    ISSUE_SEASON_MISMATCH,
    ISSUE_SETPOINT_MISMATCH,
)
from custom_components.rehom.issues import issue_id
from custom_components.rehom.repairs import SetpointMismatchRepairFlow

from .conftest import CONTROL_OPTIONS
from .harness import RehomHarness, T, set_values, termo_update

#: The house MANUAL at economy (29 °C) while the controller regulates to 26 °C.
ECO_MISMATCH = set_values({"REHOM...SET_POINT": "1"})
#: The pre-comfort temperature 25 °C (26 °C in the capture, the regulated setpoint).
PRE_COMFORT_25 = {"REHOM...TEMP_PRE": "25"}
#: The house MANUAL at pre-comfort (25 °C) while the controller regulates to 26 °C.
PRE_COMFORT_MISMATCH = set_values({**PRE_COMFORT_25, "REHOM...SET_POINT": "2"})
#: Records that fix it: economy again, with its temperature as the setpoint.
HOUSE_ECO = {"REHOM...MODO": "1", "REHOM...SET_POINT": "1", "REHOM...SET_POINT_TEMP": "29"}
#: Records that fix the capture's own mismatch: comfort again, 24 °C.
HOUSE_COMFORT = {"REHOM...MODO": "1", "REHOM...SET_POINT": "3", "REHOM...SET_POINT_TEMP": "24"}
#: The issue has matured (5 minutes after the capture start).
MATURE = T("10:28:00")
#: The logger of repairs.py.
LOGGER = "custom_components.rehom.repairs"


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """ "Enable control" on."""
    return dict(CONTROL_OPTIONS)


@pytest.fixture
def platforms() -> list[Platform]:
    """The fix needs no entity platform."""
    return []


@pytest.fixture
async def repairs_setup(hass: HomeAssistant) -> None:
    """Home Assistant's repairs integration (it loads rehom's repairs platform)."""
    assert await async_setup_component(hass, "repairs", {})


def _logged(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """(level, message) of every record repairs.py logged."""
    return [
        (record.levelno, record.getMessage()) for record in caplog.records if record.name == LOGGER
    ]


def _issue(issue_registry: ir.IssueRegistry, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return issue_registry.async_get_issue(DOMAIN, issue_id(ISSUE_SETPOINT_MISMATCH, entry.entry_id))


async def _start(hass: HomeAssistant, entry: MockConfigEntry) -> RepairsFlowResult:
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_init(
        DOMAIN, data={"issue_id": issue_id(ISSUE_SETPOINT_MISMATCH, entry.entry_id)}
    )


async def _confirm(hass: HomeAssistant, flow_id: str) -> RepairsFlowResult:
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_configure(flow_id, {})


async def _fixable_issue(
    harness: RehomHarness, issue_registry: ir.IssueRegistry, entry: MockConfigEntry
) -> ir.IssueEntry:
    await harness.advance_to(MATURE)
    issue = _issue(issue_registry, entry)
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": entry.entry_id}
    return issue


@pytest.mark.parametrize("device_patch", [ECO_MISMATCH])
async def test_fix(
    hass: HomeAssistant,
    harness: RehomHarness,
    repairs_setup: None,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Economy regulating to 26 °C: the fix re-sends economy with 29 °C; the issue goes."""
    entry = init_integration
    await _fixable_issue(harness, issue_registry, entry)

    result = await _start(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"setpoint": "26", "level_temperature": "29"}
    assert harness.writes == []  # nothing is sent before the confirmation

    result = await _confirm(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert harness.written_values == [HOUSE_ECO]
    plant = entry.runtime_data.coordinator.data.plant
    assert plant.setpoint_mismatch is False
    assert plant.controller_setpoint == 29.0
    assert _issue(issue_registry, entry) is None
    await harness.advance(120)  # and it stays deleted
    assert _issue(issue_registry, entry) is None


async def test_fix_comfort(
    hass: HomeAssistant,
    harness: RehomHarness,
    repairs_setup: None,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The capture's own mismatch: comfort (24 °C) regulating to 26 °C, fixed by comfort again."""
    entry = init_integration
    await _fixable_issue(harness, issue_registry, entry)
    result = await _start(hass, entry)
    assert result["description_placeholders"] == {"setpoint": "26", "level_temperature": "24"}
    result = await _confirm(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert harness.written_values == [HOUSE_COMFORT]
    plant = entry.runtime_data.coordinator.data.plant
    assert plant.setpoint_mismatch is False
    assert plant.controller_setpoint == 24.0
    assert _issue(issue_registry, entry) is None


@pytest.mark.parametrize("device_patch", [ECO_MISMATCH])
async def test_fix_not_confirmed(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    repairs_setup: None,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sent but never reported back: not_confirmed (it may have been applied); the issue stays."""
    entry = init_integration
    await _fixable_issue(harness, issue_registry, entry)
    result = await _start(hass, entry)
    harness.echo = False
    harness.apply = False

    result = await harness.run_until_done(_confirm(hass, result["flow_id"]))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_confirmed"
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert harness.written_values == [HOUSE_ECO]
    assert entry.runtime_data.coordinator.data.plant.setpoint_mismatch is True
    assert _issue(issue_registry, entry) is not None
    assert _logged(caplog) == [
        (logging.WARNING, "Setpoint fix sent but not confirmed: write_not_confirmed")
    ]


@pytest.mark.parametrize("device_patch", [ECO_MISMATCH])
async def test_fix_failed(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    repairs_setup: None,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The request failed on the way (it may or may not have landed): not_confirmed."""
    entry = init_integration
    await _fixable_issue(harness, issue_registry, entry)
    result = await _start(hass, entry)
    harness.errors["post_bulk_update"] = RehomTimeoutError("replay: POST timed out")
    result = await _confirm(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_confirmed"
    assert _issue(issue_registry, entry) is not None
    assert _logged(caplog) == [
        (logging.WARNING, "Setpoint fix sent but not confirmed: write_failed")
    ]


@pytest.mark.parametrize(
    ("device_patch", "entry_options"),
    [
        (PRE_COMFORT_MISMATCH, CONTROL_OPTIONS),  # pre-comfort: never sent to the house
        (ECO_MISMATCH, {CONF_TEMPORARY_COMFORT_DURATION: 2.0}),  # control off
        (ECO_MISMATCH, {**CONTROL_OPTIONS, CONF_ENABLE_CONTROL: False}),
    ],
    ids=["pre_comfort", "control_missing", "control_off"],
)
async def test_not_fixable(
    hass: HomeAssistant,
    harness: RehomHarness,
    repairs_setup: None,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The issue is raised but offers no fix, and no fix flow can start."""
    await harness.advance_to(MATURE)
    issue = _issue(issue_registry, init_integration)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.data is None
    with pytest.raises(UnknownStep):
        await _start(hass, init_integration)
    assert harness.writes == []


@pytest.mark.parametrize("device_patch", [ECO_MISMATCH])
async def test_control_turned_off_before_confirming(
    hass: HomeAssistant,
    harness: RehomHarness,
    repairs_setup: None,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The option is turned off while the flow is open: not_fixed, nothing sent."""
    entry = init_integration
    await _fixable_issue(harness, issue_registry, entry)
    result = await _start(hass, entry)
    hass.config_entries.async_update_entry(
        entry, options={**CONTROL_OPTIONS, CONF_ENABLE_CONTROL: False}
    )
    result = await _confirm(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_fixed"
    assert harness.writes == []


@pytest.mark.parametrize(
    ("device_patch", "still_fixable", "level_temperature"),
    [
        # comfort may be sent too, but the form showed economy: nothing is sent
        (
            set_values(
                {"REHOM...SET_POINT": "1"},
                extra_frames=[(T("10:28:30"), termo_update("REHOM...SET_POINT", "3"))],
            ),
            True,
            "24",
        ),
        # pre-comfort is never sent: the issue is no longer fixable
        (
            set_values(
                {**PRE_COMFORT_25, "REHOM...SET_POINT": "1"},
                extra_frames=[(T("10:28:30"), termo_update("REHOM...SET_POINT", "2"))],
            ),
            False,
            "25",
        ),
    ],
    ids=["to_comfort", "to_pre_comfort"],
)
async def test_level_changed_before_confirming(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    repairs_setup: None,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    still_fixable: bool,
    level_temperature: str,
) -> None:
    """The house moved to another level while the form showed economy: not_fixed, nothing sent."""
    entry = init_integration
    await _fixable_issue(harness, issue_registry, entry)
    result = await _start(hass, entry)
    assert result["description_placeholders"] == {"setpoint": "26", "level_temperature": "29"}
    await harness.advance_to(T("10:28:35"))
    issue = _issue(issue_registry, entry)
    assert issue is not None
    assert issue.is_fixable is still_fixable  # re-evaluated on the update
    assert issue.translation_placeholders == {
        "setpoint": "26",
        "level_temperature": level_temperature,
    }
    result = await _confirm(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_fixed"
    assert harness.writes == []


@pytest.mark.parametrize(
    "device_patch",
    [
        set_values(
            {"REHOM...SET_POINT": "1"},
            extra_frames=[(T("10:28:30"), termo_update("REHOM...TERMO_READONLY", "1"))],
        )
    ],
)
async def test_refused_by_the_controller_rules(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    repairs_setup: None,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The controller went read-only while the flow was open: not_fixed, nothing sent."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    entry = init_integration
    await _fixable_issue(harness, issue_registry, entry)
    result = await _start(hass, entry)
    await harness.advance_to(T("10:28:35"))
    result = await _confirm(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_fixed"
    assert harness.writes == []
    assert _issue(issue_registry, entry) is not None
    # a refusal caused by the plant's state (a ServiceValidationError): debug only
    assert _logged(caplog) == [(logging.DEBUG, "Setpoint fix refused, nothing sent: read_only")]


@pytest.mark.parametrize(
    "device_patch",
    [
        set_values(
            {"REHOM...SET_POINT": "1"},
            extra_frames=[(T("10:28:30"), termo_update("REHOM...WEBSERVER", "2"))],
        )
    ],
)
async def test_refused_by_the_device(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    repairs_setup: None,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The serial line went down while the flow was open: not_fixed (nothing was sent).

    bus_down is a device refusal (a HomeAssistantError, logged as a warning), but
    nothing reached the controller, so the abort is not_fixed, not not_confirmed.
    """
    entry = init_integration
    await _fixable_issue(harness, issue_registry, entry)
    result = await _start(hass, entry)
    await harness.advance_to(T("10:28:35"))
    result = await _confirm(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_fixed"
    assert harness.writes == []
    assert _logged(caplog) == [(logging.WARNING, "Setpoint fix refused, nothing sent: bus_down")]


@pytest.mark.parametrize("device_patch", [ECO_MISMATCH])
async def test_entry_unloaded_before_confirming(
    hass: HomeAssistant,
    harness: RehomHarness,
    repairs_setup: None,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The entry was unloaded while the flow was open: not_fixed (and the issue is gone)."""
    entry = init_integration
    await _fixable_issue(harness, issue_registry, entry)
    result = await _start(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    result = await _confirm(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_fixed"
    assert harness.writes == []
    assert _issue(issue_registry, entry) is None


async def test_create_fix_flow(hass: HomeAssistant) -> None:
    """Only this entry's setpoint_mismatch gets the fix; anything else a plain confirmation."""
    entry_id = "01ABC"
    data = {"entry_id": entry_id}
    flow = await repairs.async_create_fix_flow(
        hass, issue_id(ISSUE_SETPOINT_MISMATCH, entry_id), data
    )
    assert isinstance(flow, SetpointMismatchRepairFlow)
    for other_id, other_data in (
        (issue_id(ISSUE_SEASON_MISMATCH, entry_id), data),
        (issue_id(ISSUE_SETPOINT_MISMATCH, "other"), data),
        (issue_id(ISSUE_SETPOINT_MISMATCH, entry_id), None),
        (issue_id(ISSUE_SETPOINT_MISMATCH, entry_id), {"entry_id": 1}),
    ):
        flow = await repairs.async_create_fix_flow(hass, other_id, other_data)
        assert type(flow) is ConfirmRepairFlow


async def test_flow_for_a_missing_entry(hass: HomeAssistant) -> None:
    """A flow whose entry no longer exists aborts at once."""
    flow = SetpointMismatchRepairFlow("missing")
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_fixed"


@pytest.mark.parametrize("device_patch", [ECO_MISMATCH])
async def test_confirmed_without_the_form(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
) -> None:
    """A confirmation that no form showed (only possible directly) sends nothing."""
    flow = SetpointMismatchRepairFlow(init_integration.entry_id)
    flow.hass = hass
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_fixed"
    assert harness.writes == []


@pytest.mark.parametrize(
    ("error", "reason", "level", "logged"),
    [
        # a foreign error without a translation key: may have been applied
        (HomeAssistantError("boom"), "not_confirmed", logging.WARNING, "HomeAssistantError"),
        # a use-caused refusal without a key: nothing was sent
        (ServiceValidationError("no"), "not_fixed", logging.DEBUG, "ServiceValidationError"),
    ],
    ids=["foreign", "untranslated_refusal"],
)
@pytest.mark.parametrize("device_patch", [ECO_MISMATCH])
async def test_untranslated_errors(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    repairs_setup: None,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
    error: HomeAssistantError,
    reason: str,
    level: int,
    logged: str,
) -> None:
    """An error without a translation key is logged by its class name."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    entry = init_integration
    await _fixable_issue(harness, issue_registry, entry)
    result = await _start(hass, entry)
    with patch.object(control, "async_set_house_preset", side_effect=error):
        result = await _confirm(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    [(logged_level, message)] = _logged(caplog)
    assert logged_level == level
    assert message.endswith(f": {logged}")
    assert harness.writes == []
