"""control.py: the gate, the verified values, the library call and the error mapping.

The ``control`` functions are called directly, on a real client replaying the
fixture with "Enable control" on (the platforms that call them have their own
tests).  The zone offset number is loaded to show what Home Assistant displays.
The offset helpers the zone climate and the offset number share are tested here too.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable
import logging
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

from aiorehom import (
    FanSpeed,
    ForbiddenRequestError,
    MasterPreset,
    RehomAuthenticationError,
    RehomConnectionError,
    RehomHttpError,
    RehomNotReadyError,
    RehomRedirectError,
    RehomTimeoutError,
    RehomWriteNotConfirmedError,
    RehomWriteRefusedError,
    VmcMode,
    ZoneSetp,
)
import aiorehom.client
from aiorehom.transport import OVERRIDES_WRITE_PATH
import aiorehom.writes
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rehom import control
from custom_components.rehom.const import (
    CONF_ENABLE_CONTROL,
    CONF_TEMPORARY_COMFORT_DURATION,
    DEVICE_REFUSALS,
    DOMAIN,
    GENERIC_REFUSALS,
    REFUSAL_KEYS,
)
from custom_components.rehom.control import VERIFIED_VALUES, ControlOp, is_verified

from .conftest import CONTROL_OPTIONS, HOUSE_AUTO, HOUSE_AUTO_VALUES
from .harness import RehomHarness, set_values
from .platform_helpers import get_state

ZONE = "001"
VMC = "001"
OFFSET_NUMBER = "number.zona_001_temperature_offset"
#: The library's own refusals that control.py maps separately.
SPECIAL_REFUSALS = {"writes_disabled", "unavailable"}
#: device_patch: zone 001 forced off by its probe (SETP 5).
ZONE_PROBE_OFF_VALUES = {"ZONA.001..SETP_CORRENTE": "5"}

type ControlCall = Callable[[MockConfigEntry], Awaitable[bool]]

#: One verified call per write function: (id, call); the id is the ControlOp it checks.
VERIFIED_CALLS: list[tuple[str, ControlCall]] = [
    ("house_preset", lambda e: control.async_set_house_preset(e, MasterPreset.ECONOMY)),
    ("zone_offset", lambda e: control.async_set_zone_offset(e, ZONE, 1)),
    ("zone_mode", lambda e: control.async_set_zone_mode(e, ZONE, ZoneSetp.ECONOMY)),
    ("vmc_fan", lambda e: control.async_set_vmc_fan(e, VMC, FanSpeed.MED)),
    ("vmc_mode", lambda e: control.async_set_vmc_mode(e, VMC, VmcMode.VENTILATE)),
    ("predictive", lambda e: control.async_set_predictive(e, False)),
]
#: Values never tested on a real controller: refused with not_verified, nothing sent.
UNVERIFIED_CALLS: list[tuple[str, ControlCall]] = [
    *(
        (f"house_{preset}", lambda e, p=preset: control.async_set_house_preset(e, p))
        for preset in (MasterPreset.PRE_COMFORT, MasterPreset.OFF)
    ),
    *(
        (f"offset_{offset}", lambda e, o=offset: control.async_set_zone_offset(e, ZONE, o))
        for offset in (4, -4, 30)
    ),
    *(
        (f"zone_{setp.name}", lambda e, s=setp: control.async_set_zone_mode(e, ZONE, s))
        for setp in (ZoneSetp.OFF, ZoneSetp.PROBE_OFF)
    ),
    *(
        (f"fan_{speed.name}", lambda e, v=speed: control.async_set_vmc_fan(e, VMC, v))
        for speed in (FanSpeed.ATTENUATED, FanSpeed.NONE)
    ),
    ("fan_int_4", lambda e: control.async_set_vmc_fan(e, VMC, 4)),
    ("fan_int_50", lambda e: control.async_set_vmc_fan(e, VMC, 50)),
    ("fan_absent_vmc", lambda e: control.async_set_vmc_fan(e, "009", FanSpeed.MED)),
    *(
        (f"vmc_mode_{mode.name}", lambda e, m=mode: control.async_set_vmc_mode(e, VMC, m))
        for mode in (VmcMode.STOP, VmcMode.COOL, VmcMode.STANDBY, VmcMode.RAPID_RENEWAL)
    ),
]
#: Arguments of the wrong type: a bug in a platform, raised before anything is sent.
BAD_TYPE_CALLS: list[tuple[str, ControlCall]] = [
    ("preset_str", lambda e: control.async_set_house_preset(e, "economy")),  # type: ignore[arg-type]
    ("offset_float", lambda e: control.async_set_zone_offset(e, ZONE, 1.0)),  # type: ignore[arg-type]
    ("offset_bool", lambda e: control.async_set_zone_offset(e, ZONE, True)),
    ("setp_int", lambda e: control.async_set_zone_mode(e, ZONE, 2)),  # type: ignore[arg-type]
    ("fan_float", lambda e: control.async_set_vmc_fan(e, VMC, 2.0)),  # type: ignore[arg-type]
    ("mode_int", lambda e: control.async_set_vmc_mode(e, VMC, 8)),  # type: ignore[arg-type]
    ("predictive_int", lambda e: control.async_set_predictive(e, 1)),  # type: ignore[arg-type]
]


def _ids(calls: list[tuple[str, ControlCall]]) -> list[str]:
    return [name for name, _call in calls]


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """ "Enable control" on."""
    return dict(CONTROL_OPTIONS)


@pytest.fixture
def platforms() -> list[Platform]:
    """The zone offset number (shows the confirmed state)."""
    return [Platform.NUMBER]


def _assert_key(err: pytest.ExceptionInfo[HomeAssistantError], key: str) -> None:
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == key


def _assert_device_error(err: pytest.ExceptionInfo[HomeAssistantError], key: str) -> None:
    """A HomeAssistantError that is not a ServiceValidationError (not the user's fault)."""
    assert not isinstance(err.value, ServiceValidationError)
    _assert_key(err, key)


# -- the verified values ----------------------------------------------------------------


def test_verified_values_pinned() -> None:
    """Only the values tested on a real controller.  Widening this is a deliberate change."""
    assert dict(VERIFIED_VALUES) == {
        ControlOp.HOUSE_PRESET: {MasterPreset.AUTO, MasterPreset.ECONOMY, MasterPreset.COMFORT},
        ControlOp.ZONE_OFFSET: {-3, -2, -1, 0, 1, 2, 3},
        ControlOp.ZONE_MODE: {
            ZoneSetp.UNSET,
            ZoneSetp.ECONOMY,
            ZoneSetp.PRE_COMFORT,
            ZoneSetp.COMFORT,
        },
        ControlOp.VMC_FAN: {FanSpeed.MIN, FanSpeed.MED, FanSpeed.MAX},
        ControlOp.VMC_MODE: {VmcMode.DEHUMIDIFY, VmcMode.VENTILATE},
        ControlOp.PREDICTIVE: {True, False},
    }
    assert set(ControlOp) == set(VERIFIED_VALUES)
    # every operation lists its values: nothing is allowed by default
    assert all(isinstance(values, frozenset) for values in VERIFIED_VALUES.values())
    # a bool appears only as a predictive value, never as a level, speed or offset
    for op, values in VERIFIED_VALUES.items():
        bools = {value for value in values if isinstance(value, bool)}
        assert bools == ({True, False} if op is ControlOp.PREDICTIVE else set()), op


@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        (ControlOp.HOUSE_PRESET, MasterPreset.ECONOMY, True),
        (ControlOp.HOUSE_PRESET, MasterPreset.COMFORT, True),
        (ControlOp.HOUSE_PRESET, MasterPreset.AUTO, True),
        (ControlOp.HOUSE_PRESET, MasterPreset.PRE_COMFORT, False),
        (ControlOp.HOUSE_PRESET, MasterPreset.OFF, False),
        (ControlOp.HOUSE_PRESET, None, False),
        (ControlOp.ZONE_MODE, ZoneSetp.UNSET, True),
        (ControlOp.ZONE_MODE, ZoneSetp.PRE_COMFORT, True),
        (ControlOp.ZONE_MODE, ZoneSetp.COMFORT, True),
        (ControlOp.ZONE_MODE, ZoneSetp.OFF, False),
        (ControlOp.ZONE_MODE, ZoneSetp.PROBE_OFF, False),
        (ControlOp.ZONE_MODE, False, False),  # False == 0 == ZoneSetp.UNSET, never a level
        (ControlOp.VMC_FAN, FanSpeed.MED, True),
        (ControlOp.VMC_FAN, FanSpeed.MAX, True),
        (ControlOp.VMC_FAN, 2, True),  # the int of a verified speed
        (ControlOp.VMC_FAN, 3, True),
        (ControlOp.VMC_FAN, True, False),  # True == 1 == FanSpeed.MIN, but never a speed
        (ControlOp.VMC_FAN, FanSpeed.ATTENUATED, False),
        (ControlOp.VMC_FAN, FanSpeed.NONE, False),
        (ControlOp.VMC_MODE, VmcMode.VENTILATE, True),
        (ControlOp.VMC_MODE, VmcMode.STOP, False),
        (ControlOp.VMC_MODE, True, False),  # True == 1 == VmcMode.DEHUMIDIFY
        (ControlOp.ZONE_OFFSET, -3, True),
        (ControlOp.ZONE_OFFSET, 3, True),
        (ControlOp.ZONE_OFFSET, 0, True),
        (ControlOp.ZONE_OFFSET, 4, False),
        (ControlOp.ZONE_OFFSET, -4, False),
        (ControlOp.ZONE_OFFSET, 0.5, False),
        (ControlOp.ZONE_OFFSET, True, False),  # True == 1, never an offset
        (ControlOp.ZONE_OFFSET, False, False),
        (ControlOp.PREDICTIVE, False, True),
        (ControlOp.PREDICTIVE, True, True),
        (ControlOp.PREDICTIVE, 1, False),  # 1 == True, but only a bool switches it
        (ControlOp.PREDICTIVE, 0, False),
        (ControlOp.PREDICTIVE, None, False),
        (ControlOp.VMC_FAN, [2], False),  # unhashable: never a value
    ],
)
def test_is_verified(op: ControlOp, value: object, expected: bool) -> None:
    """is_verified follows VERIFIED_VALUES; a bool is a value of PREDICTIVE only."""
    assert is_verified(op, value) is expected


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
@pytest.mark.parametrize(
    ("op", "call"),
    [(ControlOp(name), call) for name, call in VERIFIED_CALLS],
    ids=_ids(VERIFIED_CALLS),
)
async def test_every_write_checks_the_table(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    op: ControlOp,
    call: ControlCall,
) -> None:
    """Each async_set_* consults its own VERIFIED_VALUES entry: emptied, nothing is sent."""
    calls = harness.transport_calls
    table = MappingProxyType({**VERIFIED_VALUES, op: frozenset()})
    with (
        patch.object(control, "VERIFIED_VALUES", table),
        patch.object(aiorehom.client.RehomClient, "_execute") as execute,
        pytest.raises(ServiceValidationError) as err,
    ):
        await call(init_integration)
    _assert_key(err, "not_verified")
    execute.assert_not_called()
    assert harness.transport_calls == calls
    assert "post_bulk_update" not in harness.transport_calls
    # with the real table the same call is sent (house in AUTO: every guard passes)
    assert await call(init_integration) is True
    assert harness.transport_calls.count("post_bulk_update") == 1


