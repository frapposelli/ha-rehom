"""Binary sensors. Read-only by nature."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, override

from aiorehom import Availability, ConnectionState, DeviceKind, RehomState, Vmc, Zone
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RehomConfigEntry, RehomCoordinator
from .entity import (
    EntityFactory,
    RehomEntity,
    alarm_attributes,
    async_setup_dynamic_entities,
    device_alarms,
    entity_unique_id,
    house_demand,
)

PARALLEL_UPDATES = 0


def _negated(value: bool | None) -> bool | None:
    """``not value``, keeping ``None`` (unknown) as ``None``."""
    return None if value is None else not value


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


# -- descriptions -----------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class RehomStateBinarySensorDescription(BinarySensorEntityDescription):
    """A hub or plant binary sensor computed from the whole state."""

    value_fn: Callable[[RehomState], bool | None]
    exists_fn: Callable[[RehomState], bool] = lambda state: True
    attrs_fn: Callable[[RehomState], dict[str, Any] | None] = lambda state: None
    #: Extra availability on top of rule 1 (plant values made only of unit data).
    available_fn: Callable[[RehomState], bool] = lambda state: True


@dataclass(frozen=True, kw_only=True)
class RehomZoneBinarySensorDescription(BinarySensorEntityDescription):
    """A zone binary sensor."""

    value_fn: Callable[[Zone], bool | None]
    attrs_fn: Callable[[Zone], dict[str, Any] | None] = lambda zone: None
    offline_exempt: bool = False


@dataclass(frozen=True, kw_only=True)
class RehomVmcBinarySensorDescription(BinarySensorEntityDescription):
    """A VMC binary sensor."""

    value_fn: Callable[[Vmc], bool | None]
    exists_fn: Callable[[Vmc], bool] = lambda vmc: True
    attrs_fn: Callable[[Vmc], dict[str, Any] | None] = lambda vmc: None
    offline_exempt: bool = False


@dataclass(frozen=True, kw_only=True)
class RehomVmcAlarmFlagDescription(BinarySensorEntityDescription):
    """One of the eight debounced VMC alarm flags (``ALLARM_*``)."""

    alarm_key: str


LIVE_UPDATES = BinarySensorEntityDescription(
    key="live_updates",
    translation_key="live_updates",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)

HUB_DESCRIPTIONS: tuple[RehomStateBinarySensorDescription, ...] = (
    RehomStateBinarySensorDescription(
        key="bus",
        translation_key="bus",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: _negated(state.health.bus_ok),
    ),
    RehomStateBinarySensorDescription(
        key="watchdog",
        translation_key="watchdog",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: _negated(state.health.watchdog_ok),
    ),
    RehomStateBinarySensorDescription(
        key="internet",
        translation_key="internet",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.health.internet_ok,
        exists_fn=lambda state: state.health.remote_access_configured,
    ),
    RehomStateBinarySensorDescription(
        key="heartbeat",
        translation_key="heartbeat",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.health.heartbeat_ok,
        attrs_fn=lambda state: {"last_heartbeat": _iso(state.health.heartbeat_at)},
    ),
    RehomStateBinarySensorDescription(
        key="installer_session",
        translation_key="installer_session",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.plant.installer_session,
        exists_fn=lambda state: state.plant.installer_session is not None,
        attrs_fn=lambda state: {"last_activity": _iso(state.plant.installer_activity_at)},
    ),
)

PLANT_DESCRIPTIONS: tuple[RehomStateBinarySensorDescription, ...] = (
    RehomStateBinarySensorDescription(
        key="demand",
        translation_key="demand",
        # Only zone data: offline zones are ignored, and like the zones' own
        # entities it is unavailable while the serial line is down (rule 5).
        value_fn=house_demand,
        available_fn=lambda state: not state.plant.serial_down,
    ),
    RehomStateBinarySensorDescription(
        key="setpoint_mismatch",
        translation_key="setpoint_mismatch",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.plant.setpoint_mismatch,
        attrs_fn=lambda state: {
            "controller_setpoint": state.plant.controller_setpoint,
            "level_temperature": state.plant.display_temperature,
            "since": _iso(state.plant.setpoint_mismatch_since),
        },
    ),
    RehomStateBinarySensorDescription(
        key="read_only",
        translation_key="read_only",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.plant.read_only,
    ),
)

ZONE_DESCRIPTIONS: tuple[RehomZoneBinarySensorDescription, ...] = (
    RehomZoneBinarySensorDescription(
        key="calling",
        translation_key="calling",
        value_fn=lambda zone: zone.calling,
    ),
    RehomZoneBinarySensorDescription(
        key="probe",
        translation_key="probe",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda zone: zone.online,
        offline_exempt=True,
    ),
    RehomZoneBinarySensorDescription(
        key="setpoint_forced",
        translation_key="setpoint_forced",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda zone: zone.forced,
        attrs_fn=lambda zone: {"forced_setpoint": zone.forced_setpoint},
    ),
)

VMC_DESCRIPTIONS: tuple[RehomVmcBinarySensorDescription, ...] = (
    RehomVmcBinarySensorDescription(
        key="dehumidifying",
        translation_key="dehumidifying",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda vmc: vmc.dehumidifying,
    ),
    RehomVmcBinarySensorDescription(
        key="integration",
        translation_key="integration",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda vmc: vmc.integration_active,
        attrs_fn=lambda vmc: {
            "direction": None if vmc.integration is None else vmc.integration.value
        },
    ),
    RehomVmcBinarySensorDescription(
        key="defrost",
        translation_key="defrost",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda vmc: vmc.defrosting,
    ),
    RehomVmcBinarySensorDescription(
        key="schedule_active",
        translation_key="schedule_active",
        value_fn=lambda vmc: vmc.schedule_active,
    ),
    RehomVmcBinarySensorDescription(
        key="compressor",
        translation_key="compressor",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda vmc: vmc.compressor,
    ),
    RehomVmcBinarySensorDescription(
        key="connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda vmc: vmc.online,
        offline_exempt=True,
    ),
    RehomVmcBinarySensorDescription(
        key="free_cooling",
        translation_key="free_cooling",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda vmc: vmc.free_cooling.on,
        exists_fn=lambda vmc: vmc.free_cooling.level is Availability.READ_ONLY,
    ),
)

#: Entity key suffix <- library alarm flag key.
VMC_ALARM_FLAGS: dict[str, str] = {
    "expansion": "ALLARM_ESPANSIONE",
    "high_pressure": "ALLARM_PRESS_ALTA_PRESS",
    "dirty_filter": "ALLARM_PRESSOSTATO_FILTRO",
    "defrost": "ALLARM_SBRINAMENTO",
    "antifreeze_probe": "ALLARM_SONDA_ANTIGELO",
    "condensate_level": "ALLARM_SONDA_LIVELLO",
    "recirculation_probe": "ALLARM_SONDA_RICIRCOLO",
    "fresh_air_probe": "ALLARM_SONDA_RINNOVO",
}

VMC_FLAG_DESCRIPTIONS: tuple[RehomVmcAlarmFlagDescription, ...] = tuple(
    RehomVmcAlarmFlagDescription(
        key=f"alarm_{suffix}",
        translation_key=f"alarm_{suffix}",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        alarm_key=alarm_key,
    )
    for suffix, alarm_key in VMC_ALARM_FLAGS.items()
)

DEVICE_ALARM = BinarySensorEntityDescription(
    key="alarm",
    translation_key="alarm",
    device_class=BinarySensorDeviceClass.PROBLEM,
)


# -- setup ------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RehomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensors that exist on this plant (and later ones)."""
    coordinator = entry.runtime_data.coordinator
    hub_id = coordinator.hub_id

    def build(state: RehomState) -> Iterable[tuple[str, EntityFactory]]:
        yield (
            entity_unique_id(hub_id, DeviceKind.HUB, None, LIVE_UPDATES.key),
            partial(RehomLiveUpdatesBinarySensor, coordinator, LIVE_UPDATES),
        )
        for kind, descriptions in (
            (DeviceKind.HUB, HUB_DESCRIPTIONS),
            (DeviceKind.PLANT, PLANT_DESCRIPTIONS),
        ):
            for description in descriptions:
                if description.exists_fn(state):
                    yield (
                        entity_unique_id(hub_id, kind, None, description.key),
                        partial(RehomStateBinarySensor, coordinator, kind, description),
                    )
        yield (
            entity_unique_id(hub_id, DeviceKind.PLANT, None, DEVICE_ALARM.key),
            partial(RehomAlarmBinarySensor, coordinator, DeviceKind.PLANT, None, DEVICE_ALARM),
        )
        for zone_id in state.zones:
            for zone_description in ZONE_DESCRIPTIONS:
                zone_class = (
                    RehomZoneConnectivityBinarySensor
                    if zone_description.offline_exempt
                    else RehomZoneBinarySensor
                )
                yield (
                    entity_unique_id(hub_id, DeviceKind.ZONE, zone_id, zone_description.key),
                    partial(zone_class, coordinator, zone_id, zone_description),
                )
            yield (
                entity_unique_id(hub_id, DeviceKind.ZONE, zone_id, DEVICE_ALARM.key),
                partial(
                    RehomAlarmBinarySensor, coordinator, DeviceKind.ZONE, zone_id, DEVICE_ALARM
                ),
            )
        for vmc_id, vmc in state.vmcs.items():
            for vmc_description in VMC_DESCRIPTIONS:
                if not vmc_description.exists_fn(vmc):
                    continue
                vmc_class = (
                    RehomVmcConnectivityBinarySensor
                    if vmc_description.offline_exempt
                    else RehomVmcBinarySensor
                )
                yield (
                    entity_unique_id(hub_id, DeviceKind.VMC, vmc_id, vmc_description.key),
                    partial(vmc_class, coordinator, vmc_id, vmc_description),
                )
            for flag_description in VMC_FLAG_DESCRIPTIONS:
                yield (
                    entity_unique_id(hub_id, DeviceKind.VMC, vmc_id, flag_description.key),
                    partial(RehomVmcAlarmFlagBinarySensor, coordinator, vmc_id, flag_description),
                )
            yield (
                entity_unique_id(hub_id, DeviceKind.VMC, vmc_id, DEVICE_ALARM.key),
                partial(RehomAlarmBinarySensor, coordinator, DeviceKind.VMC, vmc_id, DEVICE_ALARM),
            )

    async_setup_dynamic_entities(entry, async_add_entities, build)


