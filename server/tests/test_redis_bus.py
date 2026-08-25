"""Integration tests for RedisBus, against a real Redis.

Skipped when Redis isn't reachable -- see conftest.py. The cross-instance
*semantics* are covered by test_cluster.py with an in-process bus; these check
that the actual Redis wiring behaves the same way.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.bus import KEEPALIVE_CHANNEL, OpMessage, channel_for
from app.crdt import RGA
from app.hub import Hub
from app.store import InMemoryOpStore


class Inbox:
    """Collects messages a bus delivers, with a helper to wait for them."""

    def __init__(self) -> None:
        self.messages: list[OpMessage] = []

    async def __call__(self, message: OpMessage) -> None:
        self.messages.append(message)

    async def wait_for(self, count: int, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.messages) >= count:
                return True
            await asyncio.sleep(0.01)
        return False


def unique(name: str) -> str:
    return f"{name}-{time.time_ns()}"


async def test_a_published_operation_reaches_a_subscriber(redis_bus_factory: Any) -> None:
    doc = unique("prop")
    inbox = Inbox()
    publisher = await redis_bus_factory()
    subscriber = await redis_bus_factory(inbox)
    await subscriber.subscribe(doc)

    ops = RGA("alice").local_insert(0, "hi")
    await publisher.publish(OpMessage.create(doc, ops, origin="a", author="alice"))

    assert await inbox.wait_for(1), "nothing arrived over Redis"
    received = inbox.messages[0]
    assert received.doc_id == doc
    assert received.ops == ops
    assert received.origin == "a"
    assert received.author == "alice"
    assert received.published_at > 0


async def test_a_subscriber_hears_nothing_for_other_documents(redis_bus_factory: Any) -> None:
    """Per-document channels: an instance isn't woken by traffic it isn't serving."""
    inbox = Inbox()
    publisher = await redis_bus_factory()
    subscriber = await redis_bus_factory(inbox)
    await subscriber.subscribe(unique("subscribed"))

    await publisher.publish(
        OpMessage.create(unique("other"), RGA("a").local_insert(0, "x"), origin="a", author="a")
    )

    assert not await inbox.wait_for(1, timeout=0.75)


async def test_unsubscribing_stops_delivery(redis_bus_factory: Any) -> None:
    doc = unique("unsub")
    inbox = Inbox()
    publisher = await redis_bus_factory()
    subscriber = await redis_bus_factory(inbox)

    await subscriber.subscribe(doc)
    ops = RGA("a").local_insert(0, "x")
    await publisher.publish(OpMessage.create(doc, ops, origin="a", author="a"))
    assert await inbox.wait_for(1)

    await subscriber.unsubscribe(doc)
    await publisher.publish(OpMessage.create(doc, ops, origin="a", author="a"))
    assert not await inbox.wait_for(2, timeout=0.75)


async def test_subscribing_twice_is_harmless(redis_bus_factory: Any) -> None:
    """Many clients per document, one subscription -- and exactly one delivery."""
    doc = unique("twice")
    inbox = Inbox()
    publisher = await redis_bus_factory()
    subscriber = await redis_bus_factory(inbox)

    await subscriber.subscribe(doc)
    await subscriber.subscribe(doc)

    await publisher.publish(
        OpMessage.create(doc, RGA("a").local_insert(0, "x"), origin="a", author="a")
    )

    assert await inbox.wait_for(1)
    await asyncio.sleep(0.2)
    assert len(inbox.messages) == 1, "one subscription, one delivery"


async def test_a_malformed_payload_does_not_kill_the_reader(redis_bus_factory: Any) -> None:
    """The read loop drains the whole process's traffic. One bad message must not
    stop delivery for every document on the instance."""
    doc = unique("garbage")
    inbox = Inbox()
    publisher = await redis_bus_factory()
    subscriber = await redis_bus_factory(inbox)
    await subscriber.subscribe(doc)

    # Straight to Redis, bypassing OpMessage entirely.
    await publisher._client.publish(channel_for(doc), "this is not json")
    await publisher._client.publish(channel_for(doc), '{"ops": "not a list"}')
    await asyncio.sleep(0.2)

    ops = RGA("a").local_insert(0, "still working")
    await publisher.publish(OpMessage.create(doc, ops, origin="a", author="a"))

    assert await inbox.wait_for(1), "the reader stopped after the bad payloads"
    assert inbox.messages[0].ops == ops


async def test_the_keepalive_channel_is_subscribed(redis_bus_factory: Any) -> None:
    """redis-py needs a subscription before get_message has a connection to read,
    and an instance starts with no documents open."""
    bus = await redis_bus_factory()
    assert KEEPALIVE_CHANNEL in bus._pubsub.channels


async def test_two_hubs_propagate_over_real_redis(redis_bus_factory: Any) -> None:
    """The whole Stage 3 fix, over the actual transport."""
    doc = unique("hubs")
    store = InMemoryOpStore()

    bus_a = await redis_bus_factory()
    bus_b = await redis_bus_factory()
    hub_a = Hub(store=store, bus=bus_a, instance_id="a")
    hub_b = Hub(store=store, bus=bus_b, instance_id="b")
    # Hub wires its handler in __post_init__, but these buses were built first.
    bus_a.on_message(hub_a.apply_remote_ops)
    bus_b.on_message(hub_b.apply_remote_ops)

    class Conn:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, data: Any) -> None:
            self.sent.append(data)

    alice, bob = Conn(), Conn()
    await hub_a.attach(doc, "alice", alice)
    await hub_b.attach(doc, "bob", bob)

    ops = RGA("alice").local_insert(0, "across the wire")
    await hub_a.apply_ops(doc, ops, author="alice")

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if (await hub_b.document(doc)).text == "across the wire":
            break
        await asyncio.sleep(0.01)

    assert (await hub_b.document(doc)).text == "across the wire"
    assert [m for m in bob.sent if m["type"] == "ops"], "bob's socket got nothing"
    assert [m for m in alice.sent if m["type"] == "ops"] == [], "alice was echoed"
    assert hub_b.latency.summary()["count"] == 1
    assert hub_a.latency.summary() == {"count": 0}
