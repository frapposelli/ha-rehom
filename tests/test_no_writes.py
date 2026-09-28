"""Writes only through control.py, and only with "Enable control" on: a static check.

Every ``.py`` file under ``custom_components/rehom``, subpackages included, is
parsed and checked:

- Every attribute the integration reads on a ``client`` expression is on the
  read allowlist below, in every module.
- The library's write methods that Home Assistant uses (the live-verified ones)
  may be called on the client in ``control.py`` only.  Outside ``control.py``
  no attribute named like a library write member may appear on any receiver,
  so an alias of the client cannot write either.  The library's other write
  methods (comfort temperature, temporary comfort), its raw record access
  (``record_values``, ``dump``) and ``resync`` are banned everywhere, and so
  are the client's transport (``_transport``) and the transport's raw write
  (``post_bulk_update``), which skip the library's planners and guards:
  ``control.py`` included.
- A member a module may not use cannot be reached by name either: no string
  constant equals one (so ``operator.attrgetter`` and ``methodcaller`` cannot
  name it), and no dynamic lookup (``getattr``, ``setattr``, ``hasattr``,
  ``delattr``, ``attrgetter``, ``methodcaller``, or a name bound to one) takes
  a computed name, in any module.
- ``allow_writes`` is passed exactly twice: ``api.create_client`` hands its own
  keyword (default ``False``) to ``RehomClient``, and the entry setup passes
  ``entry.options.get(CONF_ENABLE_CONTROL) is True``.  The config flow's probe
  never passes it.  No call that involves a client builder (``RehomClient``,
  ``create_client``, or a name bound to one by an import alias or an
  assignment) splats ``**`` keywords.
- No module imports the library's transport, WebSocket, sync or replay
  internals.  Only ``api.py`` builds a ``RehomClient`` or uses the name at run
  time; other modules may import it for type checking only (under
  ``if TYPE_CHECKING:``, with ``TYPE_CHECKING`` from ``typing``, never rebound).

The detectors are tested at the end on samples of each known way around them.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path

from aiorehom import RehomClient
from aiorehom.transport import ReadOnlyTransport
import pytest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "rehom"


def _sources(root: Path) -> list[Path]:
    """Every Python module under ``root``, subpackages included."""
    return sorted(root.rglob("*.py"))


SOURCES = _sources(COMPONENT)
#: The only module that may call the library's write methods.
CONTROL_MODULE = "control.py"
#: The only module that may build a client (and use ``RehomClient`` at run time).
API_MODULE = "api.py"

#: Every client member the integration may read (they read or manage the connection).
CLIENT_ALLOWLIST = frozenset(
    {
        "connect",
        "close",
        "state",
        "subscribe",
        "on_connection_change",
        "available",
        "connection_state",
        "stats",
        "last_synced_at",
        "allow_writes",  # the read-only flag the entry was built with
    }
)
#: The library's write methods Home Assistant uses, in control.py only (each live-verified).
CLIENT_WRITE_ALLOWLIST = frozenset(
    {
        "set_house_preset",
        "set_zone_offset",
        "set_zone_mode",
        "set_vmc_fan",
        "set_vmc_mode",
        "set_predictive",
    }
)
#: Never used by the integration, on any receiver, in any module.
BANNED_MEMBERS = frozenset(
    {
        "set_comfort_temperature",  # not verified on a real controller
        "set_temporary_comfort",  # not verified; no way to cancel one
        "record_values",  # raw values, secrets included
        "dump",  # raw records (personal data)
        "resync",
    }
)
#: Never used in any module, control.py included: the client's transport, the
#: transport's raw write and the client's private write machinery skip the
#: library's planners, guards or confirmation.
INTERNAL_MEMBERS = frozenset(
    {
        "_transport",
        "post_bulk_update",
        "_execute",
        "_confirm_sent",
        "_allow_writes",
        "_write_lock",
        "_stores",
    }
)
#: Lookups that reach any member by a computed name; never used in the integration.
INTROSPECTION_ATTRIBUTES = frozenset({"__dict__", "__getattribute__"})
INTROSPECTION_CALLS = frozenset({"vars", "getattr_static"})
FORBIDDEN_MODULES = ("aiorehom.transport", "aiorehom.websocket", "aiorehom.sync", "aiorehom.replay")
WRITE_PREFIXES = ("set_", "write", "post", "put", "delete", "bulk")
#: Every public client member that looks like a write, as the library defines them today.
LIBRARY_WRITES = frozenset(
    name
    for name in dir(RehomClient)
    if not name.startswith("_") and name.lower().startswith(WRITE_PREFIXES)
)
#: Dynamic lookups and the position of their name argument (``None``: every argument).
DYNAMIC_LOOKUPS: Mapping[str, int | None] = {
    "getattr": 1,
    "setattr": 1,
    "delattr": 1,
    "hasattr": 1,
    "attrgetter": None,
    "methodcaller": 0,
}
#: The lookups whose first argument is the object looked into.
OBJECT_LOOKUPS = frozenset({"getattr", "setattr", "delattr", "hasattr"})
#: Module-level constants whose string value may equal a banned member name.
NAMED_STRING_EXEMPTIONS = frozenset(
    {
        # the name of the rehom.set_temporary_comfort action, not a client member
        ("const.py", "SERVICE_SET_TEMPORARY_COMFORT"),
    }
)
#: The names that build a client.
BUILDERS = frozenset({"RehomClient", "create_client"})
#: The only expression the entry setup may pass as ``allow_writes``.
OPTION_FLAG = ast.dump(
    ast.parse("entry.options.get(CONF_ENABLE_CONTROL) is True", mode="eval").body
)


def _module(path: Path) -> str:
    """``path`` relative to the component: ``control.py``, ``helpers/x.py``."""
    return path.relative_to(COMPONENT).as_posix()


@cache
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text("utf-8"), filename=str(path))


def _is_client(node: ast.expr) -> bool:
    """``client``, ``self.client``, ``runtime.client``, ``coordinator.client`` ..."""
    return (isinstance(node, ast.Name) and node.id == "client") or (
        isinstance(node, ast.Attribute) and node.attr == "client"
    )


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _client_attributes(tree: ast.Module) -> set[tuple[int, str]]:
    return {
        (node.lineno, node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and _is_client(node.value)
    }


# -- names bound to builders and lookups ------------------------------------------------------


def _resolve(node: ast.expr, aliases: Mapping[str, str]) -> str | None:
    """The original ``node`` names: a local alias, or ``x.<original>`` (any receiver)."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr in aliases.values():
        return node.attr
    return None


