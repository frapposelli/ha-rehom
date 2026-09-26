"""Alarm event entities on the replay."""

from __future__ import annotations

from typing import Any

from aiorehom import RehomConnectionError
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .harness import RehomHarness, T, termo_update
from .platform_helpers import StateChanges, get_state, is_available

VMC_001_EVENTS = "event.vmc_001_alarm_events"
DIRTY_FILTER_KEY = "DEUM.001..ALLARM_PRESSOSTATO_FILTRO"
DIRTY_FILTER_ID = "vmc:001:ALLARM_PRESSOSTATO_FILTRO"
ZONE_002_OFFLINE = "1,0,1,0,0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"
ZONE_002_ONLINE = "1,1,1,0,0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"


def _dirty_filter(*changes: tuple[str, str]) -> dict[str, Any]:
    """``device_patch``: VMC 001 dirty-filter flag changes ``(time, "0"|"1")``."""
    return {
        "extra_frames": [(T(at), termo_update(DIRTY_FILTER_KEY, value)) for at, value in changes]
    }


def _summary(changes: StateChanges) -> list[tuple[str, Any]]:
    return [
        (state.attributes["event_type"], state.attributes["alarm_id"]) for state in changes.events()
    ]


@pytest.fixture
def platforms() -> list[Platform]:
    """Only the event platform."""
    return [Platform.EVENT]


