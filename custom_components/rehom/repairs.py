"""Repairs platform: the fix flow of ``setpoint_mismatch``.

Home Assistant offers the fix only while the issue is fixable
(``issues.setpoint_fix_preset``: control enabled, the house at a level that
may be sent).  Confirming re-sends the house's current level through
``control.async_set_house_preset``, which also rewrites the controller's
setpoint to that level's temperature.  The issue is deleted once the
controller reports the new setpoint.

Nothing is sent, and the flow aborts with ``not_fixed``, when the fix is
refused: the entry is not loaded, control is off, the house is no longer at
the level the confirmation showed, or the controller cannot take the change
(a refusal, raised before sending).  When the level was sent but not
confirmed, or the request failed on the way, the flow aborts with
``not_confirmed``: it may have been applied, and the issue then closes by
itself.  Either way the issue is left as it is.
"""

from __future__ import annotations

import logging
from typing import Final

from aiorehom import MasterPreset
from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import voluptuous as vol

from . import control
from .const import DOMAIN, ISSUE_SETPOINT_MISMATCH
from .coordinator import RehomConfigEntry
from .issues import (
    FIX_DATA_ENTRY_ID,
    issue_id as entry_issue_id,
    setpoint_fix_preset,
    setpoint_placeholders,
)

_LOGGER = logging.getLogger(__name__)

#: Abort reason of a fix that was refused: nothing was sent.
ABORT_NOT_FIXED: Final = "not_fixed"
#: Abort reason of a fix that was sent but not confirmed (or failed on the way):
#: it may have been applied.
ABORT_NOT_CONFIRMED: Final = "not_confirmed"


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """The fix flow of a fixable issue (only ``setpoint_mismatch`` is ever fixable)."""
    entry_id = (data or {}).get(FIX_DATA_ENTRY_ID)
    if isinstance(entry_id, str) and issue_id == entry_issue_id(ISSUE_SETPOINT_MISMATCH, entry_id):
        return SetpointMismatchRepairFlow(entry_id)
    return ConfirmRepairFlow()


class SetpointMismatchRepairFlow(RepairsFlow):
    """Re-send the house level, which rewrites the controller's setpoint."""

    def __init__(self, entry_id: str) -> None:
        """Fix the issue of the config entry ``entry_id``."""
        self._entry_id = entry_id
        #: The level the confirmation form showed (the only one confirming may send).
        self._shown_preset: MasterPreset | None = None

    def _loaded_entry(self) -> RehomConfigEntry | None:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            return None
        return entry

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Start with the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Show the mismatch; on confirmation re-send the level (checked again now).

        The level sent is the one the form showed: if the house moved to
        another level since, nothing is sent (``not_fixed``).
        """
        entry = self._loaded_entry()
        if entry is None:
            return self.async_abort(reason=ABORT_NOT_FIXED)
        plant = entry.runtime_data.coordinator.data.plant
        preset = setpoint_fix_preset(entry, plant)
        if preset is None:  # control turned off, or the house moved to a level not sent
            return self.async_abort(reason=ABORT_NOT_FIXED)
        if user_input is None:
            self._shown_preset = preset
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=setpoint_placeholders(plant),
            )
        if preset is not self._shown_preset:  # the house moved to another level
            return self.async_abort(reason=ABORT_NOT_FIXED)
        try:
            await control.async_set_house_preset(entry, preset)
        except HomeAssistantError as err:
            reason = err.translation_key or type(err).__name__
            if control.may_have_been_applied(err):
                _LOGGER.warning("Setpoint fix sent but not confirmed: %s", reason)
                return self.async_abort(reason=ABORT_NOT_CONFIRMED)
            # A refusal (nothing sent): a device-side one is worth a warning.
            level = logging.DEBUG if isinstance(err, ServiceValidationError) else logging.WARNING
            _LOGGER.log(level, "Setpoint fix refused, nothing sent: %s", reason)
            return self.async_abort(reason=ABORT_NOT_FIXED)
        return self.async_create_entry(data={})
