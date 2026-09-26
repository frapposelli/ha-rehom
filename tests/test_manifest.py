"""Manifest and HACS metadata."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

import aiorehom
from awesomeversion import AwesomeVersion, AwesomeVersionStrategy
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import Manifest, async_get_integration

from custom_components.rehom.const import DOMAIN

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "rehom"


def _manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((COMPONENT / "manifest.json").read_text("utf-8"))
    return manifest


def test_manifest_keys_and_values() -> None:
    """Keys sorted like hassfest (domain, name, then alphabetical); expected values."""
    manifest = _manifest()
    keys = list(manifest)
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
    assert manifest == {
        "domain": "rehom",
        "name": "Rehom",
        "codeowners": ["@frapposelli"],
        "config_flow": True,
        "dependencies": [],
        "dhcp": [{"hostname": "rehomserver*"}],
        "documentation": "https://github.com/frapposelli/ha-rehom",
        "integration_type": "hub",
        "iot_class": "local_push",
        "issue_tracker": "https://github.com/frapposelli/ha-rehom/issues",
        "loggers": ["aiorehom"],
        "requirements": ["aiorehom==0.2.0"],
        "version": "0.1.0",
        "zeroconf": [{"type": "_http._tcp.local.", "name": "rehom*"}],
    }


def test_requirement_matches_library() -> None:
    """The pin follows the library the tests run against."""
    assert _manifest()["requirements"] == [f"aiorehom=={aiorehom.__version__}"]


def test_discovery_matchers() -> None:
    """``rehom*`` matches the service instance; ``rehomserver*`` the DHCP host name."""
    manifest = _manifest()
    name_pattern = manifest["zeroconf"][0]["name"]
    assert fnmatch.fnmatch("rehom._http._tcp.local.", name_pattern)
    assert not fnmatch.fnmatch("printer._http._tcp.local.", name_pattern)
    assert fnmatch.fnmatch("rehomserver", manifest["dhcp"][0]["hostname"])


def test_hacs_json() -> None:
    """HACS metadata: name and the minimum Home Assistant version."""
    hacs = json.loads((ROOT / "hacs.json").read_text("utf-8"))
    assert hacs == {"name": "Rehom", "homeassistant": "2026.9.0"}
    assert AwesomeVersion(HA_VERSION) >= AwesomeVersion(hacs["homeassistant"])


#: Keys hassfest requires in a custom integration's manifest (domain, name, docs,
#: codeowners) plus ``version``, without which the loader blocks a custom integration
#: (``homeassistant/loader.py``, "does not have a version key").
HASSFEST_REQUIRED = {"domain", "name", "codeowners", "documentation", "version"}
#: Keys HACS checks in an integration manifest.
HACS_REQUIRED = {"domain", "name", "codeowners", "documentation", "issue_tracker", "version"}
#: Keys HACS accepts in hacs.json.
HACS_JSON_KEYS = {
    "name",
    "content_in_root",
    "zip_release",
    "filename",
    "hide_default_branch",
    "country",
    "homeassistant",
    "hacs",
    "persistent_directory",
    "render_readme",
}


def test_manifest_schema() -> None:
    """Only keys Home Assistant knows; every required key; versions and URLs valid."""
    manifest = _manifest()
    known = Manifest.__required_keys__ | Manifest.__optional_keys__
    assert set(manifest) <= known
    assert set(manifest) >= HASSFEST_REQUIRED | HACS_REQUIRED
    # the loader's own version check for custom integrations
    AwesomeVersion(
        manifest["version"],
        ensure_strategy=[
            AwesomeVersionStrategy.CALVER,
            AwesomeVersionStrategy.SEMVER,
            AwesomeVersionStrategy.SIMPLEVER,
            AwesomeVersionStrategy.BUILDVER,
            AwesomeVersionStrategy.PEP440,
        ],
    )
    for key in ("documentation", "issue_tracker"):
        assert manifest[key].startswith("https://"), key
    assert all("==" in requirement for requirement in manifest["requirements"])
    assert manifest["iot_class"] == "local_push"
    assert all(isinstance(owner, str) for owner in manifest["codeowners"])


def test_hacs_json_schema() -> None:
    """hacs.json keys HACS accepts; one integration under custom_components/."""
    hacs = json.loads((ROOT / "hacs.json").read_text("utf-8"))
    assert set(hacs) <= HACS_JSON_KEYS
    assert isinstance(hacs["name"], str)
    assert hacs["name"]
    assert not hacs.get("content_in_root", False)
    assert not hacs.get("zip_release", False)
    integrations = [
        path.name
        for path in (ROOT / "custom_components").iterdir()
        if path.is_dir() and not path.name.startswith(("_", "."))
    ]
    assert integrations == [DOMAIN]
    assert (COMPONENT / "manifest.json").is_file()


async def test_loader_accepts_manifest(hass: HomeAssistant) -> None:
    """Home Assistant's loader reads the manifest as a valid custom integration."""
    integration = await async_get_integration(hass, DOMAIN)
    assert not integration.is_built_in
    assert integration.domain == DOMAIN
    assert integration.config_flow
    assert integration.iot_class == "local_push"
    assert integration.integration_type == "hub"
    assert integration.requirements == ["aiorehom==0.2.0"]
    assert str(integration.version) == "0.1.0"
    assert integration.dependencies == []