def _aliases(tree: ast.Module, names: frozenset[str]) -> dict[str, str]:
    """Every local name bound to one of ``names``, mapped to that original name.

    The names themselves, ``from x import name as alias``, and assignments
    (``alias = name``, ``alias = x.name``, ``alias: T = ...``, ``(alias := ...)``),
    chained, in any scope.
    """
    aliases = {name: name for name in names}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            bound: list[tuple[str, str]] = []
            if isinstance(node, ast.ImportFrom):
                bound = [(alias.asname or alias.name, alias.name) for alias in node.names]
                bound = [(local, name) for local, name in bound if name in names]
            elif isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr) and node.value:
                original = _resolve(node.value, aliases)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if original is not None:
                    bound = [(t.id, original) for t in targets if isinstance(t, ast.Name)]
            for local, original in bound:
                if aliases.get(local) != original:
                    aliases[local] = original
                    changed = True
    return aliases


# -- member access ----------------------------------------------------------------------------


def _banned(module: str) -> frozenset[str]:
    """The member names ``module`` may not use, on the client or on any receiver."""
    allowed = CLIENT_ALLOWLIST | (
        CLIENT_WRITE_ALLOWLIST if module == CONTROL_MODULE else frozenset()
    )
    return BANNED_MEMBERS | INTERNAL_MEMBERS | (LIBRARY_WRITES - allowed)


def _exempt_strings(tree: ast.Module, module: str) -> set[int]:
    """``id()`` of the string constants NAMED_STRING_EXEMPTIONS allows in ``module``."""
    exempt: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        names = {target.id for target in targets if isinstance(target, ast.Name)}
        if isinstance(value, ast.Constant) and any(
            (module, name) in NAMED_STRING_EXEMPTIONS for name in names
        ):
            exempt.add(id(value))
    return exempt


