"""Alarm events of the plant, each zone and each VMC.

Events are diffs of the library's debounced alarm list (``alarms_debounced``,
60 s on both edges: an alarm is announced after 60 s active and cleared after
60 s inactive, so a short dropout fires nothing), taken after the entity is
added: alarms already active at setup, reload or restart fire nothing, and a
resync (which keeps alarm ids and ``first_seen``) never re-fires them.

A new client starts every debounce afresh, so an alarm that is already active
when the entity is added is not debounced yet and matures up to 60 s later.
Those alarms are part of the baseline too (``_pending``): they join the known
set silently when they mature, and are forgotten if they end first.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import override

from aiorehom import Alarm, DeviceKind, RehomState
from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import EVENT_ALARM_CLEARED, EVENT_ALARM_RAISED
from .coordinator import RehomConfigEntry, RehomCoordinator
from .entity import (
    EntityFactory,
    RehomEntity,
    alarm_attributes,
    async_setup_dynamic_entities,
    device_alarms,
    entity_unique_id,
)

PARALLEL_UPDATES = 0

KEY = "alarm"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RehomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one alarm event entity per plant, zone and VMC (and later units)."""
    coordinator = entry.runtime_data.coordinator
    hub_id = coordinator.hub_id

    def build(state: RehomState) -> Iterable[tuple[str, EntityFactory]]:
        yield (
            entity_unique_id(hub_id, DeviceKind.PLANT, None, KEY),
            partial(RehomAlarmEvent, coordinator, DeviceKind.PLANT, None),
        )
        for zone_id in state.zones:
            yield (
                entity_unique_id(hub_id, DeviceKind.ZONE, zone_id, KEY),
                partial(RehomAlarmEvent, coordinator, DeviceKind.ZONE, zone_id),
            )
        for vmc_id in state.vmcs:
            yield (
                entity_unique_id(hub_id, DeviceKind.VMC, vmc_id, KEY),
                partial(RehomAlarmEvent, coordinator, DeviceKind.VMC, vmc_id),
            )

    async_setup_dynamic_entities(entry, async_add_entities, build)


class RehomAlarmEvent(RehomEntity, EventEntity):
    """``raised`` / ``cleared`` for each debounced alarm of one device.

    A zone's or VMC's event entity follows the unit rules (unavailable while the
    unit is offline, availability rule 2): nothing fires meanwhile, and the
    difference is fired when the unit is back.
    """

    _attr_translation_key = KEY

    def __init__(self, coordinator: RehomCoordinator, kind: DeviceKind, unit: str | None) -> None:
        """Plant, zone or VMC entity."""
        super().__init__(coordinator, kind, unit, KEY)
        self._attr_event_types = [EVENT_ALARM_RAISED, EVENT_ALARM_CLEARED]
        #: Debounced alarms already announced (or part of the baseline), by id.
        self._known: dict[str, Alarm] = {}
        #: Baseline alarms that were active but not yet debounced when added.
        self._pending: set[str] = set()

    def _current(self) -> dict[str, Alarm]:
        return {
            alarm.id: alarm for alarm in device_alarms(self.rehom_state, self._kind, self._unit)
        }

    def _active_ids(self) -> set[str]:
        return {
            alarm.id
            for alarm in device_alarms(self.rehom_state, self._kind, self._unit, debounced=False)
        }

    @override
    async def async_added_to_hass(self) -> None:
        """Start from the alarms active now (debounced or not): no event for them."""
        await super().async_added_to_hass()
        self._known = self._current()
        self._pending = self._active_ids() - self._known.keys()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        # While unavailable nothing fires and the baseline is kept, so the
        # difference is fired once the entity is available again.
        if self.available:
            current = self._current()
            known = self._known
            self._pending &= self._active_ids()  # a baseline alarm that ended is forgotten
            for alarm_id in sorted(current.keys() - known.keys()):
                if alarm_id in self._pending:
                    self._pending.discard(alarm_id)  # matured baseline alarm: silent
                    continue
                self._trigger_event(EVENT_ALARM_RAISED, alarm_attributes(current[alarm_id]))
                self.async_write_ha_state()
            for alarm_id in sorted(known.keys() - current.keys()):
                self._trigger_event(EVENT_ALARM_CLEARED, alarm_attributes(known[alarm_id]))
                self.async_write_ha_state()
            self._known = current
        super()._handle_coordinator_update()
