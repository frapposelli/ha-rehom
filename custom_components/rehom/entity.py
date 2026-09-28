"""Base entity, ids, device info and shared helpers.

Availability rules, referred to by number across the platforms:

1. The client is unavailable (liveness checks failing, or the WebSocket down
   and the HTTP fallback failing): every entity is unavailable.
2. A zone or VMC reported offline (``online is False``): all of its entities
   are unavailable except its connectivity sensor (``_offline_exempt``).
3. ``online is None`` (not reported) counts as online: available, values unknown.
4. Lock, crono and read-only flags never affect availability.
5. The serial line to the plant is down: zone and VMC entities are
   unavailable; hub and plant entities stay available.

A unit absent from the state makes its entities unavailable (they are never
removed automatically).  Plant values derived from zones follow rules 2 and 5.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import ClassVar

from aiorehom import Alarm, AlarmSource, DeviceKind, Plant, RehomState, Vmc, Zone
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, HOUSE_TEMPERATURE_ZONE, MANUFACTURER
from .coordinator import RehomConfigEntry, RehomCoordinator

#: Device kinds that get a Home Assistant device (actuators and fancoils do not).
DEVICE_KINDS: frozenset[DeviceKind] = frozenset(
    {DeviceKind.HUB, DeviceKind.PLANT, DeviceKind.ZONE, DeviceKind.VMC}
)
#: Unit device kinds (need a unit id; availability rules 2, 3 and 5 apply).
UNIT_KINDS: frozenset[DeviceKind] = frozenset({DeviceKind.ZONE, DeviceKind.VMC})


def device_identifier(hub_id: str, kind: DeviceKind, unit: str | None = None) -> tuple[str, str]:
    """Device registry identifier: hub = MAC; others ``{mac}_{kind}[_{unit}]``."""
    if kind is DeviceKind.HUB:
        return (DOMAIN, hub_id)
    return (DOMAIN, "_".join(part for part in (hub_id, kind.value, unit) if part))


def entity_unique_id(hub_id: str, kind: DeviceKind, unit: str | None, key: str) -> str:
    """``{mac}_{kind}[_{unit}]_{key}``; never names or serials."""
    return "_".join(part for part in (hub_id, kind.value, unit, key) if part)


def alarm_device(alarm: Alarm) -> tuple[DeviceKind, str | None]:
    """The HA device an alarm belongs to: its zone or VMC, else the plant.

    Hub, plant, actuator and fancoil alarms all map to the plant device.
    """
    if alarm.device in UNIT_KINDS and alarm.unit is not None:
        return (alarm.device, alarm.unit)
    return (DeviceKind.PLANT, None)


def device_alarms(
    state: RehomState, kind: DeviceKind, unit: str | None, *, debounced: bool = True
) -> tuple[Alarm, ...]:
    """The (debounced) alarms of one HA device, sorted by id.

    A zone's or VMC's own "not responding" alarm is left out: while it is
    active the unit's entities are unavailable (rule 2) and its connectivity
    sensor shows it, and after the unit is back the library still holds it for
    the clear debounce, which must not read as a new alarm of a live unit.  The
    plant's "Active alarms" lists it.  (An actuator's or fancoil's belongs to
    the plant device and stays.)
    """
    alarms = state.alarms_debounced if debounced else state.alarms
    return tuple(
        alarm
        for alarm in alarms
        if alarm_device(alarm) == (kind, unit)
        and not (kind in UNIT_KINDS and alarm.source is AlarmSource.UNIT_NOT_RESPONDING)
    )


def zone_is_live(state: RehomState, zone: Zone | None) -> bool:
    """Whether a zone's values may be shown: availability rules 2, 3 and 5.

    The zone is present, the serial line is up and the zone is not offline
    (``online is None`` is unknown, so it counts as live, rule 3).  Plant
    values derived from zones use this, so they never show data that the
    zone's own entities hide.
    """
    return zone is not None and not state.plant.serial_down and zone.online is not False


def house_temperature(state: RehomState) -> float | None:
    """``Plant.current_temperature`` (zone 001), unknown while that zone is not live."""
    if not zone_is_live(state, state.zones.get(HOUSE_TEMPERATURE_ZONE)):
        return None
    return state.plant.current_temperature


def house_demand(state: RehomState) -> bool | None:
    """Any live zone calling (``Plant.demand`` without offline zones).

    ``None`` while the serial line is down, or when no live zone reports.
    """
    if state.plant.serial_down:
        return None
    callings = [zone.calling for zone in state.zones.values() if zone_is_live(state, zone)]
    if any(calling is True for calling in callings):
        return True
    if all(calling is None for calling in callings):
        return None
    return False


def alarm_attributes(alarm: Alarm) -> dict[str, str | int | None]:
    """JSON-safe description of one alarm (entity attributes and event data)."""
    return {
        "alarm_id": alarm.id,
        "source": alarm.source.value,
        "unit": alarm.unit,
        "code": alarm.code,
        "text": alarm.text,
        "first_seen": alarm.first_seen.isoformat(),
    }


type EntityFactory = Callable[[], Entity]
type EntityBuilder = Callable[[RehomState], Iterable[tuple[str, EntityFactory]]]


@callback
def async_setup_dynamic_entities(
    entry: RehomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: EntityBuilder,
) -> None:
    """Add entities now, and later whenever new ones become possible (dynamic-devices).

    ``build(state)`` yields ``(key, factory)`` for every entity that should
    exist in ``state``; ``key`` must be unique within the platform (use the
    unique_id suffix).  Each key is created once; entities are never removed
    here (an absent unit makes its entities unavailable).
    """
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _async_add_new() -> None:
        new: list[Entity] = []
        for key, factory in build(coordinator.data):
            if key not in known:
                known.add(key)
                new.append(factory())
        if new:
            async_add_entities(new)

    _async_add_new()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))


class RehomEntity(CoordinatorEntity[RehomCoordinator]):
    """Base of every Rehom entity.

    Subclasses pass the device ``kind`` (hub, plant, zone, VMC), the unit id for
    zones and VMCs, and a ``key`` unique within that device and platform.
    ``_offline_exempt`` keeps the entity available while its unit is offline:
    only the unit's connectivity sensor (availability rule 2).
    """

    _attr_has_entity_name = True
    _offline_exempt: ClassVar[bool] = False

    def __init__(
        self,
        coordinator: RehomCoordinator,
        kind: DeviceKind,
        unit: str | None,
        key: str,
    ) -> None:
        """Set unique_id and device_info from the kind, unit and key."""
        super().__init__(coordinator)
        if kind not in DEVICE_KINDS:
            raise ValueError(f"no Home Assistant device for {kind}")
        if (kind in UNIT_KINDS) != (unit is not None):
            raise ValueError("a unit id is required for zones and VMCs only")
        self._kind = kind
        self._unit = unit
        self._attr_unique_id = entity_unique_id(coordinator.hub_id, kind, unit, key)
        self._attr_device_info = self._build_device_info()

    # -- model accessors ------------------------------------------------------

    @property
    def rehom_state(self) -> RehomState:
        """The latest state (``coordinator.data``)."""
        return self.coordinator.data

    @property
    def plant(self) -> Plant:
        """Whole-house model."""
        return self.coordinator.data.plant

    @property
    def zone(self) -> Zone | None:
        """This entity's zone (``None`` if not a zone entity or the zone is absent)."""
        if self._kind is not DeviceKind.ZONE or self._unit is None:
            return None
        return self.coordinator.data.zones.get(self._unit)

    @property
    def vmc(self) -> Vmc | None:
        """This entity's VMC (``None`` if not a VMC entity or the VMC is absent)."""
        if self._kind is not DeviceKind.VMC or self._unit is None:
            return None
        return self.coordinator.data.vmcs.get(self._unit)

    # -- availability (rules in the module docstring) --------------------------

    @property
    def available(self) -> bool:
        """Rules 1-5: coordinator, unit present/online, serial line."""
        if not super().available:  # rule 1: client UNAVAILABLE
            return False
        if self._kind not in UNIT_KINDS:
            return True  # hub and plant entities: rule 1 only
        state = self.coordinator.data
        unit_model: Zone | Vmc | None = self.zone if self._kind is DeviceKind.ZONE else self.vmc
        if unit_model is None:
            return False  # unit no longer present
        if state.plant.serial_down:
            return False  # rule 5: bus down -> unit entities unavailable
        # rule 2 (online None = unknown -> available, rule 3)
        return unit_model.online is not False or self._offline_exempt

    # -- device info ------------------------------------------------------------------

    def _build_device_info(self) -> DeviceInfo:
        coordinator = self.coordinator
        hub_id = coordinator.hub_id
        state = coordinator.data
        identifiers = {device_identifier(hub_id, self._kind, self._unit)}
        if self._kind is DeviceKind.HUB:
            # Registered with full details in async_setup_entry; link by identifier only.
            return DeviceInfo(identifiers=identifiers)
        via = coordinator.config_entry.runtime_data.hub_device_id
        if self._kind is DeviceKind.PLANT:
            return DeviceInfo(
                identifiers=identifiers,
                translation_key="plant",
                manufacturer=MANUFACTURER,
                model="Multizona",
                sw_version=state.hub.controller_version,
                via_device_id=via,
            )
        if self._kind is DeviceKind.ZONE:
            zone = state.zones.get(self._unit or "")
            name = zone.name if zone is not None else f"Zona {self._unit}"
            return DeviceInfo(
                identifiers=identifiers,
                name=name,
                # No suggested_area: HA 2026.9 builds entity ids as area + device +
                # entity, so an area named like the zone doubles the name.
                manufacturer=MANUFACTURER,
                model="Zone probe",
                sw_version=zone.identity.firmware if zone is not None else None,
                serial_number=zone.identity.serial if zone is not None else None,
                via_device_id=via,
            )
        vmc = state.vmcs.get(self._unit or "")
        return DeviceInfo(
            identifiers=identifiers,
            name=vmc.name if vmc is not None else f"VMC {self._unit}",
            manufacturer=MANUFACTURER,
            model="VMC",
            sw_version=vmc.identity.firmware if vmc is not None else None,
            serial_number=vmc.identity.serial if vmc is not None else None,
            via_device_id=via,
        )