def _lookup_violations(call: ast.Call, lookup: str, where: str) -> list[str]:
    found: list[str] = []
    if lookup in OBJECT_LOOKUPS and call.args and _is_client(call.args[0]):
        found.append(f"{where}: dynamic attribute access on the client")
    position = DYNAMIC_LOOKUPS[lookup]
    names = call.args if position is None else call.args[position : position + 1]
    if not names or not all(
        isinstance(name, ast.Constant) and isinstance(name.value, str) for name in names
    ):
        found.append(f"{where}: {lookup}() with a computed name")
    return found


def _violations(tree: ast.Module, module: str) -> list[str]:
    """Client members, banned members and dynamic attribute access ``module`` may not use."""
    allowed = CLIENT_ALLOWLIST | (CLIENT_WRITE_ALLOWLIST if module == CONTROL_MODULE else set())
    banned = _banned(module)
    exempt = _exempt_strings(tree, module)
    lookups = _aliases(tree, frozenset(DYNAMIC_LOOKUPS))
    found: list[str] = []
    for node in ast.walk(tree):
        where = f"{module}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Attribute) and node.attr in INTROSPECTION_ATTRIBUTES:
            found.append(f"{where}: .{node.attr}")
        elif isinstance(node, ast.Call) and _call_name(node) in INTROSPECTION_CALLS:
            found.append(f"{where}: {_call_name(node)}()")
        elif isinstance(node, ast.Attribute):
            if _is_client(node.value) and node.attr not in allowed:
                found.append(f"{where}: client.{node.attr}")
            elif node.attr in banned:  # any receiver: aliases of the client too
                found.append(f"{where}: .{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in banned and id(node) not in exempt:
                found.append(f"{where}: {node.value!r}")
        elif isinstance(node, ast.Call) and (lookup := _resolve(node.func, lookups)):
            found += _lookup_violations(node, lookup, where)
    return found


# -- building a client --------------------------------------------------------------------------


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Every node that runs: all but the body of ``if TYPE_CHECKING:`` (its ``else`` runs)."""
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            stack += [node.test, *node.orelse]
        else:
            stack.extend(ast.iter_child_nodes(node))


def _client_import_violations(tree: ast.Module, module: str) -> list[str]:
    """``RehomClient`` used at run time outside api.py; ``TYPE_CHECKING`` not typing's."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "TYPE_CHECKING":
            if not isinstance(node.ctx, ast.Load):
                found.append(f"{module}:{node.lineno}: TYPE_CHECKING rebound")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == "TYPE_CHECKING" and (
                    node.module,
                    alias.name,
                ) != ("typing", "TYPE_CHECKING"):
                    found.append(f"{module}:{node.lineno}: TYPE_CHECKING from {node.module}")
    if module == API_MODULE:
        return found
    for node in _runtime_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] != "aiorehom":
                continue
            for alias in node.names:
                if alias.name in {"RehomClient", "*"}:
                    found.append(f"{module}:{node.lineno}: import {alias.name} at run time")
        elif isinstance(node, ast.Attribute) and node.attr == "RehomClient":
            found.append(f"{module}:{node.lineno}: .RehomClient at run time")
        elif isinstance(node, ast.Constant) and node.value == "RehomClient":
            found.append(f"{module}:{node.lineno}: 'RehomClient' at run time")
    return found


def _constructors(tree: ast.Module) -> list[ast.Call]:
    """Calls that build a ``RehomClient``, through any alias."""
    builders = _aliases(tree, BUILDERS)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _resolve(node.func, builders) == "RehomClient"
    ]


def _splat_violations(tree: ast.Module, module: str) -> list[str]:
    """``**`` keywords in a call that involves a builder (called, or passed along)."""
    builders = _aliases(tree, BUILDERS)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or all(kw.arg is not None for kw in node.keywords):
            continue
        involved = [node.func, *node.args, *(kw.value for kw in node.keywords)]
        names = {name for expr in involved if (name := _resolve(expr, builders))}
        found += [f"{module}:{node.lineno}: **kwargs with {name}" for name in sorted(names)]
    return found


