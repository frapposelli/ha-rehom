"""Climate platform: house and zone thermostats on the replay."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    ATTR_TARGET_TEMP_STEP,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.rehom.climate import RehomZoneClimate, _levels
from custom_components.rehom.const import DOMAIN, SERVICE_GET_SCHEDULE

from .harness import FIXTURE_ZONES, RehomHarness, T, termo_update
from .platform_helpers import (
    HOUSE_CLIMATE,
    ZONE_CLIMATE,
    auto_with_override,
    get_entity,
    get_state,
    is_available,
)

FIXTURE_TARGETS = {"001": 24.0, "002": 23.0, "003": 24.0, "009": 24.0, "010": 24.0, "011": 24.0}
#: Zone 011 absent from PRESENZA_SONDE; zone 004 added.
ZONES_WITHOUT_011 = "1,1,1,0,0,0,0,0,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0"
ZONES_WITH_004 = "1,1,1,1,0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"


def _frames(*frames: tuple[str, str], at: str = "10:23:00") -> dict[str, Any]:
    """``device_patch`` with synthetic ``termo`` frames at ``at`` (before zone 002 calls)."""
    return {"extra_frames": [(T(at), termo_update(path, value)) for path, value in frames]}


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the climate platform."""
    return [Platform.CLIMATE]


