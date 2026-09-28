"""Helpers shared by the ``test_*_control.py`` modules (entities with "Enable control" on)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.rehom.const import (
    CONF_TEMPORARY_COMFORT_DURATION,
    DEFAULT_TEMPORARY_COMFORT_DURATION,
    DOMAIN,
)

from .harness import RehomHarness

#: Options of the default entry ("Enable control" not set): parametrize ``entry_options``.
CONTROL_OFF_OPTIONS: dict[str, Any] = {
    CONF_TEMPORARY_COMFORT_DURATION: DEFAULT_TEMPORARY_COMFORT_DURATION
}


async def call_action(
    hass: HomeAssistant,
    domain: str,
    service: str,
    entity_id: str,
    data: Mapping[str, Any] | None = None,
) -> None:
    """Call an entity action and wait for it, as a user or automation does."""
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity_id, **(data or {})}, blocking=True
    )


async def call_action_until_done(
    harness: RehomHarness,
    domain: str,
    service: str,
    entity_id: str,
    data: Mapping[str, Any] | None = None,
    *,
    limit: timedelta = timedelta(minutes=4),
    step: float = 1.0,
) -> None:
    """:func:`call_action` while both clocks move ``step`` seconds at a time.

    For an action that waits on the virtual clock (a write that is never
    confirmed: the echo window, then one resync); see
    :meth:`RehomHarness.run_until_done` with ``hass_action=True``.  Raises what
    the action raises; fails the test if it is still running after ``limit``.
    """
    await harness.run_until_done(
        call_action(harness.hass, domain, service, entity_id, data),
        limit=limit,
        step=step,
        hass_action=True,
    )


def assert_key(err: pytest.ExceptionInfo[HomeAssistantError], key: str) -> None:
    """The error is this integration's translated ``key``."""
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == key


def assert_device_error(err: pytest.ExceptionInfo[HomeAssistantError], key: str) -> None:
    """A HomeAssistantError that is not a ServiceValidationError (not the user's fault)."""
    assert not isinstance(err.value, ServiceValidationError)
    assert_key(err, key)
