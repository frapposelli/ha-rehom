"""Constants for the Rehom integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from aiorehom import Season
from homeassistant.const import Platform

DOMAIN: Final = "rehom"
MANUFACTURER: Final = "Rehom"
DEFAULT_TITLE: Final = "Rehom"
DEFAULT_HOST: Final = "rehomserver.local"
DEFAULT_PORT: Final = 8000

# Options (entry.options)
CONF_TEMPORARY_COMFORT_DURATION: Final = "temporary_comfort_duration"
DEFAULT_TEMPORARY_COMFORT_DURATION: Final = 2.0
MIN_TEMPORARY_COMFORT_DURATION: Final = 0.5
MAX_TEMPORARY_COMFORT_DURATION: Final = 24.0
TEMPORARY_COMFORT_DURATION_STEP: Final = 0.5

PLATFORMS: Final[list[Platform]] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.EVENT,
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Actions (services)
SERVICE_GET_SCHEDULE: Final = "get_schedule"
SERVICE_SET_TEMPORARY_COMFORT: Final = "set_temporary_comfort"
SERVICE_CLEAR_TEMPORARY_COMFORT: Final = "clear_temporary_comfort"
ATTR_DURATION: Final = "duration"

# Repair issues (not fixable from Home Assistant); issue_id = f"{key}_{entry_id}"
ISSUE_INSTALLER_SESSION_ACTIVE: Final = "installer_session_active"
ISSUE_UNSUPPORTED_API: Final = "unsupported_api"
ISSUE_SEASON_MISMATCH: Final = "season_mismatch"
ISSUE_SETPOINT_MISMATCH: Final = "setpoint_mismatch"
#: Translation key of the ``unsupported_api`` issue raised at setup (same issue id).
ISSUE_UNSUPPORTED_API_SETUP: Final = "unsupported_api_setup"
SETPOINT_MISMATCH_GRACE: Final = timedelta(minutes=5)
INSTALLER_SESSION_GRACE: Final = timedelta(minutes=15)
SEASON_MISMATCH_GRACE: Final = timedelta(minutes=15)
UNSUPPORTED_API_MIN_FAILURES: Final = 2
ISSUE_CHECK_INTERVAL: Final = timedelta(seconds=60)

# Exception translation keys (strings.json "exceptions")
EXC_CONTROL_DISABLED: Final = "control_disabled"
EXC_ZONE_ONLY: Final = "zone_only"
EXC_NOT_READY: Final = "not_ready"
EXC_CANNOT_CONNECT: Final = "cannot_connect"
EXC_INVALID_AUTH: Final = "invalid_auth"
EXC_UNSUPPORTED_API: Final = "unsupported_api"
EXC_WRONG_DEVICE: Final = "wrong_device"
EXC_UNAVAILABLE: Final = "unavailable"

# Climate
#: Whole-house comfort clamps per season, used as min/max of the house climate.
HOUSE_TEMPERATURE_RANGE: Final[dict[Season, tuple[float, float]]] = {
    Season.WINTER: (16.0, 28.5),
    Season.SUMMER: (18.0, 35.5),
}
HOUSE_TEMPERATURE_RANGE_UNKNOWN: Final = (16.0, 35.5)
HOUSE_TEMPERATURE_STEP: Final = 0.1
#: The zone whose probe is the house temperature (the web UI's "T IN";
#: ``Plant.current_temperature`` is that zone's temperature).
HOUSE_TEMPERATURE_ZONE: Final = "001"
ZONE_OFFSET_MIN: Final = -3.0
ZONE_OFFSET_MAX: Final = 3.0
ZONE_TEMPERATURE_STEP: Final = 1.0

# Climate preset names (HA side)
PRESET_PRE_COMFORT: Final = "pre_comfort"
PRESET_TEMPORARY_COMFORT: Final = "temporary_comfort"

# Event types of the alarm event entities
EVENT_ALARM_RAISED: Final = "raised"
EVENT_ALARM_CLEARED: Final = "cleared"
