"""Unit tests for the Hub.

`test_concurrent_edits_both_survive` is the Stage 0 tripwire: the same scenario
used to be asserted as data loss (`test_last_write_wins_destroys_the_earlier_edit`)
and now has to keep both edits.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.crdt import RGA
from app.hub import Hub


class FakeConnection:
    """Records what the Hub tried to send it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)


class BrokenConnection:
    async def send_json(self, data: Any) -> None:
        raise RuntimeError("socket is gone")


def test_document_is_created_empty_on_first_open() -> None:
    hub = Hub()
    doc = hub.document("demo")
    assert doc.text == ""
    assert doc.rev == 0


def test_apply_ops_merges_into_the_server_replica() -> None:
    hub = Hub()
    writer = RGA("writer")
    doc = hub.apply_ops("demo", writer.local_insert(0, "hello"))
    assert doc.text == "hello"
    assert doc.rev == 5  # one operation per character


def test_concurrent_edits_both_survive() -> None:
    """Two clients edit the same base concurrently; nothing is lost.

    Stage 0's version of this test asserted that one edit was destroyed.
    """
    hub = Hub()
    alice, bob = RGA("alice"), RGA("bob")

    base = alice.local_insert(0, "hi")
    for op in base:
        bob.apply(op)
    hub.apply_ops("demo", base)

    # Neither client has seen the other's keystroke.
    hub.apply_ops("demo", alice.local_insert(2, "A"))
    doc = hub.apply_ops("demo", bob.local_insert(2, "B"))

    assert "A" in doc.text and "B" in doc.text
    assert doc.text == "hiBA"


def test_server_replica_matches_the_clients() -> None:
    hub = Hub()
    alice, bob = RGA("alice"), RGA("bob")

    base = alice.local_insert(0, "hi")
    for op in base:
        bob.apply(op)
    hub.apply_ops("demo", base)

    alice_ops = alice.local_insert(2, "A")
    bob_ops = bob.local_insert(2, "B")
    hub.apply_ops("demo", alice_ops)
    hub.apply_ops("demo", bob_ops)

    for op in bob_ops:
        alice.apply(op)
    for op in alice_ops:
        bob.apply(op)

    assert hub.document("demo").text == alice.text == bob.text


def test_a_malformed_operation_rejects_the_whole_batch() -> None:
    hub = Hub()
    writer = RGA("writer")
    good = writer.local_insert(0, "hi")

    with pytest.raises(ValueError):
        hub.apply_ops("demo", [*good, {"kind": "explode"}])

    assert hub.document("demo").text == "", "nothing from the batch should land"


@pytest.mark.parametrize("bad", [[], "not a list", {"kind": "insert"}, None])
def test_apply_ops_rejects_a_bad_frame(bad: object) -> None:
    hub = Hub()
    with pytest.raises(ValueError):
        hub.apply_ops("demo", bad)


def test_document_survives_the_last_client_leaving() -> None:
    hub = Hub()
    hub.join("demo", "a", FakeConnection())
    hub.apply_ops("demo", RGA("writer").local_insert(0, "persisted in memory"))
    hub.leave("demo", "a")

    assert hub.client_count("demo") == 0
    assert hub.document("demo").text == "persisted in memory"


async def test_broadcast_skips_the_author() -> None:
    hub = Hub()
    author, other = FakeConnection(), FakeConnection()
    hub.join("demo", "author", author)
    hub.join("demo", "other", other)

    await hub.broadcast("demo", {"type": "ops", "ops": []}, exclude="author")

    assert author.sent == []
    assert other.sent == [{"type": "ops", "ops": []}]


async def test_broadcast_does_not_leak_into_other_documents() -> None:
    hub = Hub()
    here, elsewhere = FakeConnection(), FakeConnection()
    hub.join("doc-1", "a", here)
    hub.join("doc-2", "b", elsewhere)

    await hub.broadcast("doc-1", {"type": "ops", "ops": []})

    assert len(here.sent) == 1
    assert elsewhere.sent == []


async def test_broadcast_drops_a_dead_connection() -> None:
    hub = Hub()
    hub.join("demo", "dead", BrokenConnection())
    hub.join("demo", "alive", FakeConnection())

    await hub.broadcast("demo", {"type": "presence", "clients": 2})

    assert hub.client_count("demo") == 1
