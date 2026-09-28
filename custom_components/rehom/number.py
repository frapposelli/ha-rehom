"""Zone temperature offset as a number.

Setting a value goes through :mod:`.control` (``control_disabled`` unless
"Enable control" is on): the value is rounded to whole degrees exactly like the
zone thermostat's target (:func:`.control.round_offset`: halves round up) and
written as the zone's offset; NaN is refused with ``invalid_value``.  The
number shows the offset the controller confirms, never the requested value.
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

from . import control
from .const import ZONE_OFFSET_MAX, ZONE_OFFSET_MIN, ZONE_TEMPERATURE_STEP
from .coordinator import RehomConfigEntry, RehomCoordinator
from .entity import EntityFactory, RehomEntity, async_setup_dynamic_entities, entity_unique_id

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
    temperature (hence no ``temperature`` device class).  The offset stays
    until it is changed again, across schedule slots.
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
        self._zone_id = zone_id

    @property
    @override
    def native_value(self) -> float | None:
        zone = self.zone
        return None if zone is None else zone.offset

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Write the offset rounded to whole degrees, halves up (control.round_offset).

        Home Assistant has checked -3..+3, but its check lets NaN through:
        ``round_offset`` refuses it (``invalid_value``), after the control check.
        """
        entry = self.coordinator.config_entry
        control.ensure_control_enabled(entry)
        await control.async_set_zone_offset(entry, self._zone_id, control.round_offset(value))