# -- entities -----------------------------------------------------------------------


class RehomLiveUpdatesBinarySensor(RehomEntity, BinarySensorEntity):
    """On while the WebSocket is live (CONNECTED); off while DEGRADED (REST fallback)."""

    def __init__(
        self, coordinator: RehomCoordinator, description: BinarySensorEntityDescription
    ) -> None:
        """Hub diagnostic sensor."""
        super().__init__(coordinator, DeviceKind.HUB, None, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool:
        return self.coordinator.client.connection_state is ConnectionState.CONNECTED


class RehomStateBinarySensor(RehomEntity, BinarySensorEntity):
    """A hub or plant binary sensor."""

    entity_description: RehomStateBinarySensorDescription

    def __init__(
        self,
        coordinator: RehomCoordinator,
        kind: DeviceKind,
        description: RehomStateBinarySensorDescription,
    ) -> None:
        """Hub or plant entity (no unit)."""
        super().__init__(coordinator, kind, None, description.key)
        self.entity_description = description

    @property
    @override
    def available(self) -> bool:
        return super().available and self.entity_description.available_fn(self.rehom_state)

    @property
    @override
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.rehom_state)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attrs_fn(self.rehom_state)


class RehomZoneBinarySensor(RehomEntity, BinarySensorEntity):
    """A zone binary sensor."""

    entity_description: RehomZoneBinarySensorDescription

    def __init__(
        self,
        coordinator: RehomCoordinator,
        zone_id: str,
        description: RehomZoneBinarySensorDescription,
    ) -> None:
        """Zone entity."""
        super().__init__(coordinator, DeviceKind.ZONE, zone_id, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool | None:
        zone = self.zone
        return None if zone is None else self.entity_description.value_fn(zone)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        zone = self.zone
        return None if zone is None else self.entity_description.attrs_fn(zone)


class RehomZoneConnectivityBinarySensor(RehomZoneBinarySensor):
    """The zone probe connection: available while the probe is offline."""

    _offline_exempt = True


class RehomVmcBinarySensor(RehomEntity, BinarySensorEntity):
    """A VMC binary sensor."""

    entity_description: RehomVmcBinarySensorDescription

    def __init__(
        self,
        coordinator: RehomCoordinator,
        vmc_id: str,
        description: RehomVmcBinarySensorDescription,
    ) -> None:
        """VMC entity."""
        super().__init__(coordinator, DeviceKind.VMC, vmc_id, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool | None:
        vmc = self.vmc
        return None if vmc is None else self.entity_description.value_fn(vmc)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        vmc = self.vmc
        return None if vmc is None else self.entity_description.attrs_fn(vmc)


class RehomVmcConnectivityBinarySensor(RehomVmcBinarySensor):
    """The VMC bus connection: available while the VMC is offline."""

    _offline_exempt = True


class RehomVmcAlarmFlagBinarySensor(RehomEntity, BinarySensorEntity):
    """A VMC alarm flag, on only once the library has debounced it (60 s)."""

    entity_description: RehomVmcAlarmFlagDescription

    def __init__(
        self,
        coordinator: RehomCoordinator,
        vmc_id: str,
        description: RehomVmcAlarmFlagDescription,
    ) -> None:
        """VMC entity."""
        super().__init__(coordinator, DeviceKind.VMC, vmc_id, description.key)
        self.entity_description = description
        self._alarm_id = f"vmc:{vmc_id}:{description.alarm_key}"

    @property
    @override
    def is_on(self) -> bool | None:
        if any(alarm.id == self._alarm_id for alarm in self.rehom_state.alarms_debounced):
            return True
        vmc = self.vmc
        if vmc is None or vmc.alarm_flags.get(self.entity_description.alarm_key) is None:
            return None
        return False


class RehomAlarmBinarySensor(RehomEntity, BinarySensorEntity):
    """Any debounced alarm of one device (plant, zone or VMC).

    A zone's or VMC's alarm sensor follows the unit rules (unavailable while the
    unit is offline, availability rule 2); a unit that stops responding shows on
    its connectivity sensor and in the plant's "Active alarms".
    """

    def __init__(
        self,
        coordinator: RehomCoordinator,
        kind: DeviceKind,
        unit: str | None,
        description: BinarySensorEntityDescription,
    ) -> None:
        """Device alarm entity."""
        super().__init__(coordinator, kind, unit, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool:
        return bool(device_alarms(self.rehom_state, self._kind, self._unit))

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            "alarms": [
                alarm_attributes(alarm)
                for alarm in device_alarms(self.rehom_state, self._kind, self._unit)
            ]
        }
        if self._kind is DeviceKind.VMC:
            vmc = self.vmc
            attributes["alarm_bitmask"] = None if vmc is None else vmc.alarm_bitmask
        return attributes
