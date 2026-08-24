"""Cross-instance propagation of CRDT operations.

Redis pub/sub carries operations between server processes. Postgres remains the
source of truth -- the bus is a *live delivery* layer and nothing more.

What travels over Redis: **the same operation dicts that travel over the
WebSocket**, wrapped in a thin envelope. That choice is worth defending, because
Redis pub/sub is a genuinely weak channel: fire-and-forget, at-most-once, no
replay, no ordering guarantee between publishers, and anything published while a
subscriber is disconnected is gone forever.

An op-based CRDT is the ideal payload for exactly that channel. Operations are
commutative, so unordered delivery is fine; idempotent, so duplicate delivery is
fine; and each one is self-contained, so a subscriber needs no prior context. If
we were shipping *state* we'd need ordering the channel can't give us, and would
have to fall back on "something changed, go re-read Postgres" -- an extra round
trip per edit, and a thundering herd on every keystroke.

The dropped-message case is survivable for the same reason Stage 2 was: Postgres
has every operation, so a reconnect or a cold load heals any gap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger("converge.bus")

CHANNEL_PREFIX = "converge:ops:"

# redis-py needs at least one subscription before get_message() has a connection
# to read from, and an instance starts with no documents open. Subscribing to
# this keeps the pubsub socket alive; nothing is ever published to it.
KEEPALIVE_CHANNEL = "converge:keepalive"

DEFAULT_REDIS_URL = "redis://localhost:6379"


def redis_url() -> str | None:
    """None means single-instance mode: no bus, no cross-instance propagation."""
    return os.environ.get("REDIS_URL") or None


def channel_for(doc_id: str) -> str:
    return f"{CHANNEL_PREFIX}{doc_id}"


@dataclass(frozen=True)
class OpMessage:
    """One batch of operations, as it crosses between instances."""

    doc_id: str
    ops: list[dict[str, Any]]

    origin: str
    """Publishing instance id. The receiving instance drops its own messages by
    comparing against this -- see Hub.apply_remote_ops."""

    author: str
    """Client that made the edit. Carried so the receiving instance can avoid
    echoing to it, in the corner case where that client is also connected here."""

    published_at: float
    """`time.time()` on the publisher, for measuring propagation delay. Wall
    clock, so it is only meaningful between instances whose clocks agree -- fine
    on one machine, needs care across hosts. The defensible end-to-end number
    comes from scripts/latency_probe.py, which uses a single clock."""

    def to_json(self) -> str:
        return json.dumps(
            {
                "doc_id": self.doc_id,
                "ops": self.ops,
                "origin": self.origin,
                "author": self.author,
                "published_at": self.published_at,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> OpMessage:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("bus message must be an object")
        ops = data.get("ops")
        if not isinstance(ops, list):
            raise ValueError("bus message needs an ops list")
        return cls(
            doc_id=str(data["doc_id"]),
            ops=ops,
            origin=str(data["origin"]),
            author=str(data.get("author", "")),
            published_at=float(data.get("published_at", 0.0)),
        )

    @classmethod
    def create(cls, doc_id: str, ops: list[dict[str, Any]], origin: str, author: str) -> OpMessage:
        return cls(doc_id, ops, origin, author, time.time())


Handler = Callable[[OpMessage], Awaitable[None]]


class Bus(Protocol):
    """Subscribe per document, not globally: an instance shouldn't be woken by
    traffic for documents it isn't serving."""

    def on_message(self, handler: Handler) -> None: ...

    async def subscribe(self, doc_id: str) -> None: ...

    async def unsubscribe(self, doc_id: str) -> None: ...

    async def publish(self, message: OpMessage) -> None: ...

    async def close(self) -> None: ...


class NullBus:
    """Single-instance mode. Publishing goes nowhere, which is correct when there
    is nowhere else for it to go."""

    def on_message(self, handler: Handler) -> None:
        return None

    async def subscribe(self, doc_id: str) -> None:
        return None

    async def unsubscribe(self, doc_id: str) -> None:
        return None

    async def publish(self, message: OpMessage) -> None:
        return None

    async def close(self) -> None:
        return None