def _allow_writes_uses() -> list[tuple[str, str, ast.expr]]:
    """(module, called name, value) of every ``allow_writes=`` keyword in the integration."""
    uses: list[tuple[str, str, ast.expr]] = []
    for path in SOURCES:
        tree = _parse(path)
        builders = _aliases(tree, BUILDERS)
        uses += [
            (_module(path), _resolve(node.func, builders) or _call_name(node), keyword.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "allow_writes"
        ]
    return uses


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in _parse(path).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    )


def _forbidden_imports(tree: ast.Module) -> list[str]:
    """Library internals a module imports (``import x`` / ``from x import y``)."""
    found: list[str] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [node.module]
            if node.module == "aiorehom":
                modules += [f"aiorehom.{alias.name}" for alias in node.names]
        found += [module for module in modules if module.startswith(FORBIDDEN_MODULES)]
    return found


# -- the integration ----------------------------------------------------------------------------


def test_sources_found() -> None:
    """The check covers the core, control and every platform module."""
    names = {_module(path) for path in SOURCES}
    assert {"__init__.py", "api.py", "coordinator.py", "entity.py", "config_flow.py"} <= names
    assert {"control.py", "diagnostics.py", "issues.py", "repairs.py", "services.py"} <= names
    assert {"climate.py", "fan.py", "number.py", "select.py", "switch.py"} <= names


@pytest.mark.parametrize("path", SOURCES, ids=_module)
def test_client_members(path: Path) -> None:
    """Only allowlisted client members; write members only in control.py; nothing dynamic."""
    assert _violations(_parse(path), _module(path)) == []


def test_writes_called_only_in_control() -> None:
    """control.py calls exactly the allowed write methods; no other module names any."""
    used = {
        _module(path): {attr for _line, attr in _client_attributes(_parse(path))} & LIBRARY_WRITES
        for path in SOURCES
    }
    assert used.pop(CONTROL_MODULE) == CLIENT_WRITE_ALLOWLIST
    assert all(not writes for writes in used.values()), used


def test_library_write_members() -> None:
    """The scanner knows the library's writes; a new one needs a deliberate review here."""
    library = set(LIBRARY_WRITES)
    assert library == CLIENT_WRITE_ALLOWLIST | {"set_comfort_temperature", "set_temporary_comfort"}
    assert all(hasattr(RehomClient, name) for name in BANNED_MEMBERS)
    assert not (CLIENT_ALLOWLIST | CLIENT_WRITE_ALLOWLIST) & (BANNED_MEMBERS | INTERNAL_MEMBERS)
    assert not CLIENT_ALLOWLIST & LIBRARY_WRITES


def test_internal_members_exist() -> None:
    """The internal names are the library's: the client's transport, its raw write and more."""
    client_members = set(dir(RehomClient)) | set(RehomClient.__init__.__code__.co_names)
    assert INTERNAL_MEMBERS - {"post_bulk_update"} <= client_members
    assert callable(ReadOnlyTransport.post_bulk_update)


def test_allowlist_members_exist() -> None:
    """Every allowlisted member is a real client member (the allowlists are not stale)."""
    assert all(hasattr(RehomClient, name) for name in CLIENT_ALLOWLIST | CLIENT_WRITE_ALLOWLIST)


def test_string_exemptions_are_needed() -> None:
    """Each exempted constant exists and holds a banned name (the exemption is not stale)."""
    for module, name in NAMED_STRING_EXEMPTIONS:
        tree = _parse(COMPONENT / module)
        values = [
            node.value.value
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and isinstance(node.value, ast.Constant)
        ]
        assert len(values) == 1, (module, name)
        assert values[0] in _banned(module), (module, name)


def test_allow_writes_only_from_the_option() -> None:
    """``allow_writes=`` twice: create_client forwards its keyword; setup passes the option."""
    uses = sorted(_allow_writes_uses(), key=lambda use: use[0])
    assert [(module, called) for module, called, _value in uses] == [
        ("__init__.py", "create_client"),
        ("api.py", "RehomClient"),
    ]
    by_module = {module: value for module, _called, value in uses}
    forwarded = by_module["api.py"]
    assert isinstance(forwarded, ast.Name)
    assert forwarded.id == "allow_writes"
    assert ast.dump(by_module["__init__.py"]) == OPTION_FLAG


