"""Cross-instance propagation: two Hubs, one bus, one store.

`InProcessCluster` stands in for Redis so these run fast and deterministically in
one event loop. It deliberately delivers each message to *every* bus including
the publisher's own, so the loop-prevention path (client -> A -> bus -> A) is
exercised here rather than only in production.

The real Redis wiring is covered by test_redis_bus.py, and the real cluster by
scripts/multi_instance_demo.py and scripts/latency_probe.py.
"""

from __future__ import annotations

from typing import Any

from app.bus import InProcessCluster, OpMessage
from app.crdt import RGA
from app.hub import Hub
from app.store import InMemoryOpStore

DOC = "demo"


class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)

    @property
    def op_frames(self) -> list[dict[str, Any]]:
        return [m for m in self.sent if m.get("type") == "ops"]


def two_instances() -> tuple[Hub, Hub, InProcessCluster, InMemoryOpStore]:
    """Two Hubs sharing a bus and a store -- i.e. two processes, one Redis, one
    Postgres."""
    cluster = InProcessCluster()
    store = InMemoryOpStore()
    return (
        Hub(store=store, bus=cluster.bus(), instance_id="a"),
        Hub(store=store, bus=cluster.bus(), instance_id="b"),
        cluster,
        store,
    )


async def test_an_edit_on_one_instance_reaches_a_client_on_the_other() -> None:
    """The Stage 3 fix, in one test."""
    a, b, _, _ = two_instances()
    alice_conn, bob_conn = FakeConnection(), FakeConnection()
    await a.attach(DOC, "alice", alice_conn)
    await b.attach(DOC, "bob", bob_conn)

    ops = RGA("alice").local_insert(0, "hi")
    await a.apply_ops(DOC, ops, author="alice")

    assert bob_conn.op_frames, "bob heard nothing from the other instance"
    assert bob_conn.op_frames[-1]["ops"] == ops
    assert bob_conn.op_frames[-1]["author"] == "alice"

    doc_b = await b.document(DOC)
    assert doc_b.text == "hi", "instance b's own replica must be updated too"


async def test_the_author_never_hears_its_own_operations_back() -> None:
    """Loop prevention: client -> A -> bus -> A must stop at the origin check."""
    a, b, _, _ = two_instances()
    alice_conn = FakeConnection()
    await a.attach(DOC, "alice", alice_conn)
    await b.attach(DOC, "bob", FakeConnection())

    await a.apply_ops(DOC, RGA("alice").local_insert(0, "hi"), author="alice")

    assert alice_conn.op_frames == [], "alice was echoed her own edit"
    # And the self-message never even reached the latency window, because the
    # origin check happens before recording.
    assert a.latency.summary() == {"count": 0}
    assert b.latency.summary()["count"] == 1


async def test_remote_operations_are_not_persisted_again() -> None:
    """The originating instance already committed them before publishing."""
    a, b, _, store = two_instances()
    await a.attach(DOC, "alice", FakeConnection())
    await b.attach(DOC, "bob", FakeConnection())

    ops = RGA("alice").local_insert(0, "hello")
    await a.apply_ops(DOC, ops, author="alice")

    assert await store.count_ops(DOC) == len(ops), "written once, by the origin only"


async def test_remote_operations_are_not_republished() -> None:
    """Otherwise every instance rebroadcasts everything and the cluster
    amplifies each edit without bound."""
    a, b, cluster, _ = two_instances()
    await a.attach(DOC, "alice", FakeConnection())
    await b.attach(DOC, "bob", FakeConnection())

    await a.apply_ops(DOC, RGA("alice").local_insert(0, "hi"), author="alice")

    assert len(cluster.published) == 1


async def test_only_new_operations_are_published() -> None:
    """A reconnecting client replaying its snapshot must not fan out cluster-wide."""
    a, _, cluster, _ = two_instances()
    await a.attach(DOC, "alice", FakeConnection())

    ops = RGA("alice").local_insert(0, "hi")
    await a.apply_ops(DOC, ops, author="alice")
    await a.apply_ops(DOC, ops, author="alice")  # the replay

    assert len(cluster.published) == 1


async def test_a_duplicate_delivery_is_harmless() -> None:
    """Redis gives no delivery guarantees, so idempotency is the backstop for the
    duplicates we can't prevent."""
    a, b, _, _ = two_instances()
    await a.attach(DOC, "alice", FakeConnection())
    bob_conn = FakeConnection()
    await b.attach(DOC, "bob", bob_conn)

    ops = RGA("alice").local_insert(0, "hi")
    await a.apply_ops(DOC, ops, author="alice")
    frames_after_first = len(bob_conn.op_frames)

    # Same message again, as a redelivery would look.
    await b.apply_remote_ops(OpMessage.create(DOC, ops, origin="a", author="alice"))

    doc_b = await b.document(DOC)
    assert doc_b.text == "hi", "text must not double"
    assert len(bob_conn.op_frames) == frames_after_first, "and bob is not spammed again"


