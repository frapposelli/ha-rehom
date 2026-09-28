"""Switches: predictive algorithm and VMC free cooling.

Turning a switch on or off goes through :mod:`.control` (``control_disabled``
unless "Enable control" is on; toggle routes to the same methods).  The
predictive algorithm is written and shown once the controller confirms it
(it needs the house in AUTO).  Free cooling cannot be set from Home
Assistant: it is refused with ``not_supported``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any, override

from aiorehom import Availability, DeviceKind, Plant, RehomState, Vmc
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import control
from .coordinator import RehomConfigEntry, RehomCoordinator
from .entity import EntityFactory, RehomEntity, async_setup_dynamic_entities, entity_unique_id

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class RehomPlantSwitchDescription(SwitchEntityDescription):
    """A plant switch; ``set_fn`` is the :mod:`.control` function that writes it."""

    value_fn: Callable[[Plant], bool | None]
    set_fn: Callable[[RehomConfigEntry, bool], Awaitable[bool]]
    exists_fn: Callable[[Plant], bool] = lambda plant: True


@dataclass(frozen=True, kw_only=True)
class RehomVmcSwitchDescription(SwitchEntityDescription):
    """A VMC switch (none can be set from Home Assistant)."""

    value_fn: Callable[[Vmc], bool | None]
    exists_fn: Callable[[Vmc], bool] = lambda vmc: True
    attrs_fn: Callable[[Vmc], dict[str, Any] | None] = lambda vmc: None


PLANT_DESCRIPTIONS: tuple[RehomPlantSwitchDescription, ...] = (
    RehomPlantSwitchDescription(
        key="predictive",
        translation_key="predictive",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda plant: plant.predictive,
        set_fn=control.async_set_predictive,  # needs the house in AUTO
        exists_fn=lambda plant: plant.predictive is not None,
    ),
)

VMC_DESCRIPTIONS: tuple[RehomVmcSwitchDescription, ...] = (
    RehomVmcSwitchDescription(
        # Created only where free cooling is settable on the controller, but Home
        # Assistant has no way to set it: on and off are refused (not_supported).
        key="free_cooling",
        translation_key="free_cooling",
        value_fn=lambda vmc: vmc.free_cooling.on,
        exists_fn=lambda vmc: vmc.free_cooling.level is Availability.WRITABLE,
        attrs_fn=lambda vmc: {"error": vmc.free_cooling.error},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RehomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the switches that exist on this plant (and later ones)."""
    coordinator = entry.runtime_data.coordinator
    hub_id = coordinator.hub_id

    def build(state: RehomState) -> Iterable[tuple[str, EntityFactory]]:
        for description in PLANT_DESCRIPTIONS:
            if description.exists_fn(state.plant):
                yield (
                    entity_unique_id(hub_id, DeviceKind.PLANT, None, description.key),
                    partial(RehomPlantSwitch, coordinator, description),
                )
        for vmc_id, vmc in state.vmcs.items():
            for vmc_description in VMC_DESCRIPTIONS:
                if vmc_description.exists_fn(vmc):
                    yield (
                        entity_unique_id(hub_id, DeviceKind.VMC, vmc_id, vmc_description.key),
                        partial(RehomVmcSwitch, coordinator, vmc_id, vmc_description),
                    )

    async_setup_dynamic_entities(entry, async_add_entities, build)


class RehomSwitch(RehomEntity, SwitchEntity):
    """Common behaviour: on and off end in :meth:`_async_set`."""

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        """Refused: ``control_disabled`` while control is off, else ``not_supported``."""
        control.ensure_control_enabled(self.coordinator.config_entry)
        control.raise_not_supported()


class RehomPlantSwitch(RehomSwitch):
    """A plant setting shown as a switch."""

    entity_description: RehomPlantSwitchDescription

    def __init__(
        self, coordinator: RehomCoordinator, description: RehomPlantSwitchDescription
    ) -> None:
        """Plant entity."""
        super().__init__(coordinator, DeviceKind.PLANT, None, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.plant)

    @override
    async def _async_set(self, on: bool) -> None:
        """Write the setting (the control function runs every check)."""
        await self.entity_description.set_fn(self.coordinator.config_entry, on)


class RehomVmcSwitch(RehomSwitch):
    """A VMC setting shown as a switch."""

    entity_description: RehomVmcSwitchDescription

    def __init__(
        self,
        coordinator: RehomCoordinator,
        vmc_id: str,
        description: RehomVmcSwitchDescription,
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
