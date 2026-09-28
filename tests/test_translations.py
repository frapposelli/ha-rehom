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

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "rehom"
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
#: Issue texts with a fix flow (repairs.py) instead of a description; every other issue
#: has a title and a description (Home Assistant's schema allows one or the other).
FIXABLE_ISSUES = {const.ISSUE_SETPOINT_MISMATCH_FIXABLE}
#: Exception messages with placeholders, and the placeholders the code passes.
EXCEPTION_PLACEHOLDERS = {
    const.EXC_CANNOT_CONNECT: {"host"},
    const.EXC_WRITE_REFUSED: {"reason"},  # a library reason code, or forbidden_request
    const.EXC_TARGET_OUT_OF_RANGE: {"min", "max"},  # the zone's base ±3 °C
}
#: Error messages the README quotes (exception key -> the quoted words).
README_QUOTES = {
    const.EXC_CONTROL_DISABLED: "Control from Home Assistant is off",
    const.EXC_NOT_VERIFIED: "Home Assistant does not send this setting yet",
    const.EXC_NOT_SUPPORTED: "Home Assistant cannot do this on the Rehom controller",
    const.EXC_TEMPORARY_COMFORT_UNAVAILABLE: (
        "Temporary comfort is not available from Home Assistant yet"
    ),
    const.EXC_WRITE_NOT_CONFIRMED: "the controller did not confirm it",
    const.EXC_WRITE_FAILED: "It may or may not have been applied",
    const.EXC_ZONE_ONLY: "This action is only available for zone thermostats",
}
#: Words each language uses in the control texts (see test_control_texts).
CONTROL_WORDS = {
    "en": {
        "backup": "backup",
        "not_available": "not available",
        "refused": "refused",
        "nothing_sent": "nothing was sent",
        "may_have_been_applied": "may have been applied",
        "did_not_confirm": "did not confirm",
    },
    "it": {
        "backup": "backup",
        "not_available": "ancora disponibile",
        "refused": "rifiutata",
        "nothing_sent": "non è stato inviato nulla",
        "may_have_been_applied": "potrebbe essere stato applicato",
        "did_not_confirm": "non l'ha confermato",
    },
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
    for key, issue in STRINGS["issues"].items():
        # setpoint_mismatch has a text for when it cannot be fixed and one with the fix flow
        text = "fix_flow" if key in FIXABLE_ISSUES else "description"
        assert set(issue) == {"title", text}, key
    assert (
        STRINGS["issues"][const.ISSUE_SETPOINT_MISMATCH_FIXABLE]["title"]
        == STRINGS["issues"][const.ISSUE_SETPOINT_MISMATCH]["title"]
    )


def test_refusal_keys_translated() -> None:
    """Every library refusal with a message of its own maps to a translated EXC_* key."""
    exceptions = _constants("EXC_")
    assert set(const.REFUSAL_KEYS.values()) <= exceptions
    assert not set(const.REFUSAL_KEYS) & const.GENERIC_REFUSALS
    # generic refusals share write_refused; none has a message of its own
    assert not const.GENERIC_REFUSALS & exceptions
    for key in const.REFUSAL_KEYS.values():
        assert STRINGS["exceptions"][key]["message"], key
        assert IT["exceptions"][key]["message"], key


def test_placeholders_match_code() -> None:
    """The placeholders the code passes are the ones the strings use."""

    def placeholders(text: str) -> set[str]:
        return set(PLACEHOLDER.findall(text))

    exceptions = STRINGS["exceptions"]
    for key in exceptions:
        expected = EXCEPTION_PLACEHOLDERS.get(key, set())
        assert placeholders(exceptions[key]["message"]) == expected, key
    issues = STRINGS["issues"]
    assert placeholders(issues["setpoint_mismatch"]["description"]) == {
        "setpoint",
        "level_temperature",
    }
    # the fix flow (repairs.py) passes the issue's own placeholders to its confirm step
    assert placeholders(issues["setpoint_mismatch_fixable"]["title"]) == set()
    fix_flow = issues["setpoint_mismatch_fixable"]["fix_flow"]
    assert placeholders(fix_flow["step"]["confirm"]["description"]) == {
        "setpoint",
        "level_temperature",
    }
    assert placeholders(fix_flow["step"]["confirm"]["title"]) == set()
    assert placeholders(fix_flow["abort"]["not_fixed"]) == set()
    assert placeholders(fix_flow["abort"]["not_confirmed"]) == set()
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
    assert (
        set(init["data"])
        == set(init["data_description"])
        == {const.CONF_ENABLE_CONTROL, const.CONF_TEMPORARY_COMFORT_DURATION}
    )
    # listed in the order of the options form
    assert list(init["data"]) == [const.CONF_ENABLE_CONTROL, const.CONF_TEMPORARY_COMFORT_DURATION]


@pytest.mark.parametrize("language", ["en", "it"])
def test_control_texts(language: str) -> None:
    """The control texts name the option as the form shows it and say what is not available."""
    tree = {"en": STRINGS, "it": IT}[language]
    words = CONTROL_WORDS[language]
    init = tree["options"]["step"]["init"]
    option = init["data"][const.CONF_ENABLE_CONTROL]
    # every text that sends the user to the option uses the option's own label
    assert option in tree["exceptions"][const.EXC_CONTROL_DISABLED]["message"]
    assert option in init["description"]
    assert option in tree["issues"][const.ISSUE_SETPOINT_MISMATCH]["description"]
    # turning control on warns to take a backup first
    assert words["backup"] in init["data_description"][const.CONF_ENABLE_CONTROL]
    # temporary comfort: the actions stay registered, but say they are not available yet
    unavailable = tree["exceptions"][const.EXC_TEMPORARY_COMFORT_UNAVAILABLE]["message"]
    assert words["not_available"] in unavailable.lower()
    for service in (const.SERVICE_SET_TEMPORARY_COMFORT, const.SERVICE_CLEAR_TEMPORARY_COMFORT):
        description = tree["services"][service]["description"].lower()
        assert words["not_available"] in description, service
        assert words["refused"] in description, service
    assert words["not_available"] in (
        init["data_description"][const.CONF_TEMPORARY_COMFORT_DURATION].lower()
    )
    # refused numbers: invalid_value says nothing was sent; the range is in °C
    invalid = tree["exceptions"][const.EXC_INVALID_VALUE]["message"].lower()
    assert words["nothing_sent"] in invalid
    assert tree["exceptions"][const.EXC_TARGET_OUT_OF_RANGE]["message"].count("°C") == 2


@pytest.mark.parametrize("language", ["en", "it"])
def test_setpoint_fix_abort_texts(language: str) -> None:
    """The fix flow's aborts say whether anything was sent (repairs.py picks one)."""
    tree = {"en": STRINGS, "it": IT}[language]
    words = CONTROL_WORDS[language]
    abort = tree["issues"][const.ISSUE_SETPOINT_MISMATCH_FIXABLE]["fix_flow"]["abort"]
    not_fixed = abort["not_fixed"].lower()
    not_confirmed = abort["not_confirmed"].lower()
    # a refusal: nothing was sent, so nothing can have been applied
    assert words["nothing_sent"] in not_fixed
    assert words["may_have_been_applied"] not in not_fixed
    assert words["did_not_confirm"] not in not_fixed
    # sent, not confirmed: it may have been applied
    assert words["did_not_confirm"] in not_confirmed
    assert words["may_have_been_applied"] in not_confirmed
    assert words["nothing_sent"] not in not_confirmed


def test_readme_quotes_the_messages() -> None:
    """The README quotes the English error messages and the option name as they are."""
    readme = (ROOT / "README.md").read_text("utf-8")
    for key, words in README_QUOTES.items():
        assert words in STRINGS["exceptions"][key]["message"], key
        assert f'"{words.lower()}' in readme.lower(), key
    option = STRINGS["options"]["step"]["init"]["data"][const.CONF_ENABLE_CONTROL]
    assert f"| {option} |" in readme  # the Options table
    assert f"**{option}**" in readme


def test_no_read_only_wording_left() -> None:
    """The texts of the read-only preview are gone (control is an option now)."""
    stale = ("read-only", "this version", "only reads")
    stale_it = ("sola lettura", "questa versione", "legge soltanto")
    for path, text in _leaves(STRINGS):
        if path.startswith(("entity.", "exceptions.read_only.")):
            continue  # the controller's own read-only mode keeps its name
        assert not any(word in text.lower() for word in stale), path
    for path, text in _leaves(IT):
        if path.startswith(("entity.", "exceptions.read_only.")):
            continue
        assert not any(word in text.lower() for word in stale_it), path


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
