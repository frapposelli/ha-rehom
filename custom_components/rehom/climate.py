"""Climate entities: the house and one per zone.

Control goes through :mod:`.control`, which refuses everything with
``control_disabled`` unless "Enable control" is on.  The entities are the same
either way; only the actions change.

- ``auto`` (and preset ``none``) puts the house in AUTO, or a zone back on its
  schedule; ``turn_on`` does the same, but only while the thermostat is off.
  Preset ``none`` on a thermostat that is off changes nothing: it is the
  preset shown while off (``turn_on`` and ``auto`` switch it on).
- The season mode (``heat`` in winter, ``cool`` in summer) re-applies the last
  manual level seen on this thermostat (restored across restarts; comfort
  until one is seen).  Presets select a level directly.
- A zone forced off by its probe stays off: ``turn_on``, ``auto``, the season
  mode and the presets are refused with ``not_verified`` and nothing is sent.
- A zone's target temperature sets its offset from the level temperature,
  in whole degrees within -3..+3 (halves round up).  A target outside the
  level temperature ±3 °C is refused (``target_out_of_range``), never clamped,
  and a target that is not a number (NaN) is refused (``invalid_value``).  The
  house target (the comfort temperature) cannot be changed from Home Assistant.
- Nothing can switch the house or a zone off: there is no ``turn_off`` feature,
  and ``off`` is listed only while the thermostat is already off.
- Temporary comfort is not available: the preset and the ``rehom`` actions
  are refused.

Only the values tested on a real controller are sent (``control.VERIFIED_VALUES``);
the others are refused with ``not_verified``.  States are never optimistic: a
successful action returns once the controller has reported the change.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Mapping
from functools import partial
from typing import Any, Final, NoReturn, override

from aiorehom import (
    DeviceKind,
    HvacAction,
    MasterPreset,
    Program,
    RehomState,
    Season,
    SeasonSchedule,
    ZoneMode,
    ZonePreset,
    ZoneSetp,
)
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.core import HomeAssistant, ServiceResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoredExtraData, RestoreEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType, JsonValueType

from . import control
from .const import (
    DOMAIN,
    EXC_NOT_READY,
    EXC_ZONE_ONLY,
    HOUSE_TEMPERATURE_RANGE,
    HOUSE_TEMPERATURE_RANGE_UNKNOWN,
    HOUSE_TEMPERATURE_STEP,
    PRESET_PRE_COMFORT,
    PRESET_TEMPORARY_COMFORT,
    ZONE_OFFSET_MAX,
    ZONE_OFFSET_MIN,
    ZONE_TEMPERATURE_STEP,
)
from .coordinator import RehomConfigEntry, RehomCoordinator
from .entity import (
    EntityFactory,
    RehomEntity,
    async_setup_dynamic_entities,
    entity_unique_id,
    house_demand,
    house_temperature,
)

PARALLEL_UPDATES = 1

KEY = "climate"

SEASON_MODE: dict[Season, HVACMode] = {Season.WINTER: HVACMode.HEAT, Season.SUMMER: HVACMode.COOL}
DEMAND_ACTION: dict[Season, HVACAction] = {
    Season.WINTER: HVACAction.HEATING,
    Season.SUMMER: HVACAction.COOLING,
}

HOUSE_PRESETS: dict[MasterPreset, str] = {
    MasterPreset.OFF: PRESET_NONE,
    MasterPreset.AUTO: PRESET_NONE,
    MasterPreset.ECONOMY: PRESET_ECO,
    MasterPreset.PRE_COMFORT: PRESET_PRE_COMFORT,
    MasterPreset.COMFORT: PRESET_COMFORT,
}
#: The manual levels as Home Assistant presets (what heat/cool re-applies).
MANUAL_PRESETS: tuple[str, ...] = (PRESET_ECO, PRESET_PRE_COMFORT, PRESET_COMFORT)
#: Level preset -> house preset written (MANUAL at that level).
HOUSE_LEVELS: Mapping[str, MasterPreset] = {
    PRESET_ECO: MasterPreset.ECONOMY,
    PRESET_PRE_COMFORT: MasterPreset.PRE_COMFORT,
    PRESET_COMFORT: MasterPreset.COMFORT,
}
#: House presets that select a fixed level (MANUAL): the season mode and a target.
HOUSE_LEVEL_PRESETS = frozenset(HOUSE_LEVELS.values())

ZONE_PRESETS: dict[ZonePreset, str] = {
    ZonePreset.NONE: PRESET_NONE,
    ZonePreset.ECONOMY: PRESET_ECO,
    ZonePreset.PRE_COMFORT: PRESET_PRE_COMFORT,
    ZonePreset.COMFORT: PRESET_COMFORT,
    ZonePreset.TEMPORARY_COMFORT: PRESET_TEMPORARY_COMFORT,
}
#: Level preset -> the zone's own mode written (a fixed level).
ZONE_LEVELS: Mapping[str, ZoneSetp] = {
    PRESET_ECO: ZoneSetp.ECONOMY,
    PRESET_PRE_COMFORT: ZoneSetp.PRE_COMFORT,
    PRESET_COMFORT: ZoneSetp.COMFORT,
}
ZONE_LEVEL_PRESETS: Mapping[ZoneSetp, str] = {setp: preset for preset, setp in ZONE_LEVELS.items()}

HVAC_ACTIONS: dict[HvacAction, HVACAction] = {
    HvacAction.OFF: HVACAction.OFF,
    HvacAction.IDLE: HVACAction.IDLE,
    HvacAction.HEATING: HVACAction.HEATING,
    HvacAction.COOLING: HVACAction.COOLING,
}

#: Controller weekday convention: 0 = Sunday.
WEEKDAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")

#: Restored data: the last manual level (a preset of MANUAL_PRESETS).
ATTR_LAST_MANUAL: Final = "last_manual"
#: Decimals of a zone's min/max temperature (the level temperatures are in 0.1 °C steps).
ZONE_RANGE_DECIMALS: Final = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RehomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the house climate and one climate per zone (and later zones)."""
    coordinator = entry.runtime_data.coordinator
    hub_id = coordinator.hub_id

    def build(state: RehomState) -> Iterable[tuple[str, EntityFactory]]:
        yield (
            entity_unique_id(hub_id, DeviceKind.PLANT, None, KEY),
            partial(RehomHouseClimate, coordinator),
        )
        for zone_id in state.zones:
            yield (
                entity_unique_id(hub_id, DeviceKind.ZONE, zone_id, KEY),
                partial(RehomZoneClimate, coordinator, zone_id),
            )

    async_setup_dynamic_entities(entry, async_add_entities, build)