def test_every_op_has_a_verified_call() -> None:
    """VERIFIED_CALLS covers every ControlOp (test_every_write_checks_the_table relies on it)."""
    assert sorted(_ids(VERIFIED_CALLS)) == sorted(op.value for op in ControlOp)


# -- the gate -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry_options",
    [
        {CONF_TEMPORARY_COMFORT_DURATION: 2.0},
        {CONF_TEMPORARY_COMFORT_DURATION: 2.0, CONF_ENABLE_CONTROL: False},
        {CONF_TEMPORARY_COMFORT_DURATION: 2.0, CONF_ENABLE_CONTROL: "true"},  # not a bool
    ],
    ids=["missing", "false", "not_bool"],
)
@pytest.mark.parametrize(
    "call",
    [call for _name, call in VERIFIED_CALLS + UNVERIFIED_CALLS + BAD_TYPE_CALLS],
    ids=_ids(VERIFIED_CALLS + UNVERIFIED_CALLS + BAD_TYPE_CALLS),
)
async def test_control_disabled(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    call: ControlCall,
) -> None:
    """Control off: control_disabled before any other check, and the client is read-only."""
    calls = harness.transport_calls
    with pytest.raises(ServiceValidationError) as err:
        await call(init_integration)
    _assert_key(err, "control_disabled")
    await harness.settle()
    assert harness.transport_calls == calls
    assert harness.writes == []
    assert harness.allow_writes == [False]
    assert init_integration.runtime_data.control_enabled is False
    assert harness.client.allow_writes is False