def test_create_client_defaults_to_read_only() -> None:
    """``create_client(..., *, allow_writes: bool = False)``, and the probe never passes it."""
    func = _function(COMPONENT / "api.py", "create_client")
    defaults = dict(zip(func.args.kwonlyargs, func.args.kw_defaults, strict=True))
    (arg, default) = next(iter(defaults.items()))
    assert len(defaults) == 1
    assert arg.arg == "allow_writes"
    assert isinstance(arg.annotation, ast.Name)
    assert arg.annotation.id == "bool"
    assert isinstance(default, ast.Constant)
    assert default.value is False
    assert not func.args.kwarg  # no **kwargs to smuggle it through

    probe = _function(COMPONENT / "api.py", "async_probe")
    calls = [
        node
        for node in ast.walk(probe)
        if isinstance(node, ast.Call) and _call_name(node) == "create_client"
    ]
    assert len(calls) == 1
    assert calls[0].keywords == []


def test_no_literal_true_for_writes() -> None:
    """No ``allow_writes=True`` and no ``allow_writes: bool = True`` anywhere."""
    for path in SOURCES:
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.keyword) and node.arg == "allow_writes":
                assert not isinstance(node.value, ast.Constant), f"{path.name}:{node.lineno}"
            if isinstance(node, ast.arguments):
                params = [*node.posonlyargs, *node.args, *node.kwonlyargs]
                defaults = [*node.defaults, *node.kw_defaults]
                for param in params:
                    if param.arg == "allow_writes":
                        assert not any(
                            isinstance(value, ast.Constant) and value.value is True
                            for value in defaults
                        ), f"{path.name}: allow_writes defaults to True"


@pytest.mark.parametrize("path", SOURCES, ids=_module)
def test_no_splat_to_a_builder(path: Path) -> None:
    """No ``**`` keywords where a builder is called or passed along, through any alias."""
    assert _splat_violations(_parse(path), _module(path)) == []


@pytest.mark.parametrize("path", SOURCES, ids=_module)
def test_no_internal_imports(path: Path) -> None:
    """The integration uses the public aiorehom API only."""
    assert _forbidden_imports(_parse(path)) == []


@pytest.mark.parametrize("path", SOURCES, ids=_module)
def test_client_class_for_types_only(path: Path) -> None:
    """Outside api.py, ``RehomClient`` is imported only under ``if TYPE_CHECKING:``."""
    assert _client_import_violations(_parse(path), _module(path)) == []


def test_client_constructed_only_in_api() -> None:
    """``api.create_client`` is the only place a RehomClient is built, through any alias."""
    builders = {
        _module(path): len(_constructors(_parse(path)))
        for path in SOURCES
        if _constructors(_parse(path))
    }
    assert builders == {API_MODULE: 1}


def test_create_client_called_through_module() -> None:
    """Callers use ``api.create_client`` so the tests' single patch applies everywhere."""
    for path in SOURCES:
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if node.module.rsplit(".", 1)[-1] == "api":
                names = {alias.name for alias in node.names}
                assert not names & {"create_client", "*"}, _module(path)
                if node.level >= 1:
                    pytest.fail(f"{_module(path)}: 'from .api import ...' bypasses the attribute")


# -- the detectors themselves -------------------------------------------------------------------

SAMPLE = """
from aiorehom.transport import ReadOnlyTransport
async def f(runtime, hass, entry):
    await runtime.client.set_zone_offset("001", 1)
    await runtime.client.set_mode(1)
    client = runtime.client
    await client.resync()
    writer = runtime.client
    await writer.set_vmc_mode("001", 8)
    await writer.set_temporary_comfort("001", 60)
    getattr(writer, "set_zone_mode")
    getattr(runtime.client, "state")
    runtime.client.dump()
    await client.connect()
    api.create_client(hass, entry.data, allow_writes=True)
"""

