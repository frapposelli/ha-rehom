"""VMC operating mode as a select.

The options are the modes the installer made selectable, without the rapid
modes (timed cycles Home Assistant never starts), plus the current mode so the
state is never unknown.  Every select action ends in ``async_select_option``,
which writes the mode through :mod:`.control` (``control_disabled`` unless
"Enable control" is on; a mode never tested on a real controller is refused
with ``not_verified``).
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import override

from aiorehom import DeviceKind, RehomState, VmcMode
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import control
from .const import VMC_RAPID_MODES
from .coordinator import RehomConfigEntry, RehomCoordinator
from .entity import EntityFactory, RehomEntity, async_setup_dynamic_entities, entity_unique_id

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
    """The VMC's selected mode (``ST_MODE``), in the order of :class:`~aiorehom.VmcMode`."""

    _attr_translation_key = KEY

    def __init__(self, coordinator: RehomCoordinator, vmc_id: str) -> None:
        """VMC entity."""
        super().__init__(coordinator, DeviceKind.VMC, vmc_id, KEY)
        self._vmc_id = vmc_id

    @property
    @override
    def options(self) -> list[str]:
        vmc = self.vmc
        if vmc is None:
            return []
        modes = set(vmc.selectable_modes) - VMC_RAPID_MODES
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
        """Write the mode (Home Assistant has checked that ``option`` is listed)."""
        await control.async_set_vmc_mode(
            self.coordinator.config_entry, self._vmc_id, VmcMode[option.upper()]
        )