def _levels(program: Program | None) -> JsonValueType:
    if program is None:
        return None
    return [None if level is None else level.name.lower() for level in program]


def _season_schedule(schedule: SeasonSchedule) -> JsonObjectType:
    days: list[JsonValueType] = [
        {
            "weekday": day.weekday,
            "day": WEEKDAYS[day.weekday],
            "preset": day.preset,
            "levels": _levels(day.levels),
        }
        for day in schedule.days
    ]
    return {"source": schedule.source.value, "crono": _levels(schedule.crono), "days": days}


class RehomClimate(RehomEntity, ClimateEntity, RestoreEntity):
    """Common behaviour of the house and zone climates.

    Subclasses write AUTO (:meth:`_async_set_auto`) and a manual level
    (:meth:`_async_set_level`), and report the manual level they show
    (:meth:`_manual_level`), which is remembered for ``heat``/``cool``.
    Every action first checks that control is enabled; every action that
    would write a mode then checks :meth:`_ensure_not_forced_off`.
    """

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_TENTHS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
    )
    #: The last manual level seen (a MANUAL_PRESETS preset); comfort until one is seen.
    _last_manual: str = PRESET_COMFORT

    @property
    def _entry(self) -> RehomConfigEntry:
        return self.coordinator.config_entry

    @property
    def _season_mode(self) -> HVACMode | None:
        season = self.plant.season
        return None if season is None else SEASON_MODE[season]

    @property
    @override
    def hvac_modes(self) -> list[HVACMode]:
        # "off" cannot be selected: it is listed only while the thermostat is off,
        # so that the state is always one of the modes.
        season_mode = self._season_mode
        return (
            ([HVACMode.OFF] if self.hvac_mode is HVACMode.OFF else [])
            + [HVACMode.AUTO]
            + ([season_mode] if season_mode else [])
        )

    # -- the last manual level (restored) ---------------------------------------------

    @abstractmethod
    def _manual_level(self) -> str | None:
        """The manual level shown now (a MANUAL_PRESETS preset), if any."""
        raise NotImplementedError

    def _remember_manual_level(self) -> None:
        if (level := self._manual_level()) is not None:
            self._last_manual = level

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_extra_data()) is not None:
            restored = last.as_dict().get(ATTR_LAST_MANUAL)
            if restored in MANUAL_PRESETS:
                self._last_manual = restored
        self._remember_manual_level()  # what the controller shows now wins

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        self._remember_manual_level()
        super()._handle_coordinator_update()

    @property
    @override
    def extra_restore_state_data(self) -> RestoredExtraData:
        return RestoredExtraData({ATTR_LAST_MANUAL: self._last_manual})

    # -- control ------------------------------------------------------------------------

    @abstractmethod
    async def _async_set_auto(self) -> None:
        """The house in AUTO, or the zone on its schedule."""
        raise NotImplementedError

    @abstractmethod
    async def _async_set_level(self, preset: str) -> None:
        """A manual level (``preset`` in MANUAL_PRESETS)."""
        raise NotImplementedError

    def _ensure_not_forced_off(self) -> None:
        """Refuse to change the mode of a thermostat the controller itself switched off.

        Raises ``not_verified`` (nothing is sent).  The house never is: a house
        switched off in the official app is switched on again by ``turn_on``
        and ``auto``.  A zone is while its probe forces it off (see the zone).
        """

    @override
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        control.ensure_control_enabled(self._entry)
        if hvac_mode == HVACMode.AUTO:
            self._ensure_not_forced_off()
            await self._async_set_auto()
        elif hvac_mode != HVACMode.OFF and hvac_mode == self._season_mode:
            self._ensure_not_forced_off()
            await self._async_set_level(self._last_manual)
        else:  # off: the controller cannot be switched off from Home Assistant
            control.raise_not_supported()

    @override
    async def async_set_preset_mode(self, preset_mode: str) -> None:
        control.ensure_control_enabled(self._entry)
        if preset_mode == PRESET_TEMPORARY_COMFORT:
            control.raise_temporary_comfort_unavailable()
        self._ensure_not_forced_off()
        if preset_mode != PRESET_NONE:
            await self._async_set_level(preset_mode)
        elif self.hvac_mode is not HVACMode.OFF:
            await self._async_set_auto()
        # else: "none" is the preset shown while off, so there is nothing to change
        # (turn_on and "auto" are the ways out of off)

    @override
    async def async_turn_on(self) -> None:
        """Back to auto, only while off (a thermostat that is on is left as it is)."""
        control.ensure_control_enabled(self._entry)
        if self.hvac_mode is HVACMode.OFF:
            self._ensure_not_forced_off()
            await self._async_set_auto()

    @override
    async def async_turn_off(self) -> None:
        """Not offered (no TURN_OFF feature); reached through ``toggle``."""
        control.ensure_control_enabled(self._entry)
        control.raise_not_supported()

    async def async_set_temporary_comfort(self, duration: float | None = None) -> None:
        """``rehom.set_temporary_comfort``: not available yet."""
        control.ensure_control_enabled(self._entry)
        self._refuse_temporary_comfort()

    async def async_clear_temporary_comfort(self) -> None:
        """``rehom.clear_temporary_comfort``: not available yet."""
        control.ensure_control_enabled(self._entry)
        self._refuse_temporary_comfort()

    def _refuse_temporary_comfort(self) -> NoReturn:
        control.raise_temporary_comfort_unavailable()