class RedisBus:
    def __init__(self, url: str) -> None:
        from redis.asyncio import Redis  # imported here so tests don't need redis

        self._client = Redis.from_url(url, decode_responses=True)
        self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        self._handler: Handler | None = None
        self._channels: set[str] = set()
        self._reader: asyncio.Task[None] | None = None
        self._closed = False

    def on_message(self, handler: Handler) -> None:
        self._handler = handler

    async def start(self) -> None:
        await self._pubsub.subscribe(KEEPALIVE_CHANNEL)
        self._reader = asyncio.create_task(self._read_loop(), name="converge-bus-reader")
        log.info("bus started")

    async def subscribe(self, doc_id: str) -> None:
        channel = channel_for(doc_id)
        if channel in self._channels:
            return  # idempotent: many clients per document, one subscription
        self._channels.add(channel)
        await self._pubsub.subscribe(channel)
        log.info("subscribed %s", channel)

    async def unsubscribe(self, doc_id: str) -> None:
        channel = channel_for(doc_id)
        if channel not in self._channels:
            return
        self._channels.discard(channel)
        await self._pubsub.unsubscribe(channel)
        log.info("unsubscribed %s", channel)

    async def publish(self, message: OpMessage) -> None:
        await self._client.publish(channel_for(message.doc_id), message.to_json())

    async def _read_loop(self) -> None:
        """One task draining the pubsub connection for the whole process.

        Deliberately never lets an exception kill the loop: a malformed message,
        or a handler that raises on one document, must not stop delivery for
        every other document on this instance.
        """
        while not self._closed:
            try:
                raw = await self._pubsub.get_message(timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("bus read failed; retrying")
                await asyncio.sleep(0.5)
                continue

            if raw is None or raw.get("type") != "message":
                continue

            try:
                message = OpMessage.from_json(raw["data"])
            except (ValueError, KeyError, TypeError):
                log.warning("dropping unparseable bus message: %r", raw.get("data"))
                continue

            if self._handler is None:
                continue
            try:
                await self._handler(message)
            except Exception:
                log.exception("bus handler failed for doc=%s", message.doc_id)

    async def close(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        for closer in (self._pubsub, self._client):
            aclose = getattr(closer, "aclose", None)
            if aclose is not None:
                await aclose()
            else:  # pragma: no cover - older redis-py
                await closer.close()


class InProcessCluster:
    """A stand-in for Redis that delivers between buses in one process.

    Lets the tests run two Hubs as two "instances" with no Redis and no sockets.
    Publishing delivers to *every* bus including the publisher's own, on purpose:
    that way the loop-prevention path (client -> A -> bus -> A) is exercised by
    the tests rather than only in production.
    """

    def __init__(self) -> None:
        self._buses: list[InProcessBus] = []
        self.published: list[OpMessage] = []

    def bus(self) -> InProcessBus:
        bus = InProcessBus(self)
        self._buses.append(bus)
        return bus

    async def deliver(self, message: OpMessage) -> None:
        self.published.append(message)
        for bus in list(self._buses):
            await bus.receive(message)


class InProcessBus:
    def __init__(self, cluster: InProcessCluster) -> None:
        self._cluster = cluster
        self._handler: Handler | None = None
        self._docs: set[str] = set()

    def on_message(self, handler: Handler) -> None:
        self._handler = handler

    async def subscribe(self, doc_id: str) -> None:
        self._docs.add(doc_id)

    async def unsubscribe(self, doc_id: str) -> None:
        self._docs.discard(doc_id)

    async def publish(self, message: OpMessage) -> None:
        await self._cluster.deliver(message)

    async def receive(self, message: OpMessage) -> None:
        # Mirrors Redis: an instance only hears about documents it subscribed to.
        if message.doc_id not in self._docs or self._handler is None:
            return
        await self._handler(message)

    async def close(self) -> None:
        self._docs.clear()
