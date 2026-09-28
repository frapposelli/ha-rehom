"""Climate control with "Enable control" on: the house and zone thermostats.

Each control is called through Home Assistant's own actions on a real client
replaying the fixture.  Success checks the exact records the controller
received and the new state as soon as the blocking call returns (confirmed,
never optimistic).  Refusals check the translated key and that nothing was
sent.  "Not confirmed" turns off the replayed controller's echo and REST
update: one POST, ``write_not_confirmed``, and the state is unchanged.

Fixture facts (summer): the house is MANUAL/COMFORT (24 °C; economy 29 °C,
pre-comfort 26 °C) and the controller regulates to 26 °C; zone 001 follows
its schedule (comfort now, 24 °C, offset 0) and zone 011 is fixed at comfort.
Zone modes need the house in AUTO (:data:`HOUSE_AUTO`).

Verified values (``control.VERIFIED_VALUES``): the house takes AUTO, economy
and comfort (never pre-comfort); a zone takes its schedule and every level.
A zone forced off by its probe is never changed.
"""

from __future__ import annotations

import math
from typing import Any

from aiorehom import RehomWriteNotConfirmedError
from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    SERVICE_TOGGLE,
    SERVICE_TURN_ON,
    Platform,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.rehom.climate import RehomClimate, RehomZoneClimate
from custom_components.rehom.const import CONF_TEMPORARY_COMFORT_DURATION, DOMAIN

from .conftest import CONTROL_OPTIONS, HOUSE_AUTO, HOUSE_AUTO_VALUES
from .harness import RehomHarness, T, set_values, termo_update
from .platform_helpers import HOUSE_CLIMATE, get_entity, get_state

ZONE = "climate.zona_001"
FIXED_ZONE = "climate.zona_011"  # SETP 4: fixed at comfort
#: Records of the house writes (a level writes its temperature as the setpoint too).
HOUSE_ECO = {"REHOM...MODO": "1", "REHOM...SET_POINT": "1", "REHOM...SET_POINT_TEMP": "29"}
HOUSE_COMFORT = {"REHOM...MODO": "1", "REHOM...SET_POINT": "3", "REHOM...SET_POINT_TEMP": "24"}
HOUSE_AUTO_RECORDS = {"REHOM...MODO": "2", "REHOM...SET_POINT": "0"}
#: The mode record of zone 001.
ZONE_SETP = "ZONA.001..SETP_CORRENTE"
#: device_patch: the house switched off (MODO 0).
HOUSE_OFF = set_values({"REHOM...MODO": "0"})
#: device_patch: the house in AUTO and zone 001 switched off on its own (SETP 1).
ZONE_OFF = set_values({**HOUSE_AUTO_VALUES, ZONE_SETP: "1"})
#: device_patch: zone 001 forced off by its probe (SETP 5), the house MANUAL.
ZONE_PROBE_OFF = set_values({ZONE_SETP: "5"})
#: device_patch: zone 001 forced off by its probe, the house in AUTO.
ZONE_PROBE_OFF_HOUSE_AUTO = set_values({**HOUSE_AUTO_VALUES, ZONE_SETP: "5"})
#: device_patch: the house in AUTO and zone 001 fixed at economy (29 °C: 26..32 °C).
ZONE_ECO = set_values({**HOUSE_AUTO_VALUES, ZONE_SETP: "2"})
#: Default options: "Enable control" off.
CONTROL_OFF = {CONF_TEMPORARY_COMFORT_DURATION: 2.0}


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """ "Enable control" on."""
    return dict(CONTROL_OPTIONS)


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the climate platform."""
    return [Platform.CLIMATE]


async def _call(hass: HomeAssistant, service: str, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


def _view(hass: HomeAssistant, entity_id: str) -> tuple[str, dict[str, Any]]:
    state = get_state(hass, entity_id)
    return state.state, dict(state.attributes)


def _assert_key(
    err: pytest.ExceptionInfo[HomeAssistantError], key: str, domain: str = DOMAIN
) -> None:
    assert err.value.translation_domain == domain
    assert err.value.translation_key == key


async def _refused(
    hass: HomeAssistant,
    harness: RehomHarness,
    key: str,
    service: str,
    entity_id: str,
    **data: Any,
) -> None:
    """The action is refused with ``key``, nothing reaches the controller, the state stays."""
    calls = harness.transport_calls
    before = _view(hass, entity_id)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, service, entity_id, **data)
    _assert_key(err, key)
    await harness.settle()
    assert harness.transport_calls == calls
    assert harness.writes == []
    assert _view(hass, entity_id) == before


async def _not_confirmed(
    hass: HomeAssistant, harness: RehomHarness, service: str, entity_id: str, **data: Any
) -> None:
    """Sent once, never reported back: write_not_confirmed, and the state is unchanged."""
    harness.echo = False
    harness.apply = False
    before = _view(hass, entity_id)
    with pytest.raises(HomeAssistantError) as err:
        await harness.run_until_done(_call(hass, service, entity_id, **data), hass_action=True)
    await harness.settle()
    assert not isinstance(err.value, ServiceValidationError)
    _assert_key(err, "write_not_confirmed")
    assert isinstance(err.value.__cause__, RehomWriteNotConfirmedError)
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert _view(hass, entity_id) == before


async def _nothing_to_change(
    hass: HomeAssistant, harness: RehomHarness, service: str, entity_id: str, **data: Any
) -> None:
    """The action succeeds without sending anything, and the state stays."""
    calls = harness.transport_calls
    before = _view(hass, entity_id)
    await _call(hass, service, entity_id, **data)
    await harness.settle()
    assert harness.transport_calls == calls
    assert harness.writes == []
    assert _view(hass, entity_id) == before


# -- zone target temperature (the offset) --------------------------------------------------


@pytest.mark.parametrize(
    ("temperature", "written", "target"),
    [
        (25, "1.0", 25.0),
        (27, "3.0", 27.0),
        (21, "-3.0", 21.0),
        # halves round up, as the offset number does (control.round_offset)
        (24.5, "1.0", 25.0),
        (25.5, "2.0", 26.0),
        (26.5, "3.0", 27.0),
        (22.5, "-1.0", 23.0),
        (21.5, "-2.0", 22.0),
    ],
)
async def test_zone_set_temperature(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    temperature: float,
    written: str,
    target: float,
) -> None:
    """The offset from the level temperature (24 °C), allowed with the house MANUAL."""
    assert get_state(hass, ZONE).attributes[ATTR_TEMPERATURE] == 24.0
    await _call(hass, SERVICE_SET_TEMPERATURE, ZONE, **{ATTR_TEMPERATURE: temperature})
    assert harness.written_values == [{"ZONA.001..DELTA_SETP_CORRENTE": written}]
    state = get_state(hass, ZONE)
    assert state.attributes[ATTR_TEMPERATURE] == target  # confirmed when the call returns
    assert state.state == HVACMode.COOL  # the mode is unchanged


async def test_zone_set_temperature_nothing_to_send(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The offset the temperature needs is the current one: success, nothing sent."""
    await _call(hass, SERVICE_SET_TEMPERATURE, ZONE, **{ATTR_TEMPERATURE: 23.5})
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls


