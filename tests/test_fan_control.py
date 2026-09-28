"""VMC fan with "Enable control" on: the writes Home Assistant sends.

Each case calls the fan's action as a user or automation would and checks what
reached the replayed controller (the exact records), and what Home Assistant
shows when the call returns (the confirmed state).  Only the min (50 %), med
(75 %) and max (100 %) speeds of a discrete fan, and the dehumidify and
ventilate modes, were tested on a real controller; the attenuated speed (25 %)
and turning off (the stop mode) were not.

The fixture's VMC 001 runs in dehumidify at speed min (50 %); its selectable
modes are stop, dehumidify, dehumidify_cool, cool, rapid_renewal and ventilate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import patch

from aiorehom import FanSpeed, RehomWriteNotConfirmedError, VmcMode
from homeassistant.components.fan import (
    ATTR_PERCENTAGE,
    DOMAIN as FAN_DOMAIN,
    SERVICE_DECREASE_SPEED,
    SERVICE_INCREASE_SPEED,
    SERVICE_SET_PERCENTAGE,
    FanEntityFeature,
)
from homeassistant.const import (
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TOGGLE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rehom import control
from custom_components.rehom.control import ControlOp
from custom_components.rehom.fan import RehomVmcFan

from .conftest import CONTROL_OPTIONS
from .control_helpers import (
    CONTROL_OFF_OPTIONS,
    assert_device_error,
    assert_key,
    call_action,
    call_action_until_done,
)
from .harness import RehomHarness, T, set_values, termo_update
from .platform_helpers import get_entity, get_state

FAN = "fan.vmc_001"
MODE_PATH = "DEUM.001..ST_MODE"
SPEED_PATH = "DEUM.001..COM_VENTILA"
#: VMC 001 stopped (off) at the replay start.
STOPPED = set_values({MODE_PATH: "0"})
#: The modes that turn the fan on, all hidden: only stop is left.
ONLY_STOP = {
    f"DEUM...{key}": "0"
    for key in (
        "DEUM_ARIA_NEUTRA",
        "DEUM_INT_FREDDO",
        "ABILITA_INTEGR_FREDDO",
        "ABILITA_RINN_RAP",
        "ABILITA_VENTILA",
    )
}


def _stopped_at_10_23(start_mode: str, also: Mapping[str, str] | None = None) -> dict[str, Any]:
    """VMC 001 starts in ``start_mode`` and is stopped at 10:23:00, with the values ``also``."""
    changes = {MODE_PATH: "0", **(also or {})}
    frames = [(T("10:23:00"), termo_update(path, value)) for path, value in changes.items()]
    return set_values({MODE_PATH: start_mode}, extra_frames=frames)


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """ "Enable control" on."""
    return dict(CONTROL_OPTIONS)


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the fan platform."""
    return [Platform.FAN]


async def _call(hass: HomeAssistant, service: str, data: dict[str, Any] | None = None) -> None:
    await call_action(hass, FAN_DOMAIN, service, FAN, data)


def _on_and_percentage(hass: HomeAssistant) -> tuple[str, int | None]:
    state = get_state(hass, FAN)
    return (state.state, state.attributes[ATTR_PERCENTAGE])


# -- speed ----------------------------------------------------------------------------------


