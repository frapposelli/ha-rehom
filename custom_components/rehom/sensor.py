"""Sensors. Read-only by nature."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, override

from aiorehom import (
    ControlSource,
    DeviceKind,
    Level,
    LockState,
    RehomState,
    Season,
    Vmc,
    VmcMode,
    VmcState,
    Zone,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfRatio, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import DOMAIN
from .coordinator import RehomConfigEntry, RehomCoordinator
from .entity import (
    EntityFactory,
    RehomEntity,
    alarm_attributes,
    async_setup_dynamic_entities,
    entity_unique_id,
)

PARALLEL_UPDATES = 0

type SensorValue = StateType | datetime

#: "Controlled by" options (``Plant.lock``), in a fixed order.
CONTROLLED_BY: dict[LockState, str] = {
    LockState.NORMAL: "web",
    LockState.CRONO: "crono",
    LockState.SERIAL_DOWN: "serial_down",
}


def _lower_name(value: Level | VmcMode | VmcState | None) -> str | None:
    """``IntEnum`` member -> its lower-cased name (the HA option)."""
    return None if value is None else value.name.lower()


def _value(value: Season | ControlSource | None) -> str | None:
    return None if value is None else value.value


def _temporary_comfort_end(zone: Zone) -> datetime | None:
    override_row = zone.override
    if override_row is None or not override_row.applies:
        return None
    return override_row.expires_at


def _active_alarms(state: RehomState) -> dict[str, Any]:
    return {
        "alarms": [
            alarm_attributes(alarm) | {"device": alarm.device.value}
            for alarm in state.alarms_debounced
        ]
    }


# -- descriptions -----------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class RehomStateSensorDescription(SensorEntityDescription):
    """A hub or plant sensor computed from the whole state."""

    value_fn: Callable[[RehomState], SensorValue]
    attrs_fn: Callable[[RehomState], dict[str, Any] | None] = lambda state: None


@dataclass(frozen=True, kw_only=True)
class RehomZoneSensorDescription(SensorEntityDescription):
    """A zone sensor."""

    value_fn: Callable[[Zone], SensorValue]
    exists_fn: Callable[[Zone], bool] = lambda zone: True
    #: Keep creating the entity once it is in the entity registry (late features).
    exists_if_registered: bool = False
    #: Extra availability on top of the unit rules.
    available_fn: Callable[[Zone], bool] = lambda zone: True


@dataclass(frozen=True, kw_only=True)
class RehomVmcSensorDescription(SensorEntityDescription):
    """A VMC sensor."""

    value_fn: Callable[[Vmc], SensorValue]


HUB_DESCRIPTIONS: tuple[RehomStateSensorDescription, ...] = (
    RehomStateSensorDescription(
        key="cpu_temperature",
        translation_key="cpu_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.health.cpu_temperature,
    ),
    RehomStateSensorDescription(
        key="controlled_by",
        translation_key="controlled_by",
        device_class=SensorDeviceClass.ENUM,
        options=list(CONTROLLED_BY.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: CONTROLLED_BY.get(state.plant.lock),
    ),
)


def _level_temperature(
    key: str, value_fn: Callable[[RehomState], SensorValue]
) -> RehomStateSensorDescription:
    """A plant level temperature (diagnostic; a setting, so no state class)."""
    return RehomStateSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=value_fn,
    )


PLANT_DESCRIPTIONS: tuple[RehomStateSensorDescription, ...] = (
    RehomStateSensorDescription(
        key="season",
        translation_key="season",
        device_class=SensorDeviceClass.ENUM,
        options=[season.value for season in (Season.WINTER, Season.SUMMER)],
        value_fn=lambda state: _value(state.plant.season),
    ),
    RehomStateSensorDescription(
        key="controller_setpoint",
        translation_key="controller_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.plant.controller_setpoint,
    ),
    _level_temperature("temperature_off", lambda state: state.plant.temperature_off),
    _level_temperature("temperature_economy", lambda state: state.plant.temperature_economy),
    _level_temperature(
        "temperature_pre_comfort", lambda state: state.plant.temperature_pre_comfort
    ),
    _level_temperature("temperature_comfort", lambda state: state.plant.temperature_comfort),
    RehomStateSensorDescription(
        key="active_alarms",
        translation_key="active_alarms",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: len(state.alarms_debounced),
        attrs_fn=_active_alarms,
    ),
)

ZONE_DESCRIPTIONS: tuple[RehomZoneSensorDescription, ...] = (
    RehomZoneSensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda zone: zone.temperature,
    ),
    RehomZoneSensorDescription(
        key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        value_fn=lambda zone: zone.humidity,
        exists_fn=lambda zone: zone.has_humidity_sensor is True,
        exists_if_registered=True,
        available_fn=lambda zone: zone.has_humidity_sensor is not False,
    ),
    RehomZoneSensorDescription(
        key="level",
        translation_key="level",
        device_class=SensorDeviceClass.ENUM,
        options=[level.name.lower() for level in Level],
        value_fn=lambda zone: _lower_name(zone.level),
    ),
    RehomZoneSensorDescription(
        key="control_source",
        translation_key="control_source",
        device_class=SensorDeviceClass.ENUM,
        options=[source.value for source in ControlSource],
        value_fn=lambda zone: _value(zone.control_source),
    ),
    RehomZoneSensorDescription(
        key="temporary_comfort_end",
        translation_key="temporary_comfort_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_temporary_comfort_end,
    ),
    RehomZoneSensorDescription(
        key="next_change",
        translation_key="next_change",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda zone: zone.next_change_at,
    ),
)

VMC_DESCRIPTIONS: tuple[RehomVmcSensorDescription, ...] = (
    RehomVmcSensorDescription(
        key="effective_mode",
        translation_key="effective_mode",
        device_class=SensorDeviceClass.ENUM,
        options=[mode.name.lower() for mode in VmcMode],
        value_fn=lambda vmc: _lower_name(vmc.effective_mode),
    ),
    RehomVmcSensorDescription(
        key="vmc_state",
        translation_key="vmc_state",
        device_class=SensorDeviceClass.ENUM,
        options=[vmc_state.name.lower() for vmc_state in VmcState],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda vmc: _lower_name(vmc.state),
    ),
    RehomVmcSensorDescription(
        key="duty_cycle",
        translation_key="duty_cycle",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda vmc: vmc.duty_cycle,
    ),
    RehomVmcSensorDescription(
        key="inlet_air_temperature",
        translation_key="inlet_air_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda vmc: vmc.inlet_air_temperature,
    ),
    RehomVmcSensorDescription(
        key="renewal_position",
        translation_key="renewal_position",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda vmc: vmc.renewal_position,
    ),
)


# -- setup ------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RehomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors that exist on this plant (and later ones)."""
    coordinator = entry.runtime_data.coordinator
    hub_id = coordinator.hub_id
    registry = er.async_get(hass)

    def build(state: RehomState) -> Iterable[tuple[str, EntityFactory]]:
        for kind, descriptions in (
            (DeviceKind.HUB, HUB_DESCRIPTIONS),
            (DeviceKind.PLANT, PLANT_DESCRIPTIONS),
        ):
            for description in descriptions:
                yield (
                    entity_unique_id(hub_id, kind, None, description.key),
                    partial(RehomStateSensor, coordinator, kind, description),
                )
        for zone_id, zone in state.zones.items():
            for zone_description in ZONE_DESCRIPTIONS:
                unique_id = entity_unique_id(hub_id, DeviceKind.ZONE, zone_id, zone_description.key)
                if zone_description.exists_fn(zone) or (
                    zone_description.exists_if_registered
                    and registry.async_get_entity_id("sensor", DOMAIN, unique_id) is not None
                ):
                    yield (
                        unique_id,
                        partial(RehomZoneSensor, coordinator, zone_id, zone_description),
                    )
        for vmc_id in state.vmcs:
            for vmc_description in VMC_DESCRIPTIONS:
                yield (
                    entity_unique_id(hub_id, DeviceKind.VMC, vmc_id, vmc_description.key),
                    partial(RehomVmcSensor, coordinator, vmc_id, vmc_description),
                )

    async_setup_dynamic_entities(entry, async_add_entities, build)