@pytest.mark.parametrize("device_patch", [set_values({"REHOM...WEBSERVER": "0"})])
async def test_zone_set_temperature_crono(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The crono panel is in control: crono_mode."""
    await _refused(
        hass, harness, "crono_mode", SERVICE_SET_TEMPERATURE, ZONE, **{ATTR_TEMPERATURE: 25}
    )


@pytest.mark.parametrize("device_patch", [set_values({"REHOM...TEMP_COM": "x"})])
async def test_zone_set_temperature_no_target(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """No level temperature to offset from: no_active_target, checked before the library."""
    assert get_state(hass, ZONE).attributes[ATTR_TEMPERATURE] is None
    await _refused(
        hass, harness, "no_active_target", SERVICE_SET_TEMPERATURE, ZONE, **{ATTR_TEMPERATURE: 24}
    )


async def test_zone_set_temperature_not_confirmed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The offset was sent but never reported back."""
    await _not_confirmed(hass, harness, SERVICE_SET_TEMPERATURE, ZONE, **{ATTR_TEMPERATURE: 25})
    assert harness.written_values == [{"ZONA.001..DELTA_SETP_CORRENTE": "1.0"}]
    assert get_state(hass, ZONE).attributes[ATTR_TEMPERATURE] == 24.0


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_zone_set_temperature_with_mode(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A mode in the call is set first; the offset then follows that mode's level."""
    # zone 011 fixed at comfort (24 °C) -> its schedule (comfort now, 24 °C), +1
    await _call(
        hass,
        SERVICE_SET_TEMPERATURE,
        FIXED_ZONE,
        **{ATTR_TEMPERATURE: 25, ATTR_HVAC_MODE: HVACMode.AUTO},
    )
    assert harness.written_values == [
        {"ZONA.011..SETP_CORRENTE": "0"},
        {"ZONA.011..DELTA_SETP_CORRENTE": "1.0"},
    ]
    state = get_state(hass, FIXED_ZONE)
    assert state.state == HVACMode.AUTO
    assert state.attributes[ATTR_TEMPERATURE] == 25.0

    # zone 001: economy (29 °C) through the last manual level, then 27 °C = -2
    harness.writes.clear()
    await _call(hass, SERVICE_SET_PRESET_MODE, ZONE, **{ATTR_PRESET_MODE: "eco"})
    await _call(hass, SERVICE_SET_HVAC_MODE, ZONE, **{ATTR_HVAC_MODE: HVACMode.AUTO})
    harness.writes.clear()
    await _call(
        hass,
        SERVICE_SET_TEMPERATURE,
        ZONE,
        **{ATTR_TEMPERATURE: 27, ATTR_HVAC_MODE: HVACMode.COOL},
    )
    assert harness.written_values == [
        {"ZONA.001..SETP_CORRENTE": "2"},
        {"ZONA.001..DELTA_SETP_CORRENTE": "-2.0"},
    ]
    state = get_state(hass, ZONE)
    assert state.state == HVACMode.COOL
    assert state.attributes[ATTR_PRESET_MODE] == "eco"
    assert state.attributes[ATTR_TEMPERATURE] == 27.0


async def test_zone_set_temperature_with_invalid_mode(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The mode in the call is checked like set_hvac_mode ("off" is not offered)."""
    calls = harness.transport_calls
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            SERVICE_SET_TEMPERATURE,
            ZONE,
            **{ATTR_TEMPERATURE: 25, ATTR_HVAC_MODE: HVACMode.OFF},
        )
    _assert_key(err, "not_valid_hvac_mode", CLIMATE_DOMAIN)
    assert harness.transport_calls == calls


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_zone_set_temperature_mode_only(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Called without a temperature (only possible directly): the mode alone is set."""
    entity = get_entity(hass, FIXED_ZONE)
    assert isinstance(entity, RehomZoneClimate)
    await entity.async_set_temperature(**{ATTR_HVAC_MODE: HVACMode.AUTO})
    assert harness.written_values == [{"ZONA.011..SETP_CORRENTE": "0"}]


@pytest.mark.parametrize(
    ("device_patch", "hvac_mode"),
    [
        # the house MANUAL: the zone follows it (cool); re-applying the zone's own last
        # level would be refused (house_not_auto), so the unchanged mode is not sent
        (None, HVACMode.COOL),
        (HOUSE_AUTO, HVACMode.AUTO),
    ],
    ids=["house_manual", "house_auto"],
)
async def test_zone_set_temperature_with_the_current_mode(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    hvac_mode: HVACMode,
) -> None:
    """A mode in the call that is the current one is skipped: only the offset is sent."""
    assert _view(hass, ZONE)[0] == hvac_mode
    await _call(
        hass, SERVICE_SET_TEMPERATURE, ZONE, **{ATTR_TEMPERATURE: 25, ATTR_HVAC_MODE: hvac_mode}
    )
    assert harness.written_values == [{"ZONA.001..DELTA_SETP_CORRENTE": "1.0"}]
    state = get_state(hass, ZONE)
    assert state.state == hvac_mode
    assert state.attributes[ATTR_TEMPERATURE] == 25.0


async def _set_eco_then_auto(hass: HomeAssistant, harness: RehomHarness) -> None:
    """Zone 001 from economy (seen: the last manual level) back on its schedule."""
    await _call(hass, SERVICE_SET_HVAC_MODE, ZONE, **{ATTR_HVAC_MODE: HVACMode.AUTO})
    assert harness.written_values == [{ZONE_SETP: "0"}]
    harness.writes.clear()


@pytest.mark.parametrize("device_patch", [ZONE_ECO])
@pytest.mark.parametrize(
    ("setup", "temperature", "hvac_mode", "mode_written", "limits"),
    [
        # economy (29 °C: 26..32) -> auto (24 °C: 21..27): 32 was in range before the change
        (False, 32, HVACMode.AUTO, "0", {"min": "21", "max": "27"}),
        # auto (24 °C: 21..27) -> cool at the last level, economy (29 °C: 26..32)
        (True, 21, HVACMode.COOL, "2", {"min": "26", "max": "32"}),
    ],
    ids=["above", "below"],
)
async def test_zone_set_temperature_outside_the_new_range(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    setup: bool,
    temperature: float,
    hvac_mode: HVACMode,
    mode_written: str,
    limits: dict[str, str],
) -> None:
    """The mode changes the level: a target outside the new range is refused, never clamped.

    The mode change stays applied (the message says so); the offset is not sent.
    """
    if setup:
        await _set_eco_then_auto(hass, harness)
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            SERVICE_SET_TEMPERATURE,
            ZONE,
            **{ATTR_TEMPERATURE: temperature, ATTR_HVAC_MODE: hvac_mode},
        )
    _assert_key(err, "target_out_of_range")
    assert err.value.translation_placeholders == limits
    assert harness.written_values == [{ZONE_SETP: mode_written}]
    state = get_state(hass, ZONE)
    assert state.state == hvac_mode
    assert state.attributes["min_temp"] == float(limits["min"])
    assert state.attributes["max_temp"] == float(limits["max"])
    base = 24.0 if hvac_mode == HVACMode.AUTO else 29.0
    assert state.attributes[ATTR_TEMPERATURE] == base  # offset 0: not changed


@pytest.mark.parametrize("device_patch", [ZONE_ECO])
async def test_zone_set_temperature_at_the_end_of_the_new_range(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The ends of the new range are allowed: economy (29 °C) -> auto (24 °C), 27 °C = +3."""
    await _call(
        hass,
        SERVICE_SET_TEMPERATURE,
        ZONE,
        **{ATTR_TEMPERATURE: 27, ATTR_HVAC_MODE: HVACMode.AUTO},
    )
    assert harness.written_values == [
        {ZONE_SETP: "0"},
        {"ZONA.001..DELTA_SETP_CORRENTE": "3.0"},
    ]
    assert get_state(hass, ZONE).attributes[ATTR_TEMPERATURE] == 27.0


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
@pytest.mark.parametrize(
    "data",
    [
        {ATTR_TEMPERATURE: math.nan},
        {ATTR_TEMPERATURE: "nan"},  # coerced to a float by the action's schema
        # nothing is sent, not even the mode
        {ATTR_TEMPERATURE: math.nan, ATTR_HVAC_MODE: HVACMode.COOL},
    ],
    ids=["nan", "nan_text", "nan_with_mode"],
)
async def test_zone_set_temperature_not_a_number(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    data: dict[str, Any],
) -> None:
    """NaN passes Home Assistant's range check (no comparison holds): invalid_value."""
    await _refused(hass, harness, "invalid_value", SERVICE_SET_TEMPERATURE, ZONE, **data)


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
@pytest.mark.parametrize("temperature", [math.inf, -math.inf, math.nan])
async def test_zone_set_temperature_not_finite_direct(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    temperature: float,
) -> None:
    """Called directly (no range check), infinity is refused too, before the mode."""
    entity = get_entity(hass, ZONE)
    assert isinstance(entity, RehomZoneClimate)
    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_temperature(
            **{ATTR_TEMPERATURE: temperature, ATTR_HVAC_MODE: HVACMode.COOL}
        )
    _assert_key(err, "invalid_value")
    assert harness.writes == []


@pytest.mark.parametrize("device_patch", [set_values({"REHOM...TEMP_COM": "16.1"})])
@pytest.mark.parametrize(
    ("temperature", "written"), [(13.1, "-3.0"), (19.1, "3.0")], ids=["min", "max"]
)
async def test_zone_range_without_float_noise(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    temperature: float,
    written: str,
) -> None:
    """Level 16.1 °C: the range is exactly 13.1..19.1 (16.1 - 3 is 13.100000000000001)."""
    entity = get_entity(hass, ZONE)
    assert isinstance(entity, RehomZoneClimate)
    assert entity.zone is not None
    assert entity.zone.base == 16.1
    assert (entity.min_temp, entity.max_temp) == (13.1, 19.1)
    await _call(hass, SERVICE_SET_TEMPERATURE, ZONE, **{ATTR_TEMPERATURE: temperature})
    assert harness.written_values == [{"ZONA.001..DELTA_SETP_CORRENTE": written}]
    assert get_state(hass, ZONE).attributes[ATTR_TEMPERATURE] == temperature


# -- zone modes and presets --------------------------------------------------------------------


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_zone_modes(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """With the house in AUTO: eco, auto, the season mode (last level: eco), preset none."""
    assert _view(hass, ZONE)[0] == HVACMode.AUTO

    await _call(hass, SERVICE_SET_PRESET_MODE, ZONE, **{ATTR_PRESET_MODE: "eco"})
    state = get_state(hass, ZONE)
    assert (state.state, state.attributes[ATTR_PRESET_MODE]) == (HVACMode.COOL, "eco")
    assert state.attributes[ATTR_TEMPERATURE] == 29.0

    await _call(hass, SERVICE_SET_HVAC_MODE, ZONE, **{ATTR_HVAC_MODE: HVACMode.AUTO})
    state = get_state(hass, ZONE)
    assert (state.state, state.attributes[ATTR_PRESET_MODE]) == (HVACMode.AUTO, "none")

    await _call(hass, SERVICE_SET_HVAC_MODE, ZONE, **{ATTR_HVAC_MODE: HVACMode.COOL})
    assert get_state(hass, ZONE).attributes[ATTR_PRESET_MODE] == "eco"

    await _call(hass, SERVICE_SET_PRESET_MODE, ZONE, **{ATTR_PRESET_MODE: "none"})
    assert _view(hass, ZONE)[0] == HVACMode.AUTO
    assert harness.written_values == [
        {"ZONA.001..SETP_CORRENTE": "2"},
        {"ZONA.001..SETP_CORRENTE": "0"},
        {"ZONA.001..SETP_CORRENTE": "2"},
        {"ZONA.001..SETP_CORRENTE": "0"},
    ]


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "eco"}),
        (SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.AUTO}),
        (SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "none"}),
    ],
)
async def test_zone_mode_house_not_auto(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    service: str,
    data: dict[str, Any],
) -> None:
    """The fixture's house is MANUAL: zone modes are refused by the controller's rules."""
    await _refused(hass, harness, "house_not_auto", service, ZONE, **data)


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_zone_mode_not_confirmed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Zone economy sent but never reported back."""
    await _not_confirmed(hass, harness, SERVICE_SET_PRESET_MODE, ZONE, **{ATTR_PRESET_MODE: "eco"})
    assert harness.written_values == [{"ZONA.001..SETP_CORRENTE": "2"}]
    assert _view(hass, ZONE)[0] == HVACMode.AUTO


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_zone_levels(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Every level is verified: pre-comfort, comfort, auto, then the season mode (comfort)."""
    await _call(hass, SERVICE_SET_PRESET_MODE, ZONE, **{ATTR_PRESET_MODE: "pre_comfort"})
    state = get_state(hass, ZONE)
    assert (state.state, state.attributes[ATTR_PRESET_MODE]) == (HVACMode.COOL, "pre_comfort")
    assert state.attributes[ATTR_TEMPERATURE] == 26.0

    await _call(hass, SERVICE_SET_PRESET_MODE, ZONE, **{ATTR_PRESET_MODE: "comfort"})
    state = get_state(hass, ZONE)
    assert (state.state, state.attributes[ATTR_PRESET_MODE]) == (HVACMode.COOL, "comfort")
    assert state.attributes[ATTR_TEMPERATURE] == 24.0

    await _call(hass, SERVICE_SET_HVAC_MODE, ZONE, **{ATTR_HVAC_MODE: HVACMode.AUTO})
    await _call(hass, SERVICE_SET_HVAC_MODE, ZONE, **{ATTR_HVAC_MODE: HVACMode.COOL})
    state = get_state(hass, ZONE)
    assert (state.state, state.attributes[ATTR_PRESET_MODE]) == (HVACMode.COOL, "comfort")
    assert harness.written_values == [
        {ZONE_SETP: "3"},
        {ZONE_SETP: "4"},
        {ZONE_SETP: "0"},
        {ZONE_SETP: "4"},
    ]


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_zone_season_mode_before_any_level(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """No manual level seen on the zone yet: the season mode applies comfort."""
    await _call(hass, SERVICE_SET_HVAC_MODE, ZONE, **{ATTR_HVAC_MODE: HVACMode.COOL})
    assert harness.written_values == [{ZONE_SETP: "4"}]
    assert get_state(hass, ZONE).attributes[ATTR_PRESET_MODE] == "comfort"


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
@pytest.mark.parametrize(
    ("key", "service", "data"),
    [
        (
            "temporary_comfort_unavailable",
            SERVICE_SET_PRESET_MODE,
            {ATTR_PRESET_MODE: "temporary_comfort"},
        ),
        ("not_supported", SERVICE_TOGGLE, {}),  # toggle from on = turn off
    ],
)
async def test_zone_refused_in_home_assistant(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    key: str,
    service: str,
    data: dict[str, Any],
) -> None:
    """Temporary comfort and switching off: refused before sending."""
    await _refused(hass, harness, key, service, ZONE, **data)


@pytest.mark.parametrize(
    ("device_patch", "service"),
    [
        (ZONE_OFF, SERVICE_TURN_ON),
        (ZONE_OFF, SERVICE_TOGGLE),
        (ZONE_OFF, SERVICE_SET_HVAC_MODE),
    ],
    ids=["turn_on", "toggle", "auto"],
)
async def test_zone_turn_on(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, service: str
) -> None:
    """A zone switched off on its own goes back on its schedule; "off" is listed only while off."""
    state = get_state(hass, ZONE)
    assert state.state == HVACMode.OFF
    assert state.attributes[ATTR_HVAC_MODES] == [HVACMode.OFF, HVACMode.AUTO, HVACMode.COOL]
    data = {ATTR_HVAC_MODE: HVACMode.AUTO} if service == SERVICE_SET_HVAC_MODE else {}
    await _call(hass, service, ZONE, **data)
    assert harness.written_values == [{ZONE_SETP: "0"}]
    state = get_state(hass, ZONE)
    assert state.state == HVACMode.AUTO
    assert state.attributes[ATTR_HVAC_MODES] == [HVACMode.AUTO, HVACMode.COOL]


#: Every action that would change the mode of a zone forced off by its probe.
PROBE_OFF_ACTIONS: list[tuple[str, dict[str, Any]]] = [
    (SERVICE_TURN_ON, {}),
    (SERVICE_TOGGLE, {}),
    (SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.AUTO}),
    (SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.COOL}),
    (SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "none"}),
    (SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "eco"}),
    (SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "comfort"}),
    # the off level's temperature (38 °C in summer) is the zone's base while off: 35..41
    (SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: 38, ATTR_HVAC_MODE: HVACMode.AUTO}),
]