async def test_set_speed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Min (50 %) -> med (75 %) -> min, each shown as soon as the call returns."""
    await _call(hass, SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 75})
    assert harness.written_values == [{SPEED_PATH: "2"}]
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert _on_and_percentage(hass) == (STATE_ON, 75)
    await _call(hass, SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 50})
    assert harness.written_values == [{SPEED_PATH: "2"}, {SPEED_PATH: "1"}]
    assert _on_and_percentage(hass) == (STATE_ON, 50)


@pytest.mark.parametrize(
    ("percentage", "written"),
    [(51, "2"), (60, "2"), (26, None), (40, None)],
)
async def test_percentage_picks_a_speed(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    percentage: int,
    written: str | None,
) -> None:
    """51-75 % is med; 26-50 % is min, the current speed, so nothing is sent."""
    await _call(hass, SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: percentage})
    assert harness.written_values == ([] if written is None else [{SPEED_PATH: written}])


async def test_increase_speed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """One step up from min is med, and from med max."""
    await _call(hass, SERVICE_INCREASE_SPEED)
    assert harness.written_values == [{SPEED_PATH: "2"}]
    assert _on_and_percentage(hass) == (STATE_ON, 75)
    await _call(hass, SERVICE_INCREASE_SPEED)
    assert harness.written_values == [{SPEED_PATH: "2"}, {SPEED_PATH: "3"}]
    assert _on_and_percentage(hass) == (STATE_ON, 100)


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 100}),
        (SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 76}),
        (SERVICE_TURN_ON, {ATTR_PERCENTAGE: 100}),  # already on: only the speed
    ],
    ids=["100", "76", "turn_on_100"],
)
async def test_set_max(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    service: str,
    data: dict[str, Any],
) -> None:
    """Max (76-100 %) is verified: one record, shown as soon as the call returns."""
    await _call(hass, service, data)
    assert harness.written_values == [{SPEED_PATH: "3"}]
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert _on_and_percentage(hass) == (STATE_ON, 100)


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 25}),  # attenuated
        (SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 1}),  # attenuated
        (SERVICE_DECREASE_SPEED, {}),  # min -> attenuated
        (SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 0}),  # the stop mode
        (SERVICE_TURN_OFF, {}),
        (SERVICE_TOGGLE, {}),  # on -> off
        (SERVICE_TURN_ON, {ATTR_PERCENTAGE: 0}),  # off
        (SERVICE_TURN_ON, {ATTR_PERCENTAGE: 25}),  # already on: attenuated
    ],
    ids=["25", "1", "decrease", "set_0", "turn_off", "toggle", "turn_on_0", "turn_on_25"],
)
async def test_not_verified(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    service: str,
    data: dict[str, Any],
) -> None:
    """The attenuated speed and the stop mode, never tested: not_verified, nothing sent."""
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, service, data)
    assert_key(err, "not_verified")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls
    assert _on_and_percentage(hass) == (STATE_ON, 50)


@pytest.mark.parametrize("device_patch", [set_values({"DEUM...STEP": "-1"})])
async def test_continuous_fan_not_verified(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Only discrete fans were tested: a continuous fan's speed is refused."""
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 60})
    assert_key(err, "not_verified")
    assert harness.writes == []


@pytest.mark.parametrize("device_patch", [set_values({MODE_PATH: "5"})])
async def test_speed_fixed_in_standby(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """In standby the controller fixes the speed: fan_locked_by_mode, nothing sent."""
    assert get_state(hass, FAN).state == STATE_OFF
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 75})
    assert_key(err, "fan_locked_by_mode")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls


async def test_speed_not_confirmed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Sent, but the controller never reports it: write_not_confirmed, still 50 %."""
    harness.echo = False
    harness.apply = False
    with pytest.raises(HomeAssistantError) as err:
        await call_action_until_done(
            harness, FAN_DOMAIN, SERVICE_SET_PERCENTAGE, FAN, {ATTR_PERCENTAGE: 75}
        )
    assert_device_error(err, "write_not_confirmed")
    assert isinstance(err.value.__cause__, RehomWriteNotConfirmedError)
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert harness.written_values == [{SPEED_PATH: "2"}]
    assert _on_and_percentage(hass) == (STATE_ON, 50)


# -- turn on ---------------------------------------------------------------------------------


async def test_turn_on_while_on(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Already on: nothing to send; with a percentage, only the speed."""
    await _call(hass, SERVICE_TURN_ON)
    assert harness.writes == []
    await _call(hass, SERVICE_TURN_ON, {ATTR_PERCENTAGE: 75})
    assert harness.written_values == [{SPEED_PATH: "2"}]
    assert _on_and_percentage(hass) == (STATE_ON, 75)