# -- entities -----------------------------------------------------------------------


class RehomSensor(RehomEntity, SensorEntity):
    """Common value handling: enum sensors never report a value outside ``options``."""

    def _checked(self, value: SensorValue) -> SensorValue:
        options = self.entity_description.options
        if self.entity_description.device_class is SensorDeviceClass.ENUM and (
            options is None or value not in options
        ):
            return None
        return value


class RehomStateSensor(RehomSensor):
    """A hub or plant sensor."""

    entity_description: RehomStateSensorDescription

    def __init__(
        self,
        coordinator: RehomCoordinator,
        kind: DeviceKind,
        description: RehomStateSensorDescription,
    ) -> None:
        """Hub or plant entity (no unit)."""
        super().__init__(coordinator, kind, None, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> SensorValue:
        return self._checked(self.entity_description.value_fn(self.rehom_state))

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attrs_fn(self.rehom_state)


class RehomZoneSensor(RehomSensor):
    """A zone sensor."""

    entity_description: RehomZoneSensorDescription

    def __init__(
        self,
        coordinator: RehomCoordinator,
        zone_id: str,
        description: RehomZoneSensorDescription,
    ) -> None:
        """Zone entity."""
        super().__init__(coordinator, DeviceKind.ZONE, zone_id, description.key)
        self.entity_description = description

    @property
    @override
    def available(self) -> bool:
        if not super().available:
            return False
        zone = self.zone
        return zone is not None and self.entity_description.available_fn(zone)

    @property
    @override
    def native_value(self) -> SensorValue:
        zone = self.zone
        return None if zone is None else self._checked(self.entity_description.value_fn(zone))


class RehomVmcSensor(RehomSensor):
    """A VMC sensor."""

    entity_description: RehomVmcSensorDescription

    def __init__(
        self,
        coordinator: RehomCoordinator,
        vmc_id: str,
        description: RehomVmcSensorDescription,
    ) -> None:
        """VMC entity."""
        super().__init__(coordinator, DeviceKind.VMC, vmc_id, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> SensorValue:
        vmc = self.vmc
        return None if vmc is None else self._checked(self.entity_description.value_fn(vmc))
