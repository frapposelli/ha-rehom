"""Integration actions.

``rehom.get_schedule`` returns data.  ``rehom.set_temporary_comfort`` and
``rehom.clear_temporary_comfort`` keep their schema (so automations that use
them still load) but are always refused after it validated the input, and
never reach the controller: ``control_disabled`` while "Enable control" is off;
with it on, ``temporary_comfort_unavailable`` on a zone (not available yet) and
``zone_only`` on the house.
"""

from __future__ import annotations

from typing import Any

from aiorehom import ConnectionState
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion
import voluptuous as vol

from custom_components.rehom.const import (
    ATTR_DURATION,
    DOMAIN,
    SERVICE_CLEAR_TEMPORARY_COMFORT,
    SERVICE_GET_SCHEDULE,
    SERVICE_SET_TEMPORARY_COMFORT,
)

from .conftest import CONTROL_OPTIONS
from .harness import FIXTURE_MAC, FIXTURE_ZONES, RehomHarness

LEVELS = {"off", "economy", "pre_comfort", "comfort", None}
DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]


@pytest.fixture
def platforms() -> list[Platform]:
    """The actions target climate entities only."""
    return [Platform.CLIMATE]


def _climate(entity_registry: er.EntityRegistry, device: str) -> str:
    entity_id = entity_registry.async_get_entity_id(
        "climate", DOMAIN, f"{FIXTURE_MAC}_{device}_climate"
    )
    assert entity_id is not None
    return entity_id


async def _call(
    hass: HomeAssistant, service: str, entity_id: str, **data: Any
) -> dict[str, Any] | None:
    response = await hass.services.async_call(
        DOMAIN,
        service,
        {ATTR_ENTITY_ID: entity_id, **data},
        blocking=True,
        return_response=service == SERVICE_GET_SCHEDULE,
    )
    return dict(response) if response is not None else None


async def test_get_schedule(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Zone 001: both seasons, 7 days x 48 levels, in controller time."""
    entity_id = _climate(entity_registry, "zone_001")
    calls = harness.transport_calls
    response = await _call(hass, SERVICE_GET_SCHEDULE, entity_id)
    assert harness.transport_calls == calls  # served from the state, no request
    assert response is not None
    assert response == snapshot

    schedule = response[entity_id]
    assert schedule["zone"] == "001"
    assert schedule["season"] == "summer"
    assert schedule["timezone"] == "Europe/Rome"
    assert schedule["controller_time"].startswith("2026-09-25T12:22:06")
    for season in ("winter", "summer"):
        days = schedule[season]["days"]
        assert [day["weekday"] for day in days] == list(range(7))
        assert [day["day"] for day in days] == DAYS
        for day in days:
            assert day["preset"] == "1"  # the fixture binds preset 1 on every day
            assert len(day["levels"]) == 48
            assert set(day["levels"]) <= LEVELS


async def test_get_schedule_all_zones(
    hass: HomeAssistant, init_integration: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """One response entry per targeted zone thermostat."""
    entity_ids = [_climate(entity_registry, f"zone_{zone}") for zone in FIXTURE_ZONES]
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_SCHEDULE,
        {ATTR_ENTITY_ID: entity_ids},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    assert set(response) == set(entity_ids)
    assert [response[entity_id]["zone"] for entity_id in entity_ids] == list(FIXTURE_ZONES)  # type: ignore[index,call-overload]


async def test_get_schedule_house_is_zone_only(
    hass: HomeAssistant, init_integration: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """The house thermostat has no schedule of its own."""
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_GET_SCHEDULE, _climate(entity_registry, "plant"))
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "zone_only"


@pytest.mark.parametrize("duration", [0.3, 0.75, 24.5, 0, -1, "abc"])
async def test_set_temporary_comfort_invalid_duration(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    entity_registry: er.EntityRegistry,
    duration: Any,
) -> None:
    """Durations are 0.5-24 h in steps of 0.5; anything else fails validation."""
    calls = harness.transport_calls
    with pytest.raises(vol.Invalid):
        await _call(
            hass,
            SERVICE_SET_TEMPORARY_COMFORT,
            _climate(entity_registry, "zone_001"),
            **{ATTR_DURATION: duration},
        )
    assert harness.transport_calls == calls


TEMPORARY_COMFORT_CALLS = [
    (SERVICE_SET_TEMPORARY_COMFORT, {}),
    (SERVICE_SET_TEMPORARY_COMFORT, {ATTR_DURATION: 0.5}),
    (SERVICE_SET_TEMPORARY_COMFORT, {ATTR_DURATION: "1.5"}),
    (SERVICE_SET_TEMPORARY_COMFORT, {ATTR_DURATION: 24}),
    (SERVICE_CLEAR_TEMPORARY_COMFORT, {}),
]


async def _assert_refused(
    hass: HomeAssistant,
    harness: RehomHarness,
    entity_id: str,
    service: str,
    data: dict[str, Any],
    *,
    key: str,
) -> None:
    """The call reaches the entity and is refused with ``key``; nothing else happens."""
    state = hass.states.get(entity_id)
    calls = harness.transport_calls
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, service, entity_id, **data)
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == key
    await harness.settle()
    assert harness.transport_calls == calls
    assert harness.writes == []
    assert harness.client.connection_state is ConnectionState.CONNECTED
    assert hass.states.get(entity_id) == state


@pytest.mark.parametrize(("service", "data"), TEMPORARY_COMFORT_CALLS)
@pytest.mark.parametrize("device", ["plant", "zone_001"])
async def test_temporary_comfort_control_disabled(  # noqa: PLR0917
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    entity_registry: er.EntityRegistry,
    service: str,
    data: dict[str, Any],
    device: str,
) -> None:
    """Control off: valid calls reach the entity and are refused there, without any request."""
    assert harness.allow_writes == [False]
    await _assert_refused(
        hass, harness, _climate(entity_registry, device), service, data, key="control_disabled"
    )


@pytest.mark.parametrize("entry_options", [CONTROL_OPTIONS])
@pytest.mark.parametrize(("service", "data"), TEMPORARY_COMFORT_CALLS)
@pytest.mark.parametrize(
    ("device", "key"),
    [("plant", "zone_only"), ("zone_001", "temporary_comfort_unavailable")],
)
async def test_temporary_comfort_unavailable(  # noqa: PLR0917
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    harness: RehomHarness,
    entity_registry: er.EntityRegistry,
    service: str,
    data: dict[str, Any],
    device: str,
    key: str,
) -> None:
    """Control on: a zone's temporary comfort is not available yet; the house has none."""
    assert harness.allow_writes == [True]
    await _assert_refused(hass, harness, _climate(entity_registry, device), service, data, key=key)