@pytest.mark.parametrize(
    ("device_patch", "service"),
    [
        (STOPPED, SERVICE_TURN_ON),
        (STOPPED, SERVICE_TOGGLE),
        (set_values({MODE_PATH: "5"}), SERVICE_TURN_ON),  # standby
    ],
    ids=["stop", "stop_toggle", "standby"],
)
async def test_turn_on_from_off(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, service: str
) -> None:
    """Off, no mode seen since start: the first verified mode that turns it on (dehumidify)."""
    assert get_state(hass, FAN).state == STATE_OFF
    await _call(hass, service)
    assert harness.written_values == [{MODE_PATH: "1"}]
    assert get_state(hass, FAN).state == STATE_ON


@pytest.mark.parametrize("device_patch", [STOPPED])
@pytest.mark.parametrize(("percentage", "speed"), [(75, "2"), (100, "3")])
async def test_turn_on_with_speed(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    percentage: int,
    speed: str,
) -> None:
    """Off: the mode first, then the speed (two writes, in that order)."""
    await _call(hass, SERVICE_TURN_ON, {ATTR_PERCENTAGE: percentage})
    assert harness.written_values == [{MODE_PATH: "1"}, {SPEED_PATH: speed}]
    assert _on_and_percentage(hass) == (STATE_ON, percentage)


@pytest.mark.parametrize(
    ("device_patch", "percentage"),
    [(STOPPED, 25), (set_values({MODE_PATH: "0", "DEUM...STEP": "-1"}), 100)],
    ids=["attenuated", "continuous"],
)
async def test_turn_on_unverified_speed_sends_nothing(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, percentage: int
) -> None:
    """An unverified speed is refused before the mode is sent: all or nothing."""
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_TURN_ON, {ATTR_PERCENTAGE: percentage})
    assert_key(err, "not_verified")
    assert harness.writes == []
    assert get_state(hass, FAN).state == STATE_OFF


