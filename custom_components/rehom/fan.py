"""VMC ventilation as a fan.

Read-only in this version: turning on/off and setting the speed raise ``control_disabled``
(toggle, increase_speed and decrease_speed route to those methods).
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import Any, override

from aiorehom import Availability, DeviceKind, FanKind, FanSpeed, RehomState, VmcMode
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    ranged_value_to_percentage,
)

from .coordinator import RehomConfigEntry, RehomCoordinator
from .entity import (
    EntityFactory,
    RehomEntity,
    async_setup_dynamic_entities,
    entity_unique_id,
    raise_control_disabled,
)

PARALLEL_UPDATES = 1

KEY = "fan"

#: Discrete speeds, slowest first (``NONE`` is 0 %).
ORDERED_SPEEDS: list[FanSpeed] = [FanSpeed.ATTENUATED, FanSpeed.MIN, FanSpeed.MED, FanSpeed.MAX]
#: Operating modes in which the VMC is off.
OFF_MODES = frozenset({VmcMode.STOP, VmcMode.STANDBY})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RehomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one fan per VMC (and later VMCs)."""
    coordinator = entry.runtime_data.coordinator
    hub_id = coordinator.hub_id

    def build(state: RehomState) -> Iterable[tuple[str, EntityFactory]]:
        for vmc_id in state.vmcs:
            yield (
                entity_unique_id(hub_id, DeviceKind.VMC, vmc_id, KEY),
                partial(RehomVmcFan, coordinator, vmc_id),
            )

    async_setup_dynamic_entities(entry, async_add_entities, build)


class RehomVmcFan(RehomEntity, FanEntity):
    """The VMC fan; its operating mode is the ``mode`` select, so no presets."""

    _attr_name = None
    _attr_translation_key = "ventilation"

    def __init__(self, coordinator: RehomCoordinator, vmc_id: str) -> None:
        """VMC entity."""
        super().__init__(coordinator, DeviceKind.VMC, vmc_id, KEY)

    @property
    @override
    def supported_features(self) -> FanEntityFeature:
        vmc = self.vmc
        features = FanEntityFeature(0)
        if vmc is None:
            return features
        if vmc.fan.control is Availability.WRITABLE:
            features |= FanEntityFeature.SET_SPEED
        if vmc.mode_availability.get(VmcMode.STOP) is Availability.WRITABLE:
            features |= FanEntityFeature.TURN_OFF
        if any(mode is not VmcMode.STOP for mode in vmc.selectable_modes):
            features |= FanEntityFeature.TURN_ON
        return features

    @property
    @override
    def speed_count(self) -> int:
        vmc = self.vmc
        if vmc is None or vmc.fan.kind is FanKind.DISCRETE:
            return len(ORDERED_SPEEDS)
        return max(1, vmc.fan.step_max - vmc.fan.step_min)

    @property
    @override
    def percentage(self) -> int | None:
        vmc = self.vmc
        if vmc is None:
            return None
        fan = vmc.fan
        if fan.kind is FanKind.DISCRETE:
            if fan.speed is None:
                return None
            if fan.speed is FanSpeed.NONE:
                return 0
            return ordered_list_item_to_percentage(ORDERED_SPEEDS, fan.speed)
        if fan.value is None:
            return None
        if fan.value <= fan.step_min:
            return 0
        return ranged_value_to_percentage((fan.step_min + 1, fan.step_max), fan.value)

    @property
    @override
    def is_on(self) -> bool | None:
        vmc = self.vmc
        if vmc is None or vmc.effective_mode is None:
            return None
        return vmc.effective_mode not in OFF_MODES

    # -- control: refused (read-only) ----------------------------------------------------

    @override
    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        raise_control_disabled()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        raise_control_disabled()

    @override
    async def async_set_percentage(self, percentage: int) -> None:
        raise_control_disabled()
