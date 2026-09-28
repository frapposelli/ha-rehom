"""VMC mode select platform on the replay."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import ATTR_OPTIONS
from homeassistant.const import STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.rehom.select import RehomVmcModeSelect

from .harness import RehomHarness, T, termo_update
from .platform_helpers import get_entity, get_state, is_available

SELECT = "select.vmc_001_operating_mode"
#: The fixture's selectable modes without rapid_renewal (a timed cycle, never offered).
FIXTURE_OPTIONS = ["stop", "dehumidify", "dehumidify_cool", "cool", "ventilate"]


def _frames(*frames: tuple[str, str, str]) -> dict[str, Any]:
    """``device_patch`` with synthetic ``termo`` frames ``(time, path, value)``."""
    return {"extra_frames": [(T(at), termo_update(path, value)) for at, path, value in frames]}


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the select platform."""
    return [Platform.SELECT]


async def test_snapshot(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """Both VMC mode selects at the replay start."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_values(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Selectable modes in enum order, without the rapid ones; current mode dehumidify."""
    for entity_id in (SELECT, "select.vmc_002_operating_mode"):
        state = get_state(hass, entity_id)
        assert state.state == "dehumidify"
        assert state.attributes[ATTR_OPTIONS] == FIXTURE_OPTIONS


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:23:00", "DEUM.001..ST_MODE", "4"),
            ("10:23:10", "DEUM.001..ST_MODE", "x"),
            ("10:23:10", "REHOM...PRESENZA_DEUM", "1,0,0"),
        )
    ],
)
async def test_current_mode_not_selectable(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A mode set elsewhere but not selectable is still listed; unknown and absent handled."""
    await harness.advance_to(T("10:23:05"))
    state = get_state(hass, SELECT)
    assert state.state == "heat"
    assert state.attributes[ATTR_OPTIONS] == [
        "stop",
        "dehumidify",
        "dehumidify_cool",
        "cool",
        "heat",
        "ventilate",
    ]
    await harness.advance_to(T("10:23:15"))
    state = get_state(hass, SELECT)
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_OPTIONS] == FIXTURE_OPTIONS
    assert not is_available(hass, "select.vmc_002_operating_mode")
    absent = get_entity(hass, "select.vmc_002_operating_mode")
    assert isinstance(absent, RehomVmcModeSelect)
    assert absent.options == []
    assert absent.current_option is None


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("10:23:00", "DEUM.001..ST_MODE", "6"),
            ("10:23:00", "DEUM.002..ST_MODE", "7"),
            ("10:23:10", "DEUM.001..ST_MODE", "1"),
        )
    ],
)
async def test_rapid_mode_listed_only_while_current(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A running rapid cycle is shown (selectable or not), and dropped once it ends."""
    await harness.advance_to(T("10:23:05"))
    state = get_state(hass, SELECT)
    assert state.state == "rapid_renewal"
    assert state.attributes[ATTR_OPTIONS] == [
        "stop",
        "dehumidify",
        "dehumidify_cool",
        "cool",
        "rapid_renewal",
        "ventilate",
    ]
    state = get_state(hass, "select.vmc_002_operating_mode")
    assert state.state == "rapid_heat"
    assert state.attributes[ATTR_OPTIONS] == [
        "stop",
        "dehumidify",
        "dehumidify_cool",
        "cool",
        "rapid_heat",
        "ventilate",
    ]
    await harness.advance_to(T("10:23:15"))
    state = get_state(hass, SELECT)
    assert state.state == "dehumidify"
    assert state.attributes[ATTR_OPTIONS] == FIXTURE_OPTIONS
