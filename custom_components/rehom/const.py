"""Constants for the Rehom integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Final

from aiorehom import Season, VmcMode
from homeassistant.const import Platform

DOMAIN: Final = "rehom"
MANUFACTURER: Final = "Rehom"
DEFAULT_TITLE: Final = "Rehom"
DEFAULT_HOST: Final = "rehomserver.local"
DEFAULT_PORT: Final = 8000

# Options (entry.options)
#: Off by default: the client is built read-only and every control action is refused.
#: Only an exact ``True`` enables control (a missing value, as on older entries, is off).
CONF_ENABLE_CONTROL: Final = "enable_control"
DEFAULT_ENABLE_CONTROL: Final = False
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

# Repair issues; issue_id = f"{key}_{entry_id}".  Only setpoint_mismatch can be
# fixable from Home Assistant (repairs.py), and only while "Enable control" is on.
ISSUE_INSTALLER_SESSION_ACTIVE: Final = "installer_session_active"
ISSUE_UNSUPPORTED_API: Final = "unsupported_api"
ISSUE_SEASON_MISMATCH: Final = "season_mismatch"
ISSUE_SETPOINT_MISMATCH: Final = "setpoint_mismatch"
#: Translation key of the ``setpoint_mismatch`` issue while it is fixable (same issue
#: id).  An issue text has either a description or a fix flow, never both.
ISSUE_SETPOINT_MISMATCH_FIXABLE: Final = "setpoint_mismatch_fixable"
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
# Control, refused or failed on the Home Assistant side (control.py)
EXC_NOT_VERIFIED: Final = "not_verified"
EXC_NOT_SUPPORTED: Final = "not_supported"
EXC_TEMPORARY_COMFORT_UNAVAILABLE: Final = "temporary_comfort_unavailable"
EXC_WRITE_NOT_CONFIRMED: Final = "write_not_confirmed"
EXC_WRITE_FAILED: Final = "write_failed"
#: Exception messages with placeholders: ``cannot_connect`` (``{host}``), ``write_refused``
#: (``{reason}``) and ``target_out_of_range`` (``{min}``, ``{max}``).
EXC_WRITE_REFUSED: Final = "write_refused"
#: A requested number that is not finite (NaN or infinity), refused before any rounding.
EXC_INVALID_VALUE: Final = "invalid_value"
#: A zone target outside the zone's level temperature ±3 °C; ``{min}`` and ``{max}``.
EXC_TARGET_OUT_OF_RANGE: Final = "target_out_of_range"
# Control, refused by the library before anything was sent (its stable reason codes)
EXC_CRONO_MODE: Final = "crono_mode"
EXC_BUS_DOWN: Final = "bus_down"
EXC_READ_ONLY: Final = "read_only"
EXC_ZONE_OFFLINE: Final = "zone_offline"
EXC_VMC_OFFLINE: Final = "vmc_offline"
EXC_VMC_BUSY: Final = "vmc_busy"
EXC_HOUSE_NOT_AUTO: Final = "house_not_auto"
EXC_SETPOINT_FORCED: Final = "setpoint_forced"
EXC_TEMPORARY_COMFORT_ACTIVE: Final = "temporary_comfort_active"
EXC_NO_ACTIVE_TARGET: Final = "no_active_target"
EXC_LEVEL_TEMPERATURE_INVALID: Final = "level_temperature_invalid"
EXC_SEASON_UNKNOWN: Final = "season_unknown"
EXC_FAN_NOT_WRITABLE: Final = "fan_not_writable"
EXC_FAN_LOCKED_BY_MODE: Final = "fan_locked_by_mode"
EXC_MODE_NOT_AVAILABLE: Final = "mode_not_available"

#: Library refusal reason (``RehomWriteRefusedError.reason``) -> exception translation key.
#: A user can meet these through Home Assistant's controls; each has its own message.
#: ``writes_disabled`` and ``unavailable`` are mapped separately (control.py).
REFUSAL_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "crono_mode": EXC_CRONO_MODE,
        "bus_down": EXC_BUS_DOWN,
        "read_only": EXC_READ_ONLY,
        "zone_offline": EXC_ZONE_OFFLINE,
        "vmc_offline": EXC_VMC_OFFLINE,
        "vmc_busy": EXC_VMC_BUSY,
        "house_not_auto": EXC_HOUSE_NOT_AUTO,
        "setpoint_forced": EXC_SETPOINT_FORCED,
        "temporary_comfort_active": EXC_TEMPORARY_COMFORT_ACTIVE,
        "no_active_target": EXC_NO_ACTIVE_TARGET,
        "level_temperature_unknown": EXC_LEVEL_TEMPERATURE_INVALID,
        "level_temperature_out_of_range": EXC_LEVEL_TEMPERATURE_INVALID,
        "season_unknown": EXC_SEASON_UNKNOWN,
        "fan_not_writable": EXC_FAN_NOT_WRITABLE,
        "fan_locked_by_mode": EXC_FAN_LOCKED_BY_MODE,
        "mode_not_available": EXC_MODE_NOT_AVAILABLE,
    }
)
#: The ``REFUSAL_KEYS`` reasons caused by the device, not by the request: the controller
#: lost its serial line, or the unit is not responding, in error or forced from elsewhere.
#: They raise ``HomeAssistantError``; every other refusal is caused by how Home Assistant
#: was used or by the plant's settings and raises ``ServiceValidationError``.
DEVICE_REFUSALS: Final[frozenset[str]] = frozenset(
    {"bus_down", "zone_offline", "vmc_offline", "vmc_busy"}
)
#: Library refusal reasons without a message of their own: they report ``write_refused``
#: with the reason as ``{reason}``.  Home Assistant's controls never produce them (wrong
#: argument types or ranges, unsupported values, absent units, and the temporary-comfort
#: and comfort-temperature writes this version does not use); any reason a later library
#: adds is reported the same way.
GENERIC_REFUSALS: Final[frozenset[str]] = frozenset(
    {
        "already_comfort",
        "crosses_midnight",
        "house_not_comfort",
        "invalid_duration",
        "invalid_fan_value",
        "invalid_offset",
        "invalid_temperature",
        "not_scheduled",
        "offset_out_of_range",
        "temperature_out_of_range",
        "unknown_vmc",
        "unknown_zone",
        "unsupported_preset",
        "unsupported_vmc_mode",
        "unsupported_zone_mode",
        "zone_not_scheduled",
    }
)

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

# VMC
#: Timed renewal and heating cycles: never offered (the library never writes them).
VMC_RAPID_MODES: Final[frozenset[VmcMode]] = frozenset({VmcMode.RAPID_RENEWAL, VmcMode.RAPID_HEAT})

# Climate preset names (HA side)
PRESET_PRE_COMFORT: Final = "pre_comfort"
PRESET_TEMPORARY_COMFORT: Final = "temporary_comfort"

# Event types of the alarm event entities
EVENT_ALARM_RAISED: Final = "raised"
EVENT_ALARM_CLEARED: Final = "cleared"
