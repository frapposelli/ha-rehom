"""VMC operating mode as a select.

Read-only in this version: every select action ends in ``async_select_option``, which
raises ``control_disabled``.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import override

from aiorehom import DeviceKind, RehomState, VmcMode
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RehomConfigEntry, RehomCoordinator
from .entity import (
    EntityFactory,
    RehomEntity,
    async_setup_dynamic_entities,
    entity_unique_id,
    raise_control_disabled,
)

PARALLEL_UPDATES = 1

KEY = "mode"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RehomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one mode select per VMC (and later VMCs)."""
    coordinator = entry.runtime_data.coordinator
    hub_id = coordinator.hub_id

    def build(state: RehomState) -> Iterable[tuple[str, EntityFactory]]:
        for vmc_id in state.vmcs:
            yield (
                entity_unique_id(hub_id, DeviceKind.VMC, vmc_id, KEY),
                partial(RehomVmcModeSelect, coordinator, vmc_id),
            )

    async_setup_dynamic_entities(entry, async_add_entities, build)


class RehomVmcModeSelect(RehomEntity, SelectEntity):
    """The VMC's selected mode (``ST_MODE``)."""

    _attr_translation_key = KEY

    def __init__(self, coordinator: RehomCoordinator, vmc_id: str) -> None:
        """VMC entity."""
        super().__init__(coordinator, DeviceKind.VMC, vmc_id, KEY)

    @property
    @override
    def options(self) -> list[str]:
        vmc = self.vmc
        if vmc is None:
            return []
        modes = set(vmc.selectable_modes)
        if vmc.mode is not None:
            modes.add(vmc.mode)  # a mode set elsewhere is shown even if not selectable
        return [mode.name.lower() for mode in VmcMode if mode in modes]

    @property
    @override
    def current_option(self) -> str | None:
        vmc = self.vmc
        if vmc is None or vmc.mode is None:
            return None
        return vmc.mode.name.lower()

    @override
    async def async_select_option(self, option: str) -> None:
        raise_control_disabled()