async def test_an_instance_not_serving_a_document_ignores_it() -> None:
    a, b, _, _ = two_instances()
    await a.attach(DOC, "alice", FakeConnection())
    # b never attaches, so it never subscribed.

    await a.apply_ops(DOC, RGA("alice").local_insert(0, "hi"), author="alice")

    assert b.document_count == 0
    assert b.latency.summary() == {"count": 0}


async def test_a_malformed_remote_operation_does_not_lose_the_rest() -> None:
    a, b, _, _ = two_instances()
    await a.attach(DOC, "alice", FakeConnection())
    await b.attach(DOC, "bob", FakeConnection())

    good = RGA("alice").local_insert(0, "hi")
    poisoned = [good[0], {"kind": "nonsense"}, good[1]]

    await b.apply_remote_ops(OpMessage.create(DOC, poisoned, origin="a", author="alice"))

    doc_b = await b.document(DOC)
    assert doc_b.text == "hi", "the two valid operations still applied"


async def test_a_cached_replica_always_has_a_live_subscription() -> None:
    """The invariant. Breaking it is exactly the Stage 3 stale-replica bug."""
    a, _, _, _ = two_instances()
    conn = FakeConnection()

    await a.attach(DOC, "alice", conn)
    assert a.document_count == 1

    await a.detach(DOC, "alice")
    assert a.document_count == 0, "last client left, so the replica must go too"

    # Reopening reloads from the store rather than serving something stale.
    await a.attach(DOC, "alice", conn)
    assert a.document_count == 1


async def test_a_second_client_does_not_drop_the_subscription() -> None:
    a, _, _, _ = two_instances()
    await a.attach(DOC, "alice", FakeConnection())
    await a.attach(DOC, "bob", FakeConnection())

    await a.detach(DOC, "alice")
    assert a.document_count == 1, "bob is still here"
    assert a.client_count(DOC) == 1


async def test_a_reopened_document_picks_up_what_it_missed() -> None:
    """While b had nobody connected it heard nothing -- but Postgres kept it, so
    reopening is correct rather than stale. This is why unsubscribing is safe."""
    a, b, _, _ = two_instances()
    await a.attach(DOC, "alice", FakeConnection())

    writer = RGA("alice")
    await a.apply_ops(DOC, writer.local_insert(0, "written while b was away"), author="alice")

    bob_conn = FakeConnection()
    doc_b = await b.attach(DOC, "bob", bob_conn)
    assert doc_b.text == "written while b was away"


async def test_documents_do_not_leak_across_channels() -> None:
    a, b, _, _ = two_instances()
    await a.attach("doc-1", "alice", FakeConnection())
    bob_conn = FakeConnection()
    await b.attach("doc-2", "bob", bob_conn)

    await a.apply_ops("doc-1", RGA("alice").local_insert(0, "one"), author="alice")

    assert bob_conn.op_frames == []
    assert (await b.document("doc-2")).text == ""


async def test_three_instances_all_converge() -> None:
    cluster = InProcessCluster()
    store = InMemoryOpStore()
    hubs = [
        Hub(store=store, bus=cluster.bus(), instance_id=name) for name in ("a", "b", "c")
    ]
    replicas = [RGA(name) for name in ("ca", "cb", "cc")]

    for hub, name in zip(hubs, ("ca", "cb", "cc")):
        await hub.attach(DOC, name, FakeConnection())

    # Each instance's client seeds the document, then edits concurrently.
    await hubs[0].apply_ops(DOC, replicas[0].local_insert(0, "hi"), author="ca")
    for replica in replicas[1:]:
        for op in (await hubs[0].document(DOC)).crdt.snapshot_ops():
            replica.apply(op)

    await hubs[1].apply_ops(DOC, replicas[1].local_insert(2, " B"), author="cb")
    await hubs[2].apply_ops(DOC, replicas[2].local_insert(2, " C"), author="cc")

    texts = {(await hub.document(DOC)).text for hub in hubs}
    assert len(texts) == 1, f"instances diverged: {texts}"
    assert "B" in texts.pop()


def test_op_message_round_trips_through_json() -> None:
    ops = RGA("alice").local_insert(0, "hi")
    original = OpMessage.create(DOC, ops, origin="a", author="alice")
    restored = OpMessage.from_json(original.to_json())

    assert restored == original


def test_latency_window_ignores_negative_samples() -> None:
    """A negative sample means the two clocks disagree. Recording it would
    silently flatter the numbers."""
    from app.hub import PropagationLatency

    window = PropagationLatency()
    window.record(-0.5)
    assert window.summary() == {"count": 0}

    window.record(0.010)
    window.record(0.020)
    summary = window.summary()
    assert summary["count"] == 2
    assert summary["max_ms"] == 20.0