class RehomHouseClimate(RehomClimate):
    """The whole house (``REHOM`` MODO / SET_POINT), on the plant device."""

    _attr_translation_key = "house"
    _attr_target_temperature_step = HOUSE_TEMPERATURE_STEP

    def __init__(self, coordinator: RehomCoordinator) -> None:
        """Plant entity."""
        super().__init__(coordinator, DeviceKind.PLANT, None, KEY)
        self._attr_preset_modes = [PRESET_NONE, PRESET_ECO, PRESET_PRE_COMFORT, PRESET_COMFORT]

    @property
    @override
    def hvac_mode(self) -> HVACMode | None:
        preset = self.plant.preset
        if preset is None:
            return None
        if preset is MasterPreset.OFF:
            return HVACMode.OFF
        if preset is MasterPreset.AUTO:
            return HVACMode.AUTO
        return self._season_mode

    @property
    @override
    def preset_mode(self) -> str | None:
        preset = self.plant.preset
        return None if preset is None else HOUSE_PRESETS[preset]

    @property
    @override
    def hvac_action(self) -> HVACAction | None:
        # Zone data under the unit rules: offline zones ignored, unknown while
        # the serial line is down (the zones' own entities are unavailable).
        demand = house_demand(self.rehom_state)
        plant = self.plant
        if demand is None:
            return None
        if demand:
            return None if plant.season is None else DEMAND_ACTION[plant.season]
        return HVACAction.OFF if plant.preset is MasterPreset.OFF else HVACAction.IDLE

    @property
    @override
    def current_temperature(self) -> float | None:
        return house_temperature(self.rehom_state)

    @property
    @override
    def target_temperature(self) -> float | None:
        plant = self.plant
        return plant.display_temperature if plant.preset in HOUSE_LEVEL_PRESETS else None

    @property
    def _range(self) -> tuple[float, float]:
        season = self.plant.season
        return (
            HOUSE_TEMPERATURE_RANGE_UNKNOWN if season is None else HOUSE_TEMPERATURE_RANGE[season]
        )

    @property
    @override
    def min_temp(self) -> float:
        return self._range[0]

    @property
    @override
    def max_temp(self) -> float:
        return self._range[1]

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        plant = self.plant
        return {
            "controller_setpoint": plant.controller_setpoint,
            "setpoint_mismatch": plant.setpoint_mismatch,
        }

    @override
    def _manual_level(self) -> str | None:
        preset = self.plant.preset
        return HOUSE_PRESETS[preset] if preset in HOUSE_LEVEL_PRESETS else None

    @override
    async def _async_set_auto(self) -> None:
        await control.async_set_house_preset(self._entry, MasterPreset.AUTO)

    @override
    async def _async_set_level(self, preset: str) -> None:
        # A MANUAL level also rewrites the controller's setpoint to its temperature.
        await control.async_set_house_preset(self._entry, HOUSE_LEVELS[preset])

    @override
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """The house target is the comfort temperature: not verified, so refused."""
        control.ensure_control_enabled(self._entry)
        control.raise_not_verified()

    @override
    def _refuse_temporary_comfort(self) -> NoReturn:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key=EXC_ZONE_ONLY)

    async def async_get_schedule(self) -> ServiceResponse:
        """``rehom.get_schedule`` targets zone thermostats only."""
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key=EXC_ZONE_ONLY)