async def test_snapshot(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """Every alarm event entity at the replay start (no event yet)."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_no_event_on_timeline(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Nothing fires at setup, nor for the 14-s VMC 001 probe glitch at 10:37:07."""
    changes = {
        state.entity_id: StateChanges(hass, state.entity_id)
        for state in hass.states.async_all("event")
    }
    assert len(changes) == 9
    assert {state.state for state in hass.states.async_all("event")} == {STATE_UNKNOWN}
    await harness.advance_to(T("10:37:25"))
    assert all(not recorder.events() for recorder in changes.values())
    assert {state.state for state in hass.states.async_all("event")} == {STATE_UNKNOWN}


@pytest.mark.parametrize("device_patch", [_dirty_filter(("10:23:00", "1"), ("10:25:00", "0"))])
async def test_raised_and_cleared(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """One ``raised`` 60 s after the flag, one ``cleared`` 60 s after it drops, with its data."""
    changes = StateChanges(hass, VMC_001_EVENTS)
    await harness.advance_to(T("10:23:59"))
    assert changes.events() == []
    await harness.advance_to(T("10:24:01"))
    raised = get_state(hass, VMC_001_EVENTS)
    assert raised.attributes["event_type"] == "raised"
    assert raised.attributes["alarm_id"] == DIRTY_FILTER_ID
    assert raised.attributes["source"] == "vmc_flag"
    assert raised.attributes["unit"] == "001"
    assert raised.attributes["text"] == "dirty filter"
    assert raised.attributes["first_seen"] == "2026-09-25T10:23:00.100000+00:00"
    await harness.advance_to(T("10:25:59"))  # dropped at 10:25:00: the clear is debounced
    assert _summary(changes) == [("raised", DIRTY_FILTER_ID)]
    await harness.advance_to(T("10:26:05"))
    cleared = get_state(hass, VMC_001_EVENTS)
    assert cleared.attributes["event_type"] == "cleared"
    assert cleared.attributes["alarm_id"] == DIRTY_FILTER_ID
    assert cleared.attributes["first_seen"] == raised.attributes["first_seen"]
    assert _summary(changes) == [("raised", DIRTY_FILTER_ID), ("cleared", DIRTY_FILTER_ID)]
    assert get_state(hass, "event.vmc_002_alarm_events").state == STATE_UNKNOWN
    assert get_state(hass, "event.rehom_plant_alarm_events").state == STATE_UNKNOWN


@pytest.mark.parametrize("device_patch", [_dirty_filter(("10:23:00", "1"), ("10:27:00", "0"))])
async def test_reload_does_not_refire(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A reload with the alarm still active (its debounce restarts) fires nothing new."""
    changes = StateChanges(hass, VMC_001_EVENTS)
    await harness.advance_to(T("10:24:30"))
    assert _summary(changes) == [("raised", DIRTY_FILTER_ID)]
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await harness.settle()
    await harness.advance_to(T("10:26:00"))  # the new client debounced it at ~10:25:30
    assert DIRTY_FILTER_ID in {alarm.id for alarm in harness.client.state.alarms_debounced}
    assert _summary(changes) == [("raised", DIRTY_FILTER_ID)]
    await harness.advance_to(T("10:28:05"))  # dropped at 10:27:00, cleared 60 s later
    assert _summary(changes) == [("raised", DIRTY_FILTER_ID), ("cleared", DIRTY_FILTER_ID)]


@pytest.mark.parametrize(
    ("device_patch", "expected"),
    [
        # active at the first sync, matures later: silent; its end is announced
        (_dirty_filter(("10:22:00", "1"), ("10:25:00", "0")), [("cleared", DIRTY_FILTER_ID)]),
        # active at the first sync, ends before maturing, then a new alarm: announced
        (
            _dirty_filter(
                ("10:22:00", "1"), ("10:22:30", "0"), ("10:23:00", "1"), ("10:25:00", "0")
            ),
            [("raised", DIRTY_FILTER_ID), ("cleared", DIRTY_FILTER_ID)],
        ),
    ],
)
async def test_active_at_first_sync(
    hass: HomeAssistant,
    harness: RehomHarness,
    init_integration: MockConfigEntry,
    expected: list[tuple[str, str]],
) -> None:
    """An alarm already active at setup never fires ``raised`` (first sync, reload, restart)."""
    changes = StateChanges(hass, VMC_001_EVENTS)
    assert {alarm.id for alarm in harness.client.state.alarms} == {DIRTY_FILTER_ID}
    await harness.advance_to(T("10:26:05"))  # the 10:25:00 end is announced at 10:26:00
    assert _summary(changes) == expected


@pytest.mark.parametrize(
    "device_patch",
    [
        {
            "extra_frames": [
                (T("10:23:00"), termo_update("REHOM...STATO_SONDE", ZONE_002_OFFLINE)),
                (T("10:25:00"), termo_update("REHOM...STATO_SONDE", ZONE_002_ONLINE)),
            ]
        }
    ],
)
async def test_offline_zone_event_is_unavailable(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """Rule 2: an offline zone's alarm event is unavailable; "not responding" fires nothing.

    The zone's probe connection sensor shows the outage.  When the zone is back,
    the library still holds the ended alarm for 60 s: that is no event either.
    """
    changes = StateChanges(hass, "event.zona_002_alarm_events")
    await harness.advance_to(T("10:23:05"))
    assert not is_available(hass, "event.zona_002_alarm_events")
    await harness.advance_to(T("10:24:05"))
    assert not is_available(hass, "event.zona_002_alarm_events")
    await harness.advance_to(T("10:25:05"))
    assert is_available(hass, "event.zona_002_alarm_events")
    await harness.advance_to(T("10:26:30"))  # past the release of the held alarm
    assert changes.events() == []
    assert get_state(hass, "event.zona_002_alarm_events").state == STATE_UNKNOWN


@pytest.mark.parametrize(
    "device_patch",
    [_dirty_filter(("10:23:00", "1"), ("10:25:00", "0"), ("10:25:05", "1"))],
)
async def test_short_dropout_fires_nothing(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """A 5-s dropout of a debounced alarm is no clear and no second raise."""
    changes = StateChanges(hass, VMC_001_EVENTS)
    await harness.advance_to(T("10:27:30"))
    assert _summary(changes) == [("raised", DIRTY_FILTER_ID)]


@pytest.mark.parametrize("device_patch", [_dirty_filter(("10:24:00", "1"))])
async def test_deferred_while_unavailable(
    hass: HomeAssistant, harness: RehomHarness, init_integration: MockConfigEntry
) -> None:
    """While the controller is unavailable nothing fires; the difference fires on recovery."""
    changes = StateChanges(hass, VMC_001_EVENTS)
    harness.errors["get_alive"] = RehomConnectionError("replay: controller unreachable")
    await harness.advance(150)  # three failed 30-s /alive/ polls
    assert get_state(hass, VMC_001_EVENTS).state == STATE_UNAVAILABLE
    await harness.advance_to(T("10:25:30"))  # the alarm matured at ~10:25:00
    assert DIRTY_FILTER_ID in {alarm.id for alarm in harness.client.state.alarms_debounced}
    assert changes.events() == []
    harness.errors.clear()
    await harness.advance(60)
    assert is_available(hass, VMC_001_EVENTS)
    assert _summary(changes) == [("raised", DIRTY_FILTER_ID)]
