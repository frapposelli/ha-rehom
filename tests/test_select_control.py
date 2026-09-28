"""VMC mode select with "Enable control" on: the writes Home Assistant sends.

Each case calls the select's action as a user or automation would and checks
what reached the replayed controller (the exact records), and what Home
Assistant shows when the call returns (the confirmed mode).  Only dehumidify
and ventilate were tested on a real controller; the other listed modes are
refused with ``not_verified``.
"""

from __future__ import annotations

from typing import Any

from aiorehom import RehomWriteNotConfirmedError
from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_FIRST,
    SERVICE_SELECT_LAST,
    SERVICE_SELECT_NEXT,
    SERVICE_SELECT_OPTION,
    SERVICE_SELECT_PREVIOUS,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import CONTROL_OPTIONS
from .control_helpers import (
    CONTROL_OFF_OPTIONS,
    assert_device_error,
    assert_key,
    call_action,
    call_action_until_done,
)
from .harness import RehomHarness, set_values
from .platform_helpers import get_state

SELECT = "select.vmc_001_operating_mode"
MODE_PATH = "DEUM.001..ST_MODE"


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """ "Enable control" on."""
    return dict(CONTROL_OPTIONS)


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the select platform."""
    return [Platform.SELECT]


async def _select(hass: HomeAssistant, option: str) -> None:
    await call_action(hass, SELECT_DOMAIN, SERVICE_SELECT_OPTION, SELECT, {ATTR_OPTION: option})


async def test_select_mode(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Dehumidify -> ventilate -> dehumidify, each shown as soon as the call returns."""
    assert get_state(hass, SELECT).state == "dehumidify"
    await _select(hass, "ventilate")
    assert harness.written_values == [{MODE_PATH: "8"}]
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert get_state(hass, SELECT).state == "ventilate"
    await _select(hass, "dehumidify")
    assert harness.written_values == [{MODE_PATH: "8"}, {MODE_PATH: "1"}]
    assert get_state(hass, SELECT).state == "dehumidify"


async def test_select_last(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """select_last routes to the same write (ventilate is the last option)."""
    await call_action(hass, SELECT_DOMAIN, SERVICE_SELECT_LAST, SELECT)
    assert harness.written_values == [{MODE_PATH: "8"}]
    assert get_state(hass, SELECT).state == "ventilate"


async def test_same_mode_sends_nothing(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Selecting the current mode: success, and nothing is sent."""
    await _select(hass, "dehumidify")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_SELECT_OPTION, {ATTR_OPTION: "cool"}),
        (SERVICE_SELECT_OPTION, {ATTR_OPTION: "stop"}),
        (SERVICE_SELECT_OPTION, {ATTR_OPTION: "dehumidify_cool"}),
        (SERVICE_SELECT_NEXT, {}),  # dehumidify -> dehumidify_cool
        (SERVICE_SELECT_PREVIOUS, {}),  # dehumidify -> stop
        (SERVICE_SELECT_FIRST, {}),  # stop
    ],
    ids=["cool", "stop", "dehumidify_cool", "next", "previous", "first"],
)
async def test_not_verified(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    service: str,
    data: dict[str, Any],
) -> None:
    """Modes never tested on a real controller: not_verified, nothing sent."""
    with pytest.raises(ServiceValidationError) as err:
        await call_action(hass, SELECT_DOMAIN, service, SELECT, data)
    assert_key(err, "not_verified")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls
    assert get_state(hass, SELECT).state == "dehumidify"


async def test_rapid_mode_not_offered(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Rapid renewal is not an option: Home Assistant refuses it before the integration."""
    assert "rapid_renewal" not in get_state(hass, SELECT).attributes[ATTR_OPTIONS]
    with pytest.raises(ServiceValidationError) as err:
        await _select(hass, "rapid_renewal")
    assert err.value.translation_domain == SELECT_DOMAIN
    assert err.value.translation_key == "not_valid_option"
    assert harness.writes == []


@pytest.mark.parametrize("device_patch", [set_values({"DEUM.001..ST_MODE": "6"})])
async def test_running_rapid_mode_not_verified(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A running rapid cycle is listed (it is the state) but cannot be selected."""
    assert get_state(hass, SELECT).state == "rapid_renewal"
    with pytest.raises(ServiceValidationError) as err:
        await _select(hass, "rapid_renewal")
    assert_key(err, "not_verified")
    assert harness.writes == []


@pytest.mark.parametrize(
    "device_patch", [set_values({"DEUM.001..ST_MODE": "8", "DEUM...ABILITA_VENTILA": "0"})]
)
async def test_mode_not_available(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Ventilate is running but no longer selectable: it is listed, and refused."""
    state = get_state(hass, SELECT)
    assert state.state == "ventilate"
    assert "ventilate" in state.attributes[ATTR_OPTIONS]
    with pytest.raises(ServiceValidationError) as err:
        await _select(hass, "ventilate")
    assert_key(err, "mode_not_available")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls


@pytest.mark.parametrize(
    "device_patch",
    [set_values({"DEUM.001..ST_STATO_DEUM": "3"}), set_values({"DEUM.001..ST_STATO_DEUM": "2"})],
    ids=["forced", "error"],
)
async def test_vmc_busy_is_a_device_error(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A VMC forced from elsewhere or in error: vmc_busy, a device error, nothing sent."""
    with pytest.raises(HomeAssistantError) as err:
        await _select(hass, "ventilate")
    assert_device_error(err, "vmc_busy")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls
    assert get_state(hass, SELECT).state == "dehumidify"


@pytest.mark.parametrize("entry_options", [CONTROL_OFF_OPTIONS])
async def test_control_disabled(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The default entry (control off): control_disabled, no request, the mode unchanged."""
    calls = harness.transport_calls
    with pytest.raises(ServiceValidationError) as err:
        await _select(hass, "ventilate")
    assert_key(err, "control_disabled")
    await harness.settle()
    assert harness.transport_calls == calls
    assert get_state(hass, SELECT).state == "dehumidify"


async def test_not_confirmed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Sent, but the controller never reports it: write_not_confirmed, the mode unchanged."""
    harness.echo = False
    harness.apply = False
    with pytest.raises(HomeAssistantError) as err:
        await call_action_until_done(
            harness, SELECT_DOMAIN, SERVICE_SELECT_OPTION, SELECT, {ATTR_OPTION: "ventilate"}
        )
    assert_device_error(err, "write_not_confirmed")
    assert isinstance(err.value.__cause__, RehomWriteNotConfirmedError)
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert harness.written_values == [{MODE_PATH: "8"}]
    assert get_state(hass, SELECT).state == "dehumidify"
