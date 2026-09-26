"""VMC fan platform on the replay."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import ATTR_PERCENTAGE, ATTR_PERCENTAGE_STEP, FanEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES, STATE_OFF, STATE_ON, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.rehom.fan import RehomVmcFan

from .harness import RehomHarness, T, termo_update
from .platform_helpers import get_entity, get_state, is_available

FAN = "fan.vmc_001"
ALL_FEATURES = FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_OFF | FanEntityFeature.TURN_ON


def _frames(*frames: tuple[str, str, str]) -> dict[str, Any]:
    """``device_patch`` with synthetic ``termo`` frames ``(time, path, value)``."""
    return {"extra_frames": [(T(at), termo_update(path, value)) for at, path, value in frames]}


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the fan platform."""
    return [Platform.FAN]


async def test_snapshot(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """Both VMC fans at the replay start."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_values(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Both VMCs on at 50 % (speed MIN of 4), with speed, off and on features."""
    for entity_id in ("fan.vmc_001", "fan.vmc_002"):
        state = get_state(hass, entity_id)
        assert state.state == STATE_ON
        assert state.attributes[ATTR_PERCENTAGE] == 50
        assert state.attributes[ATTR_PERCENTAGE_STEP] == 25
        assert state.attributes[ATTR_SUPPORTED_FEATURES] == ALL_FEATURES


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:23:00", "DEUM.001..COM_VENTILA", "0"),
            ("10:23:10", "DEUM.001..COM_VENTILA", "4"),
            ("10:23:20", "DEUM.001..COM_VENTILA", "2"),
            ("10:23:30", "DEUM.001..COM_VENTILA", "3"),
            ("10:23:40", "DEUM.001..ST_MODE", "0"),
            ("10:23:50", "DEUM.001..ST_MODE", "5"),
        )
    ],
)
async def test_discrete_speeds_and_modes(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Speed NONE is 0 % (still on), ATTENUATED 25 %, MED 75 %, MAX 100 %; STOP/STANDBY off."""
    expected = [
        ("10:23:05", STATE_ON, 0),
        ("10:23:15", STATE_ON, 25),
        ("10:23:25", STATE_ON, 75),
        ("10:23:35", STATE_ON, 100),
        ("10:23:45", STATE_OFF, 100),
        ("10:23:55", STATE_OFF, 100),
    ]
    for at, on_off, percentage in expected:
        await harness.advance_to(T(at))
        state = get_state(hass, FAN)
        assert (state.state, state.attributes[ATTR_PERCENTAGE]) == (on_off, percentage), at


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:23:00", "DEUM...STEP", "-1"),
            ("10:23:00", "DEUM.001..COM_VENTILA", "60"),
            ("10:23:10", "DEUM.001..COM_VENTILA", "0"),
        )
    ],
)
async def test_continuous_fan(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Continuous fans (global STEP -1): 100 steps over STEP_MIN..STEP_MAX, 0 at STEP_MIN."""
    await harness.advance_to(T("10:23:05"))
    state = get_state(hass, FAN)
    assert state.attributes[ATTR_PERCENTAGE] == 60
    assert state.attributes[ATTR_PERCENTAGE_STEP] == 1
    await harness.advance_to(T("10:23:15"))
    assert get_state(hass, FAN).attributes[ATTR_PERCENTAGE] == 0


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:23:00", "DEUM.001..ABILITA_VENTOLA", "1"),
            ("10:23:00", "DEUM...ABILITA_STOP", "0"),
        )
    ],
)
async def test_features_follow_availability(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A read-only fan has no speed feature; a hidden STOP mode removes turn-off."""
    await harness.advance_to(T("10:23:05"))
    assert get_state(hass, FAN).attributes[ATTR_SUPPORTED_FEATURES] == FanEntityFeature.TURN_ON
    # ABILITA_STOP is global: both VMCs lose turn-off; ABILITA_VENTOLA is per VMC
    assert get_state(hass, "fan.vmc_002").attributes[ATTR_SUPPORTED_FEATURES] == (
        FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_ON
    )


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            *(
                ("10:23:00", f"DEUM...{key}", "0")
                for key in (
                    "DEUM_ARIA_NEUTRA",
                    "DEUM_INT_FREDDO",
                    "ABILITA_INTEGR_FREDDO",
                    "ABILITA_RINN_RAP",
                    "ABILITA_VENTILA",
                )
            )
        )
    ],
)
async def test_only_stop_selectable(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """With STOP as the only selectable mode the fan cannot be turned on."""
    await harness.advance_to(T("10:23:05"))
    assert get_state(hass, FAN).attributes[ATTR_SUPPORTED_FEATURES] == (
        FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_OFF
    )


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:23:00", "REHOM...PRESENZA_DEUM", "1,0,0"),
            ("10:23:00", "DEUM.001..COM_VENTILA", "x"),
            ("10:23:00", "DEUM.001..ST_MODE", "x"),
        )
    ],
)
async def test_unknown_and_absent(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Unknown speed/mode read as unknown; an absent VMC's fan is unavailable."""
    await harness.advance_to(T("10:23:05"))
    fan = get_entity(hass, FAN)
    assert isinstance(fan, RehomVmcFan)
    assert fan.percentage is None
    assert fan.is_on is None
    assert not is_available(hass, "fan.vmc_002")
    absent = get_entity(hass, "fan.vmc_002")
    assert isinstance(absent, RehomVmcFan)
    assert absent.percentage is None
    assert absent.is_on is None
    assert absent.speed_count == 4
    assert absent.supported_features == FanEntityFeature(0)


@pytest.mark.parametrize(
    "device_patch",
    [_frames(("10:23:00", "DEUM...STEP", "-1"), ("10:23:00", "DEUM.001..COM_VENTILA", "x"))],
)
async def test_continuous_unknown_value(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A continuous fan with an unreadable value has an unknown percentage."""
    await harness.advance_to(T("10:23:05"))
    fan = get_entity(hass, FAN)
    assert isinstance(fan, RehomVmcFan)
    assert fan.percentage is None
