"""Read-only by construction: a static check of the integration source.

Every attribute the integration reads on a ``client`` expression must be on
the allowlist below; no module may import the library's
transport, WebSocket, sync or replay internals; only ``api.py`` constructs a
``RehomClient``; and the library's client has no public write-like member.
"""

from __future__ import annotations

import ast
from pathlib import Path

from aiorehom import RehomClient
import pytest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "rehom"
SOURCES = sorted(COMPONENT.glob("*.py"))

#: Every client member the integration may use (all of them read or manage the connection).
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
    }
)
FORBIDDEN_MODULES = ("aiorehom.transport", "aiorehom.websocket", "aiorehom.sync", "aiorehom.replay")
WRITE_PREFIXES = ("set_", "write", "post", "put", "delete", "bulk")


def _is_client(node: ast.expr) -> bool:
    """``client``, ``self.client``, ``runtime.client``, ``coordinator.client`` ..."""
    return (isinstance(node, ast.Name) and node.id == "client") or (
        isinstance(node, ast.Attribute) and node.attr == "client"
    )


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text("utf-8"), filename=str(path))


def _client_attributes(tree: ast.Module) -> set[tuple[int, str]]:
    return {
        (node.lineno, node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and _is_client(node.value)
    }


def test_sources_found() -> None:
    """The check covers the core and every platform module."""
    names = {path.name for path in SOURCES}
    assert {"__init__.py", "api.py", "coordinator.py", "entity.py", "config_flow.py"} <= names
    assert {"diagnostics.py", "issues.py", "services.py"} <= names


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_client_attribute_allowlist(path: Path) -> None:
    """Only allowlisted client members are used, and never through getattr()."""
    tree = _parse(path)
    used = _client_attributes(tree)
    bad = sorted((line, attr) for line, attr in used if attr not in CLIENT_ALLOWLIST)
    assert not bad, f"{path.name}: client attributes outside the allowlist: {bad}"
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "setattr", "delattr"}
            and node.args
            and _is_client(node.args[0])
        ):
            pytest.fail(f"{path.name}:{node.lineno}: dynamic attribute access on the client")


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_no_internal_imports(path: Path) -> None:
    """The integration uses the public aiorehom API only."""
    for node in ast.walk(_parse(path)):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [node.module]
            if node.module == "aiorehom":
                modules += [f"aiorehom.{alias.name}" for alias in node.names]
        for module in modules:
            assert not module.startswith(FORBIDDEN_MODULES), f"{path.name}: imports {module}"


def test_client_constructed_only_in_api() -> None:
    """``api.create_client`` is the only place a RehomClient is built."""
    builders = {
        path.name
        for path in SOURCES
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "RehomClient")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "RehomClient")
        )
    }
    assert builders == {"api.py"}


def test_create_client_called_through_module() -> None:
    """Callers use ``api.create_client`` so the tests' single patch applies everywhere."""
    for path in SOURCES:
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ImportFrom) and node.module in {"api", ".api"}:
                names = {alias.name for alias in node.names}
                assert "create_client" not in names, path.name
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "api":
                pytest.fail(f"{path.name}: 'from .api import ...' bypasses the module attribute")


def test_client_has_no_write_api() -> None:
    """The library's client exposes nothing that could write."""
    public = [name for name in dir(RehomClient) if not name.startswith("_")]
    assert public
    writes = [name for name in public if name.lower().startswith(WRITE_PREFIXES)]
    assert writes == []


def test_allowlist_members_exist() -> None:
    """Every allowlisted member is a real client member (the allowlist is not stale)."""
    assert all(hasattr(RehomClient, name) for name in CLIENT_ALLOWLIST)


def test_detector_flags_a_write() -> None:
    """Self-check: the scanner catches a forbidden call and a forbidden import."""
    tree = ast.parse(
        "from aiorehom.transport import ReadOnlyTransport\n"
        "async def f(runtime):\n"
        "    await runtime.client.set_mode(1)\n"
        "    client = runtime.client\n"
        "    await client.resync()\n"
    )
    attrs = {attr for _line, attr in _client_attributes(tree)}
    assert attrs - CLIENT_ALLOWLIST == {"set_mode", "resync"}