#: The ways around the first version of this check (a platform module with control on).
EVASIONS = """
import operator
from operator import attrgetter as pick
from aiorehom import RehomClient as RC
async def f(self, name, path, records, host):
    c = self.coordinator.client
    await getattr(c, "set_" + name)("001", 1)
    await operator.attrgetter("set_vmc_mode")(c)("001", 8)
    await operator.methodcaller("set_zone_mode", "001", ZoneSetp.COMFORT)(c)
    await c._transport.post_bulk_update(path, records)
    await pick(SERVICE_SET_TEMPORARY_COMFORT)(c)("001", 60)
    RC(host=host, **{"allow_writes": True})
    make = RC
    make(host, **{"allow_writes": True})
    functools.partial(RehomClient, **{"allow_writes": True})
    aiorehom.RehomClient(host)
    hasattr(c, *name)
    rebuild = api.create_client
    rebuild(self.hass, {}, **{"allow_writes": True})
"""


def test_detector_outside_control() -> None:
    """Outside control.py: writes on the client or on an alias, bans and getattr are caught."""
    found = sorted(_violations(ast.parse(SAMPLE), "climate.py"))
    assert found == sorted(
        [
            "climate.py:4: client.set_zone_offset",
            "climate.py:5: client.set_mode",
            "climate.py:7: client.resync",
            "climate.py:9: .set_vmc_mode",
            "climate.py:10: .set_temporary_comfort",
            "climate.py:11: 'set_zone_mode'",
            "climate.py:12: dynamic attribute access on the client",
            "climate.py:13: client.dump",
        ]
    )
    assert _forbidden_imports(ast.parse(SAMPLE)) == ["aiorehom.transport"]


def test_detector_in_control() -> None:
    """In control.py the verified writes are allowed on the client; the rest still is not."""
    found = sorted(_violations(ast.parse(SAMPLE), CONTROL_MODULE))
    assert found == sorted(
        [
            "control.py:5: client.set_mode",
            "control.py:7: client.resync",
            "control.py:10: .set_temporary_comfort",
            "control.py:12: dynamic attribute access on the client",
            "control.py:13: client.dump",
        ]
    )
    # an alias carries an allowed name without being flagged: the alias rule is for other modules
    assert "control.py:9: .set_vmc_mode" not in found


def test_detector_subpackage_is_not_control() -> None:
    """Only the top-level control.py may write: a control.py in a subpackage may not."""
    found = sorted(_violations(ast.parse(SAMPLE), "helpers/control.py"))
    assert "helpers/control.py:4: client.set_zone_offset" in found
    assert len(found) == len(_violations(ast.parse(SAMPLE), "climate.py"))


def test_detector_evasions_outside_control() -> None:
    """Computed names, operator helpers and the private transport are caught."""
    found = sorted(_violations(ast.parse(EVASIONS), "fan.py"))
    assert found == sorted(
        [
            "fan.py:7: getattr() with a computed name",
            "fan.py:8: 'set_vmc_mode'",
            "fan.py:9: 'set_zone_mode'",
            "fan.py:10: ._transport",
            "fan.py:10: .post_bulk_update",
            "fan.py:11: attrgetter() with a computed name",
            "fan.py:17: hasattr() with a computed name",
        ]
    )


def test_detector_evasions_in_control() -> None:
    """control.py may name its verified writes, but never the transport, nor compute a name."""
    found = sorted(_violations(ast.parse(EVASIONS), CONTROL_MODULE))
    assert found == sorted(
        [
            "control.py:7: getattr() with a computed name",
            "control.py:10: ._transport",
            "control.py:10: .post_bulk_update",
            "control.py:11: attrgetter() with a computed name",
            "control.py:17: hasattr() with a computed name",
        ]
    )


INTROSPECTION = """
import inspect
async def f(self, name):
    c = self.coordinator.client
    await c._execute(lambda st: plan(st))
    c.__getattribute__("set_" + name)
    object.__getattribute__(c, "set_" + name)
    inspect.getattr_static(c, "set_" + name)
    type(c).__dict__["set_" + name]
    vars(c)[name]
"""


def test_detector_introspection() -> None:
    """The client's private write path and introspection by computed name are caught."""
    found = sorted(_violations(ast.parse(INTROSPECTION), "fan.py"))
    assert found == sorted(
        [
            "fan.py:5: ._execute",
            "fan.py:6: .__getattribute__",
            "fan.py:7: .__getattribute__",
            "fan.py:8: getattr_static()",
            "fan.py:9: .__dict__",
            "fan.py:10: vars()",
        ]
    )
    assert "control.py:5: ._execute" in _violations(ast.parse(INTROSPECTION), CONTROL_MODULE)


