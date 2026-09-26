"""Translation completeness.

Complements ``test_translations.py``:

- ``strings.json``, ``translations/en.json`` and ``translations/it.json`` have the
  same structure (same key tree, same ``{placeholders}`` per string);
- every entity translation key the code declares (including entities this plant
  does not create, such as ``internet`` and ``free_cooling``) exists in all three,
  with every state value an entity can report (enum options, select options,
  presets, event types), and nothing is translated that no code declares;
- every ``translation_key`` literal in the code (exceptions, issues, devices,
  entities) exists in all three files;
- Home Assistant's own translation loader reads both languages with the same keys,
  and every exception message resolves;
- every icon belongs to a declared translation key and every action has an icon.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
import importlib
import json
from pathlib import Path
import re
from types import ModuleType
from typing import Any

from aiorehom import ControlSource, Level, VmcMode, VmcState
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import translation
from homeassistant.helpers.entity import EntityDescription
from homeassistant.util.yaml import load_yaml_dict
import pytest

from custom_components.rehom import const
from custom_components.rehom.const import DOMAIN, PLATFORMS
from custom_components.rehom.entity import RehomEntity

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "rehom"
PLACEHOLDER = re.compile(r"\{(\w+)\}")
#: Main-feature entities named after their device (``_attr_name = None``).
NAME_NONE_KEYS = {("climate", "house"), ("climate", "zone"), ("fan", "ventilation")}
#: Exception classes whose ``translation_key`` is an ``exceptions`` key.
EXCEPTION_CALLS = {
    "ServiceValidationError",
    "HomeAssistantError",
    "ConfigEntryAuthFailed",
    "ConfigEntryError",
    "ConfigEntryNotReady",
    "UpdateFailed",
}


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((COMPONENT / name).read_text("utf-8"))
    return data


STRINGS = _load("strings.json")
EN = _load("translations/en.json")
IT = _load("translations/it.json")
ICONS = _load("icons.json")
LANGUAGES: dict[str, dict[str, Any]] = {"strings.json": STRINGS, "en": EN, "it": IT}


def _shape(tree: dict[str, Any]) -> dict[str, Any]:
    """The key tree with each string replaced by its set of placeholders."""
    shape: dict[str, Any] = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            shape[key] = _shape(value)
        else:
            assert isinstance(value, str), key
            shape[key] = frozenset(PLACEHOLDER.findall(value))
    return shape


def _leaves(tree: dict[str, Any], prefix: str = "") -> Iterator[str]:
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _leaves(value, path)
        else:
            yield path


def _get(tree: dict[str, Any], path: str) -> Any:
    node: Any = tree
    for part in path.split("."):
        assert isinstance(node, dict), path
        assert part in node, path
        node = node[part]
    return node


def _platform_module(platform: str) -> ModuleType:
    return importlib.import_module(f"custom_components.rehom.{platform}")


def _is_description(value: Any) -> bool:
    """HA's frozen-dataclass compat re-creates the classes: match the MRO by name."""
    return any(cls.__name__ == "EntityDescription" for cls in type(value).__mro__)


