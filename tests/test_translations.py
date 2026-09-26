"""Translations and icons.

``strings.json`` is the English source and must equal ``translations/en.json``
(custom integrations load ``translations/*.json`` directly).  Italian has
exactly the same keys and ``{placeholders}``.  Every key the code uses exists.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
import json
from pathlib import Path
import re
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.yaml import load_yaml_dict
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rehom import const
from custom_components.rehom.const import PLATFORMS

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "rehom"
PLACEHOLDER = re.compile(r"\{(\w+)\}")
#: Main-feature entities: the name is the device name (``_attr_name = None``).
NAME_NONE_KEYS = {("climate", "house"), ("climate", "zone"), ("fan", "ventilation")}
#: Reasons produced by Home Assistant's own ConfigFlow helpers used in config_flow.py.
HELPER_ABORT_REASONS = {
    "already_configured",  # _abort_if_unique_id_configured / _async_abort_entries_match
    "already_in_progress",  # async_set_unique_id(raise_on_progress=True)
    "reauth_successful",  # async_update_reload_and_abort (reauth)
    "reconfigure_successful",  # async_update_reload_and_abort (reconfigure)
}


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((COMPONENT / name).read_text("utf-8"))
    return data


STRINGS = _load("strings.json")
EN = _load("translations/en.json")
IT = _load("translations/it.json")
ICONS = _load("icons.json")


def _leaves(tree: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, str]]:
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _leaves(value, path)
        else:
            assert isinstance(value, str), path
            yield path, value


def _constants(prefix: str) -> set[str]:
    return {
        value
        for name, value in vars(const).items()
        if name.startswith(prefix) and isinstance(value, str)
    }


def _string_literals(path: Path, keyword: str) -> set[str]:
    """String values passed as ``keyword=...`` anywhere in ``path``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (
                    kw.arg == keyword
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    found.add(kw.value.value)
    return found


def _error_literals(path: Path) -> set[str]:
    """Values assigned to ``errors[...]`` in ``path``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "errors"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ):
            found.add(node.value.value)
    return found


# -- files ------------------------------------------------------------------------------


def test_english_equals_strings() -> None:
    """``translations/en.json`` is ``strings.json`` verbatim (no ``[%key:...%]`` references)."""
    assert EN == STRINGS
    assert "[%key:" not in json.dumps(STRINGS)


def test_italian_matches_english() -> None:
    """Same key tree, same placeholders per string, nothing left untranslated-empty."""
    english = dict(_leaves(STRINGS))
    italian = dict(_leaves(IT))
    assert set(italian) == set(english)
    for path, text in english.items():
        assert set(PLACEHOLDER.findall(italian[path])) == set(PLACEHOLDER.findall(text)), path
        assert italian[path].strip(), path


def test_no_trailing_whitespace_or_empty_strings() -> None:
    """Every string is non-empty and trimmed."""
    for translations in (STRINGS, IT):
        for path, text in _leaves(translations):
            assert text, path
            assert text == text.strip(), path


# -- keys used by the code ---------------------------------------------------------------


def test_exceptions_and_issues_exist() -> None:
    """Every EXC_* and ISSUE_* constant is translated, and nothing else is."""
    assert set(STRINGS["exceptions"]) == _constants("EXC_")
    assert set(STRINGS["issues"]) == _constants("ISSUE_")
    for issue in STRINGS["issues"].values():
        assert set(issue) == {"title", "description"}


def test_placeholders_match_code() -> None:
    """The placeholders the code passes are the ones the strings use."""

    def placeholders(text: str) -> set[str]:
        return set(PLACEHOLDER.findall(text))

    exceptions = STRINGS["exceptions"]
    assert placeholders(exceptions["cannot_connect"]["message"]) == {"host"}
    for key in set(exceptions) - {"cannot_connect"}:
        assert placeholders(exceptions[key]["message"]) == set(), key
    issues = STRINGS["issues"]
    assert placeholders(issues["setpoint_mismatch"]["description"]) == {
        "setpoint",
        "level_temperature",
    }
    assert placeholders(issues["unsupported_api"]["description"]) == {"version"}
    # raised at setup without any state: no version, no "keeps the last known state"
    assert placeholders(issues["unsupported_api_setup"]["description"]) == set()
    assert "last known state" not in issues["unsupported_api_setup"]["description"]
    assert "last known state" in issues["unsupported_api"]["description"]
    assert placeholders(issues["installer_session_active"]["description"]) == set()
    assert placeholders(issues["season_mismatch"]["description"]) == set()
    steps = STRINGS["config"]["step"]
    assert placeholders(steps["discovery_confirm"]["description"]) == {"host"}
    assert placeholders(steps["reauth_confirm"]["description"]) == {"host"}
    assert placeholders(STRINGS["config"]["flow_title"]) == {"name"}


def test_config_flow_keys_exist() -> None:
    """Every step, form field, error and abort reason of the flows is translated."""
    config_flow = COMPONENT / "config_flow.py"
    config = STRINGS["config"]
    step_ids = _string_literals(config_flow, "step_id")
    assert step_ids - {"init"} == set(config["step"])
    assert "init" in STRINGS["options"]["step"]
    assert _error_literals(config_flow) <= set(config["error"])
    assert _string_literals(config_flow, "reason") | HELPER_ABORT_REASONS == set(config["abort"])
    fields = {
        "user": {"host", "port", "username", "password"},
        "discovery_confirm": {"username", "password"},
        "reauth_confirm": {"username", "password"},
        "reconfigure": {"host", "port"},
    }
    for step, keys in fields.items():
        assert set(config["step"][step]["data"]) == keys, step
        assert set(config["step"][step]["data_description"]) == keys, step
        assert {"title", "description"} <= set(config["step"][step]), step
    init = STRINGS["options"]["step"]["init"]
    assert set(init["data"]) == set(init["data_description"]) == {"temporary_comfort_duration"}


def test_devices_translated() -> None:
    """The hub and plant device names come from translation keys."""
    assert STRINGS["device"] == {
        "hub": {"name": "Rehom server"},
        "plant": {"name": "Rehom plant"},
    }


def test_services_translated() -> None:
    """services.yaml, strings.json and icons.json describe the same actions and fields."""
    services = load_yaml_dict(str(COMPONENT / "services.yaml"))
    assert set(services) == set(STRINGS["services"]) == set(ICONS["services"])
    assert set(services) == {
        const.SERVICE_GET_SCHEDULE,
        const.SERVICE_SET_TEMPORARY_COMFORT,
        const.SERVICE_CLEAR_TEMPORARY_COMFORT,
    }
    for name, spec in services.items():
        fields = set((spec or {}).get("fields", {}))
        assert set(STRINGS["services"][name].get("fields", {})) == fields, name
        assert {"name", "description"} <= set(STRINGS["services"][name]), name


def test_icon_keys_are_translation_keys() -> None:
    """Every icon belongs to a translated entity key (or a name-None main feature)."""
    entity_strings = STRINGS["entity"]
    for domain, keys in ICONS["entity"].items():
        for key, icon in keys.items():
            translated = key in entity_strings.get(domain, {})
            assert translated or (domain, key) in NAME_NONE_KEYS, f"{domain}.{key}"
            assert icon["default"].startswith("mdi:"), f"{domain}.{key}"
            if "state" in icon:
                states = entity_strings[domain][key].get("state", {})
                if states:
                    assert set(icon["state"]) <= set(states) | {"on", "off"}, f"{domain}.{key}"
            if "state_attributes" in icon:
                for attribute, values in icon["state_attributes"].items():
                    translated_states = entity_strings[domain][key]["state_attributes"][attribute]
                    assert set(values["state"]) == set(translated_states["state"])


def test_translated_domains_are_platforms() -> None:
    """Entity translations exist only for platforms the integration loads."""
    platforms = {str(platform) for platform in PLATFORMS}
    assert set(STRINGS["entity"]) <= platforms
    assert set(ICONS["entity"]) <= platforms


# -- every registered entity has a name --------------------------------------------------


@pytest.mark.parametrize("platforms", [list(PLATFORMS)])
async def test_every_entity_is_named(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    platforms: list[Platform],
) -> None:
    """Every translation_key of every entity resolves to a name (or is a name-None entity)."""
    entries = er.async_entries_for_config_entry(entity_registry, init_integration.entry_id)
    assert entries
    for entry in entries:
        key = entry.translation_key
        if key is None:
            # device-class names (temperature, humidity, connectivity)
            assert entry.original_device_class is not None, entry.entity_id
            continue
        translation = STRINGS["entity"].get(entry.domain, {}).get(key)
        if (entry.domain, key) in NAME_NONE_KEYS:
            assert entry.original_name is None, entry.entity_id
            continue
        assert translation is not None, f"{entry.entity_id}: {entry.domain}.{key}"
        assert entry.original_name == translation["name"], entry.entity_id
        state = hass.states.get(entry.entity_id)
        assert state is not None
        assert "None" not in state.attributes.get("friendly_name", ""), entry.entity_id
