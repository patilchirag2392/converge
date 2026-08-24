"""End-to-end tests over a real WebSocket.

The two Stage 0 tests that asserted the bug -- `test_concurrent_edits_lose_data`
and `test_clients_can_diverge_permanently` -- are now their positive opposites:
`test_concurrent_edits_both_survive` and `test_every_replica_converges`.

Note what changed about the assertions themselves. Stage 0's tests had to be
written order-independently ("exactly one edit survived, we can't say which"),
because the outcome depended on arrival order. These assert exact text: with
operations that commute, arrival order stops mattering, so the result is
reproducible.

Every test builds its own app via `create_app(InMemoryOpStore())`, so there is no
shared state between tests -- `app.main` no longer holds a module-level Hub.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.crdt import RGA
from app.main import create_app
from app.store import InMemoryOpStore
from helpers import Tab


INSTANCE = "test-1"


def fresh_client(
    store: InMemoryOpStore | None = None, instance_id: str = INSTANCE
) -> TestClient:
    """An app with its own Hub and its own store. Pass a store to share one
    across two apps, which is how a restart gets simulated."""
    return TestClient(
        create_app(
            store if store is not None else InMemoryOpStore(), instance_id=instance_id
        )
    )


def test_snapshot_is_sent_on_connect() -> None:
    with fresh_client() as client, client.websocket_connect("/ws/t-snap?client_id=a") as s:
        assert s.receive_json() == {
            "type": "snapshot",
            "doc_id": "t-snap",
            "ops": [],
            "rev": 0,
            "clients": 1,
            "client_id": "a",
            "instance": INSTANCE,
        }


def test_a_new_tab_rebuilds_the_document_from_operations() -> None:
    with fresh_client() as client:
        with client.websocket_connect("/ws/t-join?client_id=a") as sa:
            a = Tab(sa, "a")
            a.type(0, "already here")

            with client.websocket_connect("/ws/t-join?client_id=b") as sb:
                b = Tab(sb, "b")
                # b was sent operations, not text, and rendered them itself.
                assert b.snapshot["ops"] != []
                assert b.text == "already here"
                assert b.snapshot["clients"] == 2
                assert a.session.receive_json() == {"type": "presence", "clients": 2}


def test_ops_are_broadcast_to_others_but_not_the_author() -> None:
    with fresh_client() as client:
        with client.websocket_connect("/ws/t-bcast?client_id=a") as sa:
            a = Tab(sa, "a")
            with client.websocket_connect("/ws/t-bcast?client_id=b") as sb:
                b = Tab(sb, "b")

                a.type(0, "typed by a")
                frame = b.receive_ops()
                assert frame["author"] == "a"
                assert b.text == "typed by a"

                # The author never hears its own operations back -- which was
                # fatal in Stage 0 and is now harmless, because ops commute.
                b.type(10, " and b")
                a.receive_ops()
                assert a.text == b.text == "typed by a and b"


def test_concurrent_edits_both_survive() -> None:
    """THE Stage 1 demonstration -- the exact scenario that lost data in Stage 0.

    Both tabs hold "hi" and neither has seen the other's keystroke. Both edits
    are kept, and the result is the same everywhere no matter which frame the
    server happened to process first.
    """
    with fresh_client() as client:
        with client.websocket_connect("/ws/t-race?client_id=a") as sa:
            a = Tab(sa, "a")
            with client.websocket_connect("/ws/t-race?client_id=b") as sb:
                b = Tab(sb, "b")

                a.type(0, "hi")
                b.receive_ops()
                assert a.text == b.text == "hi"

                # Concurrent: both computed from "hi", neither has seen the other.
                a.type(2, " A")
                b.type(2, " B")

                b.receive_ops()
                a.receive_ops()

                server = client.get("/api/doc/t-race").json()

    assert "A" in a.text and "B" in a.text, "neither edit was lost"
    assert a.text == b.text == server["text"] == "hi B A"


def test_every_replica_converges() -> None:
    """Both tabs and the server agree -- including the tab that never heard its
    own operations back. This is what Stage 0 could not do at any price."""
    with fresh_client() as client:
        with client.websocket_connect("/ws/t-conv?client_id=a") as sa:
            a = Tab(sa, "a")
            with client.websocket_connect("/ws/t-conv?client_id=b") as sb:
                b = Tab(sb, "b")

                a.type(0, "the fox")
                b.receive_ops()

                # A tangle of concurrent inserts and deletes at different spots.
                a.type(4, "quick ")
                b.type(0, "SO: ")
                b.erase(4, 4)

                a.receive_ops()
                a.receive_ops()
                b.receive_ops()

                server = client.get("/api/doc/t-conv").json()

    assert a.text == b.text == server["text"]
    assert a.text == "SO: quick fox"


def test_a_late_joiner_converges_with_everyone_else() -> None:
    with fresh_client() as client:
        with client.websocket_connect("/ws/t-late?client_id=a") as sa:
            a = Tab(sa, "a")
            with client.websocket_connect("/ws/t-late?client_id=b") as sb:
                b = Tab(sb, "b")
                a.type(0, "hello")
                b.receive_ops()
                b.erase(0, 1)
                a.receive_ops()

                # c arrives only now, and rebuilds from the snapshot.
                with client.websocket_connect("/ws/t-late?client_id=c") as sc:
                    c = Tab(sc, "c")
                    assert c.text == a.text == b.text == "ello"

                    c.type(4, "!")
                    a.receive_ops()
                    b.receive_ops()
                    assert a.text == b.text == c.text == "ello!"


def test_documents_are_isolated_by_id() -> None:
    with fresh_client() as client:
        with client.websocket_connect("/ws/t-iso-1?client_id=a") as sa:
            a = Tab(sa, "a")
            with client.websocket_connect("/ws/t-iso-2?client_id=b") as sb:
                Tab(sb, "b")
                a.type(0, "only in doc 1")

        assert client.get("/api/doc/t-iso-1").json()["text"] == "only in doc 1"
        assert client.get("/api/doc/t-iso-2").json()["text"] == ""


def test_presence_drops_when_a_client_disconnects() -> None:
    with fresh_client() as client:
        with client.websocket_connect("/ws/t-leave?client_id=a") as sa:
            Tab(sa, "a")
            with client.websocket_connect("/ws/t-leave?client_id=b") as sb:
                Tab(sb, "b")
                assert sa.receive_json()["clients"] == 2

                # Close explicitly rather than letting the context manager do
                # it: `WebSocketTestSession.__exit__` *cancels* the server task,
                # so the handler's finally-block would be torn down mid-await
                # and never send this. `close()` delivers a real
                # websocket.disconnect, which is what uvicorn does in prod.
                sb.close()
                assert sa.receive_json() == {"type": "presence", "clients": 1}

        assert client.get("/api/doc/t-leave").json()["clients"] == 0


def test_malformed_frames_are_rejected_without_killing_the_socket() -> None:
    with fresh_client() as client, client.websocket_connect("/ws/t-bad?client_id=a") as s:
        a = Tab(s, "a")

        s.send_text("this is not json")
        assert s.receive_json() == {"type": "error", "detail": "malformed JSON"}

        s.send_json({"type": "edit", "text": "the Stage 0 protocol"})
        assert s.receive_json()["type"] == "error"

        s.send_json({"type": "ops", "ops": []})  # empty batch
        assert s.receive_json()["type"] == "error"

        s.send_json({"type": "ops", "ops": [{"kind": "insert", "id": ["a", 1]}]})
        assert s.receive_json()["type"] == "error"

        # A bad frame must not have corrupted the replica or the socket.
        a.type(0, "fine")
        assert client.get("/api/doc/t-bad").json()["text"] == "fine"


def test_healthz() -> None:
    with fresh_client() as client:
        assert client.get("/healthz").json() == {"status": "ok", "instance": INSTANCE}


def test_a_document_outlives_the_app_that_created_it() -> None:
    """A restart in miniature: same store, brand new app, Hub and replica.

    test_persistence.py does this against real Postgres; this keeps the fast
    suite honest about the same behaviour.
    """
    store = InMemoryOpStore()

    with fresh_client(store) as client:
        with client.websocket_connect("/ws/t-restart?client_id=a") as sa:
            a = Tab(sa, "a")
            a.type(0, "survives a restart")
            a.sync()
            assert client.get("/api/doc/t-restart").json()["text"] == "survives a restart"

    # Every replica and every connection is gone at this point; only the log remains.
    with fresh_client(store) as client:
        assert client.get("/api/doc/t-restart").json()["text"] == "survives a restart"

        with client.websocket_connect("/ws/t-restart?client_id=b") as sb:
            b = Tab(sb, "b")
            assert b.text == "survives a restart", "rebuilt from the operation log"

            b.type(18, " and keeps going")
            b.sync()
            assert client.get("/api/doc/t-restart").json()["text"] == (
                "survives a restart and keeps going"
            )


def test_a_client_replaying_its_snapshot_is_not_rebroadcast() -> None:
    """What reconnect healing looks like from the server's side.

    A reconnecting client re-sends everything it knows, so the server must
    recognise that none of it is new and stay quiet, rather than fanning a
    duplicate burst out to every other tab.
    """
    with fresh_client() as client:
        with client.websocket_connect("/ws/t-replay?client_id=a") as sa:
            a = Tab(sa, "a")
            with client.websocket_connect("/ws/t-replay?client_id=b") as sb:
                b = Tab(sb, "b")

                a.type(0, "hi")
                b.receive_ops()

                # b replays its whole replica, exactly as it does on reconnect.
                sb.send_json({"type": "ops", "ops": b.crdt.snapshot_ops()})
                b.sync()

                # Now b types one genuinely new character. If the replay had been
                # rebroadcast, this would not be the next frame a sees.
                b.type(2, "!")
                frame = a.receive_ops()

    assert len(frame["ops"]) == 1, "only the new character should have been relayed"
    assert a.text == "hi!"


def test_an_unpersisted_edit_is_reported_as_an_error() -> None:
    """If the write fails the client must be told, not quietly served an edit
    that would vanish at the next restart."""

    class FailingStore(InMemoryOpStore):
        async def append_ops(self, doc_id: str, raw_ops: list[Any]) -> None:
            raise RuntimeError("disk on fire")

    with fresh_client(FailingStore()) as client:
        with client.websocket_connect("/ws/t-fail?client_id=a") as sa:
            a = Tab(sa, "a")
            ops = a.crdt.local_insert(0, "hi")
            sa.send_json({"type": "ops", "ops": ops})

            reply = sa.receive_json()
            assert reply["type"] == "error"
            assert "persist" in reply["detail"]

        assert client.get("/api/doc/t-fail").json()["text"] == ""
