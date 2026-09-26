"""Climate entities: the house and one per zone.

Read-only in this version: every control method raises ``control_disabled`` through
:func:`~.entity.raise_control_disabled` before anything touches the client.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import Any, override

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
)
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import PRECISION_TENTHS, UnitOfTemperature
from homeassistant.core import HomeAssistant, ServiceResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType, JsonValueType

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
    raise_control_disabled,
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
#: House presets that select a fixed level (MANUAL): the season mode and a target.
HOUSE_LEVEL_PRESETS = frozenset(
    {MasterPreset.ECONOMY, MasterPreset.PRE_COMFORT, MasterPreset.COMFORT}
)

ZONE_PRESETS: dict[ZonePreset, str] = {
    ZonePreset.NONE: PRESET_NONE,
    ZonePreset.ECONOMY: PRESET_ECO,
    ZonePreset.PRE_COMFORT: PRESET_PRE_COMFORT,
    ZonePreset.COMFORT: PRESET_COMFORT,
    ZonePreset.TEMPORARY_COMFORT: PRESET_TEMPORARY_COMFORT,
}

HVAC_ACTIONS: dict[HvacAction, HVACAction] = {
    HvacAction.OFF: HVACAction.OFF,
    HvacAction.IDLE: HVACAction.IDLE,
    HvacAction.HEATING: HVACAction.HEATING,
    HvacAction.COOLING: HVACAction.COOLING,
}

#: Controller weekday convention: 0 = Sunday.
WEEKDAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


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


class RehomClimate(RehomEntity, ClimateEntity):
    """Common read-only behaviour of the house and zone climates."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_TENTHS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    @property
    def _season_mode(self) -> HVACMode | None:
        season = self.plant.season
        return None if season is None else SEASON_MODE[season]

    @property
    @override
    def hvac_modes(self) -> list[HVACMode]:
        season_mode = self._season_mode
        return [HVACMode.OFF, HVACMode.AUTO] + ([season_mode] if season_mode else [])

    # -- control: refused (read-only; toggle routes to turn_on/turn_off) ---------------

    @override
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        raise_control_disabled()

    @override
    async def async_set_preset_mode(self, preset_mode: str) -> None:
        raise_control_disabled()

    @override
    async def async_set_temperature(self, **kwargs: Any) -> None:
        raise_control_disabled()

    @override
    async def async_turn_on(self) -> None:
        raise_control_disabled()

    @override
    async def async_turn_off(self) -> None:
        raise_control_disabled()

    async def async_set_temporary_comfort(self, duration: float | None = None) -> None:
        """``rehom.set_temporary_comfort``: refused (read-only)."""
        raise_control_disabled()

    async def async_clear_temporary_comfort(self) -> None:
        """``rehom.clear_temporary_comfort``: refused (read-only)."""
        raise_control_disabled()


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

    @property
    @override
    def min_temp(self) -> float:
        zone = self.zone
        if zone is None or zone.base is None:
            return DEFAULT_MIN_TEMP
        return zone.base + ZONE_OFFSET_MIN

    @property
    @override
    def max_temp(self) -> float:
        zone = self.zone
        if zone is None or zone.base is None:
            return DEFAULT_MAX_TEMP
        return zone.base + ZONE_OFFSET_MAX

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = self.zone
        return {"controller_setpoint": None if zone is None else zone.controller_setpoint}

    async def async_get_schedule(self) -> ServiceResponse:
        """``rehom.get_schedule``: both season schedules, in controller time."""
        state = self.rehom_state
        zone = self.zone
        if zone is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key=EXC_NOT_READY)
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
