"""Shared fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import PropertyMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from syrupy.assertion import SnapshotAssertion

from custom_components.rehom.const import (
    CONF_TEMPORARY_COMFORT_DURATION,
    DEFAULT_TEMPORARY_COMFORT_DURATION,
    DEFAULT_TITLE,
    DOMAIN,
    PLATFORMS,
)

from .harness import FIXTURE_MAC, RehomHarness, load_device

ENTRY_DATA = {
    CONF_HOST: "rehomserver.local",
    CONF_PORT: 8000,
    CONF_USERNAME: "user",
    CONF_PASSWORD: "test-password",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/rehom in every test."""


@pytest.fixture(autouse=True)
async def quiet_slow_callback_warnings() -> None:
    """Freezer jumps look like 60-s callbacks to asyncio debug mode; silence that noise."""
    asyncio.get_running_loop().slow_callback_duration = 3600


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Home Assistant's serializer and ``tests/snapshots/`` for every module.

    syrupy's plain ``snapshot`` fixture shadows the one PHACC registers.
    """
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Create disabled-by-default entities enabled (snapshot_platform needs them all)."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
        new_callable=PropertyMock,
    ):
        yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A config entry for the fixture controller."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_TITLE,
        unique_id=FIXTURE_MAC,
        data=dict(ENTRY_DATA),
        options={CONF_TEMPORARY_COMFORT_DURATION: DEFAULT_TEMPORARY_COMFORT_DURATION},
        version=1,
        minor_version=1,
    )


@pytest.fixture
def device_patch() -> dict[str, Any] | None:
    """Override in a test module to patch the snapshot (``ReplayData``) or add frames.

    Return ``{"patch": callable, "extra_frames": [...]}`` from an override;
    the default is the unmodified fixture.
    """
    return


@pytest.fixture
def harness(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    device_patch: dict[str, Any] | None,
) -> Generator[RehomHarness]:
    """The replayed controller; patches ``api.create_client`` for the whole test."""
    kwargs = device_patch or {}
    harness = RehomHarness(hass, freezer, load_device(**kwargs))
    with patch("custom_components.rehom.api.create_client", side_effect=harness.create_client):
        yield harness


@pytest.fixture
def platforms() -> list[Platform]:
    """Platforms to load; override per test module (snapshot_platform needs one)."""
    return list(PLATFORMS)


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    harness: RehomHarness,
    mock_config_entry: MockConfigEntry,
    platforms: list[Platform],
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the entry on the replay (state at the capture start, 10:22:06.806Z)."""
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.rehom.PLATFORMS", platforms):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await harness.settle()
        yield mock_config_entry
        # Unload while PLATFORMS is still patched (only the loaded platforms unload).
        if mock_config_entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
            await hass.async_block_till_done()
