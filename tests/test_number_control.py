"""Zone offset number with "Enable control" on: the writes Home Assistant sends.

Each case calls the number's action as a user or automation would and checks
what reached the replayed controller (the exact records), and what Home
Assistant shows when the call returns (the confirmed value, never the
requested one).  The value is rounded like the zone thermostat's target
(``control.round_offset``: halves round up).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from aiorehom import RehomTimeoutError, RehomWriteNotConfirmedError
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, Platform, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rehom import control
from custom_components.rehom.number import RehomZoneOffsetNumber

from .conftest import CONTROL_OPTIONS
from .control_helpers import (
    CONTROL_OFF_OPTIONS,
    assert_device_error,
    assert_key,
    call_action,
    call_action_until_done,
)
from .harness import RehomHarness, set_values
from .platform_helpers import get_entity, get_state

NUMBER = "number.zona_001_temperature_offset"
OFFSET_PATH = "ZONA.001..DELTA_SETP_CORRENTE"


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """ "Enable control" on."""
    return dict(CONTROL_OPTIONS)


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the number platform."""
    return [Platform.NUMBER]


async def _set_value(hass: HomeAssistant, value: float, entity_id: str = NUMBER) -> None:
    await call_action(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, entity_id, {ATTR_VALUE: value})


async def test_set_offset(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """0 -> +1 -> 0: one record each, and Home Assistant shows the value when the call returns."""
    assert get_state(hass, NUMBER).state == "0.0"
    await _set_value(hass, 1)
    assert harness.written_values == [{OFFSET_PATH: "1.0"}]
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert get_state(hass, NUMBER).state == "1.0"
    await _set_value(hass, 0)
    assert harness.written_values == [{OFFSET_PATH: "1.0"}, {OFFSET_PATH: "0.0"}]
    assert get_state(hass, NUMBER).state == "0.0"


@pytest.mark.parametrize(
    ("value", "written"),
    [
        (2.4, "2.0"),
        (-2.6, "-3.0"),
        (-0.4, None),
        # halves round up, as on the zone thermostat (not to even, as round() does)
        (0.5, "1.0"),
        (1.5, "2.0"),
        (2.5, "3.0"),
        (-0.5, None),
        (-1.5, "-1.0"),
        (-2.5, "-2.0"),
    ],
    ids=str,
)
async def test_value_rounded_to_whole_degrees(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    value: float,
    written: str | None,
) -> None:
    """The controller takes whole degrees: halves round up (-0.4 and -0.5 are 0: nothing sent)."""
    await _set_value(hass, value)
    assert harness.written_values == ([] if written is None else [{OFFSET_PATH: written}])
    assert get_state(hass, NUMBER).state == (written or "0.0")


async def test_rounded_by_the_shared_helper(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The number rounds with control.round_offset, the zone thermostat's helper."""
    with patch.object(control, "round_offset", wraps=control.round_offset) as round_offset:
        await _set_value(hass, 2.5)
    round_offset.assert_called_once_with(2.5)
    assert harness.written_values == [{OFFSET_PATH: "3.0"}]


@pytest.mark.parametrize("value", [float("nan"), "nan", "NaN"], ids=["float", "str", "str_upper"])
async def test_nan_refused(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    value: float | str,
) -> None:
    """NaN passes Home Assistant's range check: invalid_value, nothing sent, the value unchanged."""
    with pytest.raises(ServiceValidationError) as err:
        await call_action(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, NUMBER, {ATTR_VALUE: value})
    assert_key(err, "invalid_value")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls
    assert get_state(hass, NUMBER).state == "0.0"


@pytest.mark.parametrize("value", [float("inf"), float("-inf")], ids=["inf", "-inf"])
async def test_infinity_refused_by_home_assistant(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, value: float
) -> None:
    """±infinity is out of range: Home Assistant refuses it before the integration."""
    with pytest.raises(ServiceValidationError) as err:
        await _set_value(hass, value)
    assert err.value.translation_domain == NUMBER_DOMAIN
    assert harness.writes == []


async def test_non_finite_direct_call(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Called directly (no range check), NaN and ±infinity are refused with invalid_value."""
    number = get_entity(hass, NUMBER)
    assert isinstance(number, RehomZoneOffsetNumber)
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ServiceValidationError) as err:
            await number.async_set_native_value(value)
        assert_key(err, "invalid_value")
    assert harness.writes == []


async def test_fahrenheit(
    hass: HomeAssistant,
    harness: RehomHarness,
    entity_registry: er.EntityRegistry,
    init_integration: MockConfigEntry,
) -> None:
    """Shown in °F: +1.8 °F is a +1 °C offset (a difference, not an absolute temperature)."""
    entity_registry.async_update_entity_options(
        NUMBER, "number", {"unit_of_measurement": UnitOfTemperature.FAHRENHEIT}
    )
    await hass.async_block_till_done()
    assert get_state(hass, NUMBER).attributes[ATTR_UNIT_OF_MEASUREMENT] == "°F"
    await _set_value(hass, 1.8)
    assert harness.written_values == [{OFFSET_PATH: "1.0"}]
    assert float(get_state(hass, NUMBER).state) == pytest.approx(1.8)


async def test_same_value_sends_nothing(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Zone 002 is already at -1: success, and nothing is sent."""
    await _set_value(hass, -1, "number.zona_002_temperature_offset")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls


@pytest.mark.parametrize("device_patch", [set_values({"REHOM...TERMO_READONLY": "1"})])
async def test_read_only_refused(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The controller is set read-only: read_only, nothing sent, the value unchanged."""
    with pytest.raises(ServiceValidationError) as err:
        await _set_value(hass, 1)
    assert_key(err, "read_only")
    assert harness.writes == []
    assert "post_bulk_update" not in harness.transport_calls
    assert get_state(hass, NUMBER).state == "0.0"


@pytest.mark.parametrize("entry_options", [CONTROL_OFF_OPTIONS])
@pytest.mark.parametrize("value", [1, float("nan")], ids=["1", "nan"])
async def test_control_disabled(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry, value: float
) -> None:
    """The default entry (control off): control_disabled (before NaN is checked), no request."""
    calls = harness.transport_calls
    with pytest.raises(ServiceValidationError) as err:
        await _set_value(hass, value)
    assert_key(err, "control_disabled")
    await harness.settle()
    assert harness.transport_calls == calls
    assert harness.allow_writes == [False]
    assert get_state(hass, NUMBER).state == "0.0"


async def test_not_confirmed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Sent, but the controller never reports it: write_not_confirmed, the value unchanged."""
    harness.echo = False
    harness.apply = False
    with pytest.raises(HomeAssistantError) as err:
        await call_action_until_done(
            harness, NUMBER_DOMAIN, SERVICE_SET_VALUE, NUMBER, {ATTR_VALUE: 1}
        )
    assert_device_error(err, "write_not_confirmed")
    assert isinstance(err.value.__cause__, RehomWriteNotConfirmedError)
    assert harness.transport_calls.count("post_bulk_update") == 1
    assert harness.written_values == [{OFFSET_PATH: "1.0"}]
    assert get_state(hass, NUMBER).state == "0.0"


async def test_write_failed(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """The request itself failed (it may or may not have landed): write_failed."""
    harness.errors["post_bulk_update"] = RehomTimeoutError("replay: POST timed out")
    with pytest.raises(HomeAssistantError) as err:
        await _set_value(hass, 1)
    assert_device_error(err, "write_failed")
    assert get_state(hass, NUMBER).state == "0.0"
