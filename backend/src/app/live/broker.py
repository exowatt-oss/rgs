"""In-process live event fan-out (design notes).

The app is one uvicorn process (pipeline worker in-process, design notes/0098),
so live fan-out is an in-memory pub/sub on the event loop; the durable
`live_events` table is the source of truth for replay (`since_seq`).
**Multi-replica note:** with more than one app process, publishers would
need a shared bus (the app already declares RabbitMQ — a per-session fanout
exchange) and the audio WebSocket for a session would need sticky routing to
the process running its transcription loop.

Publishing is thread-safe: calls from worker threads (sync routes, MCP tool
bodies) are marshalled onto the bound loop with `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)

SUBSCRIBER_QUEUE_MAX = 2000


class Subscription:
    def __init__(self, broker: LiveBroker, session_id: UUID) -> None:
        self.broker = broker
        self.session_id = session_id
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self.overflowed = False

    def offer(self, event: dict[str, Any]) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.overflowed = True

    async def get(self) -> dict[str, Any]:
        return await self.queue.get()

    def close(self) -> None:
        self.broker.unsubscribe(self)


class LiveBroker:
    def __init__(self) -> None:
        self._subs: dict[UUID, set[Subscription]] = defaultdict(set)
        self._waiters: dict[UUID, set[asyncio.Future[None]]] = defaultdict(set)
        self._interim: dict[UUID, dict[str, Any] | None] = {}
        self._gate_tick: dict[UUID, dict[str, Any]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._listeners: list[Callable[[UUID, dict[str, Any]], None]] = []

    def add_listener(self, fn: Callable[[UUID, dict[str, Any]], None]) -> None:
        """Observe every delivered event on the loop thread (errors swallowed)."""
        if fn not in self._listeners:
            self._listeners.append(fn)

    # ---- loop binding ------------------------------------------------------

    def bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._loop_thread = threading.get_ident()
            # Futures/queues from a previous (closed) loop are unusable.
            self._subs.clear()
            self._waiters.clear()

    def _on_loop(self) -> bool:
        return self._loop is not None and threading.get_ident() == self._loop_thread

    # ---- subscribe ---------------------------------------------------------

    def subscribe(self, session_id: UUID) -> Subscription:
        self.bind_loop()
        sub = Subscription(self, session_id)
        self._subs[session_id].add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.session_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                self._subs.pop(sub.session_id, None)

    def subscriber_count(self, session_id: UUID) -> int:
        return len(self._subs.get(session_id, ()))

    async def wait(self, session_id: UUID, timeout: float) -> bool:
        """Wait until any durable event is published for the session."""
        self.bind_loop()
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters[session_id].add(fut)
        try:
            await asyncio.wait_for(fut, timeout=timeout)
            return True
        except TimeoutError:
            return False
        finally:
            waiters = self._waiters.get(session_id)
            if waiters is not None:
                waiters.discard(fut)
                if not waiters:
                    self._waiters.pop(session_id, None)

    # ---- publish -----------------------------------------------------------

    def interim(self, session_id: UUID) -> dict[str, Any] | None:
        return self._interim.get(session_id)

    def gate_tick(self, session_id: UUID) -> dict[str, Any] | None:
        """The latest ephemeral `gate.tick` payload (with its `ts`), for snapshots/long-poll."""
        return self._gate_tick.get(session_id)

    def clear(self, session_id: UUID) -> None:
        self._interim.pop(session_id, None)
        self._gate_tick.pop(session_id, None)

    def publish(self, session_id: UUID, event: dict[str, Any] | None) -> None:
        if event is None or self._loop is None:
            return
        if self._on_loop():
            self._deliver(session_id, event)
            return
        if self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._deliver, session_id, event)
        except RuntimeError:  # loop closed between the check and the call
            pass

    def _deliver(self, session_id: UUID, event: dict[str, Any]) -> None:
        if event.get("type") == "segment.interim":
            self._interim[session_id] = event.get("data")
        elif event.get("type") == "gate.tick":
            self._gate_tick[session_id] = {**(event.get("data") or {}), "ts": event.get("ts")}
        elif event.get("type") == "segment.final":
            # A final supersedes whatever interim it was built from; the next
            # interim (if any) follows immediately from the same tick.
            pass
        for listener in list(self._listeners):
            try:
                listener(session_id, event)
            except Exception:  # noqa: BLE001 — observers never break delivery
                log.exception("live.broker_listener_failed")
        for sub in list(self._subs.get(session_id, ())):
            sub.offer(event)
        if event.get("seq") is not None:
            for fut in list(self._waiters.get(session_id, ())):
                if not fut.done():
                    fut.set_result(None)


broker = LiveBroker()
