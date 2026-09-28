"""Replay harness: a real RehomClient on the sanitised recording shipped with aiorehom.

Every client the integration creates (config flow probe or entry setup) is a
real ``aiorehom.RehomClient`` on a ``ReplayTransport``/``ReplayConnection`` of
one shared :class:`~aiorehom.replay.ReplayDevice`, with a shared
:class:`~aiorehom.clock.VirtualClock`.  :meth:`RehomHarness.advance_to` moves
the virtual clock (delivering every recorded frame on the way), then Home
Assistant's frozen clock, fires HA timers and waits for HA to settle.  Both
clocks therefore read the same instant, which is what the repair "grace"
rules (5/15 min) rely on.

Writes: a client created with ``allow_writes=True`` (the entry with "Enable
control" on) gets a :class:`WritableTransport`.  Its ``post_bulk_update`` runs
the body through the library's own write gate (``check_write_body``), records
it in :attr:`RehomHarness.writes`, and then behaves like the live controller:
with :attr:`RehomHarness.apply` the written values show in every later REST
snapshot, and with :attr:`RehomHarness.echo` the controller's WebSocket echo
arrives before the POST returns, so the write is confirmed at once.  With both
off the write is never confirmed; :meth:`RehomHarness.run_until_done` moves
time until such a call gives up.  A read-only client's transport has no write
method at all.

Nothing here opens a socket (PHACC's pytest-socket also blocks them).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiorehom import ClientOptions, RehomClient, RehomConnectionError
from aiorehom.clock import VirtualClock
from aiorehom.replay import (
    DEFAULT_LATENCY,
    ReplayData,
    ReplayDevice,
    ReplayDriver,
    ReplayTransport,
)
from aiorehom.sync import WsConnection, WsConnector
from aiorehom.transport import check_write_body
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

#: The only recording the tests use: the sanitised fixture that ships with aiorehom
#: (CI checks the library out into lib/aiorehom).
FIXTURE_DIR = (
    Path(__file__).resolve().parents[1]
    / "lib"
    / "aiorehom"
    / "tests"
    / "fixtures"
    / "20260925T102117Z"
)
#: Values of the sanitised fixture (pseudonymised MAC, zone and VMC ids).
FIXTURE_MAC = "02:00:00:31:7a:5a"
FIXTURE_ZONES = ("001", "002", "003", "009", "010", "011")
FIXTURE_VMCS = ("001", "002")
#: Replay start = the capture's WS connect event.
FIXTURE_START = datetime(2026, 9, 25, 10, 22, 6, 806000, tzinfo=UTC)

#: Zero virtual latency: ``connect()`` completes without advancing the clock.
ZERO_LATENCY: Mapping[str, float] = dict.fromkeys(DEFAULT_LATENCY, 0.0)

Frame = Mapping[str, Any]
#: ``(Gruppo, Unita, SubUni, Key)`` of a record.
RecordKey = tuple[str, str, str, str]
#: One POST the harness accepted: (bulk-write path, records as the write gate rebuilt them).
Write = tuple[str, list[dict[str, object]]]


def T(hms: str) -> datetime:
    """2026-09-25 ``hh:mm:ss[.ffffff]`` UTC (the capture day)."""
    return datetime.fromisoformat(f"2026-09-25T{hms}+00:00")


def termo_update(path: str, value: str) -> dict[str, Any]:
    """A synthetic ``termo`` update frame (``path`` = ``G.U.S.K``)."""
    return {
        "domain": "termo",
        "type": "update",
        "value": value,
        "gruppo": path.split(".", 1)[0],
        "path": path,
    }


def bus_update(key: str, value: str) -> dict[str, Any]:
    """A synthetic plant-conf (``bus``) update frame."""
    return {"domain": "bus", "type": "update", "key": key, "value": value}


def set_values(
    values: Mapping[str, str], *, extra_frames: Sequence[tuple[datetime, Frame]] = ()
) -> dict[str, Any]:
    """A ``device_patch`` that sets interface values in the snapshot (``path`` -> ``Valore``).

    ``path`` is the record's ``G.U.S.K`` path as the fixture spells it
    (``"REHOM...MODO"``, ``"ZONA.001..DELTA_SETP_CORRENTE"``).  A path the fixture
    does not have fails the test (``KeyError``), so a typo cannot pass silently.
    """
    wanted = dict(values)

    def patch(data: ReplayData) -> None:
        found: set[str] = set()
        for row in data.interface:
            path = row.get("path")
            if isinstance(path, str) and path in wanted:
                row["Valore"] = wanted[path]
                found.add(path)
        if missing := sorted(set(wanted) - found):
            raise KeyError(f"not in the fixture: {missing}")

    device_patch: dict[str, Any] = {"patch": patch}
    if extra_frames:
        device_patch["extra_frames"] = list(extra_frames)
    return device_patch


def record_path(record: Mapping[str, object]) -> str:
    """``G.U.S.K`` of a written record (interface records carry it; overrides do not)."""
    return (
        f"{record['Gruppo']}.{record.get('Unita', '')}.{record.get('SubUni', '')}.{record['Key']}"
    )


def load_device(
    *,
    extra_frames: Sequence[tuple[datetime, Frame]] = (),
    patch: Callable[[ReplayData], None] | None = None,
) -> ReplayDevice:
    """The fixture as a ReplayDevice, optionally patched and with synthetic frames.

    Synthetic frames become part of the device history, so REST resyncs see
    them too (``ReplayDevice.state_at``), exactly like recorded frames.
    """
    base = ReplayDevice.from_dir(FIXTURE_DIR, patch=patch)
    if not extra_frames:
        return base
    frames = sorted([*base.frames, *extra_frames], key=lambda item: item[0])
    end = max(base.end, *(when for when, _frame in extra_frames))
    return ReplayDevice(base.data, frames, start=base.start, end=end)


class FaultyTransport:
    """Wraps a ReplayTransport; methods named in ``harness.errors`` raise instead.

    REST snapshots also show every value a write applied (:attr:`RehomHarness.apply`),
    for every client: the device keeps what was written.
    """

    def __init__(self, inner: ReplayTransport, harness: RehomHarness) -> None:
        self._inner = inner
        self._harness = harness
        self.errors = harness.errors

    @property
    def has_token(self) -> bool:
        return self._inner.has_token

    def _check(self, name: str) -> None:
        if (err := self.errors.get(name)) is not None:
            raise err

    async def login(self, username: str, password: str) -> None:
        self._check("login")
        await self._inner.login(username, password)

    async def get_alive(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        self._check("get_alive")
        return await self._inner.get_alive(timeout=timeout)

    async def get_config(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        self._check("get_config")
        return await self._inner.get_config(timeout=timeout)

    async def get_interface(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        self._check("get_interface")
        return self._harness.applied_interface(await self._inner.get_interface(timeout=timeout))

    async def get_overrides(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        self._check("get_overrides")
        return self._harness.applied_overrides(await self._inner.get_overrides(timeout=timeout))

    async def get_plant_conf(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        self._check("get_plant_conf")
        return await self._inner.get_plant_conf(timeout=timeout)

    async def get_history(
        self,
        key: str,
        unita: str,
        gte: str,
        lte: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Any:
        self._check("get_history")
        return await self._inner.get_history(key, unita, gte, lte, timeout=timeout)

    async def close(self) -> None:
        await self._inner.close()


class WritableTransport(FaultyTransport):
    """A FaultyTransport that also accepts bulk writes (:meth:`RehomHarness.post_bulk_update`)."""

    async def post_bulk_update(
        self,
        path: str,
        records: Any,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> int:
        return await self._harness.post_bulk_update(path, records)


class RehomHarness:
    """One replayed controller shared by every client the integration creates."""

    def __init__(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        device: ReplayDevice,
        *,
        options: ClientOptions | None = None,
    ) -> None:
        self.hass = hass
        self.freezer = freezer
        self.device = device
        self.clock = VirtualClock(device.start)
        self.driver = ReplayDriver(device, self.clock)
        self.options = options
        #: Transport method -> exception, applied to every client (current and future).
        self.errors: dict[str, BaseException] = {}
        #: Every client created through :meth:`create_client`, in order.
        self.clients: list[RehomClient] = []
        #: ``data`` passed to each :meth:`create_client` call (config flow input, entry data).
        self.client_data: list[dict[str, Any]] = []
        #: ``allow_writes`` passed to each :meth:`create_client` call, in order.
        self.allow_writes: list[bool] = []
        #: While True, WebSocket (re)connects fail (see :meth:`block_ws`).
        self.ws_blocked = False
        #: Every POST that passed the write gate, in order (any client).
        self.writes: list[Write] = []
        #: Written values show in later REST snapshots (``get_interface``/``get_overrides``).
        self.apply = True
        #: The controller echoes each written value on the WebSocket before the POST returns.
        self.echo = True
        self._interface_applied: dict[RecordKey, str] = {}
        self._overrides_applied: dict[tuple[str, str, str], dict[str, Any]] = {}
        freezer.move_to(device.start)

    # -- the patched api.create_client ----------------------------------------

    def create_client(
        self, hass: HomeAssistant, data: Mapping[str, Any], *, allow_writes: bool = False
    ) -> RehomClient:
        """Replacement for ``custom_components.rehom.api.create_client``."""
        self.client_data.append(dict(data))
        self.allow_writes.append(allow_writes)
        inner = self.device.transport(self.clock, latency=ZERO_LATENCY)
        transport = (
            WritableTransport(inner, self) if allow_writes is True else FaultyTransport(inner, self)
        )
        client = RehomClient(
            data[CONF_HOST],  # validated like production (ValueError on a bad host)
            port=int(data[CONF_PORT]),
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            transport=transport,
            ws_connector=self._ws_connector(),
            clock=self.clock,
            options=self.options,
            allow_writes=allow_writes,  # TypeError unless a bool, like production
        )
        self.clients.append(client)
        return client

    # -- writes ----------------------------------------------------------------

    async def post_bulk_update(self, path: str, records: Any) -> int:
        """What the replayed controller does with a bulk write (every WritableTransport).

        ``errors["post_bulk_update"]`` raises first (nothing recorded).  Then the
        call is logged in ``transport_calls``, the body goes through the library's
        write gate (``ForbiddenRequestError`` for a body the live transport would
        refuse) and is recorded in :attr:`writes`; the values are applied to later
        REST snapshots (:attr:`apply`) and echoed on the open WebSocket
        (:attr:`echo`), both before the POST returns, as the live controller does.
        In REST snapshots an applied value wins over the recording for good; recorded
        frames that change the same record later still arrive on the WebSocket.
        """
        if (err := self.errors.get("post_bulk_update")) is not None:
            raise err
        self.device.transport_calls.append("post_bulk_update")
        body = check_write_body(path, records)
        self.writes.append((path, body))
        for record in body:
            value = str(record["Valore"])
            key = (
                str(record["Gruppo"]),
                str(record["Unita"]),
                str(record["SubUni"]),
                str(record["Key"]),
            )
            is_override = record["Gruppo"] == "PROG_OVERRIDE"
            if self.apply:
                if is_override:
                    self._overrides_applied[key[1:]] = {
                        name: record[name]
                        for name in (
                            "Gruppo",
                            "Unita",
                            "SubUni",
                            "Key",
                            "Valore",
                            "Impostazione",
                            "Scadenza",
                        )
                    }
                else:
                    self._interface_applied[key] = value
            if self.echo:
                frame = termo_update(record_path(record), value)
                if is_override:
                    frame |= {"issuedAt": record["Impostazione"], "expiresAt": record["Scadenza"]}
                self.device.deliver(frame)  # dropped while no WebSocket is open
        await self.clock.settle()  # the client reads the echo before the POST returns
        return 204

    def applied_interface(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``/interface/`` rows with the applied writes (new copies; ``rows`` is untouched)."""
        if not self._interface_applied:
            return rows
        applied = self._interface_applied
        out: list[dict[str, Any]] = []
        for row in rows:
            key = (row.get("Gruppo"), row.get("Unita"), row.get("SubUni"), row.get("Key"))
            value = applied.get(key)
            out.append(row if value is None else {**row, "Valore": value})
        return out

    def applied_overrides(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``/overrides/`` rows with the applied override writes (one row per path)."""
        if not self._overrides_applied:
            return rows
        applied = dict(self._overrides_applied)
        out: list[dict[str, Any]] = []
        for row in rows:
            key = (row.get("Unita"), row.get("SubUni"), row.get("Key"))
            written = applied.pop(key, None)
            out.append(row if written is None else dict(written))
        out.extend(dict(row) for row in applied.values())
        return out

    @property
    def written_values(self) -> list[dict[str, str]]:
        """Each accepted write as ``{G.U.S.K path: value}`` (for concise assertions)."""
        return [
            {record_path(record): str(record["Valore"]) for record in records}
            for _path, records in self.writes
        ]

    def _ws_connector(self) -> WsConnector:
        inner = self.device.ws_connector(self.clock)

        async def connect() -> WsConnection:
            if self.ws_blocked:
                raise RehomConnectionError("replay: WebSocket blocked by the test")
            return await inner()

        return connect

    def block_ws(self) -> None:
        """Drop the live WebSocket and refuse reconnects (the client goes DEGRADED)."""
        self.ws_blocked = True
        self.device.disconnect()

    def unblock_ws(self) -> None:
        """Allow WebSocket reconnects again (the client reconnects after its backoff)."""
        self.ws_blocked = False

    @property
    def client(self) -> RehomClient:
        """The most recently created client (normally the entry's)."""
        return self.clients[-1]

    @property
    def transport_calls(self) -> list[str]:
        """Every transport method called on the device so far (a copy)."""
        return list(self.device.transport_calls)

    # -- time ------------------------------------------------------------------

    @property
    def now(self) -> datetime:
        """The shared instant (virtual clock == HA frozen clock)."""
        return self.clock.utcnow()

    async def advance_to(self, when: datetime) -> None:
        """Replay every frame up to ``when``, then move HA to ``when`` and settle.

        Steps longer than 60 s are split into 60-s steps, so HA's periodic
        timers (e.g. the 60-s issue check) run at their own instants.
        """
        while self.clock.utcnow() < when:
            step = min(when, self.clock.utcnow() + timedelta(seconds=60))
            await self.driver.run_until(step)
            self.freezer.move_to(step)
            async_fire_time_changed(self.hass, step)
            await self.hass.async_block_till_done()
        await self.clock.settle()
        await self.hass.async_block_till_done()

    async def advance(self, seconds: float) -> None:
        """Advance by ``seconds`` (see :meth:`advance_to`)."""
        await self.advance_to(self.clock.utcnow() + timedelta(seconds=seconds))

    async def settle(self) -> None:
        """Let queued library and HA work run without moving time."""
        await self.clock.settle()
        await self.hass.async_block_till_done()
        await asyncio.sleep(0)

    async def run_until_done[T](
        self,
        awaitable: Awaitable[T],
        *,
        limit: timedelta = timedelta(minutes=4),
        step: float = 1.0,
        hass_action: bool = False,
    ) -> T:
        """Await ``awaitable`` while moving both clocks ``step`` seconds at a time.

        For calls that wait on the virtual clock, such as a write that is not
        confirmed (the echo window, then one resync).  Returns its result or
        raises its exception; fails the test if it is still running after ``limit``.

        ``hass_action=True`` is required when ``awaitable`` is a blocking Home
        Assistant action call (``hass.services.async_call(..., blocking=True)``):
        Home Assistant runs the entity action in a task it tracks, so the steps
        must not wait for Home Assistant's tasks (``async_block_till_done`` would
        wait for the action, which waits for the virtual clock: a deadlock).
        Home Assistant's timers still fire at their instants.
        """
        task = asyncio.ensure_future(awaitable)
        deadline = self.now + limit
        if hass_action:
            await self.clock.settle()
        else:
            await self.settle()
        while not task.done():
            if self.now >= deadline:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise AssertionError(f"still running after {limit}")
            if hass_action:
                await self._step_without_waiting_for_hass(step)
            else:
                await self.advance(step)
        if hass_action:
            await self.hass.async_block_till_done()  # the action is done: safe now
        return task.result()

    async def _step_without_waiting_for_hass(self, seconds: float) -> None:
        """Move both clocks ``seconds`` and fire HA timers, without ``async_block_till_done``."""
        when = self.now + timedelta(seconds=seconds)
        await self.driver.run_until(when)
        self.freezer.move_to(when)
        async_fire_time_changed(self.hass, when)
        await self.clock.settle()
