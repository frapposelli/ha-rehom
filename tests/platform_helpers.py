"""Small helpers shared by the platform tests (builder B)."""

from __future__ import annotations

from typing import Any

from aiorehom.replay import ReplayData
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import async_get_platforms

from custom_components.rehom.const import DOMAIN

# Entity ids on the fixture (area-less devices: "<device name> <entity name>").
HOUSE_CLIMATE = "climate.rehom_plant"
ZONE_CLIMATE = "climate.zona_{zone}"


def get_state(hass: HomeAssistant, entity_id: str) -> State:
    """The state of ``entity_id`` (fails if the entity does not exist)."""
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} does not exist"
    return state


def is_available(hass: HomeAssistant, entity_id: str) -> bool:
    """``True`` unless the entity's state is ``unavailable``."""
    return get_state(hass, entity_id).state != STATE_UNAVAILABLE


def get_entity(hass: HomeAssistant, entity_id: str) -> Entity:
    """The live entity object behind ``entity_id`` (to call its properties directly)."""
    for platform in async_get_platforms(hass, DOMAIN):
        if entity_id in platform.entities:
            return platform.entities[entity_id]
    raise AssertionError(f"{entity_id} is not a rehom entity")


def snapshot_states(hass: HomeAssistant, domain: str) -> dict[str, tuple[str, Any, Any]]:
    """(state, attributes, last_updated) of every entity of ``domain``."""
    return {
        state.entity_id: (state.state, dict(state.attributes), state.last_updated)
        for state in hass.states.async_all(domain)
    }


class StateChanges:
    """Records the new states of one entity (for event entities: each event)."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.entity_id = entity_id
        self.states: list[State] = []
        self._unsub = hass.bus.async_listen(EVENT_STATE_CHANGED, self._record)

    @callback
    def _record(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if event.data["entity_id"] == self.entity_id and new is not None:
            self.states.append(new)

    def events(self) -> list[State]:
        """One state per fired event (an event entity's state is its unique timestamp).

        A state written again after the entity was unavailable repeats the last
        event's timestamp and is not a new event.
        """
        seen: set[str] = set()
        events: list[State] = []
        for state in self.states:
            if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) or state.state in seen:
                continue
            if state.attributes.get("event_type") is None:
                continue
            seen.add(state.state)
            events.append(state)
        return events

    def stop(self) -> None:
        """Stop recording."""
        self._unsub()


def auto_with_override(data: ReplayData) -> None:
    """House in AUTO and a 12:00-14:00 local (10:00-12:00Z) comfort override on zone 001.

    Every zone of the fixture binds preset "1" on every day, so the override applies.
    """
    for row in data.interface:
        if row.get("path") == "REHOM...MODO":
            row["Valore"] = "2"
    data.overrides.append(
        {
            "Gruppo": "PROG",
            "Unita": "001",
            "SubUni": "1",
            "Key": "PROG_GIORNO_ESTATE",
            "Valore": ",".join(["3"] * 48),
            "Impostazione": "2026-09-25 12:00:00",
            "Scadenza": "2026-09-25 14:00:00",
        }
    )