def test_detector_builder_aliases() -> None:
    """An import alias or an assignment of a builder is still a builder."""
    tree = ast.parse(EVASIONS)
    assert sorted(_splat_violations(tree, "fan.py")) == sorted(
        [
            "fan.py:12: **kwargs with RehomClient",
            "fan.py:14: **kwargs with RehomClient",
            "fan.py:15: **kwargs with RehomClient",
            "fan.py:19: **kwargs with create_client",
        ]
    )
    assert sorted(call.lineno for call in _constructors(tree)) == [12, 14, 16]
    assert sorted(_client_import_violations(tree, "fan.py")) == sorted(
        [
            "fan.py:4: import RehomClient at run time",
            "fan.py:16: .RehomClient at run time",
        ]
    )
    assert _client_import_violations(tree, API_MODULE) == []


def test_detector_type_checking_imports() -> None:
    """Imports under ``if TYPE_CHECKING:`` are for types; its ``else`` and functions run."""
    sample = """
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import aiorehom
    from aiorehom import RehomClient
    x: aiorehom.RehomClient
else:
    from aiorehom import RehomClient as Client
def f() -> None:
    from aiorehom.client import RehomClient
    getattr(aiorehom, "RehomClient")
from aiorehom import *
"""
    assert sorted(_client_import_violations(ast.parse(sample), "coordinator.py")) == sorted(
        [
            "coordinator.py:8: import RehomClient at run time",
            "coordinator.py:10: import RehomClient at run time",
            "coordinator.py:11: 'RehomClient' at run time",
            "coordinator.py:12: import * at run time",
        ]
    )


def test_detector_type_checking_rebound() -> None:
    """``TYPE_CHECKING`` must be typing's: rebinding it (or importing another) is caught."""
    sample = """
from typing_extensions import TYPE_CHECKING as TC
from other import TYPE_CHECKING
TYPE_CHECKING = True
if TYPE_CHECKING:
    from aiorehom import RehomClient
"""
    assert sorted(_client_import_violations(ast.parse(sample), "entity.py")) == sorted(
        [
            "entity.py:3: TYPE_CHECKING from other",
            "entity.py:4: TYPE_CHECKING rebound",
        ]
    )
    assert sorted(_client_import_violations(ast.parse(sample), API_MODULE)) == [
        "api.py:3: TYPE_CHECKING from other",
        "api.py:4: TYPE_CHECKING rebound",
    ]


def test_detector_string_exemption() -> None:
    """The exemption covers only the named module-level constant, in its own module."""
    sample = """
SERVICE_SET_TEMPORARY_COMFORT: Final = "set_temporary_comfort"
OTHER = "set_temporary_comfort"
"""
    assert _violations(ast.parse(sample), "const.py") == ["const.py:3: 'set_temporary_comfort'"]
    assert sorted(_violations(ast.parse(sample), "services.py")) == [
        "services.py:2: 'set_temporary_comfort'",
        "services.py:3: 'set_temporary_comfort'",
    ]


def test_sources_include_subpackages(tmp_path: Path) -> None:
    """A module in a subpackage is scanned too."""
    (tmp_path / "helpers").mkdir()
    (tmp_path / "helpers" / "x.py").write_text("", "utf-8")
    (tmp_path / "a.py").write_text("", "utf-8")
    (tmp_path / "strings.json").write_text("{}", "utf-8")
    found = [path.relative_to(tmp_path).as_posix() for path in _sources(tmp_path)]
    assert found == ["a.py", "helpers/x.py"]


def test_detector_allow_writes() -> None:
    """A literal ``allow_writes=True`` is not the option expression."""
    call = next(
        node
        for node in ast.walk(ast.parse(SAMPLE))
        if isinstance(node, ast.Call) and _call_name(node) == "create_client"
    )
    (keyword,) = call.keywords
    assert isinstance(keyword.value, ast.Constant)
    assert ast.dump(keyword.value) != OPTION_FLAG