class RehomZoneClimate(RehomClimate):
    """One zone thermostat (the library resolves mode and preset)."""

    _attr_translation_key = "zone"
    _attr_target_temperature_step = ZONE_TEMPERATURE_STEP

    def __init__(self, coordinator: RehomCoordinator, zone_id: str) -> None:
        """Zone entity."""
        super().__init__(coordinator, DeviceKind.ZONE, zone_id, KEY)
        self._zone_id = zone_id
        self._attr_preset_modes = [
            PRESET_NONE,
            PRESET_ECO,
            PRESET_PRE_COMFORT,
            PRESET_COMFORT,
            PRESET_TEMPORARY_COMFORT,
        ]

    @property
    @override
    def hvac_mode(self) -> HVACMode | None:
        zone = self.zone
        if zone is None or zone.mode is None:
            return None
        if zone.mode is ZoneMode.OFF:
            return HVACMode.OFF
        if zone.mode is ZoneMode.AUTO:
            return HVACMode.AUTO
        return self._season_mode

    @property
    @override
    def preset_mode(self) -> str | None:
        zone = self.zone
        if zone is None or zone.preset is None:
            return None
        return ZONE_PRESETS[zone.preset]

    @property
    @override
    def hvac_action(self) -> HVACAction | None:
        zone = self.zone
        if zone is None or zone.hvac_action is None:
            return None
        return HVAC_ACTIONS[zone.hvac_action]

    @property
    @override
    def current_temperature(self) -> float | None:
        zone = self.zone
        return None if zone is None else zone.temperature

    @property
    @override
    def current_humidity(self) -> float | None:
        zone = self.zone
        return None if zone is None else zone.humidity

    @property
    @override
    def target_temperature(self) -> float | None:
        zone = self.zone
        return None if zone is None else zone.target

    # The range Home Assistant checks a target against: the level temperature ±3 °C,
    # rounded so that float noise (16.1 - 3 = 13.100000000000001) never refuses an end.

    @property
    @override
    def min_temp(self) -> float:
        zone = self.zone
        if zone is None or zone.base is None:
            return DEFAULT_MIN_TEMP
        return round(zone.base + ZONE_OFFSET_MIN, ZONE_RANGE_DECIMALS)

    @property
    @override
    def max_temp(self) -> float:
        zone = self.zone
        if zone is None or zone.base is None:
            return DEFAULT_MAX_TEMP
        return round(zone.base + ZONE_OFFSET_MAX, ZONE_RANGE_DECIMALS)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = self.zone
        return {"controller_setpoint": None if zone is None else zone.controller_setpoint}

    @override
    def _manual_level(self) -> str | None:
        # The zone's own level only: a zone that follows a MANUAL house shows the
        # house's level, which heat/cool on the zone would not write.
        zone = self.zone
        return None if zone is None or zone.setp is None else ZONE_LEVEL_PRESETS.get(zone.setp)

    @override
    def _ensure_not_forced_off(self) -> None:
        # Switching a zone on again from an off forced by its probe was never tested
        # on a real controller, so Home Assistant leaves it to the controller (and to
        # the official app).  control.async_set_zone_mode refuses it too.
        zone = self.zone
        if zone is not None and zone.setp is ZoneSetp.PROBE_OFF:
            control.raise_not_verified()

    @override
    async def _async_set_auto(self) -> None:
        await control.async_set_zone_mode(self._entry, self._zone_id, ZoneSetp.UNSET)

    @override
    async def _async_set_level(self, preset: str) -> None:
        await control.async_set_zone_mode(self._entry, self._zone_id, ZONE_LEVELS[preset])

    @override
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the offset that brings the target nearest the temperature.

        In this order:

        1. a temperature that is not a finite number is refused
           (``invalid_value``) before anything is sent;
        2. a ``hvac_mode`` in the call is applied, unless it is the current
           mode (checked like ``climate.set_hvac_mode``);
        3. the temperature is checked against the level temperature ±3 °C of
           the mode the zone is in now (``target_out_of_range``): Home Assistant
           checked it against the range before a mode change.  It is never
           clamped; a mode applied in step 2 stays applied;
        4. the offset (whole degrees, halves up) is sent
           (``control.zone_offset_for_target``).
        """
        entry = self._entry
        control.ensure_control_enabled(entry)
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None:
            temperature = control.ensure_finite(float(temperature))
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        if hvac_mode is not None and hvac_mode != self.hvac_mode:
            await self.async_handle_set_hvac_mode_service(hvac_mode)
        if temperature is None:
            return
        zone = self.zone
        offset = control.zone_offset_for_target(temperature, None if zone is None else zone.base)
        await control.async_set_zone_offset(entry, self._zone_id, offset)

    async def async_get_schedule(self) -> ServiceResponse:
        """``rehom.get_schedule``: both season schedules, in controller time."""
        state = self.rehom_state
        zone = self.zone
        if zone is None:  # absent from the controller's state: not a usage error
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key=EXC_NOT_READY)
        clock = state.device_clock
        season = state.plant.season
        return {
            "zone": zone.id,
            "season": None if season is None else season.value,
            "controller_time": clock.local_now(dt_util.utcnow()).isoformat(),
            "timezone": clock.tz_name,
            "winter": _season_schedule(zone.schedule.winter),
            "summer": _season_schedule(zone.schedule.summer),
        }
