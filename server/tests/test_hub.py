"""Unit tests for the Hub, against the in-memory store.

The store double mirrors Postgres' primary-key dedup, so these cover the write
path's semantics without needing a database; test_store.py and
test_persistence.py cover the real thing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.crdt import RGA
from app.hub import Hub
from app.store import InMemoryOpStore


def make_hub() -> Hub:
    return Hub(store=InMemoryOpStore())


class FakeConnection:
    """Records what the Hub tried to send it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)


class BrokenConnection:
    async def send_json(self, data: Any) -> None:
        raise RuntimeError("socket is gone")


async def test_document_is_created_empty_on_first_open() -> None:
    doc = await make_hub().document("demo")
    assert doc.text == ""
    assert doc.rev == 0


async def test_apply_ops_merges_into_the_server_replica() -> None:
    hub = make_hub()
    writer = RGA("writer")
    doc, fresh = await hub.apply_ops("demo", writer.local_insert(0, "hello"))
    assert doc.text == "hello"
    assert doc.rev == 5  # one operation per character
    assert len(fresh) == 5


async def test_concurrent_edits_both_survive() -> None:
    """Two clients edit the same base concurrently; nothing is lost.

    Stage 0's version of this test asserted that one edit was destroyed.
    """
    hub = make_hub()
    alice, bob = RGA("alice"), RGA("bob")

    base = alice.local_insert(0, "hi")
    for op in base:
        bob.apply(op)
    await hub.apply_ops("demo", base)

    # Neither client has seen the other's keystroke.
    await hub.apply_ops("demo", alice.local_insert(2, "A"))
    doc, _ = await hub.apply_ops("demo", bob.local_insert(2, "B"))

    assert "A" in doc.text and "B" in doc.text
    assert doc.text == "hiBA"


async def test_server_replica_matches_the_clients() -> None:
    hub = make_hub()
    alice, bob = RGA("alice"), RGA("bob")

    base = alice.local_insert(0, "hi")
    for op in base:
        bob.apply(op)
    await hub.apply_ops("demo", base)

    alice_ops = alice.local_insert(2, "A")
    bob_ops = bob.local_insert(2, "B")
    await hub.apply_ops("demo", alice_ops)
    await hub.apply_ops("demo", bob_ops)

    for op in bob_ops:
        alice.apply(op)
    for op in alice_ops:
        bob.apply(op)

    doc = await hub.document("demo")
    assert doc.text == alice.text == bob.text


async def test_replaying_a_batch_reports_nothing_new() -> None:
    """What a client's post-reconnect snapshot replay looks like from here."""
    hub = make_hub()
    ops = RGA("writer").local_insert(0, "hi")

    _, first = await hub.apply_ops("demo", ops)
    doc, second = await hub.apply_ops("demo", ops)

    assert len(first) == 2
    assert second == [], "a replay must not be rebroadcast"
    assert doc.text == "hi"


async def test_a_malformed_operation_rejects_the_whole_batch() -> None:
    hub = make_hub()
    good = RGA("writer").local_insert(0, "hi")

    with pytest.raises(ValueError):
        await hub.apply_ops("demo", [*good, {"kind": "explode"}])

    doc = await hub.document("demo")
    assert doc.text == "", "nothing from the batch should land"
    assert await hub.store.count_ops("demo") == 0, "and nothing should be persisted"


@pytest.mark.parametrize("bad", [[], "not a list", {"kind": "insert"}, None])
async def test_apply_ops_rejects_a_bad_frame(bad: object) -> None:
    hub = make_hub()
    with pytest.raises(ValueError):
        await hub.apply_ops("demo", bad)


async def test_ops_are_persisted_as_they_are_applied() -> None:
    hub = make_hub()
    writer = RGA("writer")
    await hub.apply_ops("demo", writer.local_insert(0, "abc"))
    await hub.apply_ops("demo", writer.local_delete(1, 1))

    assert await hub.store.count_ops("demo") == 4  # 3 inserts + 1 delete


async def test_a_failed_write_applies_nothing() -> None:
    """Postgres is the durability boundary, so it must never lag the replica."""

    class FailingStore(InMemoryOpStore):
        async def append_ops(self, doc_id: str, raw_ops: list[Any]) -> None:
            raise RuntimeError("disk on fire")

    hub = Hub(store=FailingStore())
    with pytest.raises(RuntimeError):
        await hub.apply_ops("demo", RGA("writer").local_insert(0, "hi"))

    doc = await hub.document("demo")
    assert doc.text == "", "an unpersisted edit must not be served"


async def test_concurrent_first_opens_load_the_document_once() -> None:
    """Two clients joining at once must not build competing replicas."""
    loads = 0

    class CountingStore(InMemoryOpStore):
        async def load_ops(self, doc_id: str) -> list[dict[str, Any]]:
            nonlocal loads
            loads += 1
            await asyncio.sleep(0)  # force a suspension mid-load
            return await super().load_ops(doc_id)

    hub = Hub(store=CountingStore())
    first, second = await asyncio.gather(hub.document("demo"), hub.document("demo"))

    assert loads == 1
    assert first is second


async def test_document_survives_the_last_client_leaving() -> None:
    hub = make_hub()
    hub.join("demo", "a", FakeConnection())
    await hub.apply_ops("demo", RGA("writer").local_insert(0, "still here"))
    hub.leave("demo", "a")

    assert hub.client_count("demo") == 0
    doc = await hub.document("demo")
    assert doc.text == "still here"


async def test_broadcast_skips_the_author() -> None:
    hub = make_hub()
    author, other = FakeConnection(), FakeConnection()
    hub.join("demo", "author", author)
    hub.join("demo", "other", other)

    await hub.broadcast("demo", {"type": "ops", "ops": []}, exclude="author")

    assert author.sent == []
    assert other.sent == [{"type": "ops", "ops": []}]


async def test_broadcast_does_not_leak_into_other_documents() -> None:
    hub = make_hub()
    here, elsewhere = FakeConnection(), FakeConnection()
    hub.join("doc-1", "a", here)
    hub.join("doc-2", "b", elsewhere)

    await hub.broadcast("doc-1", {"type": "ops", "ops": []})

    assert len(here.sent) == 1
    assert elsewhere.sent == []


async def test_broadcast_drops_a_dead_connection() -> None:
    hub = make_hub()
    hub.join("demo", "dead", BrokenConnection())
    hub.join("demo", "alive", FakeConnection())

    await hub.broadcast("demo", {"type": "presence", "clients": 2})

    assert hub.client_count("demo") == 1