def _descriptions(value: Any) -> Iterator[EntityDescription]:
    if _is_description(value):
        yield value
    elif isinstance(value, (tuple, list, frozenset, set)):
        for item in value:
            yield from _descriptions(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _descriptions(item)


def _declared_entity_keys() -> dict[str, dict[str, list[str] | None]]:
    """platform -> translation_key -> enum options (``None`` if not an enum).

    Collected from every module-level entity description and every entity class
    of each platform module, so gated entities absent from this plant count too.
    """
    declared: dict[str, dict[str, list[str] | None]] = {}
    for platform in PLATFORMS:
        module = _platform_module(str(platform))
        keys: dict[str, list[str] | None] = {}
        for name, value in vars(module).items():
            if name.startswith("__"):
                continue
            for description in _descriptions(value):
                if description.translation_key is None:
                    continue
                options = getattr(description, "options", None)
                enum = getattr(description, "device_class", None) is SensorDeviceClass.ENUM
                keys[description.translation_key] = list(options) if enum and options else None
            if (
                isinstance(value, type)
                and issubclass(value, RehomEntity)
                and value.__module__ == module.__name__
            ):
                # HA's CachedProperties metaclass moves the class attribute
                # ``_attr_translation_key`` to ``__attr_translation_key``.
                key = getattr(value, "__attr_translation_key", None)
                if isinstance(key, str):
                    keys.setdefault(key, None)
        declared[str(platform)] = keys
    return declared


DECLARED = _declared_entity_keys()


# -- the three files ---------------------------------------------------------------------


def test_files_structurally_identical() -> None:
    """Same key tree and the same placeholders per string in all three files."""
    reference = _shape(STRINGS)
    for name, tree in LANGUAGES.items():
        assert _shape(tree) == reference, name
    assert EN == STRINGS  # en.json is strings.json verbatim
    # Italian is a translation, not a copy of the English file: a string may be
    # identical only for terms used as-is in Italian (Host, Password, Comfort, Stop, ...)
    for path in _leaves(STRINGS):
        english = _get(STRINGS, path)
        if _get(IT, path) == english:
            assert len(english.split()) <= 2, path
            assert "." not in english, path


def test_top_level_sections() -> None:
    """Exactly the sections the integration uses (HA loads categories by these keys)."""
    for name, tree in LANGUAGES.items():
        assert set(tree) == {
            "config",
            "options",
            "device",
            "entity",
            "exceptions",
            "issues",
            "services",
        }, name


# -- entity keys and states --------------------------------------------------------------


def test_declared_entity_keys_are_translated() -> None:
    """Every declared entity key has a name (or is a main feature) and its enum states."""
    # the scan is not vacuous: test_no_orphan_entity_translations proves it found every
    # translated key; these two exist in code only (gated out on this plant)
    assert "internet" in DECLARED["binary_sensor"]  # gated out on this plant
    assert "free_cooling" in DECLARED["switch"]  # gated out on this plant
    for platform, keys in DECLARED.items():
        for key, options in keys.items():
            for name, tree in LANGUAGES.items():
                where = f"{name}: entity.{platform}.{key}"
                if (platform, key) in NAME_NONE_KEYS:
                    # named after the device; a state_attributes entry is optional
                    assert "name" not in tree["entity"].get(platform, {}).get(key, {}), where
                    continue
                entry = _get(tree, f"entity.{platform}.{key}")
                assert entry["name"], where
                if options is not None:
                    assert set(entry["state"]) == set(options), where


def test_no_orphan_entity_translations() -> None:
    """Nothing is translated that no entity declares."""
    for platform, keys in STRINGS["entity"].items():
        assert set(keys) <= set(DECLARED[platform]), platform


def test_every_state_value_is_translated() -> None:
    """Select options, enum sensors, presets and event types over their full value sets."""
    climate = _platform_module("climate")
    vmc_modes = {mode.name.lower() for mode in VmcMode}
    expected = {
        "entity.select.mode.state": vmc_modes,
        "entity.sensor.effective_mode.state": vmc_modes,
        "entity.sensor.vmc_state.state": {state.name.lower() for state in VmcState},
        "entity.sensor.level.state": {level.name.lower() for level in Level},
        "entity.sensor.control_source.state": {source.value for source in ControlSource},
        "entity.sensor.season.state": {"winter", "summer"},
        "entity.sensor.controlled_by.state": {"web", "crono", "serial_down"},
        "entity.climate.house.state_attributes.preset_mode.state": set(
            climate.HOUSE_PRESETS.values()
        ),
        "entity.climate.zone.state_attributes.preset_mode.state": set(
            climate.ZONE_PRESETS.values()
        ),
        "entity.event.alarm.state_attributes.event_type.state": {
            const.EVENT_ALARM_RAISED,
            const.EVENT_ALARM_CLEARED,
        },
    }
    for name, tree in LANGUAGES.items():
        for path, values in expected.items():
            assert set(_get(tree, path)) == values, f"{name}: {path}"
        for key, entry in tree["entity"]["binary_sensor"].items():
            assert set(entry.get("state", {})) <= {"on", "off"}, f"{name}: {key}"


# -- translation_key literals in the code ------------------------------------------------


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _resolve(node: ast.expr, module: ModuleType) -> str | None:
    """A string literal, or a module-level name bound to a string; else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        value = getattr(module, node.id, None)
        return value if isinstance(value, str) else None
    return None


def _code_translation_keys() -> Iterator[tuple[str, str, str | None, int]]:
    """(module, category, key or None if dynamic, line) for every translation_key use."""
    for path in sorted(COMPONENT.glob("*.py")):
        module = importlib.import_module(f"custom_components.rehom.{path.stem}")
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                for keyword in node.keywords:
                    if keyword.arg != "translation_key":
                        continue
                    if name in EXCEPTION_CALLS:
                        category = "exceptions"
                    elif name == "async_create_issue":
                        category = "issues"
                    elif name in {"DeviceInfo", "async_get_or_create"}:
                        category = "device"
                    elif name.endswith("Description"):
                        category = f"entity.{path.stem}"
                    else:
                        category = f"unknown call {name}"
                    yield path.stem, category, _resolve(keyword.value, module), node.lineno
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_attr_translation_key"
                for target in node.targets
            ):
                yield path.stem, f"entity.{path.stem}", _resolve(node.value, module), node.lineno


def test_code_translation_keys_exist() -> None:
    """Every translation_key the code passes exists in strings.json, en.json and it.json."""
    uses = list(_code_translation_keys())
    categories = {category for _module, category, _key, _line in uses}
    assert {"exceptions", "device", "entity.sensor", "entity.climate"} <= categories
    for module, category, key, line in uses:
        where = f"{module}.py:{line}"
        assert not category.startswith("unknown"), f"{where}: {category}"
        if category == "issues":
            # issues.py creates issues from ISSUE_* constants only (checked below)
            assert module == "issues", where
            continue
        if key is None:
            # computed keys: only entity descriptions, all covered by DECLARED above
            assert category.startswith("entity."), f"{where}: dynamic {category} key"
            continue
        for name, tree in LANGUAGES.items():
            if category.startswith("entity."):
                platform = category.split(".", 1)[1]
                assert key in DECLARED[platform], f"{where}: {key}"
                if (platform, key) not in NAME_NONE_KEYS:
                    assert key in tree["entity"][platform], f"{name} {where}: {key}"
            else:
                assert key in tree[category], f"{name} {where}: {category}.{key}"


def test_issue_keys_are_constants() -> None:
    """issues.py raises and deletes issues only through ISSUE_* constants."""
    issue_constants = {name for name in vars(const) if name.startswith("ISSUE_")}
    tree = ast.parse((COMPONENT / "issues.py").read_text("utf-8"))
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "_set":
            first = node.args[0]
            assert isinstance(first, ast.Name), ast.unparse(node)
            used.add(first.id)
        if isinstance(node, ast.Call) and _call_name(node) == "async_create_issue":
            key = next(kw.value for kw in node.keywords if kw.arg == "translation_key")
            assert isinstance(key, ast.Name), ast.unparse(node)
            used.add(key.id)
    assert used - {"key"} <= issue_constants
    assert {"ISSUE_SETPOINT_MISMATCH", "ISSUE_UNSUPPORTED_API"} <= used


def test_constant_keys_exist() -> None:
    """Every EXC_* and ISSUE_* constant in all three files, with the right sub-keys."""
    exceptions = {v for k, v in vars(const).items() if k.startswith("EXC_")}
    issues = {v for k, v in vars(const).items() if k.startswith("ISSUE_") and isinstance(v, str)}
    for name, tree in LANGUAGES.items():
        assert set(tree["exceptions"]) == exceptions, name
        assert set(tree["issues"]) == issues, name
        for key in exceptions:
            assert set(tree["exceptions"][key]) == {"message"}, f"{name}: {key}"
        for key in issues:
            assert set(tree["issues"][key]) == {"title", "description"}, f"{name}: {key}"


# -- Home Assistant's loader --------------------------------------------------------------


@pytest.mark.parametrize("category", sorted(STRINGS))
async def test_home_assistant_loads_both_languages(hass: HomeAssistant, category: str) -> None:
    """HA reads translations/en.json and it.json with exactly the same flattened keys."""
    loaded = {
        language: await translation.async_get_translations(hass, language, category, [DOMAIN])
        for language in ("en", "it")
    }
    expected = {f"component.{DOMAIN}.{category}.{path}" for path in _leaves(STRINGS[category])}
    assert set(loaded["en"]) == expected
    assert set(loaded["it"]) == expected
    assert all(loaded["it"].values())


@pytest.mark.parametrize("platforms", [[]])
@pytest.mark.usefixtures("init_integration")
async def test_exception_messages_resolve(hass: HomeAssistant, platforms: list[str]) -> None:
    """Each exception key renders its English message once loaded (HA drops the final period)."""
    for key, entry in STRINGS["exceptions"].items():
        placeholders = dict.fromkeys(PLACEHOLDER.findall(entry["message"]), "192.0.2.10")
        err = HomeAssistantError(
            translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
        )
        assert str(err) == entry["message"].rstrip(".").format(**placeholders), key


# -- icons -------------------------------------------------------------------------------


def test_icons_complete() -> None:
    """Icons only for declared keys; each action has one; state icons match translations."""
    for platform, keys in ICONS["entity"].items():
        for key, icon in keys.items():
            assert key in DECLARED[platform], f"{platform}.{key}"
            assert icon.get("default", "mdi:").startswith("mdi:"), f"{platform}.{key}"
            for state in icon.get("state", {}):
                translated = STRINGS["entity"].get(platform, {}).get(key, {}).get("state", {})
                assert state in set(translated) | {"on", "off"}, f"{platform}.{key}.{state}"
    services = load_yaml_dict(str(COMPONENT / "services.yaml"))
    assert set(ICONS["services"]) == set(services)
    for name, icon in ICONS["services"].items():
        assert icon["service"].startswith("mdi:"), name
