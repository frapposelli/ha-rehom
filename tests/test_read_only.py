"""Control off (the default): read-only at runtime.

An entry without "Enable control" builds a read-only client.  Every Home
Assistant control action on every Rehom entity that supports it is called with
valid arguments (so Home Assistant's own validation passes) and must raise
``ServiceValidationError(rehom, control_disabled)`` without touching the
client: no write reaches the replay device, its transport log and the client's
connection state are unchanged, and so is the entity's state.  Turning a
thermostat off is not offered at all (Home Assistant refuses it as not
supported before the integration is called).

There is no button: Home Assistant records a button press (the entity's state)
before ``async_press`` could refuse it, so a refused "Reset alarms" would
still look pressed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.fan import (
    ATTR_PERCENTAGE,
    DOMAIN as FAN_DOMAIN,
    SERVICE_DECREASE_SPEED,
    SERVICE_INCREASE_SPEED,
    SERVICE_SET_PERCENTAGE,
)
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.select import (
    ATTR_OPTION,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_FIRST,
    SERVICE_SELECT_LAST,
    SERVICE_SELECT_NEXT,
    SERVICE_SELECT_OPTION,
    SERVICE_SELECT_PREVIOUS,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    SERVICE_TOGGLE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceNotSupported, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.rehom.const import (
    ATTR_DURATION,
    DOMAIN,
    PLATFORMS,
    SERVICE_CLEAR_TEMPORARY_COMFORT,
    SERVICE_GET_SCHEDULE,
    SERVICE_SET_TEMPORARY_COMFORT,
)

from .harness import RehomHarness
from .platform_helpers import HOUSE_CLIMATE, get_state

ZONE = "climate.zona_001"
FAN = "fan.vmc_001"
SELECT = "select.vmc_001_operating_mode"
NUMBER = "number.zona_001_temperature_offset"
SWITCH = "switch.rehom_plant_predictive_algorithm"

#: Actions Home Assistant refuses itself: the thermostats have no "turn off" feature
#: (the controller cannot switch the house or a zone off from Home Assistant).
NOT_SUPPORTED = {
    (CLIMATE_DOMAIN, SERVICE_TURN_OFF, HOUSE_CLIMATE),
    (CLIMATE_DOMAIN, SERVICE_TURN_OFF, ZONE),
}

#: (action domain, action, entity id, data): every control action of every platform.
CONTROL_ACTIONS: list[tuple[str, str, str, dict[str, Any]]] = [
    *(
        item
        for entity_id in (HOUSE_CLIMATE, ZONE)
        for item in (
            (CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, entity_id, {ATTR_HVAC_MODE: "auto"}),
            (CLIMATE_DOMAIN, SERVICE_SET_PRESET_MODE, entity_id, {ATTR_PRESET_MODE: "eco"}),
            (CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE, entity_id, {ATTR_TEMPERATURE: 24}),
            (CLIMATE_DOMAIN, SERVICE_TURN_ON, entity_id, {}),
            (CLIMATE_DOMAIN, SERVICE_TURN_OFF, entity_id, {}),
            (CLIMATE_DOMAIN, SERVICE_TOGGLE, entity_id, {}),
            (DOMAIN, SERVICE_SET_TEMPORARY_COMFORT, entity_id, {}),
            (DOMAIN, SERVICE_SET_TEMPORARY_COMFORT, entity_id, {ATTR_DURATION: 1.5}),
            (DOMAIN, SERVICE_CLEAR_TEMPORARY_COMFORT, entity_id, {}),
        )
    ),
    (FAN_DOMAIN, SERVICE_TURN_ON, FAN, {}),
    (FAN_DOMAIN, SERVICE_TURN_ON, FAN, {ATTR_PERCENTAGE: 75}),
    (FAN_DOMAIN, SERVICE_TURN_OFF, FAN, {}),
    (FAN_DOMAIN, SERVICE_TOGGLE, FAN, {}),
    (FAN_DOMAIN, SERVICE_SET_PERCENTAGE, FAN, {ATTR_PERCENTAGE: 75}),
    (FAN_DOMAIN, SERVICE_SET_PERCENTAGE, FAN, {ATTR_PERCENTAGE: 0}),
    (FAN_DOMAIN, SERVICE_INCREASE_SPEED, FAN, {}),
    (FAN_DOMAIN, SERVICE_DECREASE_SPEED, FAN, {}),
    (SELECT_DOMAIN, SERVICE_SELECT_OPTION, SELECT, {ATTR_OPTION: "cool"}),
    (SELECT_DOMAIN, SERVICE_SELECT_NEXT, SELECT, {}),
    (SELECT_DOMAIN, SERVICE_SELECT_PREVIOUS, SELECT, {}),
    (SELECT_DOMAIN, SERVICE_SELECT_FIRST, SELECT, {}),
    (SELECT_DOMAIN, SERVICE_SELECT_LAST, SELECT, {}),
    (NUMBER_DOMAIN, SERVICE_SET_VALUE, NUMBER, {ATTR_VALUE: 1}),
    (SWITCH_DOMAIN, SERVICE_TURN_ON, SWITCH, {}),
    (SWITCH_DOMAIN, SERVICE_TURN_OFF, SWITCH, {}),
    (SWITCH_DOMAIN, SERVICE_TOGGLE, SWITCH, {}),
]


@pytest.fixture
def loaded(
    entity_registry_enabled_by_default: None, init_integration: MockConfigEntry
) -> MockConfigEntry:
    """Every platform loaded and every entity enabled, at the replay start."""
    return init_integration


@pytest.mark.parametrize(
    "action",
    CONTROL_ACTIONS,
    ids=[
        f"{domain}.{service}-{entity_id}-{data}"
        for domain, service, entity_id, data in CONTROL_ACTIONS
    ],
)
async def test_control_action_refused(
    hass: HomeAssistant,
    harness: RehomHarness,
    loaded: MockConfigEntry,
    action: tuple[str, str, str, dict[str, Any]],
) -> None:
    """The action raises control_disabled and changes nothing, on the device or in HA."""
    domain, service, entity_id, data = action
    client = harness.client
    assert harness.allow_writes == [False]  # the entry's client is read-only
    calls = harness.transport_calls
    connection = client.connection_state
    before = get_state(hass, entity_id)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
        )
    if (domain, service, entity_id) in NOT_SUPPORTED:
        assert isinstance(err.value, ServiceNotSupported)
    else:
        assert err.value.translation_domain == DOMAIN
        assert err.value.translation_key == "control_disabled"
    await harness.settle()

    assert harness.transport_calls == calls
    assert harness.writes == []
    assert client.connection_state is connection
    assert harness.client is client  # no new client was created either
    after = get_state(hass, entity_id)
    assert (after.state, after.attributes, after.last_updated) == (
        before.state,
        before.attributes,
        before.last_updated,
    )


async def test_every_control_platform_covered() -> None:
    """The matrix covers every platform that has control actions."""
    assert {domain for domain, _service, _entity_id, _data in CONTROL_ACTIONS} == {
        CLIMATE_DOMAIN,
        FAN_DOMAIN,
        SELECT_DOMAIN,
        NUMBER_DOMAIN,
        SWITCH_DOMAIN,
        DOMAIN,
    }


async def test_no_button(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    """No button: a refused press would still be recorded as a press by Home Assistant."""
    assert Platform.BUTTON not in PLATFORMS
    assert hass.states.async_all("button") == []


async def test_get_schedule_on_house(
    hass: HomeAssistant, harness: RehomHarness, loaded: MockConfigEntry
) -> None:
    """rehom.get_schedule is for zone thermostats only."""
    calls = harness.transport_calls
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_SCHEDULE,
            {ATTR_ENTITY_ID: HOUSE_CLIMATE},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "zone_only"
    assert harness.transport_calls == calls


async def test_get_schedule_reads_no_device(
    hass: HomeAssistant, harness: RehomHarness, loaded: MockConfigEntry
) -> None:
    """rehom.get_schedule answers from the state model, without any request."""
    calls = harness.transport_calls
    response = await hass.services.async_call(
        DOMAIN, SERVICE_GET_SCHEDULE, {ATTR_ENTITY_ID: ZONE}, blocking=True, return_response=True
    )
    assert response is not None
    assert ZONE in response
    assert harness.transport_calls == calls


@pytest.mark.parametrize("duration", [0.3, 0.75, 24.5])
async def test_invalid_duration(
    hass: HomeAssistant, loaded: MockConfigEntry, duration: float
) -> None:
    """Durations outside 0.5-24 h or not a multiple of 0.5 h are rejected by the schema."""
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_TEMPORARY_COMFORT,
            {ATTR_ENTITY_ID: ZONE, ATTR_DURATION: duration},
            blocking=True,
        )