@pytest.mark.parametrize(
    "device_patch", [set_values({MODE_PATH: "0", "DEUM.001..ABILITA_VENTOLA": "1"})]
)
async def test_turn_on_speed_not_settable_sends_nothing(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A fan whose speed cannot be set: turning on at 75 % is refused before the mode is sent."""
    assert not get_state(hass, FAN).attributes[ATTR_SUPPORTED_FEATURES] & FanEntityFeature.SET_SPEED
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_TURN_ON, {ATTR_PERCENTAGE: 75})
    assert_key(err, "fan_not_writable")
    assert harness.writes == []
    await _call(hass, SERVICE_TURN_ON)  # without a speed it turns on
    assert harness.written_values == [{MODE_PATH: "1"}]
    assert get_state(hass, FAN).state == STATE_ON


@pytest.mark.parametrize(
    ("device_patch", "written"),
    [
        (_stopped_at_10_23("8"), "8"),  # ventilate, as before
        (_stopped_at_10_23("8", {"DEUM...ABILITA_VENTILA": "0"}), "1"),  # no longer selectable
        (_stopped_at_10_23("6"), "1"),  # a rapid cycle is never restored
    ],
    ids=["last_mode", "last_mode_hidden", "rapid"],
)
async def test_turn_on_restores_the_last_mode(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, written: str
) -> None:
    """Turning on selects the mode the VMC last ran in, if it can still be selected."""
    await harness.advance_to(T("10:23:05"))
    assert get_state(hass, FAN).state == STATE_OFF
    await _call(hass, SERVICE_TURN_ON)
    assert harness.written_values == [{MODE_PATH: written}]
    assert get_state(hass, FAN).state == STATE_ON


@pytest.mark.parametrize("device_patch", [_stopped_at_10_23("8")])
async def test_last_mode_not_kept_across_a_reload(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The last mode is remembered only while the entity lives: after a reload, the first mode.

    Ventilate ran until 10:23; the reloaded fan has only seen it stopped, so
    turning on selects the first selectable verified mode (dehumidify).
    """
    await harness.advance_to(T("10:23:05"))
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await harness.settle()
    assert get_state(hass, FAN).state == STATE_OFF
    await _call(hass, SERVICE_TURN_ON)
    assert harness.written_values == [{MODE_PATH: "1"}]
    assert get_state(hass, FAN).state == STATE_ON


@pytest.mark.parametrize(
    "device_patch",
    [
        # dehumidify hidden: dehumidify_cool and cool come first, ventilate is verified
        set_values({MODE_PATH: "0", "DEUM...DEUM_ARIA_NEUTRA": "0"}),
        set_values({MODE_PATH: "5", "DEUM...DEUM_ARIA_NEUTRA": "0"}),  # from standby
    ],
    ids=["stop", "standby"],
)
async def test_turn_on_first_verified_mode(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """No mode seen since start: the first selectable mode that is verified (ventilate)."""
    assert get_state(hass, FAN).state == STATE_OFF
    await _call(hass, SERVICE_TURN_ON)
    assert harness.written_values == [{MODE_PATH: "8"}]
    assert get_state(hass, FAN).state == STATE_ON


@pytest.mark.parametrize(
    "device_patch",
    [set_values({MODE_PATH: "0", "DEUM...DEUM_ARIA_NEUTRA": "0", "DEUM...ABILITA_VENTILA": "0"})],
)
async def test_turn_on_no_verified_mode(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Only unverified on-modes are selectable (dehumidify_cool, cool): not_verified, no write."""
    assert get_state(hass, FAN).attributes[ATTR_SUPPORTED_FEATURES] & FanEntityFeature.TURN_ON
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_TURN_ON, {ATTR_PERCENTAGE: 75})
    assert_key(err, "not_verified")
    assert harness.writes == []
    assert get_state(hass, FAN).state == STATE_OFF


@pytest.mark.parametrize("device_patch", [_stopped_at_10_23("3")])
async def test_turn_on_unverified_last_mode(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The last mode was cool, never tested on a real controller: not_verified, nothing sent."""
    await harness.advance_to(T("10:23:05"))
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_TURN_ON)
    assert_key(err, "not_verified")
    assert harness.writes == []


@pytest.mark.parametrize("device_patch", [set_values({MODE_PATH: "0", **ONLY_STOP})])
async def test_nothing_to_turn_on_to(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Only stop is selectable: no turn-on feature, and toggle is refused (mode_not_available)."""
    features = get_state(hass, FAN).attributes[ATTR_SUPPORTED_FEATURES]
    assert features == FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_OFF
    with pytest.raises(ServiceNotSupported):
        await _call(hass, SERVICE_TURN_ON)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_TOGGLE)
    assert_key(err, "mode_not_available")
    assert harness.writes == []


@pytest.mark.parametrize(
    "device_patch", [set_values({MODE_PATH: "0", "DEUM.001..ST_STATO_DEUM": "3"})]
)
async def test_turn_on_forced_vmc(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A VMC forced from elsewhere cannot be changed: vmc_busy (a device error), nothing sent."""
    assert get_state(hass, FAN).state == STATE_OFF
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, SERVICE_TURN_ON)
    assert_device_error(err, "vmc_busy")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls


@pytest.mark.parametrize("device_patch", [STOPPED])
async def test_turn_on_not_confirmed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The mode is sent but never reported: write_not_confirmed, still off, no speed sent."""
    harness.echo = False
    harness.apply = False
    with pytest.raises(HomeAssistantError) as err:
        await call_action_until_done(
            harness, FAN_DOMAIN, SERVICE_TURN_ON, FAN, {ATTR_PERCENTAGE: 75}
        )
    assert_device_error(err, "write_not_confirmed")
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert harness.written_values == [{MODE_PATH: "1"}]
    assert get_state(hass, FAN).state == STATE_OFF


# -- once more values are verified ---------------------------------------------------------


async def test_widened_verified_values(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Verifying stop and attenuated (a change to control.VERIFIED_VALUES only) enables them."""
    verified = control.VERIFIED_VALUES
    widened = {
        **verified,
        ControlOp.VMC_FAN: verified[ControlOp.VMC_FAN] | {FanSpeed.ATTENUATED},
        ControlOp.VMC_MODE: verified[ControlOp.VMC_MODE] | {VmcMode.STOP},
    }
    with patch.object(control, "VERIFIED_VALUES", widened):
        await _call(hass, SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 25})
        assert _on_and_percentage(hass) == (STATE_ON, 25)
        await _call(hass, SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 0})
        assert get_state(hass, FAN).state == STATE_OFF
        await _call(hass, SERVICE_TURN_ON, {ATTR_PERCENTAGE: 75})  # back to dehumidify
        assert _on_and_percentage(hass) == (STATE_ON, 75)
        await _call(hass, SERVICE_TURN_ON, {ATTR_PERCENTAGE: 0})
        assert get_state(hass, FAN).state == STATE_OFF
    assert harness.written_values == [
        {SPEED_PATH: "4"},
        {MODE_PATH: "0"},
        {MODE_PATH: "1"},
        {SPEED_PATH: "2"},
        {MODE_PATH: "0"},
    ]


