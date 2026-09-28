"""Repair issues: when each one is raised and deleted.

Only ``setpoint_mismatch`` can be fixable, and only while "Enable control" is
on and the house is at a level Home Assistant may send
(:func:`setpoint_fix_preset`); its fix flow is in ``repairs.py``, the repairs
platform Home Assistant loads.  While fixable, the issue keeps its id and takes
the ``setpoint_mismatch_fixable`` texts (the fix flow).  The other issues are
never fixable from Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

from aiorehom import MasterPreset, Plant
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    INSTALLER_SESSION_GRACE,
    ISSUE_CHECK_INTERVAL,
    ISSUE_INSTALLER_SESSION_ACTIVE,
    ISSUE_SEASON_MISMATCH,
    ISSUE_SETPOINT_MISMATCH,
    ISSUE_SETPOINT_MISMATCH_FIXABLE,
    ISSUE_UNSUPPORTED_API,
    ISSUE_UNSUPPORTED_API_SETUP,
    SEASON_MISMATCH_GRACE,
    SETPOINT_MISMATCH_GRACE,
    UNSUPPORTED_API_MIN_FAILURES,
)
from .control import ControlOp, ensure_control_enabled, is_verified
from .coordinator import RehomConfigEntry, RehomCoordinator

#: Issues derived from the live state (deleted on unload).
STATE_ISSUES: tuple[str, ...] = (
    ISSUE_INSTALLER_SESSION_ACTIVE,
    ISSUE_SEASON_MISMATCH,
    ISSUE_SETPOINT_MISMATCH,
    ISSUE_UNSUPPORTED_API,
)


def issue_id(key: str, entry_id: str) -> str:
    """Issue ids are per config entry."""
    return f"{key}_{entry_id}"


#: Key of the config entry id in a fixable issue's ``data`` (for its fix flow).
FIX_DATA_ENTRY_ID: Final = "entry_id"
#: Issue key -> its translation key while it is fixable.  An issue text has either a
#: description or a fix flow (Home Assistant's schema), so the fixable variant has its own.
FIXABLE_TRANSLATION_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {ISSUE_SETPOINT_MISMATCH: ISSUE_SETPOINT_MISMATCH_FIXABLE}
)
#: House presets that select a level (MANUAL): re-sending one rewrites the setpoint.
LEVEL_PRESETS: Final = frozenset(
    {MasterPreset.ECONOMY, MasterPreset.PRE_COMFORT, MasterPreset.COMFORT}
)


def _fmt(value: float | None) -> str:
    return "?" if value is None else f"{value:g}"


def setpoint_placeholders(plant: Plant) -> dict[str, str]:
    """``{setpoint}`` and ``{level_temperature}`` of the setpoint_mismatch texts."""
    return {
        "setpoint": _fmt(plant.controller_setpoint),
        "level_temperature": _fmt(plant.display_temperature),
    }


def setpoint_fix_preset(entry: RehomConfigEntry, plant: Plant) -> MasterPreset | None:
    """The house level whose re-sending fixes ``setpoint_mismatch``, or ``None``.

    Sending a MANUAL level always rewrites the controller's setpoint to that
    level's temperature.  Home Assistant may do it only while control is
    enabled (``control.ensure_control_enabled``) and for a level tested on a
    real controller (``control.is_verified``).
    """
    try:
        ensure_control_enabled(entry)
    except HomeAssistantError:
        return None
    preset = plant.preset
    if preset not in LEVEL_PRESETS or not is_verified(ControlOp.HOUSE_PRESET, preset):
        return None
    return preset


@callback
def async_create_unsupported_api_issue(
    hass: HomeAssistant, entry_id: str, version: str | None
) -> None:
    """Raise ``unsupported_api`` at runtime (repeated resync parse failures).

    The integration keeps serving the last good state, as the text says.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(ISSUE_UNSUPPORTED_API, entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_UNSUPPORTED_API,
        translation_placeholders={"version": version or "?"},
    )


@callback
def async_create_unsupported_api_setup_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Raise ``unsupported_api`` at setup: the entry could not be set up at all.

    Same issue id as the runtime case (one issue per entry, deleted by a good
    setup); its own text, because there is no state and no automatic retry.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(ISSUE_UNSUPPORTED_API, entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_UNSUPPORTED_API_SETUP,
    )


@callback
def async_delete_issues(hass: HomeAssistant, entry_id: str, keys: tuple[str, ...]) -> None:
    """Delete this entry's issues ``keys`` (missing ones are ignored)."""
    for key in keys:
        ir.async_delete_issue(hass, DOMAIN, issue_id(key, entry_id))


