"""Integration actions (registered in async_setup)."""

from __future__ import annotations

from homeassistant.components.climate.const import DOMAIN as CLIMATE_DOMAIN
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.helpers import service
import voluptuous as vol

from .const import (
    ATTR_DURATION,
    DOMAIN,
    MAX_TEMPORARY_COMFORT_DURATION,
    MIN_TEMPORARY_COMFORT_DURATION,
    SERVICE_CLEAR_TEMPORARY_COMFORT,
    SERVICE_GET_SCHEDULE,
    SERVICE_SET_TEMPORARY_COMFORT,
    TEMPORARY_COMFORT_DURATION_STEP,
)


def _half_hours(value: float) -> float:
    """Durations are whole multiples of 0.5 h."""
    if (value / TEMPORARY_COMFORT_DURATION_STEP) % 1:
        raise vol.Invalid("duration must be a multiple of 0.5 hours")
    return value


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register rehom.get_schedule, rehom.set_temporary_comfort, rehom.clear_temporary_comfort."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_GET_SCHEDULE,
        entity_domain=CLIMATE_DOMAIN,
        schema=None,
        func="async_get_schedule",
        supports_response=SupportsResponse.ONLY,
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_TEMPORARY_COMFORT,
        entity_domain=CLIMATE_DOMAIN,
        schema={
            vol.Optional(ATTR_DURATION): vol.All(
                vol.Coerce(float),
                vol.Range(min=MIN_TEMPORARY_COMFORT_DURATION, max=MAX_TEMPORARY_COMFORT_DURATION),
                _half_hours,
            )
        },
        func="async_set_temporary_comfort",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_CLEAR_TEMPORARY_COMFORT,
        entity_domain=CLIMATE_DOMAIN,
        schema=None,
        func="async_clear_temporary_comfort",
    )
