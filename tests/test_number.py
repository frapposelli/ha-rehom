"""Zone offset number platform on the replay."""

from __future__ import annotations

from homeassistant.components.number import (
    ATTR_MAX,
    ATTR_MIN,
    ATTR_MODE,
    ATTR_STEP,
    NumberDeviceClass,
    NumberMode,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    EntityCategory,
    Platform,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.rehom.number import RehomZoneOffsetNumber

from .harness import FIXTURE_ZONES, RehomHarness, T, termo_update
from .platform_helpers import get_entity, get_state, is_available


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the number platform."""
    return [Platform.NUMBER]


async def test_snapshot(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """Every zone offset at the replay start."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_values(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, init_integration: MockConfigEntry
) -> None:
    """Zone 002 = -1, others 0; -3..3 step 1 °C, box, config, temperature difference."""
    for zone in FIXTURE_ZONES:
        entity_id = f"number.zona_{zone}_temperature_offset"
        state = get_state(hass, entity_id)
        assert state.state == ("-1.0" if zone == "002" else "0.0")
        assert state.attributes[ATTR_MIN] == -3.0
        assert state.attributes[ATTR_MAX] == 3.0
        assert state.attributes[ATTR_STEP] == 1.0
        assert state.attributes[ATTR_MODE] == NumberMode.BOX
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTemperature.CELSIUS
        assert state.attributes[ATTR_DEVICE_CLASS] == NumberDeviceClass.TEMPERATURE_DELTA
        entry = entity_registry.async_get(entity_id)
        assert entry is not None
        assert entry.entity_category is EntityCategory.CONFIG


@pytest.mark.parametrize(
    "device_patch",
    [
        {
            "extra_frames": [
                (T("10:23:00"), termo_update("ZONA.001..DELTA_SETP_CORRENTE", "2")),
                (
                    T("10:23:00"),
                    termo_update(
                        "REHOM...PRESENZA_SONDE",
                        "1,1,1,0,0,0,0,0,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0",
                    ),
                ),
            ]
        }
    ],
)
async def test_changes_and_absent_zone(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The value follows DELTA_SETP_CORRENTE; an absent zone's offset is unavailable."""
    await harness.advance_to(T("10:23:05"))
    assert get_state(hass, "number.zona_001_temperature_offset").state == "2.0"
    assert not is_available(hass, "number.zona_011_temperature_offset")
    absent = get_entity(hass, "number.zona_011_temperature_offset")
    assert isinstance(absent, RehomZoneOffsetNumber)
    assert absent.native_value is None


async def test_fahrenheit_converts_the_offset_as_a_difference(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, init_integration: MockConfigEntry
) -> None:
    """Shown in °F (entity setting), -1 °C of offset reads -1.8 °F, not 30.2 °F (absolute).

    Without a device class Home Assistant offers no unit choice at all.
    """
    entity_id = "number.zona_002_temperature_offset"
    entity_registry.async_update_entity_options(
        entity_id, "number", {"unit_of_measurement": UnitOfTemperature.FAHRENHEIT}
    )
    await hass.async_block_till_done()
    state = get_state(hass, entity_id)
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTemperature.FAHRENHEIT
    assert float(state.state) == pytest.approx(-1.8)
    assert state.attributes[ATTR_MIN] == pytest.approx(-5.4)
    assert state.attributes[ATTR_MAX] == pytest.approx(5.4)