class RehomIssueTracker:
    """Evaluates the time-qualified issue rules on every update and every minute.

    Time is Home Assistant's ``dt_util.utcnow()``.  ``setpoint_mismatch`` uses
    the library's ``Plant.setpoint_mismatch_since``; the tracker keeps its own
    "since" for the installer session and the season mismatch.

    A grace only counts time in which the controller was observed: while the
    coordinator is unavailable the state is stale, so the time-qualified rules
    are not evaluated (existing issues are kept, none matures) and after the
    outage every grace starts again from the recovery (``_observed_since``).
    """

    def __init__(
        self, hass: HomeAssistant, entry: RehomConfigEntry, coordinator: RehomCoordinator
    ) -> None:
        """Bind to one entry; call :meth:`async_start` once the coordinator has data."""
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._installer_since: datetime | None = None
        self._season_since: datetime | None = None
        #: Start of the current stretch with live data (``None`` during an outage).
        self._observed_since: datetime | None = None

    @callback
    def async_start(self) -> None:
        """Evaluate now, on every coordinator update and every ISSUE_CHECK_INTERVAL."""
        entry = self._entry
        entry.async_on_unload(self._coordinator.async_add_listener(self._async_on_update))
        entry.async_on_unload(
            async_track_time_interval(self._hass, self._async_on_tick, ISSUE_CHECK_INTERVAL)
        )
        entry.async_on_unload(self.async_clear)
        self.async_evaluate()

    @callback
    def _async_on_update(self) -> None:
        self.async_evaluate()

    @callback
    def _async_on_tick(self, now: datetime) -> None:
        self.async_evaluate(now)

    @callback
    def async_clear(self) -> None:
        """Delete every state-derived issue of this entry (on unload)."""
        async_delete_issues(self._hass, self._entry.entry_id, STATE_ISSUES)

    def _set(
        self,
        key: str,
        active: bool,
        placeholders: dict[str, str] | None = None,
        *,
        fixable: bool = False,
    ) -> None:
        """Create (or update) the issue ``key`` while ``active``, else delete it.

        A fixable issue keeps its issue id and uses its fixable translation key
        (:data:`FIXABLE_TRANSLATION_KEYS`), whose text is the fix flow.
        """
        if active:
            translation_key = FIXABLE_TRANSLATION_KEYS[key] if fixable else key
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id(key, self._entry.entry_id),
                data={FIX_DATA_ENTRY_ID: self._entry.entry_id} if fixable else None,
                is_fixable=fixable,
                severity=ir.IssueSeverity.WARNING,
                translation_key=translation_key,
                translation_placeholders=placeholders,
            )
        else:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id(key, self._entry.entry_id))

    @staticmethod
    def _since(since: datetime | None, now: datetime, condition: bool | None) -> datetime | None:
        if condition is True:
            return since or now
        return None

    def _mature(self, since: datetime | None, now: datetime, grace: timedelta) -> bool:
        """``since`` is ``grace`` old, counting only time observed since the last outage."""
        if since is None or self._observed_since is None:
            return False
        return now - max(since, self._observed_since) >= grace

    @callback
    def async_evaluate(self, now: datetime | None = None) -> None:
        """Create or delete each issue according to its rule."""
        now = now or dt_util.utcnow()
        self._async_evaluate_unsupported_api()
        if not self._coordinator.last_update_success:
            # Stale data: keep the issues as they are, mature nothing, and start
            # every grace again once the controller is back.
            self._observed_since = None
            self._installer_since = None
            self._season_since = None
            return
        if self._observed_since is None:
            self._observed_since = now
        plant = self._coordinator.data.plant

        # setpoint_mismatch: MANUAL and SET_POINT_TEMP != level temperature for > 5 min;
        # fixable while Home Assistant may re-send the level (checked on every evaluation)
        if plant.setpoint_mismatch is False:
            self._set(ISSUE_SETPOINT_MISMATCH, False)
        elif plant.setpoint_mismatch is True and self._mature(
            plant.setpoint_mismatch_since, now, SETPOINT_MISMATCH_GRACE
        ):
            self._set(
                ISSUE_SETPOINT_MISMATCH,
                True,
                setpoint_placeholders(plant),
                fixable=setpoint_fix_preset(self._entry, plant) is not None,
            )

        # installer_session_active: a plant-conf session flag = 1 for > 15 min
        self._installer_since = self._since(self._installer_since, now, plant.installer_session)
        if plant.installer_session is False:
            self._set(ISSUE_INSTALLER_SESSION_ACTIVE, False)
        elif self._mature(self._installer_since, now, INSTALLER_SESSION_GRACE):
            self._set(ISSUE_INSTALLER_SESSION_ACTIVE, True)

        # season_mismatch: REHOM.STAGIONE != plant-conf stagione for > 15 min
        known = plant.season is not None and plant.conf_season is not None
        differs = known and plant.season != plant.conf_season
        self._season_since = self._since(self._season_since, now, differs)
        if known and not differs:
            self._set(ISSUE_SEASON_MISMATCH, False)
        elif self._mature(self._season_since, now, SEASON_MISMATCH_GRACE):
            self._set(ISSUE_SEASON_MISMATCH, True)

    @callback
    def _async_evaluate_unsupported_api(self) -> None:
        """Runtime ``unsupported_api``: consecutive resyncs failing to parse.

        Evaluated during an outage too: it is about the client's resyncs, not
        about a condition held over time in the (possibly stale) state.
        """
        stats = self._coordinator.client.stats
        if stats.sync_failure_streak == 0:
            self._set(ISSUE_UNSUPPORTED_API, False)
        elif (
            stats.last_sync_error == "RehomResponseError"
            and stats.sync_failure_streak >= UNSUPPORTED_API_MIN_FAILURES
        ):
            async_create_unsupported_api_issue(
                self._hass, self._entry.entry_id, self._coordinator.data.hub.web_version
            )