async def test_runtime_flag_off(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The option is on but the running client was built without writes: control_disabled."""
    init_integration.runtime_data.control_enabled = False
    with pytest.raises(ServiceValidationError) as err:
        await control.async_set_zone_offset(init_integration, ZONE, 1)
    _assert_key(err, "control_disabled")
    with pytest.raises(ServiceValidationError):
        control.ensure_control_enabled(init_integration)
    assert "post_bulk_update" not in harness.transport_calls


async def test_option_turned_off_before_reload(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The option is off again but the entry has not reloaded: refused at call time."""
    hass.config_entries.async_update_entry(
        init_integration, options={**CONTROL_OPTIONS, CONF_ENABLE_CONTROL: False}
    )
    await hass.async_block_till_done()
    assert init_integration.runtime_data.control_enabled is True
    assert harness.client.allow_writes is True
    with pytest.raises(ServiceValidationError) as err:
        await control.async_set_zone_offset(init_integration, ZONE, 1)
    _assert_key(err, "control_disabled")
    assert "post_bulk_update" not in harness.transport_calls


async def test_entry_not_loaded(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """An unloaded entry (no runtime data) cannot write: not_ready."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError) as err:
        control.ensure_control_enabled(init_integration)
    _assert_device_error(err, "not_ready")
    with pytest.raises(HomeAssistantError) as err:
        await control.async_set_zone_offset(init_integration, ZONE, 1)
    _assert_device_error(err, "not_ready")
    assert "post_bulk_update" not in harness.transport_calls


async def test_control_enabled(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Control on: the client is writable and the gate lets calls through."""
    assert harness.allow_writes == [True]
    assert harness.client.allow_writes is True
    assert init_integration.runtime_data.control_enabled is True
    control.ensure_control_enabled(init_integration)  # does not raise


@pytest.mark.parametrize(
    ("helper", "key"),
    [
        (control.raise_not_verified, "not_verified"),
        (control.raise_not_supported, "not_supported"),
        (control.raise_temporary_comfort_unavailable, "temporary_comfort_unavailable"),
        (control.raise_invalid_value, "invalid_value"),
    ],
)
def test_raise_helpers(helper: Callable[[], None], key: str) -> None:
    """The platforms' own refusals are ServiceValidationErrors."""
    with pytest.raises(ServiceValidationError) as err:
        helper()
    _assert_key(err, key)
    assert not err.value.translation_placeholders


@pytest.mark.parametrize(
    ("low", "high", "placeholders"),
    [(21.0, 27.0, {"min": "21", "max": "27"}), (17.5, 23.5, {"min": "17.5", "max": "23.5"})],
)
def test_raise_target_out_of_range(low: float, high: float, placeholders: dict[str, str]) -> None:
    """The zone's allowed range fills {min} and {max}, without trailing zeros."""
    with pytest.raises(ServiceValidationError) as err:
        control.raise_target_out_of_range(low, high)
    _assert_key(err, "target_out_of_range")
    assert err.value.translation_placeholders == placeholders


# -- the offset helpers (zone climate and offset number) ------------------------------------


NON_FINITE = [math.nan, math.inf, -math.inf]


@pytest.mark.parametrize(
    ("value", "offset"),
    [
        (0.0, 0),
        (1.0, 1),
        (0.5, 1),  # halves round up, unlike round() (0)
        (1.5, 2),
        (2.5, 3),  # round() gives 2
        (-0.5, 0),  # round() gives 0 too, but -1.5 below differs
        (-1.5, -1),  # round() gives -2
        (-2.5, -2),
        (2.4, 2),
        (-2.6, -3),
        (-0.4, 0),
        (0.49, 0),
        (-0.51, -1),
        (3.0, 3),
        (-3.0, -3),
        (3.4999, 3),
        (3.5, 3),  # clamped
        (10.0, 3),
        (-10.0, -3),
        (1e300, 3),
        (2, 2),  # an int is fine
    ],
)
def test_round_offset(value: float, offset: int) -> None:
    """floor(value + 0.5) (halves up), within -3..+3, as a Python int."""
    result = control.round_offset(value)
    assert result == offset
    assert type(result) is int
    assert is_verified(ControlOp.ZONE_OFFSET, result)


@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_non_finite_refused(value: float) -> None:
    """NaN and infinity are refused with invalid_value before any rounding."""
    for helper in (
        control.ensure_finite,
        control.round_offset,
        lambda v: control.zone_offset_for_target(v, 24.0),
        lambda v: control.zone_offset_for_target(v, None),  # checked before the base
    ):
        with pytest.raises(ServiceValidationError) as err:
            helper(value)
        _assert_key(err, "invalid_value")
        assert not err.value.translation_placeholders


def test_ensure_finite() -> None:
    """A finite value is returned as it is."""
    assert control.ensure_finite(-2.5) == -2.5
    assert control.ensure_finite(0) == 0


@pytest.mark.parametrize(
    ("temperature", "base", "offset"),
    [
        (25.0, 24.0, 1),
        (24.0, 24.0, 0),
        (24.49, 24.0, 0),
        (24.5, 24.0, 1),  # halves round up, as the offset number does
        (26.5, 24.0, 3),
        (23.5, 24.0, 0),
        (22.5, 24.0, -1),
        (23.49, 24.0, -1),
        (27.0, 29.0, -2),
        (27.0, 24.0, 3),  # the ends of the range are allowed
        (21.0, 24.0, -3),
        # float noise at the ends: 16.1 - 3 is 13.100000000000001 and 18.1 - 15.1 is
        # 3.0000000000000018; both are still the end of the range
        (13.1, 16.1, -3),
        (18.1, 15.1, 3),
        # float noise at a half: 16.4 - 15.9 is 0.4999..., 15.6 - 16.1 is -0.5000...1;
        # halves still round up, as on the offset number
        (16.4, 15.9, 1),
        (15.6, 16.1, 0),
        (14.6, 16.1, -1),
        (18.6, 18.1, 1),
    ],
)
def test_zone_offset_for_target(temperature: float, base: float, offset: int) -> None:
    """The offset nearest the target, rounded like round_offset."""
    result = control.zone_offset_for_target(temperature, base)
    assert result == offset
    assert type(result) is int


@pytest.mark.parametrize(
    ("temperature", "base", "placeholders"),
    [
        (32.0, 24.0, {"min": "21", "max": "27"}),  # never clamped silently to +3
        (27.1, 24.0, {"min": "21", "max": "27"}),
        (20.9, 24.0, {"min": "21", "max": "27"}),
        (10.0, 20.5, {"min": "17.5", "max": "23.5"}),
        (13.09, 16.1, {"min": "13.1", "max": "19.1"}),
    ],
)
def test_zone_target_out_of_range(
    temperature: float, base: float, placeholders: dict[str, str]
) -> None:
    """A target outside the base ±3 °C is refused with the range, not clamped."""
    with pytest.raises(ServiceValidationError) as err:
        control.zone_offset_for_target(temperature, base)
    _assert_key(err, "target_out_of_range")
    assert err.value.translation_placeholders == placeholders


@pytest.mark.parametrize("base", [None, math.nan, math.inf])
def test_zone_target_without_base(base: float | None) -> None:
    """A zone without a level temperature has no target to set: no_active_target."""
    with pytest.raises(ServiceValidationError) as err:
        control.zone_offset_for_target(24.0, base)
    _assert_key(err, "no_active_target")


# -- verified values and argument types ------------------------------------------------------


@pytest.mark.parametrize(
    "call", [call for _name, call in UNVERIFIED_CALLS], ids=_ids(UNVERIFIED_CALLS)
)
async def test_not_verified(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    call: ControlCall,
) -> None:
    """A value never tested on a real controller is refused before the library is called."""
    calls = harness.transport_calls
    with (
        patch.object(aiorehom.client.RehomClient, "_execute") as execute,
        pytest.raises(ServiceValidationError) as err,
    ):
        await call(init_integration)
    _assert_key(err, "not_verified")
    execute.assert_not_called()
    assert harness.transport_calls == calls


@pytest.mark.parametrize("device_patch", [set_values({"DEUM...STEP": "-1"})])
async def test_continuous_fan_not_verified(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Only discrete fans were tested: a continuous fan is refused whatever the value."""
    for value in (FanSpeed.MIN, FanSpeed.MED, 50):
        with pytest.raises(ServiceValidationError) as err:
            await control.async_set_vmc_fan(init_integration, VMC, value)
        _assert_key(err, "not_verified")
    assert harness.writes == []


@pytest.mark.parametrize("call", [call for _name, call in BAD_TYPE_CALLS], ids=_ids(BAD_TYPE_CALLS))
async def test_bad_argument_type(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    call: ControlCall,
) -> None:
    """Platforms pass exact enums, ints and bools; anything else is a bug and sends nothing."""
    with pytest.raises(TypeError):
        await call(init_integration)
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls


# -- success: the exact records, and the confirmed state when the call returns --------------


async def _write_once(
    hass: HomeAssistant, harness: RehomHarness, entry: MockConfigEntry, call: ControlCall
) -> list[dict[str, str]]:
    assert await call(entry) is True
    assert harness.transport_calls.count("post_bulk_update") == 1
    return harness.written_values


async def test_zone_offset(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Offset 0 -> +1 on zone 001 (house MANUAL: allowed in any house mode)."""
    assert get_state(hass, OFFSET_NUMBER).state == "0.0"
    written = await _write_once(
        hass, harness, init_integration, lambda e: control.async_set_zone_offset(e, ZONE, 1)
    )
    assert written == [{"ZONA.001..DELTA_SETP_CORRENTE": "1.0"}]
    # confirmed state, already in Home Assistant when the call returns
    assert init_integration.runtime_data.coordinator.data.zones[ZONE].offset == 1
    assert get_state(hass, OFFSET_NUMBER).state == "1.0"


@pytest.mark.parametrize(
    ("preset", "records"),
    [
        (
            MasterPreset.ECONOMY,
            {"REHOM...MODO": "1", "REHOM...SET_POINT": "1", "REHOM...SET_POINT_TEMP": "29"},
        ),
        (MasterPreset.AUTO, {"REHOM...MODO": "2", "REHOM...SET_POINT": "0"}),
    ],
)
async def test_house_preset(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    preset: MasterPreset,
    records: dict[str, str],
) -> None:
    """House COMFORT -> ECONOMY (with its level temperature) or -> AUTO."""
    written = await _write_once(
        hass, harness, init_integration, lambda e: control.async_set_house_preset(e, preset)
    )
    assert written == [records]
    assert init_integration.runtime_data.coordinator.data.plant.preset is preset


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_house_comfort(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """House AUTO -> COMFORT, with the comfort temperature (24 °C) as the setpoint."""
    assert init_integration.runtime_data.coordinator.data.plant.temperature_comfort == 24
    written = await _write_once(
        hass,
        harness,
        init_integration,
        lambda e: control.async_set_house_preset(e, MasterPreset.COMFORT),
    )
    assert written == [
        {"REHOM...MODO": "1", "REHOM...SET_POINT": "3", "REHOM...SET_POINT_TEMP": "24"}
    ]
    assert init_integration.runtime_data.coordinator.data.plant.preset is MasterPreset.COMFORT


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_zone_mode(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Zone 001 schedule -> each verified level -> schedule, with the house in AUTO."""
    entry = init_integration
    for setp in (ZoneSetp.ECONOMY, ZoneSetp.PRE_COMFORT, ZoneSetp.COMFORT, ZoneSetp.UNSET):
        assert await control.async_set_zone_mode(entry, ZONE, setp) is True
        assert entry.runtime_data.coordinator.data.zones[ZONE].setp is setp
    assert harness.written_values == [
        {"ZONA.001..SETP_CORRENTE": "2"},
        {"ZONA.001..SETP_CORRENTE": "3"},
        {"ZONA.001..SETP_CORRENTE": "4"},
        {"ZONA.001..SETP_CORRENTE": "0"},
    ]


@pytest.mark.parametrize(
    "device_patch",
    [set_values(ZONE_PROBE_OFF_VALUES), set_values({**HOUSE_AUTO_VALUES, **ZONE_PROBE_OFF_VALUES})],
    ids=["house_manual", "house_auto"],
)
@pytest.mark.parametrize(
    "setp", [ZoneSetp.UNSET, ZoneSetp.ECONOMY, ZoneSetp.PRE_COMFORT, ZoneSetp.COMFORT]
)
async def test_zone_forced_off_by_probe(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    setp: ZoneSetp,
) -> None:
    """A zone forced off by its probe is never turned back on: not_verified, nothing sent."""
    assert init_integration.runtime_data.coordinator.data.zones[ZONE].setp is ZoneSetp.PROBE_OFF
    calls = harness.transport_calls
    with (
        patch.object(aiorehom.client.RehomClient, "_execute") as execute,
        pytest.raises(ServiceValidationError) as err,
    ):
        await control.async_set_zone_mode(init_integration, ZONE, setp)
    _assert_key(err, "not_verified")
    execute.assert_not_called()
    assert harness.transport_calls == calls
    # another zone is not affected
    assert init_integration.runtime_data.coordinator.data.zones["002"].setp is not (
        ZoneSetp.PROBE_OFF
    )


@pytest.mark.parametrize(("speed", "written"), [(FanSpeed.MED, "2"), (FanSpeed.MAX, "3")])
async def test_vmc_fan(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    speed: FanSpeed,
    written: str,
) -> None:
    """VMC 001 fan MIN -> MED or MAX."""
    values = await _write_once(
        hass, harness, init_integration, lambda e: control.async_set_vmc_fan(e, VMC, speed)
    )
    assert values == [{"DEUM.001..COM_VENTILA": written}]
    assert init_integration.runtime_data.coordinator.data.vmcs[VMC].fan.speed is speed


async def test_vmc_mode(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """VMC 001 dehumidify -> ventilate."""
    written = await _write_once(
        hass,
        harness,
        init_integration,
        lambda e: control.async_set_vmc_mode(e, VMC, VmcMode.VENTILATE),
    )
    assert written == [{"DEUM.001..ST_MODE": "8"}]
    assert init_integration.runtime_data.coordinator.data.vmcs[VMC].mode is VmcMode.VENTILATE


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_predictive(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Predictive on -> off, with the house in AUTO."""
    written = await _write_once(
        hass, harness, init_integration, lambda e: control.async_set_predictive(e, False)
    )
    assert written == [{"REHOM...ALG_ATTIVO": "0"}]
    assert init_integration.runtime_data.coordinator.data.plant.predictive is False


async def test_nothing_to_send(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The controller already reports the value: success, and nothing is sent."""
    entry = init_integration
    assert await control.async_set_zone_offset(entry, ZONE, 0) is False
    assert await control.async_set_vmc_mode(entry, VMC, VmcMode.DEHUMIDIFY) is False
    assert await control.async_set_vmc_fan(entry, VMC, FanSpeed.MIN) is False
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls


async def test_no_optimistic_state(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Without the echo, the old value stays shown until a resync confirms the write."""
    harness.echo = False  # the write lands (REST shows it), but no WebSocket echo
    snapshots = harness.transport_calls.count("get_interface")
    task = asyncio.ensure_future(control.async_set_zone_offset(init_integration, ZONE, 1))
    await harness.advance(10)
    assert not task.done()
    assert harness.written_values == [{"ZONA.001..DELTA_SETP_CORRENTE": "1.0"}]
    assert get_state(hass, OFFSET_NUMBER).state == "0.0"
    assert init_integration.runtime_data.coordinator.data.zones[ZONE].offset == 0
    assert await harness.run_until_done(task) is True
    assert harness.transport_calls.count("get_interface") == snapshots + 1  # the resync
    assert get_state(hass, OFFSET_NUMBER).state == "1.0"


# -- failures ---------------------------------------------------------------------------------


async def test_not_confirmed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Sent but never reported back: write_not_confirmed, and the state is unchanged."""
    harness.echo = False
    harness.apply = False
    with pytest.raises(HomeAssistantError) as err:
        await harness.run_until_done(control.async_set_zone_offset(init_integration, ZONE, 1))
    _assert_device_error(err, "write_not_confirmed")
    assert isinstance(err.value.__cause__, RehomWriteNotConfirmedError)
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert harness.written_values == [{"ZONA.001..DELTA_SETP_CORRENTE": "1.0"}]
    assert init_integration.runtime_data.coordinator.data.zones[ZONE].offset == 0
    assert get_state(hass, OFFSET_NUMBER).state == "0.0"


@pytest.mark.parametrize(
    "error",
    [
        RehomTimeoutError("replay: POST timed out"),
        RehomConnectionError("replay: connection reset"),
        RehomHttpError(500),
        RehomRedirectError(302),
    ],
    ids=["timeout", "connection", "http", "redirect"],
)
async def test_write_failed(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    error: Exception,
) -> None:
    """The POST itself failed (it may or may not have landed): write_failed."""
    harness.errors["post_bulk_update"] = error
    with pytest.raises(HomeAssistantError) as err:
        await control.async_set_zone_offset(init_integration, ZONE, 1)
    _assert_device_error(err, "write_failed")
    assert err.value.__cause__ is error
    assert init_integration.runtime_data.coordinator.data.zones[ZONE].offset == 0


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A refused token on the write: invalid_auth, and a reauth flow starts."""
    harness.errors["post_bulk_update"] = RehomAuthenticationError(401)
    with pytest.raises(HomeAssistantError) as err:
        await control.async_set_zone_offset(init_integration, ZONE, 1)
    _assert_device_error(err, "invalid_auth")
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert flows[0]["context"]["entry_id"] == init_integration.entry_id


async def test_not_ready(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The client is closed (for example while Home Assistant stops): not_ready, nothing sent."""
    await harness.client.close()
    with pytest.raises(HomeAssistantError) as err:
        await control.async_set_zone_offset(init_integration, ZONE, 1)
    _assert_device_error(err, "not_ready")
    assert isinstance(err.value.__cause__, RehomNotReadyError)
    assert harness.writes == []


async def test_forbidden_is_logged(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The library's write gate refused its own plan (a bug): write_refused, logged as an error."""
    client = harness.client
    refused = ForbiddenRequestError("POST refused: test")
    with (
        patch.object(client, "set_zone_offset", side_effect=refused),
        pytest.raises(HomeAssistantError) as err,
    ):
        await control.async_set_zone_offset(init_integration, ZONE, 1)
    _assert_device_error(err, "write_refused")
    assert err.value.translation_placeholders == {"reason": "forbidden_request"}
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "write gate" in errors[0].getMessage()


# -- refusals: the mapping ------------------------------------------------------------------


def _refusal_literals(module: Any) -> set[str]:
    """Reasons the library raises: ``_refuse("x", ...)``, ``RehomWriteRefusedError("x", ...)``."""
    tree = ast.parse(Path(module.__file__).read_text("utf-8"))
    reasons: set[str] = set()
    forwarded = 0  # the one helper that forwards its ``reason`` parameter
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in {"_refuse", "RehomWriteRefusedError"} and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == "reason":
                forwarded += 1
                continue
            assert isinstance(first, ast.Constant), ast.unparse(node)
            assert isinstance(first.value, str), ast.unparse(node)
            reasons.add(first.value)
    assert forwarded <= 1, f"{module.__name__}: more than one call forwards a reason"
    return reasons


def test_refusal_reasons_match_the_library() -> None:
    """Every reason the library can raise is mapped, and nothing else (drift check)."""
    library = _refusal_literals(aiorehom.writes) | _refusal_literals(aiorehom.client)
    assert len(library) >= 34
    assert set(REFUSAL_KEYS) | GENERIC_REFUSALS | SPECIAL_REFUSALS == library
    assert not set(REFUSAL_KEYS) & GENERIC_REFUSALS
    assert not (set(REFUSAL_KEYS) | GENERIC_REFUSALS) & SPECIAL_REFUSALS


def test_refusal_keys() -> None:
    """Each user-facing refusal has its own message; two share level_temperature_invalid."""
    assert {reason: key for reason, key in REFUSAL_KEYS.items() if reason != key} == {
        "level_temperature_unknown": "level_temperature_invalid",
        "level_temperature_out_of_range": "level_temperature_invalid",
    }


def test_device_refusals() -> None:
    """Exactly the refusals caused by the device: each has its own message."""
    assert {"bus_down", "zone_offline", "vmc_offline", "vmc_busy"} == DEVICE_REFUSALS
    assert set(REFUSAL_KEYS) >= DEVICE_REFUSALS


#: (exception class, translation key, placeholders) of every refusal with a mapping of its own.
EXPECTED_SPECIAL: dict[str, tuple[type[HomeAssistantError], str]] = {
    "writes_disabled": (ServiceValidationError, "control_disabled"),
    "unavailable": (HomeAssistantError, "unavailable"),
    # the device cannot take a change now: not a usage error
    "bus_down": (HomeAssistantError, "bus_down"),
    "zone_offline": (HomeAssistantError, "zone_offline"),
    "vmc_offline": (HomeAssistantError, "vmc_offline"),
    "vmc_busy": (HomeAssistantError, "vmc_busy"),
}


def _expected(reason: str) -> tuple[type[HomeAssistantError], str, dict[str, str] | None]:
    """(exception class, translation key, placeholders) for a library refusal."""
    if reason in EXPECTED_SPECIAL:
        kind, key = EXPECTED_SPECIAL[reason]
        return kind, key, None
    if reason in REFUSAL_KEYS:
        return ServiceValidationError, REFUSAL_KEYS[reason], None
    return ServiceValidationError, "write_refused", {"reason": reason}


ALL_REASONS = sorted({*REFUSAL_KEYS, *GENERIC_REFUSALS, *SPECIAL_REFUSALS, "added_later"})


@pytest.mark.parametrize("reason", ALL_REASONS)
def test_raise_refused(reason: str) -> None:
    """raise_refused maps every reason; an unknown one is write_refused with the reason."""
    kind, key, placeholders = _expected(reason)
    with pytest.raises(HomeAssistantError) as err:
        control.raise_refused(reason)
    assert type(err.value) is kind
    _assert_key(err, key)
    assert (err.value.translation_placeholders or None) == placeholders


async def test_library_refusals_mapped(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A refusal raised by the library call maps like raise_refused, for every reason."""
    client = harness.client
    for reason in ALL_REASONS:
        kind, key, placeholders = _expected(reason)
        refusal = RehomWriteRefusedError(reason, f"refused: {reason}")
        with (
            patch.object(client, "set_zone_offset", side_effect=refusal),
            pytest.raises(HomeAssistantError) as err,
        ):
            await control.async_set_zone_offset(init_integration, ZONE, 1)
        assert type(err.value) is kind, reason
        _assert_key(err, key)
        assert (err.value.translation_placeholders or None) == placeholders, reason
        assert err.value.__cause__ is refusal
    assert harness.writes == []


@pytest.mark.parametrize(
    ("device_patch", "call", "key"),
    [
        (
            None,
            lambda e: control.async_set_zone_mode(e, ZONE, ZoneSetp.ECONOMY),
            "house_not_auto",
        ),
        (None, lambda e: control.async_set_predictive(e, False), "house_not_auto"),
        (
            set_values({"REHOM...TERMO_READONLY": "1"}),
            lambda e: control.async_set_zone_offset(e, ZONE, 1),
            "read_only",
        ),
        (
            set_values({**HOUSE_AUTO_VALUES, "REHOM...WEBSERVER": "0"}),
            lambda e: control.async_set_zone_mode(e, ZONE, ZoneSetp.ECONOMY),
            "crono_mode",
        ),
        (
            set_values({"DEUM.001..ST_MODE": "5"}),
            lambda e: control.async_set_vmc_fan(e, VMC, FanSpeed.MED),
            "fan_locked_by_mode",
        ),
        (
            set_values({"DEUM...ABILITA_VENTILA": "0"}),
            lambda e: control.async_set_vmc_mode(e, VMC, VmcMode.VENTILATE),
            "mode_not_available",
        ),
    ],
    ids=["zone_mode_manual", "predictive_manual", "read_only", "crono", "standby", "no_ventilate"],
)
async def test_real_refusals(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    call: ControlCall,
    key: str,
) -> None:
    """The library's own guards on the replayed state: translated, and nothing is sent."""
    calls = harness.transport_calls
    with pytest.raises(ServiceValidationError) as err:
        await call(init_integration)
    _assert_key(err, key)
    assert harness.transport_calls == calls


@pytest.mark.parametrize(
    ("device_patch", "call", "key"),
    [
        (
            set_values({"REHOM...WEBSERVER": "2"}),
            lambda e: control.async_set_house_preset(e, MasterPreset.ECONOMY),
            "bus_down",
        ),
        (
            set_values({"DEUM.001..ST_STATO_DEUM": "3"}),
            lambda e: control.async_set_vmc_mode(e, VMC, VmcMode.VENTILATE),
            "vmc_busy",
        ),
        (
            set_values({"DEUM.001..ST_STATO_DEUM": "2"}),
            lambda e: control.async_set_vmc_fan(e, VMC, FanSpeed.MED),
            "vmc_busy",
        ),
    ],
    ids=["bus_down", "vmc_forced", "vmc_error"],
)
async def test_real_device_refusals(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    call: ControlCall,
    key: str,
) -> None:
    """The device cannot take a change now: HomeAssistantError, and nothing is sent."""
    calls = harness.transport_calls
    with pytest.raises(HomeAssistantError) as err:
        await call(init_integration)
    _assert_device_error(err, key)
    assert isinstance(err.value.__cause__, RehomWriteRefusedError)
    assert harness.transport_calls == calls


async def test_unknown_zone(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A reason without a message of its own: write_refused with the reason."""
    with pytest.raises(ServiceValidationError) as err:
        await control.async_set_zone_offset(init_integration, "099", 1)
    _assert_key(err, "write_refused")
    assert err.value.translation_placeholders == {"reason": "unknown_zone"}
    assert harness.writes == []


# -- may_have_been_applied (the setpoint repair's two abort reasons) -------------------------


def _error(kind: type[HomeAssistantError], key: str, domain: str = DOMAIN) -> HomeAssistantError:
    return kind(translation_domain=domain, translation_key=key)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_error(HomeAssistantError, "write_not_confirmed"), True),
        (_error(HomeAssistantError, "write_failed"), True),
        (HomeAssistantError("unexpected"), True),  # unknown: assume it may have
        (_error(HomeAssistantError, "some_key", "other_domain"), True),
        (_error(HomeAssistantError, "unavailable"), False),
        (_error(HomeAssistantError, "not_ready"), False),
        (_error(HomeAssistantError, "invalid_auth"), False),
        (_error(HomeAssistantError, "write_refused"), False),  # forbidden_request
        *((_error(HomeAssistantError, reason), False) for reason in sorted(DEVICE_REFUSALS)),
        (_error(ServiceValidationError, "house_not_auto"), False),
        (_error(ServiceValidationError, "not_verified"), False),
        (_error(ServiceValidationError, "control_disabled"), False),
        (ServiceValidationError("untranslated"), False),
    ],
)
def test_may_have_been_applied(error: HomeAssistantError, expected: bool) -> None:
    """Only a write sent (or maybe sent) and not confirmed may have reached the controller."""
    assert control.may_have_been_applied(error) is expected


@pytest.mark.parametrize("reason", sorted({*REFUSAL_KEYS, *GENERIC_REFUSALS, *SPECIAL_REFUSALS}))
def test_refusals_were_not_applied(reason: str) -> None:
    """Every library refusal maps to an error that says nothing was sent."""
    with pytest.raises(HomeAssistantError) as err:
        control.raise_refused(reason)
    assert control.may_have_been_applied(err.value) is False


async def test_write_errors_may_have_been_applied(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The errors of a write that failed or was not confirmed may have been applied."""
    harness.errors["post_bulk_update"] = RehomTimeoutError("replay: POST timed out")
    with pytest.raises(HomeAssistantError) as err:
        await control.async_set_zone_offset(init_integration, ZONE, 1)
    assert control.may_have_been_applied(err.value) is True


# -- the harness's write path (every control test relies on it) ----------------------------


async def test_harness_override_write(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """An override write is gate-checked, recorded, applied (one row per path) and echoed."""
    record = {
        "Gruppo": "PROG_OVERRIDE",
        "Unita": ZONE,
        "SubUni": "1",
        "Key": "PROG_GIORNO_ESTATE",
        "Valore": ",".join(["3"] * 48),
        "Impostazione": "2026-09-25 12:00:00",
        "Scadenza": "2026-09-25 13:59:59",
    }
    dropped = harness.device.frames_dropped
    assert await harness.post_bulk_update(OVERRIDES_WRITE_PATH, [record]) == 204
    assert harness.written_values == [{"PROG_OVERRIDE.001.1.PROG_GIORNO_ESTATE": record["Valore"]}]
    assert harness.device.frames_dropped == dropped  # the echo was delivered
    old = {**record, "Valore": ",".join(["1"] * 48)}
    other = {**record, "Unita": "002"}
    assert harness.applied_overrides([old, other]) == [record, other]
    assert harness.applied_overrides([]) == [record]
    assert harness.applied_interface(rows := [{"Gruppo": "REHOM", "Key": "MODO"}]) is rows

    with pytest.raises(ForbiddenRequestError):  # the library's own write gate
        await harness.post_bulk_update(OVERRIDES_WRITE_PATH, [{**record, "Unita": "000"}])
    assert len(harness.writes) == 1
