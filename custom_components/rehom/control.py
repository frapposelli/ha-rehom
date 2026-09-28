"""Control: the only module that writes to the Rehom controller.

Every Home Assistant control (climate, fan, select, number, switch and the
``rehom`` actions) ends in one of the ``async_set_*`` functions below; nothing
else calls the client's write methods.  Each function:

1. refuses with ``control_disabled`` unless "Enable control" is on
   (:func:`ensure_control_enabled`), before anything touches the client;
2. refuses with ``not_verified`` a value outside :data:`VERIFIED_VALUES`
   (only values tested on a real controller are sent; every function checks
   its own entry of the table);
3. calls the library, which plans the write against the latest state, checks
   its own guards, sends it once and waits until the controller reports it;
4. turns every library outcome into a translated Home Assistant error:
   a refusal caused by the request or by the plant's settings is a
   ``ServiceValidationError``; a refusal caused by the device
   (``DEVICE_REFUSALS``: ``bus_down``, ``zone_offline``, ``vmc_offline``,
   ``vmc_busy``), an unreachable or unready controller, and a write that was
   sent but not confirmed, or that failed on the way, is a
   ``HomeAssistantError``.

A platform that must refuse on its own (``not_supported``,
``temporary_comfort_unavailable``, ``zone_only``, ``invalid_value``,
``target_out_of_range``) calls :func:`ensure_control_enabled` first and then
the ``raise_*`` helper, so a disabled entry always answers
``control_disabled``.  The zone offset is computed in one place for the zone
climate and the offset number (:func:`round_offset`,
:func:`zone_offset_for_target`).

The library returning ``False`` (the controller already reports the requested
values, so nothing was sent) is a success.  State is confirmed, never
optimistic: when a function returns, the coordinator has already received the
state that confirms the write.  There is no timeout or cancellation here: a
cancelled write may still land, and the library runs writes one at a time, in
call order.  While the live connection is down a call can take minutes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from enum import StrEnum
import logging
import math
from types import MappingProxyType
from typing import Final, NoReturn

from aiorehom import (
    FanKind,
    FanSpeed,
    ForbiddenRequestError,
    MasterPreset,
    RehomAuthenticationError,
    RehomError,
    RehomNotReadyError,
    RehomWriteNotConfirmedError,
    RehomWriteRefusedError,
    VmcMode,
    ZoneSetp,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import (
    CONF_ENABLE_CONTROL,
    DEVICE_REFUSALS,
    DOMAIN,
    EXC_CONTROL_DISABLED,
    EXC_INVALID_AUTH,
    EXC_INVALID_VALUE,
    EXC_NOT_READY,
    EXC_NOT_SUPPORTED,
    EXC_NOT_VERIFIED,
    EXC_TARGET_OUT_OF_RANGE,
    EXC_TEMPORARY_COMFORT_UNAVAILABLE,
    EXC_UNAVAILABLE,
    EXC_WRITE_FAILED,
    EXC_WRITE_NOT_CONFIRMED,
    EXC_WRITE_REFUSED,
    REFUSAL_KEYS,
    ZONE_OFFSET_MAX,
    ZONE_OFFSET_MIN,
)
from .coordinator import RehomConfigEntry, RehomRuntimeData

__all__ = [
    "VERIFIED_VALUES",
    "ControlOp",
    "async_set_house_preset",
    "async_set_predictive",
    "async_set_vmc_fan",
    "async_set_vmc_mode",
    "async_set_zone_mode",
    "async_set_zone_offset",
    "ensure_control_enabled",
    "ensure_finite",
    "is_verified",
    "may_have_been_applied",
    "raise_invalid_value",
    "raise_not_supported",
    "raise_not_verified",
    "raise_refused",
    "raise_target_out_of_range",
    "raise_temporary_comfort_unavailable",
    "round_offset",
    "zone_offset_for_target",
]

_LOGGER = logging.getLogger(__name__)

#: Library refusal reasons with a mapping of their own (not in ``REFUSAL_KEYS``).
REASON_WRITES_DISABLED: Final = "writes_disabled"  # the client was built read-only
REASON_UNAVAILABLE: Final = "unavailable"  # the controller is not reachable
#: ``{reason}`` of ``write_refused`` when the library's write gate refuses a body (a bug).
REASON_FORBIDDEN: Final = "forbidden_request"
#: A zone without a level temperature: its target cannot be set (``no_active_target``).
REASON_NO_ACTIVE_TARGET: Final = "no_active_target"

#: The whole-degree offset range of a zone (the library refuses anything else).
OFFSET_MIN: Final = int(ZONE_OFFSET_MIN)
OFFSET_MAX: Final = int(ZONE_OFFSET_MAX)

#: Float noise allowed at the ends of a zone's target range (``16.1 - 3`` is
#: ``13.100000000000001``); round_offset's clamp absorbs it.
_RANGE_TOLERANCE: Final = 1e-6

#: Errors raised after the write may have reached the controller (see may_have_been_applied).
_MAYBE_APPLIED_KEYS: Final = frozenset({EXC_WRITE_NOT_CONFIRMED, EXC_WRITE_FAILED})


class ControlOp(StrEnum):
    """A kind of write, one per ``async_set_*`` function (the keys of VERIFIED_VALUES)."""

    HOUSE_PRESET = "house_preset"
    ZONE_OFFSET = "zone_offset"
    ZONE_MODE = "zone_mode"
    VMC_FAN = "vmc_fan"
    VMC_MODE = "vmc_mode"
    PREDICTIVE = "predictive"


type VerifiedValue = MasterPreset | ZoneSetp | FanSpeed | VmcMode | int | bool

#: The values each write may send: those tested on a real controller.  Every
#: operation lists its values explicitly and its ``async_set_*`` function checks
#: them (:func:`is_verified`) before the library is called.  Widening a set is a
#: one-line change here, plus the pinned copy in ``tests/test_control.py``.
VERIFIED_VALUES: Final[Mapping[ControlOp, frozenset[VerifiedValue]]] = MappingProxyType(
    {
        # PRE_COMFORT and OFF are never sent to the house.
        ControlOp.HOUSE_PRESET: frozenset(
            {MasterPreset.AUTO, MasterPreset.ECONOMY, MasterPreset.COMFORT}
        ),
        # Whole degrees only.
        ControlOp.ZONE_OFFSET: frozenset({-3, -2, -1, 0, 1, 2, 3}),
        # OFF and PROBE_OFF are never sent; a zone forced off by its probe is not
        # changed at all (async_set_zone_mode).
        ControlOp.ZONE_MODE: frozenset(
            {ZoneSetp.UNSET, ZoneSetp.ECONOMY, ZoneSetp.PRE_COMFORT, ZoneSetp.COMFORT}
        ),
        # Discrete speeds on a discrete fan only (async_set_vmc_fan refuses any other
        # fan kind); ATTENUATED is never sent.
        ControlOp.VMC_FAN: frozenset({FanSpeed.MIN, FanSpeed.MED, FanSpeed.MAX}),
        ControlOp.VMC_MODE: frozenset({VmcMode.DEHUMIDIFY, VmcMode.VENTILATE}),
        ControlOp.PREDICTIVE: frozenset({True, False}),
    }
)


def is_verified(op: ControlOp, value: object) -> bool:
    """Whether ``value`` may be sent for ``op`` (see :data:`VERIFIED_VALUES`).

    ``PREDICTIVE`` takes only a ``bool``, and no other operation ever takes
    one: ``True == 1 == FanSpeed.MIN``, but a ``bool`` is never a speed, a
    level or an offset (and ``1`` is never "on").  Otherwise values compare by
    equality, so the plain ``int`` of a verified speed or offset is verified;
    the ``async_set_*`` functions also require the exact argument type.
    """
    if isinstance(value, bool) is not (op is ControlOp.PREDICTIVE):
        return False
    try:
        return value in VERIFIED_VALUES[op]
    except TypeError:  # unhashable: never a value the library takes
        return False


# -- refusals -------------------------------------------------------------------------


def _control_disabled() -> ServiceValidationError:
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=EXC_CONTROL_DISABLED)


def _enabled_runtime(entry: RehomConfigEntry) -> RehomRuntimeData:
    """The runtime data of an entry whose client may write; see ensure_control_enabled."""
    if entry.options.get(CONF_ENABLE_CONTROL) is not True:
        raise _control_disabled()
    # Home Assistant deletes runtime_data when the entry unloads.
    runtime: RehomRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is None:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key=EXC_NOT_READY)
    if runtime.control_enabled is not True:
        raise _control_disabled()
    return runtime


def ensure_control_enabled(entry: RehomConfigEntry) -> None:
    """Refuse unless "Enable control" is on, both in the options and in the running entry.

    Call it first in every control method, before any other check.  Raises
    ``ServiceValidationError(control_disabled)`` when the option is not
    exactly ``True`` or the entry's client was built without writes (the
    option changed and the entry has not reloaded yet), and
    ``HomeAssistantError(not_ready)`` when the entry is not loaded.
    """
    _enabled_runtime(entry)


def raise_not_verified() -> NoReturn:
    """Refuse a value that was never tested on a real controller (``not_verified``)."""
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key=EXC_NOT_VERIFIED)


def raise_not_supported() -> NoReturn:
    """Refuse something the controller cannot do from Home Assistant (``not_supported``)."""
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key=EXC_NOT_SUPPORTED)


def raise_temporary_comfort_unavailable() -> NoReturn:
    """Refuse a temporary comfort: not available yet (``temporary_comfort_unavailable``)."""
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key=EXC_TEMPORARY_COMFORT_UNAVAILABLE
    )


def raise_invalid_value() -> NoReturn:
    """Refuse a requested number that is not finite, NaN or infinity (``invalid_value``)."""
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key=EXC_INVALID_VALUE)


def _format_temperature(value: float) -> str:
    return f"{value:g}"


def raise_target_out_of_range(minimum: float, maximum: float) -> NoReturn:
    """Refuse a zone target outside ``minimum``..``maximum`` °C (``target_out_of_range``).

    The bounds fill the ``{min}`` and ``{max}`` placeholders, formatted with
    ``:g`` (``27.0`` shows as ``27``, ``23.5`` as ``23.5``).
    """
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=EXC_TARGET_OUT_OF_RANGE,
        translation_placeholders={
            "min": _format_temperature(minimum),
            "max": _format_temperature(maximum),
        },
    )


def _refusal(reason: str) -> HomeAssistantError:
    """The Home Assistant error for a library refusal ``reason`` (nothing was sent)."""
    if reason == REASON_WRITES_DISABLED:
        return _control_disabled()
    if reason == REASON_UNAVAILABLE:
        return HomeAssistantError(translation_domain=DOMAIN, translation_key=EXC_UNAVAILABLE)
    key = REFUSAL_KEYS.get(reason)
    if key is None:  # GENERIC_REFUSALS, and any reason a later library adds
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=EXC_WRITE_REFUSED,
            translation_placeholders={"reason": reason},
        )
    if reason in DEVICE_REFUSALS:  # the device cannot take a change now: not a usage error
        return HomeAssistantError(translation_domain=DOMAIN, translation_key=key)
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key)


def raise_refused(reason: str) -> NoReturn:
    """Raise what the library's refusal ``reason`` maps to, for a check done in Home Assistant.

    ``REFUSAL_KEYS`` reasons give their own key: a ``HomeAssistantError`` for
    the ``DEVICE_REFUSALS`` (``bus_down``, ``zone_offline``, ``vmc_offline``,
    ``vmc_busy``), a ``ServiceValidationError`` for the others;
    ``writes_disabled`` gives ``control_disabled``; ``unavailable`` gives
    ``HomeAssistantError(unavailable)``; any other reason gives
    ``ServiceValidationError(write_refused)`` with the reason as ``{reason}``.
    """
    raise _refusal(reason)


def may_have_been_applied(err: HomeAssistantError) -> bool:
    """Whether the change behind ``err``, raised by a control call, may have reached the device.

    ``False`` for a ``ServiceValidationError`` and for this integration's
    other errors raised before anything was sent (the ``DEVICE_REFUSALS``,
    ``unavailable``, ``not_ready``, ``invalid_auth``, ``write_refused``).
    ``True`` for ``write_not_confirmed`` and ``write_failed`` (sent, or maybe
    sent, and not confirmed), and for any error this module does not raise.
    """
    if isinstance(err, ServiceValidationError):
        return False
    if err.translation_domain != DOMAIN:
        return True
    return err.translation_key in _MAYBE_APPLIED_KEYS


# -- numbers the platforms turn into writes --------------------------------------------------


def ensure_finite(value: float) -> float:
    """Return ``value``; refuse NaN and ±infinity with ``invalid_value``.

    Home Assistant's schemas coerce ``"nan"`` to a float and its range checks
    let NaN through (every comparison with NaN is false), so call this before
    any comparison or rounding.
    """
    if not math.isfinite(value):
        raise_invalid_value()
    return value


def round_offset(value: float) -> int:
    """The whole-degree zone offset nearest ``value`` °C, as sent to the controller.

    ``floor(value + 0.5)``: halves round up (``0.5 -> 1``, ``2.5 -> 3``,
    ``-0.5 -> 0``, ``-1.5 -> -1``), unlike the built-in ``round``.  The result
    is clamped to -3..+3, so float noise at the ends of the range still gives
    a valid offset.  The zone climate and the offset number both round here.
    Raises ``ServiceValidationError(invalid_value)`` for NaN or infinity,
    before any rounding.
    """
    offset = math.floor(ensure_finite(value) + 0.5)
    return max(OFFSET_MIN, min(OFFSET_MAX, offset))


def zone_offset_for_target(temperature: float, base: float | None) -> int:
    """The zone offset that brings the target nearest ``temperature`` °C.

    ``base`` is the zone's level temperature now (``Zone.base``): after a mode
    change it is the base of the new mode.  In this order:

    - ``invalid_value`` if ``temperature`` is NaN or infinity;
    - ``no_active_target`` (``raise_refused``) if the zone has no base;
    - ``target_out_of_range`` with the base ±3 °C as ``{min}``/``{max}`` if
      ``temperature`` is outside it (beyond float noise): the request is never
      clamped silently;
    - otherwise :func:`round_offset` of ``temperature - base``.

    Home Assistant checks the target against ``min_temp``/``max_temp`` (the
    same base ±3 °C) before the entity sees it, so the range check matters when
    the base changed after that check, as with a ``hvac_mode`` in the same call.
    """
    ensure_finite(temperature)
    if base is None or not math.isfinite(base):
        raise_refused(REASON_NO_ACTIVE_TARGET)
    low = base + OFFSET_MIN
    high = base + OFFSET_MAX
    if not low - _RANGE_TOLERANCE <= temperature <= high + _RANGE_TOLERANCE:
        raise_target_out_of_range(low, high)
    # Snap float noise first (16.4 - 15.9 is 0.4999...), so a half rounds up
    # exactly as it does on the offset number.
    return round_offset(round(temperature - base, 6))


# -- writes -------------------------------------------------------------------------------


def _require(value: object, kind: type[object], name: str) -> None:
    """Platforms pass exact types; anything else is a bug, never sent."""
    if isinstance(value, bool) is not (kind is bool) or not isinstance(value, kind):
        raise TypeError(f"{name} must be {kind.__name__}, not {type(value).__name__}")


async def _async_write(
    entry: RehomConfigEntry, runtime: RehomRuntimeData, write: Awaitable[bool]
) -> bool:
    """Await a library write; map every failure to a translated Home Assistant error."""
    try:
        sent = await write
    except RehomWriteRefusedError as err:
        _LOGGER.debug("Control refused before sending: %s", err.reason)
        raise _refusal(err.reason) from err
    except RehomWriteNotConfirmedError as err:
        _LOGGER.debug("Control write sent but not confirmed: %s", err)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key=EXC_WRITE_NOT_CONFIRMED
        ) from err
    except RehomNotReadyError as err:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key=EXC_NOT_READY) from err
    except RehomAuthenticationError as err:
        entry.async_start_reauth(runtime.coordinator.hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key=EXC_INVALID_AUTH
        ) from err
    except ForbiddenRequestError as err:
        # The library plans only bodies its write gate accepts: this is a bug.
        _LOGGER.error("A control write was refused by the library's write gate: %s", err)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=EXC_WRITE_REFUSED,
            translation_placeholders={"reason": REASON_FORBIDDEN},
        ) from err
    except RehomError as err:  # timeout, connection, HTTP status, redirect
        _LOGGER.debug("Control write failed: %s", type(err).__name__)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key=EXC_WRITE_FAILED
        ) from err
    return sent


async def async_set_house_preset(entry: RehomConfigEntry, preset: MasterPreset) -> bool:
    """Whole house to AUTO, or to MANUAL at a verified level (ECONOMY, COMFORT).

    A MANUAL level also rewrites the controller's setpoint to that level's
    temperature, so re-sending the current level clears a setpoint mismatch.
    Returns ``True`` once a sent write is confirmed, ``False`` when nothing
    needed sending; raises the errors described in the module docstring.
    """
    runtime = _enabled_runtime(entry)
    _require(preset, MasterPreset, "preset")
    if not is_verified(ControlOp.HOUSE_PRESET, preset):
        raise_not_verified()
    return await _async_write(entry, runtime, runtime.client.set_house_preset(preset))


async def async_set_zone_offset(entry: RehomConfigEntry, zone_id: str, offset: int) -> bool:
    """A zone's offset in whole degrees (-3..+3); allowed in any house mode.

    ``offset`` must be an ``int``: platforms compute it with
    :func:`round_offset` or :func:`zone_offset_for_target`.  The offset stays
    until it is changed again, across schedule slots.
    """
    runtime = _enabled_runtime(entry)
    _require(offset, int, "offset")
    if not is_verified(ControlOp.ZONE_OFFSET, offset):
        raise_not_verified()
    return await _async_write(entry, runtime, runtime.client.set_zone_offset(zone_id, offset))


async def async_set_zone_mode(entry: RehomConfigEntry, zone_id: str, setp: ZoneSetp) -> bool:
    """A zone back on its schedule (``ZoneSetp.UNSET``) or at a manual level.

    Needs the house in AUTO (``house_not_auto``).  A zone forced off by its
    probe (``ZoneSetp.PROBE_OFF``) is never changed from Home Assistant, not
    even back to its schedule: ``not_verified``, nothing sent.
    """
    runtime = _enabled_runtime(entry)
    _require(setp, ZoneSetp, "setp")
    zone = runtime.coordinator.data.zones.get(zone_id)
    if not is_verified(ControlOp.ZONE_MODE, setp) or (
        zone is not None and zone.setp is ZoneSetp.PROBE_OFF
    ):
        raise_not_verified()
    return await _async_write(entry, runtime, runtime.client.set_zone_mode(zone_id, setp))


async def async_set_vmc_fan(entry: RehomConfigEntry, vmc_id: str, value: int) -> bool:
    """A VMC's fan speed: a :class:`FanSpeed` (or its ``int``) on a discrete fan.

    A continuous fan, or a VMC absent from the state, is refused with
    ``not_verified`` (only discrete speeds were tested).
    """
    runtime = _enabled_runtime(entry)
    _require(value, int, "value")
    vmc = runtime.coordinator.data.vmcs.get(vmc_id)
    if (
        not is_verified(ControlOp.VMC_FAN, value)
        or vmc is None
        or vmc.fan.kind is not FanKind.DISCRETE
    ):
        raise_not_verified()
    return await _async_write(entry, runtime, runtime.client.set_vmc_fan(vmc_id, value))


async def async_set_vmc_mode(entry: RehomConfigEntry, vmc_id: str, mode: VmcMode) -> bool:
    """A VMC's operating mode (``mode_not_available`` unless the installer enabled it)."""
    runtime = _enabled_runtime(entry)
    _require(mode, VmcMode, "mode")
    if not is_verified(ControlOp.VMC_MODE, mode):
        raise_not_verified()
    return await _async_write(entry, runtime, runtime.client.set_vmc_mode(vmc_id, mode))


async def async_set_predictive(entry: RehomConfigEntry, enabled: bool) -> bool:
    """The predictive algorithm on or off (needs the house in AUTO: ``house_not_auto``)."""
    runtime = _enabled_runtime(entry)
    _require(enabled, bool, "enabled")
    if not is_verified(ControlOp.PREDICTIVE, enabled):
        raise_not_verified()
    return await _async_write(entry, runtime, runtime.client.set_predictive(enabled))