# -- control off, absent VMC ------------------------------------------------------------------

DISABLED_ACTIONS: list[tuple[str, dict[str, Any]]] = [
    (SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 75}),
    (SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: 0}),
    (SERVICE_INCREASE_SPEED, {}),
    (SERVICE_DECREASE_SPEED, {}),
    (SERVICE_TURN_ON, {}),
    (SERVICE_TURN_ON, {ATTR_PERCENTAGE: 75}),
    (SERVICE_TURN_OFF, {}),
    (SERVICE_TOGGLE, {}),
]


@pytest.mark.parametrize("entry_options", [CONTROL_OFF_OPTIONS])
@pytest.mark.parametrize("device_patch", [None, STOPPED], ids=["on", "off"])
@pytest.mark.parametrize(
    ("service", "data"),
    DISABLED_ACTIONS,
    ids=[f"{service}-{data}" for service, data in DISABLED_ACTIONS],
)
async def test_control_disabled(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    service: str,
    data: dict[str, Any],
) -> None:
    """The default entry (control off): control_disabled, no request, the state unchanged."""
    calls = harness.transport_calls
    before = _on_and_percentage(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, service, data)
    assert_key(err, "control_disabled")
    await harness.settle()
    assert harness.transport_calls == calls
    assert _on_and_percentage(hass) == before


@pytest.mark.parametrize(
    "device_patch",
    [
        {
            "extra_frames": [
                (T("10:23:00"), termo_update("REHOM...PRESENZA_DEUM", "1,0,0")),
            ]
        }
    ],
)
async def test_absent_vmc(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Home Assistant calls no action on an unavailable fan; a direct call is refused."""
    await harness.advance_to(T("10:23:05"))
    fan = get_entity(hass, "fan.vmc_002")
    assert isinstance(fan, RehomVmcFan)
    with pytest.raises(ServiceValidationError) as err:
        await fan.async_turn_on()
    assert_key(err, "write_refused")
    assert err.value.translation_placeholders == {"reason": "unknown_vmc"}
    with pytest.raises(ServiceValidationError) as err:
        await fan.async_set_percentage(75)
    assert_key(err, "write_refused")
    assert err.value.translation_placeholders == {"reason": "unknown_vmc"}
    with pytest.raises(ServiceValidationError) as err:
        await fan.async_turn_off()
    assert_key(err, "not_verified")
    assert harness.writes == []