@pytest.mark.parametrize(
    "device_patch", [ZONE_PROBE_OFF, ZONE_PROBE_OFF_HOUSE_AUTO], ids=["manual", "house_auto"]
)
@pytest.mark.parametrize(
    ("service", "data"),
    PROBE_OFF_ACTIONS,
    ids=[f"{service}-{data}" for service, data in PROBE_OFF_ACTIONS],
)
async def test_zone_forced_off_by_its_probe(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    service: str,
    data: dict[str, Any],
) -> None:
    """A zone forced off by its probe stays off: not_verified, nothing sent, whatever the house."""
    state = get_state(hass, ZONE)
    assert state.state == HVACMode.OFF
    assert state.attributes[ATTR_PRESET_MODE] == "none"
    assert state.attributes[ATTR_HVAC_MODES] == [HVACMode.OFF, HVACMode.AUTO, HVACMode.COOL]
    await _refused(hass, harness, "not_verified", service, ZONE, **data)


@pytest.mark.parametrize("entry_options", [CONTROL_OFF])
@pytest.mark.parametrize("device_patch", [ZONE_PROBE_OFF])
@pytest.mark.parametrize(
    ("service", "data"),
    PROBE_OFF_ACTIONS,
    ids=[f"{service}-{data}" for service, data in PROBE_OFF_ACTIONS],
)
async def test_zone_forced_off_by_its_probe_control_disabled(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    service: str,
    data: dict[str, Any],
) -> None:
    """Control off: control_disabled comes before the probe-off refusal, and nothing is sent."""
    assert harness.allow_writes == [False]
    assert _view(hass, ZONE)[0] == HVACMode.OFF
    await _refused(hass, harness, "control_disabled", service, ZONE, **data)