async def test_snapshot(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """Every climate entity at the replay start."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_timeline(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """House and zones on the recorded timeline (one replay: 10:22:06.8 -> 10:41:35)."""
    house = get_state(hass, HOUSE_CLIMATE)
    assert house.state == HVACMode.COOL
    assert house.attributes[ATTR_HVAC_MODES] == [HVACMode.OFF, HVACMode.AUTO, HVACMode.COOL]
    assert house.attributes[ATTR_PRESET_MODES] == ["none", "eco", "pre_comfort", "comfort"]
    assert house.attributes[ATTR_PRESET_MODE] == "comfort"
    assert house.attributes[ATTR_TEMPERATURE] == 24.0
    assert house.attributes[ATTR_CURRENT_TEMPERATURE] == 24.2
    assert house.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE
    assert house.attributes[ATTR_MIN_TEMP] == 18.0
    assert house.attributes[ATTR_MAX_TEMP] == 35.5
    assert house.attributes[ATTR_TARGET_TEMP_STEP] == 0.1
    assert house.attributes["controller_setpoint"] == 26.0
    assert house.attributes["setpoint_mismatch"] is True
    for zone in FIXTURE_ZONES:
        state = get_state(hass, ZONE_CLIMATE.format(zone=zone))
        assert state.state == HVACMode.COOL
        assert state.attributes[ATTR_PRESET_MODE] == "comfort"
        assert state.attributes[ATTR_PRESET_MODES] == [
            "none",
            "eco",
            "pre_comfort",
            "comfort",
            "temporary_comfort",
        ]
        assert state.attributes[ATTR_TEMPERATURE] == FIXTURE_TARGETS[zone]
        assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE
        assert state.attributes["controller_setpoint"] == 26.0
        # base 24 -> 21..27, whole degrees
        assert state.attributes[ATTR_MIN_TEMP] == 21.0
        assert state.attributes[ATTR_MAX_TEMP] == 27.0
        assert state.attributes[ATTR_TARGET_TEMP_STEP] == 1.0
    assert get_state(hass, "climate.zona_001").attributes[ATTR_CURRENT_TEMPERATURE] == 24.2
    assert get_state(hass, "climate.zona_001").attributes[ATTR_CURRENT_HUMIDITY] == 53.0
    # zone 011 has no humidity sensor: the attribute is left out
    assert ATTR_CURRENT_HUMIDITY not in get_state(hass, "climate.zona_011").attributes

    # 10:24:34.6 zone 002 starts calling: the house and zone 002 cool
    await harness.advance_to(T("10:24:30"))
    assert get_state(hass, HOUSE_CLIMATE).attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE
    await harness.advance_to(T("10:24:40"))
    assert get_state(hass, HOUSE_CLIMATE).attributes[ATTR_HVAC_ACTION] == HVACAction.COOLING
    assert get_state(hass, "climate.zona_002").attributes[ATTR_HVAC_ACTION] == HVACAction.COOLING
    assert get_state(hass, "climate.zona_001").attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE

    # 10:41:06.4 / 10:41:10.8 TEMP_COM and SET_POINT_TEMP 24.1 then 24: mismatch over
    await harness.advance_to(T("10:41:05"))
    assert get_state(hass, HOUSE_CLIMATE).attributes["controller_setpoint"] == 26.0
    await harness.advance_to(T("10:41:15"))
    house = get_state(hass, HOUSE_CLIMATE)
    assert house.attributes["controller_setpoint"] == 24.0
    assert house.attributes["setpoint_mismatch"] is False
    assert house.attributes[ATTR_TEMPERATURE] == 24.0

    # 10:41:18-10:41:25 zone SET_POINT_TEMP rows -> 24
    await harness.advance_to(T("10:41:26"))
    for zone in FIXTURE_ZONES:
        state = get_state(hass, ZONE_CLIMATE.format(zone=zone))
        assert state.attributes["controller_setpoint"] == 24.0

    # 10:41:33.7-10:41:34.0 zones 001, 009, 010 start calling
    await harness.advance_to(T("10:41:35"))
    actions = {
        zone: get_state(hass, ZONE_CLIMATE.format(zone=zone)).attributes[ATTR_HVAC_ACTION]
        for zone in FIXTURE_ZONES
    }
    assert actions == {
        "001": HVACAction.COOLING,
        "002": HVACAction.COOLING,
        "003": HVACAction.IDLE,
        "009": HVACAction.COOLING,
        "010": HVACAction.COOLING,
        "011": HVACAction.IDLE,
    }


@pytest.mark.parametrize("device_patch", [_frames(("REHOM...MODO", "0"))])
async def test_house_off(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """MODO 0: house and zones off, preset none, no target."""
    await harness.advance_to(T("10:23:05"))
    house = get_state(hass, HOUSE_CLIMATE)
    assert house.state == HVACMode.OFF
    assert house.attributes[ATTR_PRESET_MODE] == "none"
    assert house.attributes[ATTR_HVAC_ACTION] == HVACAction.OFF
    assert house.attributes[ATTR_TEMPERATURE] is None
    zone = get_state(hass, "climate.zona_001")
    assert zone.state == HVACMode.OFF
    assert zone.attributes[ATTR_PRESET_MODE] == "none"


@pytest.mark.parametrize("device_patch", [_frames(("REHOM...MODO", "2"))])
async def test_house_auto(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """MODO 2: house auto; zones following their schedule are auto, a fixed zone keeps its level."""
    await harness.advance_to(T("10:23:05"))
    house = get_state(hass, HOUSE_CLIMATE)
    assert house.state == HVACMode.AUTO
    assert house.attributes[ATTR_PRESET_MODE] == "none"
    assert house.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE
    assert house.attributes[ATTR_TEMPERATURE] is None
    assert get_state(hass, "climate.zona_001").state == HVACMode.AUTO  # SETP 0
    assert get_state(hass, "climate.zona_001").attributes[ATTR_PRESET_MODE] == "none"
    fixed = get_state(hass, "climate.zona_011")  # SETP 4 = comfort
    assert fixed.state == HVACMode.COOL
    assert fixed.attributes[ATTR_PRESET_MODE] == "comfort"


@pytest.mark.parametrize("device_patch", [{"patch": auto_with_override}])
async def test_temporary_comfort_preset(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """MODO 2, SETP 0 and an applying override: auto with the temporary_comfort preset."""
    zone = get_state(hass, "climate.zona_001")
    assert zone.state == HVACMode.AUTO
    assert zone.attributes[ATTR_PRESET_MODE] == "temporary_comfort"
    assert get_state(hass, "climate.zona_003").attributes[ATTR_PRESET_MODE] == "none"


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            ("REHOM...MODO", "x"),
            *((f"ZONA.{zone}..ATTIVA", "x") for zone in FIXTURE_ZONES),
        )
    ],
)
async def test_house_unknown(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Unreadable MODO and zone calls: house mode, preset, target and action unknown."""
    await harness.advance_to(T("10:23:05"))
    house = get_state(hass, HOUSE_CLIMATE)
    assert house.state == STATE_UNKNOWN
    assert house.attributes[ATTR_PRESET_MODE] is None
    assert house.attributes[ATTR_TEMPERATURE] is None
    assert ATTR_HVAC_ACTION not in house.attributes


def test_levels_helper() -> None:
    """Unbound, dangling or malformed programs are null in get_schedule."""
    assert _levels(None) is None


@pytest.mark.parametrize("device_patch", [_frames(("REHOM...STAGIONE", "0"))])
async def test_winter(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Winter: heat mode, winter clamps, demand -> heating."""
    await harness.advance_to(T("10:23:05"))
    house = get_state(hass, HOUSE_CLIMATE)
    assert house.state == HVACMode.HEAT
    assert house.attributes[ATTR_HVAC_MODES] == [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT]
    assert house.attributes[ATTR_MIN_TEMP] == 16.0
    assert house.attributes[ATTR_MAX_TEMP] == 28.5
    assert get_state(hass, "climate.zona_001").state == HVACMode.HEAT
    await harness.advance_to(T("10:24:40"))
    assert get_state(hass, HOUSE_CLIMATE).attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING


@pytest.mark.parametrize("device_patch", [_frames(("REHOM...STAGIONE", "7"))])
async def test_unknown_season(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Unknown season: no season mode, the manual level has no HA mode, demand has no action."""
    await harness.advance_to(T("10:23:05"))
    house = get_state(hass, HOUSE_CLIMATE)
    assert house.state == STATE_UNKNOWN
    assert house.attributes[ATTR_HVAC_MODES] == [HVACMode.OFF, HVACMode.AUTO]
    assert house.attributes[ATTR_MIN_TEMP] == 16.0
    assert house.attributes[ATTR_MAX_TEMP] == 35.5
    assert get_state(hass, "climate.zona_001").state == STATE_UNKNOWN
    await harness.advance_to(T("10:24:40"))
    assert ATTR_HVAC_ACTION not in get_state(hass, HOUSE_CLIMATE).attributes


@pytest.mark.parametrize("device_patch", [_frames(("REHOM...PRESENZA_SONDE", ZONES_WITHOUT_011))])
async def test_absent_zone(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A zone that disappears keeps its entity, unavailable; values read as unknown."""
    await harness.advance_to(T("10:23:05"))
    assert not is_available(hass, "climate.zona_011")
    assert is_available(hass, "climate.zona_010")
    entity = get_entity(hass, "climate.zona_011")
    assert isinstance(entity, RehomZoneClimate)
    assert entity.zone is None
    assert entity.hvac_mode is None
    assert entity.preset_mode is None
    assert entity.hvac_action is None
    assert entity.current_temperature is None
    assert entity.current_humidity is None
    assert entity.target_temperature is None
    assert entity.min_temp == 7
    assert entity.max_temp == 35
    assert entity.extra_state_attributes == {"controller_setpoint": None}
    with pytest.raises(ServiceValidationError) as err:
        await entity.async_get_schedule()
    assert err.value.translation_key == "not_ready"


@pytest.mark.parametrize("device_patch", [_frames(("REHOM...PRESENZA_SONDE", ZONES_WITH_004))])
async def test_new_zone(
    hass: HomeAssistant,
    harness: RehomHarness,
    entity_registry: er.EntityRegistry,
    init_integration: MockConfigEntry,
) -> None:
    """A zone that appears gets its thermostat without a reload (dynamic devices)."""
    assert hass.states.get("climate.zona_004") is None
    await harness.advance_to(T("10:23:05"))
    entry = entity_registry.async_get("climate.zona_004")
    assert entry is not None
    assert entry.unique_id == f"{init_integration.unique_id}_zone_004_climate"


async def test_get_schedule(
    hass: HomeAssistant, init_integration: MockConfigEntry, snapshot: SnapshotAssertion
) -> None:
    """rehom.get_schedule on zone 001: both seasons, 7 days x 48 levels, controller time."""
    entity_id = "climate.zona_001"
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_SCHEDULE,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    schedule = response[entity_id]
    assert isinstance(schedule, dict)
    assert schedule["zone"] == "001"
    assert schedule["season"] == "summer"
    assert schedule["controller_time"] == "2026-09-25T12:22:06.806000"  # Europe/Rome, UTC+2
    assert schedule["timezone"] == "Europe/Rome"
    for season in ("winter", "summer"):
        season_schedule = schedule[season]
        assert isinstance(season_schedule, dict)
        days = season_schedule["days"]
        assert isinstance(days, list)
        assert [day["weekday"] for day in days] == list(range(7))  # type: ignore[index]
        assert days[0]["day"] == "sunday"  # type: ignore[index]
        for day in days:
            assert day["preset"] == "1"  # type: ignore[index]
            assert len(day["levels"]) == 48  # type: ignore[index,arg-type]
    assert response == snapshot


ZONE_001_OFFLINE = "0,1,1,0,0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"
ZONES_001_002_OFFLINE = "0,0,1,0,0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"


@pytest.mark.parametrize(
    "device_patch",
    [
        {
            "extra_frames": [
                (T("10:23:00"), termo_update("REHOM...STATO_SONDE", ZONE_001_OFFLINE)),
                (T("10:25:00"), termo_update("REHOM...STATO_SONDE", ZONES_001_002_OFFLINE)),
            ]
        }
    ],
)
async def test_house_ignores_offline_zones(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Rule 2 for the house: no temperature from an offline zone 001, no stale calls."""
    assert get_state(hass, HOUSE_CLIMATE).attributes[ATTR_CURRENT_TEMPERATURE] == 24.2
    await harness.advance_to(T("10:23:05"))
    assert not is_available(hass, ZONE_CLIMATE.format(zone="001"))
    assert harness.client.state.plant.current_temperature == 24.2  # stale in the model
    house = get_state(hass, HOUSE_CLIMATE)
    assert house.state == HVACMode.COOL
    assert house.attributes[ATTR_CURRENT_TEMPERATURE] is None
    assert house.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE

    await harness.advance_to(T("10:24:40"))  # zone 002 (live) calls from 10:24:34.6
    assert get_state(hass, HOUSE_CLIMATE).attributes[ATTR_HVAC_ACTION] == HVACAction.COOLING

    await harness.advance_to(T("10:25:05"))  # zone 002 offline too: its ATTIVA=1 is stale
    assert harness.client.state.zones["002"].calling is True
    assert get_state(hass, HOUSE_CLIMATE).attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE


@pytest.mark.parametrize(
    "device_patch",
    [
        {
            "extra_frames": [
                (T("10:25:00"), termo_update("REHOM...WEBSERVER", "2")),
                (T("10:26:00"), termo_update("REHOM...WEBSERVER", "1")),
            ]
        }
    ],
)
async def test_house_with_the_serial_line_down(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Rule 5 for the house: zone data unknown; the house's own settings stay shown."""
    await harness.advance_to(T("10:24:40"))
    house = get_state(hass, HOUSE_CLIMATE)
    assert house.attributes[ATTR_HVAC_ACTION] == HVACAction.COOLING
    temperature = house.attributes[ATTR_CURRENT_TEMPERATURE]
    assert temperature is not None

    await harness.advance_to(T("10:25:05"))
    assert not is_available(hass, ZONE_CLIMATE.format(zone="002"))
    assert harness.client.state.plant.demand is True  # stale in the model
    house = get_state(hass, HOUSE_CLIMATE)
    assert house.state == HVACMode.COOL  # REHOM rows are the server's own
    assert house.attributes[ATTR_PRESET_MODE] == "comfort"
    assert house.attributes[ATTR_TEMPERATURE] == 24.0
    assert house.attributes.get(ATTR_HVAC_ACTION) is None
    assert house.attributes[ATTR_CURRENT_TEMPERATURE] is None

    await harness.advance_to(T("10:26:05"))
    house = get_state(hass, HOUSE_CLIMATE)
    assert house.attributes[ATTR_HVAC_ACTION] == HVACAction.COOLING
    assert house.attributes[ATTR_CURRENT_TEMPERATURE] is not None
