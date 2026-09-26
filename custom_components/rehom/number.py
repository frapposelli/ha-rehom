"""Zone temperature offset as a number.

Read-only in this version: setting a value raises ``control_disabled``.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import override

from aiorehom import DeviceKind, RehomState
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ZONE_OFFSET_MAX, ZONE_OFFSET_MIN, ZONE_TEMPERATURE_STEP
from .coordinator import RehomConfigEntry, RehomCoordinator
from .entity import (
    EntityFactory,
    RehomEntity,
    async_setup_dynamic_entities,
    entity_unique_id,
    raise_control_disabled,
)

PARALLEL_UPDATES = 1

KEY = "temperature_offset"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RehomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one offset number per zone (and later zones)."""
    coordinator = entry.runtime_data.coordinator
    hub_id = coordinator.hub_id

    def build(state: RehomState) -> Iterable[tuple[str, EntityFactory]]:
        for zone_id in state.zones:
            yield (
                entity_unique_id(hub_id, DeviceKind.ZONE, zone_id, KEY),
                partial(RehomZoneOffsetNumber, coordinator, zone_id),
            )

    async_setup_dynamic_entities(entry, async_add_entities, build)


class RehomZoneOffsetNumber(RehomEntity, NumberEntity):
    """The user's -3..+3 °C correction of a zone (``DELTA_SETP_CORRENTE``).

    ``TEMPERATURE_DELTA``: an offset is a difference of temperatures, so Home
    Assistant converts it as one (+1 °C = +1.8 °F), never like an absolute
    temperature (hence no ``temperature`` device class).
    """

    _attr_translation_key = KEY
    _attr_device_class = NumberDeviceClass.TEMPERATURE_DELTA
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = ZONE_OFFSET_MIN
    _attr_native_max_value = ZONE_OFFSET_MAX
    _attr_native_step = ZONE_TEMPERATURE_STEP
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: RehomCoordinator, zone_id: str) -> None:
        """Zone entity."""
        super().__init__(coordinator, DeviceKind.ZONE, zone_id, KEY)

    @property
    @override
    def native_value(self) -> float | None:
        zone = self.zone
        return None if zone is None else zone.offset

    @override
    async def async_set_native_value(self, value: float) -> None:
        raise_control_disabled()
