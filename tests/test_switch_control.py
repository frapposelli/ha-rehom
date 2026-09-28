"""Switches with "Enable control" on: predictive algorithm writes, free cooling refused.

Each case calls the switch's action as a user or automation would and checks
what reached the replayed controller (the exact records), and what Home
Assistant shows when the call returns (the confirmed state).
"""

from __future__ import annotations

from typing import Any

from aiorehom import RehomWriteNotConfirmedError
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import SERVICE_TOGGLE, SERVICE_TURN_OFF, SERVICE_TURN_ON, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import CONTROL_OPTIONS, HOUSE_AUTO, HOUSE_AUTO_VALUES
from .control_helpers import (
    CONTROL_OFF_OPTIONS,
    assert_device_error,
    assert_key,
    call_action,
    call_action_until_done,
)
from .harness import RehomHarness, set_values
from .platform_helpers import get_state

PREDICTIVE = "switch.rehom_plant_predictive_algorithm"
PREDICTIVE_PATH = "REHOM...ALG_ATTIVO"
FREE_COOLING = "switch.vmc_001_free_cooling"
#: Free cooling settable on the controller: the switch exists (hidden on the fixture).
FREE_COOLING_WRITABLE = set_values({**HOUSE_AUTO_VALUES, "DEUM...ABILITA_F_COOLING": "2"})
SWITCH_ACTIONS = [SERVICE_TURN_ON, SERVICE_TURN_OFF, SERVICE_TOGGLE]


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """ "Enable control" on."""
    return dict(CONTROL_OPTIONS)


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the switch platform."""
    return [Platform.SWITCH]


# -- predictive algorithm ---------------------------------------------------------------


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_predictive(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """House in AUTO: off, on, toggle; each shown as soon as the call returns."""
    assert get_state(hass, PREDICTIVE).state == "on"
    await call_action(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, PREDICTIVE)
    assert harness.written_values == [{PREDICTIVE_PATH: "0"}]
    assert get_state(hass, PREDICTIVE).state == "off"
    await call_action(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, PREDICTIVE)
    assert get_state(hass, PREDICTIVE).state == "on"
    await call_action(hass, SWITCH_DOMAIN, SERVICE_TOGGLE, PREDICTIVE)
    assert get_state(hass, PREDICTIVE).state == "off"
    assert harness.written_values == [
        {PREDICTIVE_PATH: "0"},
        {PREDICTIVE_PATH: "1"},
        {PREDICTIVE_PATH: "0"},
    ]
    assert harness.transport_calls.count("post_bulk_update") == 3


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_predictive_already_on(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Turning on a switch that is on: success, and nothing is sent."""
    await call_action(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, PREDICTIVE)
    assert harness.writes == []
    assert get_state(hass, PREDICTIVE).state == "on"


@pytest.mark.parametrize("service", SWITCH_ACTIONS)
async def test_predictive_needs_house_auto(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, service: str
) -> None:
    """The fixture's house is in manual: house_not_auto (even to the current value)."""
    with pytest.raises(ServiceValidationError) as err:
        await call_action(hass, SWITCH_DOMAIN, service, PREDICTIVE)
    assert_key(err, "house_not_auto")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls
    assert get_state(hass, PREDICTIVE).state == "on"


@pytest.mark.parametrize(
    "device_patch", [set_values({**HOUSE_AUTO_VALUES, "REHOM...WEBSERVER": "2"})]
)
async def test_predictive_bus_down(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The serial line to the plant is down: bus_down, a device error, nothing sent.

    The switch stays available (it belongs to the plant, not to a zone or VMC).
    """
    assert get_state(hass, PREDICTIVE).state == "on"
    with pytest.raises(HomeAssistantError) as err:
        await call_action(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, PREDICTIVE)
    assert_device_error(err, "bus_down")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls
    assert get_state(hass, PREDICTIVE).state == "on"


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
@pytest.mark.parametrize("entry_options", [CONTROL_OFF_OPTIONS])
@pytest.mark.parametrize("service", SWITCH_ACTIONS)
async def test_predictive_control_disabled(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, service: str
) -> None:
    """The default entry (control off): control_disabled, no request, the state unchanged."""
    calls = harness.transport_calls
    with pytest.raises(ServiceValidationError) as err:
        await call_action(hass, SWITCH_DOMAIN, service, PREDICTIVE)
    assert_key(err, "control_disabled")
    await harness.settle()
    assert harness.transport_calls == calls
    assert get_state(hass, PREDICTIVE).state == "on"


@pytest.mark.parametrize("device_patch", [HOUSE_AUTO])
async def test_predictive_not_confirmed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Sent, but the controller never reports it: write_not_confirmed, still shown on."""
    harness.echo = False
    harness.apply = False
    with pytest.raises(HomeAssistantError) as err:
        await call_action_until_done(harness, SWITCH_DOMAIN, SERVICE_TURN_OFF, PREDICTIVE)
    assert_device_error(err, "write_not_confirmed")
    assert isinstance(err.value.__cause__, RehomWriteNotConfirmedError)
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert harness.written_values == [{PREDICTIVE_PATH: "0"}]
    assert get_state(hass, PREDICTIVE).state == "on"


# -- free cooling -------------------------------------------------------------------------


@pytest.mark.parametrize("device_patch", [FREE_COOLING_WRITABLE])
@pytest.mark.parametrize("service", SWITCH_ACTIONS)
async def test_free_cooling_not_supported(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, service: str
) -> None:
    """Home Assistant cannot set free cooling: not_supported, nothing sent."""
    before = get_state(hass, FREE_COOLING)
    with pytest.raises(ServiceValidationError) as err:
        await call_action(hass, SWITCH_DOMAIN, service, FREE_COOLING)
    assert_key(err, "not_supported")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls
    assert get_state(hass, FREE_COOLING).state == before.state


@pytest.mark.parametrize("device_patch", [FREE_COOLING_WRITABLE])
@pytest.mark.parametrize("entry_options", [CONTROL_OFF_OPTIONS])
async def test_free_cooling_control_disabled(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Control off answers control_disabled first, like every other control."""
    calls = harness.transport_calls
    with pytest.raises(ServiceValidationError) as err:
        await call_action(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, FREE_COOLING)
    assert_key(err, "control_disabled")
    await harness.settle()
    assert harness.transport_calls == calls
