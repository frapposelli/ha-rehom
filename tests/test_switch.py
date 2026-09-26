"""Switch platform on the replay."""

from __future__ import annotations

from aiorehom.replay import ReplayData
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.rehom.switch import RehomVmcSwitch

from .harness import RehomHarness, T, termo_update
from .platform_helpers import get_entity, get_state, is_available

PREDICTIVE = "switch.rehom_plant_predictive_algorithm"


def _without_predictive(data: ReplayData) -> None:
    """A controller without the ALG_ATTIVO row."""
    data.interface[:] = [row for row in data.interface if row.get("Key") != "ALG_ATTIVO"]


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the switch platform."""
    return [Platform.SWITCH]


async def test_snapshot(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """Only the predictive switch exists on the reference plant (free cooling hidden)."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_values(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, init_integration: MockConfigEntry
) -> None:
    """Predictive algorithm on (config category); no free-cooling switch."""
    assert get_state(hass, PREDICTIVE).state == STATE_ON
    entry = entity_registry.async_get(PREDICTIVE)
    assert entry is not None
    assert entry.entity_category is EntityCategory.CONFIG
    assert hass.states.async_entity_ids("switch") == [PREDICTIVE]


@pytest.mark.parametrize(
    "device_patch",
    [{"extra_frames": [(T("10:23:00"), termo_update("REHOM...ALG_ATTIVO", "0"))]}],
)
async def test_predictive_off(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The switch follows ALG_ATTIVO."""
    await harness.advance_to(T("10:23:05"))
    assert get_state(hass, PREDICTIVE).state == STATE_OFF


@pytest.mark.parametrize("device_patch", [{"patch": _without_predictive}])
async def test_no_predictive_row(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Without ALG_ATTIVO there is no predictive switch."""
    assert hass.states.async_entity_ids("switch") == []


@pytest.mark.parametrize(
    "device_patch",
    [
        {
            "extra_frames": [
                (T("10:23:00"), termo_update("DEUM...ABILITA_F_COOLING", "2")),
                (T("10:23:10"), termo_update("DEUM.001..FREE_COOLING", "1")),
                (T("10:23:10"), termo_update("DEUM.001..ERR_FREE_COOLING", "1")),
                (T("10:23:20"), termo_update("REHOM...PRESENZA_DEUM", "1,0,0")),
            ]
        }
    ],
)
async def test_writable_free_cooling(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Writable free cooling adds a switch per VMC (without a reload), with its error flag."""
    entity_id = "switch.vmc_001_free_cooling"
    assert hass.states.get(entity_id) is None
    await harness.advance_to(T("10:23:05"))
    state = get_state(hass, entity_id)
    assert state.state == STATE_OFF
    assert state.attributes["error"] is False
    assert get_state(hass, "switch.vmc_002_free_cooling").state == STATE_OFF
    await harness.advance_to(T("10:23:15"))
    state = get_state(hass, entity_id)
    assert state.state == STATE_ON
    assert state.attributes["error"] is True
    await harness.advance_to(T("10:23:25"))
    assert not is_available(hass, "switch.vmc_002_free_cooling")
    absent = get_entity(hass, "switch.vmc_002_free_cooling")
    assert isinstance(absent, RehomVmcSwitch)
    assert absent.is_on is None
    assert absent.extra_state_attributes is None
