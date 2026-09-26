"""Sensor platform on the replay."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.components.sensor import ATTR_OPTIONS, SensorDeviceClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.rehom.const import DOMAIN, PLATFORMS
from custom_components.rehom.sensor import RehomSensor, RehomVmcSensor, RehomZoneSensor

from .harness import FIXTURE_MAC, RehomHarness, T, termo_update
from .platform_helpers import (
    auto_with_override,
    get_entity,
    get_state,
    is_available,
    snapshot_states,
)

HUMIDITY_011 = "sensor.zona_011_humidity"


def _frames(*frames: tuple[str, str, str]) -> dict[str, Any]:
    """``device_patch`` with synthetic ``termo`` frames ``(time, path, value)``."""
    return {"extra_frames": [(T(at), termo_update(path, value)) for at, path, value in frames]}


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the sensor platform."""
    return [Platform.SENSOR]


async def test_snapshot(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """Every sensor at the replay start."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_values_at_start(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Fixture values at the replay start."""
    expected = {
        "sensor.rehom_plant_season": "summer",
        "sensor.rehom_plant_controller_setpoint": "26.0",
        "sensor.rehom_plant_off_temperature": "38.0",
        "sensor.rehom_plant_economy_temperature": "29.0",
        "sensor.rehom_plant_pre_comfort_temperature": "26.0",
        "sensor.rehom_plant_comfort_temperature": "24.0",
        "sensor.rehom_plant_active_alarms": "0",
        "sensor.rehom_server_controlled_by": "web",
        "sensor.zona_001_active_level": "comfort",
        "sensor.zona_001_control_source": "house",
        "sensor.zona_001_next_change": STATE_UNKNOWN,  # MANUAL: never changes by time
        "sensor.zona_001_temporary_comfort_ends": STATE_UNKNOWN,
        "sensor.zona_001_temperature": "24.2",
        "sensor.zona_001_humidity": "53.0",
        "sensor.vmc_001_effective_mode": "dehumidify",
        "sensor.vmc_001_state": "running",
    }
    assert {entity_id: get_state(hass, entity_id).state for entity_id in expected} == expected
    assert get_state(hass, "sensor.rehom_plant_active_alarms").attributes["alarms"] == []


async def test_entities_by_device(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    init_integration: MockConfigEntry,
) -> None:
    """Sensors per device: hub 2, plant 7, zone 6 (011 without humidity: 5), VMC 5."""
    names: Counter[str] = Counter()
    for entry in er.async_entries_for_config_entry(entity_registry, init_integration.entry_id):
        assert entry.device_id is not None
        device = device_registry.async_get(entry.device_id)
        assert device is not None
        names[device.name or ""] += 1
    assert names == {
        "Rehom server": 2,
        "Rehom plant": 7,
        "Zona 001": 6,
        "Zona 002": 6,
        "Zona 003": 6,
        "Zona 009": 6,
        "Zona 010": 6,
        "Zona 011": 5,
        "VMC 001": 5,
        "VMC 002": 5,
    }


@pytest.mark.parametrize("platforms", [list(PLATFORMS)])
async def test_entity_totals(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    init_integration: MockConfigEntry,
) -> None:
    """All platforms: 144 entities on 10 devices, 10 disabled by default."""
    entries = er.async_entries_for_config_entry(entity_registry, init_integration.entry_id)
    assert len(entries) == 144
    assert Counter(entry.domain for entry in entries) == {
        "binary_sensor": 63,
        "sensor": 54,
        "event": 9,
        "climate": 7,
        "number": 6,
        "fan": 2,
        "select": 2,
        "switch": 1,
    }
    disabled = sorted(
        entry.unique_id.removeprefix(f"{FIXTURE_MAC}_") for entry in entries if entry.disabled_by
    )
    assert disabled == [
        "hub_cpu_temperature",
        "hub_heartbeat",
        "vmc_001_compressor",
        "vmc_001_duty_cycle",
        "vmc_001_inlet_air_temperature",
        "vmc_001_renewal_position",
        "vmc_002_compressor",
        "vmc_002_duty_cycle",
        "vmc_002_inlet_air_temperature",
        "vmc_002_renewal_position",
    ]
    assert all(entry.unique_id.startswith(f"{FIXTURE_MAC}_") for entry in entries)

    devices = dr.async_entries_for_config_entry(device_registry, init_integration.entry_id)
    assert len(devices) == 10
    hub = device_registry.async_get_device_by_identifier(
        (DOMAIN, FIXTURE_MAC), init_integration.entry_id
    )
    assert hub is not None
    for device in devices:
        assert device.area_id is None
        if device.id != hub.id:
            assert device.via_device_id == hub.id
    assert sorted(device.name or "" for device in devices) == [
        "Rehom plant",
        "Rehom server",
        "VMC 001",
        "VMC 002",
        "Zona 001",
        "Zona 002",
        "Zona 003",
        "Zona 009",
        "Zona 010",
        "Zona 011",
    ]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_cpu_temperature(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """CPU temperature follows PROC TEMP_RASPBERRY (every 30 s in the capture)."""
    entity_id = "sensor.rehom_server_cpu_temperature"
    assert get_state(hass, entity_id).state == "64.5"
    await harness.advance_to(T("10:22:45"))
    assert get_state(hass, entity_id).state == "63.9"
    await harness.advance_to(T("10:23:15"))
    assert get_state(hass, entity_id).state == "65.5"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_weather_refresh_changes_nothing(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The 10:34:03 METEO remove/re-create burst (+ forecast frames) changes no sensor."""
    await harness.advance_to(T("10:34:02"))
    before = snapshot_states(hass, "sensor")
    await harness.advance_to(T("10:34:05"))
    assert harness.client.stats.forecast_frames > 0
    assert snapshot_states(hass, "sensor") == before


@pytest.mark.parametrize(
    "device_patch",
    [_frames(("10:23:00", "ZONA.011..UMIDITA", "50"), ("10:24:00", "ZONA.011..UMIDITA", "255"))],
)
async def test_late_humidity_sensor(
    hass: HomeAssistant,
    harness: RehomHarness,
    entity_registry: er.EntityRegistry,
    init_integration: MockConfigEntry,
) -> None:
    """Zone 011's humidity sensor appears with a reading and stays (unavailable) after 255."""
    assert hass.states.get(HUMIDITY_011) is None
    await harness.advance_to(T("10:23:05"))
    assert get_state(hass, HUMIDITY_011).state == "50.0"
    await harness.advance_to(T("10:24:05"))
    assert get_state(hass, HUMIDITY_011).state == STATE_UNAVAILABLE

    # Once registered it is created again after a reload, even without a sensor.
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await harness.settle()
    assert entity_registry.async_get(HUMIDITY_011) is not None
    assert get_state(hass, HUMIDITY_011).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("device_patch", "expected"),
    [
        (_frames(("10:23:00", "REHOM...WEBSERVER", "2")), "serial_down"),
        (_frames(("10:23:00", "REHOM...WEBSERVER", "0")), "crono"),
    ],
)
async def test_controlled_by(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    expected: str,
) -> None:
    """The lock state: web (normal), crono, serial line down; hub/plant stay available."""
    await harness.advance_to(T("10:23:05"))
    assert get_state(hass, "sensor.rehom_server_controlled_by").state == expected
    assert is_available(hass, "sensor.rehom_plant_season")


@pytest.mark.parametrize(
    "device_patch", [_frames(("10:23:00", "DEUM.001..ALLARM_PRESSOSTATO_FILTRO", "1"))]
)
async def test_active_alarms(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Active alarms counts debounced alarms only (60 s) and lists them with their device."""
    await harness.advance_to(T("10:23:59"))
    assert get_state(hass, "sensor.rehom_plant_active_alarms").state == "0"
    await harness.advance_to(T("10:24:01"))
    state = get_state(hass, "sensor.rehom_plant_active_alarms")
    assert state.state == "1"
    assert state.attributes["alarms"] == [
        {
            "alarm_id": "vmc:001:ALLARM_PRESSOSTATO_FILTRO",
            "source": "vmc_flag",
            "unit": "001",
            "code": None,
            "text": "dirty filter",
            "first_seen": "2026-09-25T10:23:00.100000+00:00",  # end of the frame batch
            "device": "vmc",
        }
    ]


@pytest.mark.parametrize("device_patch", [_frames(("10:23:00", "REHOM...MODO", "2"))])
async def test_schedule_timestamps(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """In AUTO a zone following its schedule has a next change; enum values stay in options."""
    await harness.advance_to(T("10:23:05"))
    next_change = get_state(hass, "sensor.zona_001_next_change")
    assert next_change.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.TIMESTAMP
    assert next_change.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    assert get_state(hass, "sensor.zona_001_control_source").state == "schedule"
    for state in hass.states.async_all("sensor"):
        if state.attributes.get(ATTR_DEVICE_CLASS) == SensorDeviceClass.ENUM:
            assert state.state in (*state.attributes[ATTR_OPTIONS], STATE_UNKNOWN)


@pytest.mark.parametrize("device_patch", [{"patch": auto_with_override}])
async def test_temporary_comfort_end(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An applying override: its end (UTC) and the override control source."""
    assert get_state(hass, "sensor.zona_001_temporary_comfort_ends").state == (
        "2026-09-25T12:00:00+00:00"
    )
    assert get_state(hass, "sensor.zona_001_control_source").state == "override"
    assert get_state(hass, "sensor.zona_003_temporary_comfort_ends").state == STATE_UNKNOWN


async def test_enum_outside_options(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """An enum sensor reports unknown rather than a value outside its options."""
    entity = get_entity(hass, "sensor.rehom_plant_season")
    assert isinstance(entity, RehomSensor)
    assert entity._checked("summer") == "summer"
    assert entity._checked("spring") is None
    temperature = get_entity(hass, "sensor.rehom_plant_comfort_temperature")
    assert isinstance(temperature, RehomSensor)
    assert temperature._checked(24.0) == 24.0  # not an enum: unchanged


@pytest.mark.parametrize(
    "device_patch",
    [
        _frames(
            (
                "10:23:00",
                "REHOM...PRESENZA_SONDE",
                "1,1,1,0,0,0,0,0,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0",
            ),
            ("10:23:00", "REHOM...PRESENZA_DEUM", "1,0,0"),
        )
    ],
)
async def test_absent_units(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A zone or VMC that disappears keeps its sensors, unavailable, with unknown values."""
    await harness.advance_to(T("10:23:05"))
    assert not is_available(hass, "sensor.zona_011_temperature")
    assert not is_available(hass, "sensor.vmc_002_effective_mode")
    assert is_available(hass, "sensor.zona_010_temperature")
    assert is_available(hass, "sensor.vmc_001_effective_mode")
    zone_sensor = get_entity(hass, "sensor.zona_011_temperature")
    assert isinstance(zone_sensor, RehomZoneSensor)
    assert zone_sensor.native_value is None
    vmc_sensor = get_entity(hass, "sensor.vmc_002_effective_mode")
    assert isinstance(vmc_sensor, RehomVmcSensor)
    assert vmc_sensor.native_value is None
