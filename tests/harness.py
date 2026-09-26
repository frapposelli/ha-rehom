"""Replay harness: a real RehomClient on the sanitised recording shipped with aiorehom.

Every client the integration creates (config flow probe or entry setup) is a
real ``aiorehom.RehomClient`` on a ``ReplayTransport``/``ReplayConnection`` of
one shared :class:`~aiorehom.replay.ReplayDevice`, with a shared
:class:`~aiorehom.clock.VirtualClock`.  :meth:`RehomHarness.advance_to` moves
the virtual clock (delivering every recorded frame on the way), then Home
Assistant's frozen clock, fires HA timers and waits for HA to settle.  Both
clocks therefore read the same instant, which is what the repair "grace"
rules (5/15 min) rely on.

Nothing here opens a socket (PHACC's pytest-socket also blocks them).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
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
    """Wraps a ReplayTransport; methods named in ``errors`` raise instead."""

    def __init__(self, inner: ReplayTransport, errors: dict[str, BaseException]) -> None:
        self._inner = inner
        self.errors = errors

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
        return await self._inner.get_interface(timeout=timeout)

    async def get_overrides(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        self._check("get_overrides")
        return await self._inner.get_overrides(timeout=timeout)

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
        #: While True, WebSocket (re)connects fail (see :meth:`block_ws`).
        self.ws_blocked = False
        freezer.move_to(device.start)

    # -- the patched api.create_client ----------------------------------------

    def create_client(self, hass: HomeAssistant, data: Mapping[str, Any]) -> RehomClient:
        """Replacement for ``custom_components.rehom.api.create_client``."""
        self.client_data.append(dict(data))
        transport = FaultyTransport(
            self.device.transport(self.clock, latency=ZERO_LATENCY), self.errors
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
        )
        self.clients.append(client)
        return client

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