@pytest.mark.parametrize("device_patch", [ZONE_PROBE_OFF])
async def test_zone_forced_off_by_its_probe_direct(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The entity refuses on its own, before control.async_set_zone_mode is reached."""
    entity = get_entity(hass, ZONE)
    assert isinstance(entity, RehomZoneClimate)
    with pytest.raises(ServiceValidationError) as err:
        entity._ensure_not_forced_off()
    _assert_key(err, "not_verified")
    # a zone that is on, and the house (never forced off): nothing raised
    other = get_entity(hass, FIXED_ZONE)
    assert isinstance(other, RehomZoneClimate)
    other._ensure_not_forced_off()
    house = get_entity(hass, HOUSE_CLIMATE)
    assert isinstance(house, RehomClimate)
    house._ensure_not_forced_off()
    assert harness.writes == []


@pytest.mark.parametrize(
    ("device_patch", "entity_id"),
    [
        (HOUSE_OFF, HOUSE_CLIMATE),  # switched off in the official app
        (HOUSE_OFF, ZONE),  # off because the house is off
        (ZONE_OFF, ZONE),  # switched off on its own
    ],
    ids=["house", "zone_house_off", "zone_off"],
)
async def test_preset_none_while_off(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, entity_id: str
) -> None:
    """Preset "none" is the preset shown while off: nothing changes, nothing is sent."""
    state = get_state(hass, entity_id)
    assert (state.state, state.attributes[ATTR_PRESET_MODE]) == (HVACMode.OFF, "none")
    await _nothing_to_change(
        hass, harness, SERVICE_SET_PRESET_MODE, entity_id, **{ATTR_PRESET_MODE: "none"}
    )


@pytest.mark.parametrize("device_patch", [ZONE_OFF])
async def test_zone_off_is_not_selectable(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """ "off" is listed while the zone is off, but selecting it is refused."""
    await _refused(hass, harness, "not_supported", SERVICE_SET_HVAC_MODE, ZONE, hvac_mode="off")


@pytest.mark.parametrize("device_patch", [HOUSE_OFF])
async def test_zone_turn_on_with_the_house_off(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A zone that is off because the house is off stays off: house_not_auto."""
    assert _view(hass, ZONE)[0] == HVACMode.OFF
    await _refused(hass, harness, "house_not_auto", SERVICE_TURN_ON, ZONE)


# -- house ---------------------------------------------------------------------------------------


async def test_house_modes(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """COMFORT -> eco -> none (AUTO) -> the season mode (last level: eco) -> auto."""
    await _call(hass, SERVICE_SET_PRESET_MODE, HOUSE_CLIMATE, **{ATTR_PRESET_MODE: "eco"})
    state = get_state(hass, HOUSE_CLIMATE)
    assert (state.state, state.attributes[ATTR_PRESET_MODE]) == (HVACMode.COOL, "eco")
    assert state.attributes[ATTR_TEMPERATURE] == 29.0
    assert state.attributes["controller_setpoint"] == 29.0
    assert state.attributes["setpoint_mismatch"] is False

    await _call(hass, SERVICE_SET_PRESET_MODE, HOUSE_CLIMATE, **{ATTR_PRESET_MODE: "none"})
    state = get_state(hass, HOUSE_CLIMATE)
    assert (state.state, state.attributes[ATTR_PRESET_MODE]) == (HVACMode.AUTO, "none")
    assert state.attributes[ATTR_TEMPERATURE] is None

    await _call(hass, SERVICE_SET_HVAC_MODE, HOUSE_CLIMATE, **{ATTR_HVAC_MODE: HVACMode.COOL})
    assert get_state(hass, HOUSE_CLIMATE).attributes[ATTR_PRESET_MODE] == "eco"

    await _call(hass, SERVICE_SET_HVAC_MODE, HOUSE_CLIMATE, **{ATTR_HVAC_MODE: HVACMode.AUTO})
    assert _view(hass, HOUSE_CLIMATE)[0] == HVACMode.AUTO
    assert harness.written_values == [HOUSE_ECO, HOUSE_AUTO_RECORDS, HOUSE_ECO, HOUSE_AUTO_RECORDS]
    # a zone following the house shows the house's level
    assert get_state(hass, ZONE).state == HVACMode.AUTO


@pytest.mark.parametrize("device_patch", [set_values({"REHOM...TEMP_MAN": "x"})])
async def test_house_level_temperature_unknown(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The economy temperature is unreadable: level_temperature_invalid."""
    await _refused(
        hass,
        harness,
        "level_temperature_invalid",
        SERVICE_SET_PRESET_MODE,
        HOUSE_CLIMATE,
        **{ATTR_PRESET_MODE: "eco"},
    )


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "comfort"}),
        # the house shows comfort: the season mode re-applies it
        (SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.COOL}),
    ],
    ids=["preset", "season_mode"],
)
async def test_house_comfort_again(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    service: str,
    data: dict[str, Any],
) -> None:
    """Comfort re-sent: its temperature (24 °C) becomes the setpoint again (26 °C before)."""
    state = get_state(hass, HOUSE_CLIMATE)
    assert state.attributes["controller_setpoint"] == 26.0
    assert state.attributes["setpoint_mismatch"] is True
    await _call(hass, service, HOUSE_CLIMATE, **data)
    assert harness.written_values == [HOUSE_COMFORT]
    state = get_state(hass, HOUSE_CLIMATE)
    assert (state.state, state.attributes[ATTR_PRESET_MODE]) == (HVACMode.COOL, "comfort")
    assert state.attributes["controller_setpoint"] == 24.0
    assert state.attributes["setpoint_mismatch"] is False


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_house_comfort_from_auto(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """AUTO -> comfort (24 °C), then the season mode from AUTO re-applies comfort."""
    await _call(hass, SERVICE_SET_PRESET_MODE, HOUSE_CLIMATE, **{ATTR_PRESET_MODE: "comfort"})
    state = get_state(hass, HOUSE_CLIMATE)
    assert (state.state, state.attributes[ATTR_PRESET_MODE]) == (HVACMode.COOL, "comfort")
    assert state.attributes[ATTR_TEMPERATURE] == 24.0
    await _call(hass, SERVICE_SET_HVAC_MODE, HOUSE_CLIMATE, **{ATTR_HVAC_MODE: HVACMode.AUTO})
    await _call(hass, SERVICE_SET_HVAC_MODE, HOUSE_CLIMATE, **{ATTR_HVAC_MODE: HVACMode.COOL})
    assert get_state(hass, HOUSE_CLIMATE).attributes[ATTR_PRESET_MODE] == "comfort"
    assert harness.written_values == [HOUSE_COMFORT, HOUSE_AUTO_RECORDS, HOUSE_COMFORT]


@pytest.mark.parametrize(
    ("key", "service", "data"),
    [
        # pre-comfort is never sent to the house
        ("not_verified", SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "pre_comfort"}),
        # the comfort temperature: never tested
        ("not_verified", SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: 25}),
        (
            "not_verified",
            SERVICE_SET_TEMPERATURE,
            {ATTR_TEMPERATURE: 25, ATTR_HVAC_MODE: HVACMode.AUTO},
        ),
        ("not_supported", SERVICE_TOGGLE, {}),
    ],
)
async def test_house_refused_in_home_assistant(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    key: str,
    service: str,
    data: dict[str, Any],
) -> None:
    """Untested values, the target temperature and switching off: refused, nothing sent."""
    await _refused(hass, harness, key, service, HOUSE_CLIMATE, **data)


async def test_house_not_confirmed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """House economy sent but never reported back."""
    await _not_confirmed(
        hass, harness, SERVICE_SET_PRESET_MODE, HOUSE_CLIMATE, **{ATTR_PRESET_MODE: "eco"}
    )
    assert harness.written_values == [HOUSE_ECO]
    assert get_state(hass, HOUSE_CLIMATE).attributes[ATTR_PRESET_MODE] == "comfort"


@pytest.mark.parametrize("device_patch", [HOUSE_OFF])
@pytest.mark.parametrize("service", [SERVICE_TURN_ON, SERVICE_TOGGLE, SERVICE_SET_HVAC_MODE])
async def test_house_turn_on(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, service: str
) -> None:
    """The house off -> AUTO; "off" is listed only while off."""
    state = get_state(hass, HOUSE_CLIMATE)
    assert state.state == HVACMode.OFF
    assert state.attributes[ATTR_HVAC_MODES] == [HVACMode.OFF, HVACMode.AUTO, HVACMode.COOL]
    data = {ATTR_HVAC_MODE: HVACMode.AUTO} if service == SERVICE_SET_HVAC_MODE else {}
    await _call(hass, service, HOUSE_CLIMATE, **data)
    assert harness.written_values == [HOUSE_AUTO_RECORDS]
    state = get_state(hass, HOUSE_CLIMATE)
    assert state.state == HVACMode.AUTO
    assert state.attributes[ATTR_HVAC_MODES] == [HVACMode.AUTO, HVACMode.COOL]


@pytest.mark.parametrize("device_patch", [HOUSE_OFF])
async def test_house_off_is_not_selectable(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """ "off" is listed while the house is off, but selecting it is refused."""
    await _refused(
        hass, harness, "not_supported", SERVICE_SET_HVAC_MODE, HOUSE_CLIMATE, hvac_mode="off"
    )


@pytest.mark.parametrize("entity_id", [HOUSE_CLIMATE, ZONE])
async def test_turn_on_when_on(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, entity_id: str
) -> None:
    """Turning on a thermostat that is on changes nothing and sends nothing."""
    before = _view(hass, entity_id)
    await _call(hass, SERVICE_TURN_ON, entity_id)
    assert harness.writes == []
    assert _view(hass, entity_id) == before


@pytest.mark.parametrize("entity_id", [HOUSE_CLIMATE, ZONE])
async def test_direct_calls_outside_the_modes(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, entity_id: str
) -> None:
    """Values Home Assistant's own checks never pass on are refused too (no write)."""
    entity = get_entity(hass, entity_id)
    assert isinstance(entity, RehomClimate)
    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_hvac_mode(HVACMode.HEAT)  # winter mode in summer
    _assert_key(err, "not_supported")
    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_preset_mode("temporary_comfort")
    _assert_key(err, "temporary_comfort_unavailable")
    assert harness.writes == []


@pytest.mark.parametrize(
    "device_patch",
    [
        {
            "extra_frames": [
                (
                    T("10:23:00"),
                    termo_update(
                        "REHOM...PRESENZA_SONDE",
                        "1,1,1,0,0,0,0,0,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0",  # no zone 011
                    ),
                )
            ]
        }
    ],
)
async def test_absent_zone_has_no_target(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A zone no longer present has no target to offset from (direct call; HA skips it)."""
    await harness.advance_to(T("10:23:05"))
    entity = get_entity(hass, FIXED_ZONE)
    assert isinstance(entity, RehomZoneClimate)
    assert entity.zone is None
    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_temperature(**{ATTR_TEMPERATURE: 24})
    _assert_key(err, "no_active_target")
    assert harness.writes == []


# -- the last manual level, restored across restarts ------------------------------------------

LAST_ECO = {"last_manual": "eco"}


@pytest.fixture
def restore_cache(hass: HomeAssistant, request: pytest.FixtureRequest) -> None:
    """The house and zone 001 last saved with the given extra data."""
    extra: dict[str, Any] = request.param
    mock_restore_cache_with_extra_data(
        hass,
        (
            (State(HOUSE_CLIMATE, HVACMode.AUTO), extra),
            (State(ZONE, HVACMode.AUTO), extra),
        ),
    )


@pytest.fixture
async def restored(restore_cache: None, init_integration: MockConfigEntry) -> MockConfigEntry:
    """The entry set up after the restore cache was filled."""
    return init_integration


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
@pytest.mark.parametrize("restore_cache", [LAST_ECO], indirect=True)
async def test_last_manual_restored(
    hass: HomeAssistant, harness: RehomHarness, restored: MockConfigEntry
) -> None:
    """Both in AUTO after a restart: the season mode re-applies the saved level (eco)."""
    for entity_id in (HOUSE_CLIMATE, ZONE):
        entity = get_entity(hass, entity_id)
        assert isinstance(entity, RehomClimate)
        assert entity.extra_restore_state_data.as_dict() == LAST_ECO
    await _call(hass, SERVICE_SET_HVAC_MODE, ZONE, **{ATTR_HVAC_MODE: HVACMode.COOL})
    await _call(hass, SERVICE_SET_HVAC_MODE, HOUSE_CLIMATE, **{ATTR_HVAC_MODE: HVACMode.COOL})
    assert harness.written_values == [{"ZONA.001..SETP_CORRENTE": "2"}, HOUSE_ECO]


@pytest.mark.parametrize("restore_cache", [LAST_ECO], indirect=True)
async def test_shown_level_wins_over_restored(
    hass: HomeAssistant, harness: RehomHarness, restored: MockConfigEntry
) -> None:
    """The house shows comfort now: that is the last level, whatever was saved."""
    entity = get_entity(hass, HOUSE_CLIMATE)
    assert isinstance(entity, RehomClimate)
    assert entity.extra_restore_state_data.as_dict() == {"last_manual": "comfort"}
    await _call(hass, SERVICE_SET_HVAC_MODE, HOUSE_CLIMATE, **{ATTR_HVAC_MODE: HVACMode.COOL})
    assert harness.written_values == [HOUSE_COMFORT]  # comfort again, not the saved eco


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
@pytest.mark.parametrize(
    "restore_cache", [{"last_manual": "boost"}, {"last_manual": None}, {}], indirect=True
)
async def test_bad_restored_level_ignored(
    hass: HomeAssistant, harness: RehomHarness, restored: MockConfigEntry
) -> None:
    """Saved data that is not a manual level is ignored: comfort, as before any level."""
    for entity_id in (HOUSE_CLIMATE, ZONE):
        entity = get_entity(hass, entity_id)
        assert isinstance(entity, RehomClimate)
        assert entity.extra_restore_state_data.as_dict() == {"last_manual": "comfort"}
    await _call(hass, SERVICE_SET_HVAC_MODE, ZONE, **{ATTR_HVAC_MODE: HVACMode.COOL})
    await _call(hass, SERVICE_SET_HVAC_MODE, HOUSE_CLIMATE, **{ATTR_HVAC_MODE: HVACMode.COOL})
    assert harness.written_values == [{ZONE_SETP: "4"}, HOUSE_COMFORT]


# -- control off --------------------------------------------------------------------------------

#: (entity id, action, data) of every climate control this module exercises.
DISABLED_ACTIONS: list[tuple[str, str, dict[str, Any]]] = [
    (ZONE, SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: 25}),
    (ZONE, SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: 25, ATTR_HVAC_MODE: HVACMode.AUTO}),
    (ZONE, SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: math.nan}),  # before invalid_value
    (ZONE, SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.COOL}),
    (ZONE, SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "none"}),
    (ZONE, SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "comfort"}),
    (ZONE, SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "temporary_comfort"}),
    (ZONE, SERVICE_TOGGLE, {}),
    (HOUSE_CLIMATE, SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: 25}),
    (HOUSE_CLIMATE, SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.COOL}),
    (HOUSE_CLIMATE, SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "none"}),
    (HOUSE_CLIMATE, SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "pre_comfort"}),
    (HOUSE_CLIMATE, SERVICE_TOGGLE, {}),
]


@pytest.mark.parametrize("entry_options", [CONTROL_OFF])
@pytest.mark.parametrize(
    ("entity_id", "service", "data"),
    DISABLED_ACTIONS,
    ids=[f"{entity_id}-{service}-{data}" for entity_id, service, data in DISABLED_ACTIONS],
)
async def test_control_disabled(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    entity_id: str,
    service: str,
    data: dict[str, Any],
) -> None:
    """Control off: control_disabled before any other check, and nothing is sent."""
    assert harness.allow_writes == [False]
    await _refused(hass, harness, "control_disabled", service, entity_id, **data)


@pytest.mark.parametrize("entry_options", [CONTROL_OFF])
@pytest.mark.parametrize("device_patch", [HOUSE_OFF])
@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_TURN_ON, {}),
        (SERVICE_TOGGLE, {}),
        (SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.OFF}),
        (SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: "none"}),  # a no-op with control on
    ],
)
@pytest.mark.parametrize("entity_id", [HOUSE_CLIMATE, ZONE])
async def test_control_disabled_while_off(  # noqa: PLR0917
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    entity_id: str,
    service: str,
    data: dict[str, Any],
) -> None:
    """Control off: turning on, "off" (never allowed) and preset none are control_disabled."""
    assert _view(hass, entity_id)[0] == HVACMode.OFF
    await _refused(hass, harness, "control_disabled", service, entity_id, **data)
