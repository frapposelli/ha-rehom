"""VMC ventilation as a fan.

Every fan action goes through :mod:`.control` (``control_disabled`` unless
"Enable control" is on; toggle, increase_speed and decrease_speed route to the
methods below):

- **Speed**: a discrete fan's percentage picks one of its four speeds
  (attenuated 25 %, min 50 %, med 75 %, max 100 %); only speeds tested on a real
  controller are sent (``control.VERIFIED_VALUES``: min, med and max), so
  attenuated and continuous fans are refused with ``not_verified``.  The
  controller fixes the speed while the VMC is off.
- **Turn on** (while off, from stop or standby): the operating mode the VMC last
  ran in, as seen since this entity was created (Home Assistant start or
  integration reload; it is not restored), or else the first selectable mode
  that is verified and neither off nor rapid; then the speed, if one is given.
  A last mode that was never verified is refused (``not_verified``), never
  replaced by another.  While on, only the speed.
- **Turn off**, and 0 %: the stop mode, refused with ``not_verified`` for now.

The fan shows what the controller confirms, never the requested value.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import Any, override

from aiorehom import Availability, DeviceKind, FanKind, FanSpeed, RehomState, Vmc, VmcMode
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from . import control
from .const import VMC_RAPID_MODES
from .control import ControlOp
from .coordinator import RehomConfigEntry, RehomCoordinator
from .entity import EntityFactory, RehomEntity, async_setup_dynamic_entities, entity_unique_id

PARALLEL_UPDATES = 1

KEY = "fan"

#: Discrete speeds, slowest first (``NONE`` is 0 %).
ORDERED_SPEEDS: list[FanSpeed] = [FanSpeed.ATTENUATED, FanSpeed.MIN, FanSpeed.MED, FanSpeed.MAX]
#: Operating modes in which the VMC is off.
OFF_MODES = frozenset({VmcMode.STOP, VmcMode.STANDBY})


def is_on_mode(mode: VmcMode | None) -> bool:
    """Whether turning the fan on may select ``mode``: on, and not a rapid (timed) mode."""
    return mode is not None and mode not in OFF_MODES and mode not in VMC_RAPID_MODES


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
        self._vmc_id = vmc_id
        #: The last mode seen while on (:func:`is_on_mode`), for turning on again.
        self._last_on_mode: VmcMode | None = None
        self._remember_on_mode()

    def _remember_on_mode(self) -> None:
        vmc = self.vmc
        if vmc is not None and is_on_mode(vmc.mode):
            self._last_on_mode = vmc.mode

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        self._remember_on_mode()
        super()._handle_coordinator_update()

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
        if any(is_on_mode(mode) for mode in vmc.selectable_modes):
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

    # -- control ------------------------------------------------------------------------

    def _present_vmc(self) -> Vmc:
        """This entity's VMC (Home Assistant calls no action while it is absent)."""
        vmc = self.vmc
        if vmc is None:
            control.raise_refused("unknown_vmc")
        return vmc

    @staticmethod
    def _fan_value(vmc: Vmc, percentage: int) -> int:
        """The fan value for ``percentage`` (> 0): a speed, or a continuous fan's step."""
        fan = vmc.fan
        if fan.kind is FanKind.DISCRETE:
            return percentage_to_ordered_list_item(ORDERED_SPEEDS, percentage)
        return round(percentage_to_ranged_value((fan.step_min + 1, fan.step_max), percentage))

    def _turn_on_mode(self, vmc: Vmc) -> VmcMode | None:
        """The last on-mode if still selectable, else the first selectable verified on-mode.

        ``None`` when no on-mode is selectable (``mode_not_available``).  When
        on-modes are selectable but none is verified, the first of them, which
        control refuses (``not_verified``).
        """
        selectable = vmc.selectable_modes
        if self._last_on_mode in selectable:
            return self._last_on_mode
        on_modes = [mode for mode in selectable if is_on_mode(mode)]
        verified = (mode for mode in on_modes if control.is_verified(ControlOp.VMC_MODE, mode))
        return next(verified, on_modes[0] if on_modes else None)

    @override
    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """While off: the operating mode, then the speed if given.  While on: the speed."""
        entry = self.coordinator.config_entry
        control.ensure_control_enabled(entry)
        if percentage == 0:
            await self.async_turn_off()
            return
        if self.is_on:
            if percentage is not None:
                await self.async_set_percentage(percentage)
            return
        vmc = self._present_vmc()
        if (mode := self._turn_on_mode(vmc)) is None:
            control.raise_refused("mode_not_available")
        value = None if percentage is None else self._fan_value(vmc, percentage)
        if value is not None:  # refuse the speed before the mode is sent: all or nothing
            if not (
                vmc.fan.kind is FanKind.DISCRETE and control.is_verified(ControlOp.VMC_FAN, value)
            ):
                control.raise_not_verified()
            if vmc.fan.control is not Availability.WRITABLE:
                control.raise_refused("fan_not_writable")
        await control.async_set_vmc_mode(entry, self._vmc_id, mode)
        if value is not None:
            await control.async_set_vmc_fan(entry, self._vmc_id, value)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """The stop mode (``not_verified`` until it is tested on a real controller)."""
        await control.async_set_vmc_mode(self.coordinator.config_entry, self._vmc_id, VmcMode.STOP)

    @override
    async def async_set_percentage(self, percentage: int) -> None:
        """0 % turns off; any other percentage sets the matching speed."""
        if percentage == 0:
            await self.async_turn_off()
            return
        entry = self.coordinator.config_entry
        control.ensure_control_enabled(entry)
        vmc = self._present_vmc()
        await control.async_set_vmc_fan(entry, self._vmc_id, self._fan_value(vmc, percentage))
