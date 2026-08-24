"""Restart tests: the process dies, the document doesn't.

Each `with TestClient(create_app())` block is a server lifetime. Leaving the
block closes the pool and throws away every in-memory replica, so the next block
is a genuinely cold process that can only know what Postgres told it.

Skipped when Postgres isn't reachable -- see conftest.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.crdt import RGA
from app.main import create_app
from helpers import Tab

DOC = "restart-me"


def test_a_document_survives_a_restart(pg_url: str) -> None:
    with TestClient(create_app()) as client:
        with client.websocket_connect(f"/ws/{DOC}?client_id=a") as sa:
            a = Tab(sa, "a")
            a.type(0, "written before the crash")
            a.sync()
            assert client.get(f"/api/doc/{DOC}").json()["text"] == "written before the crash"

    # Cold process: nothing in memory, no connections, only the log.
    with TestClient(create_app()) as client:
        state = client.get(f"/api/doc/{DOC}").json()
        assert state["text"] == "written before the crash"
        assert state["rev"] == len("written before the crash")


def test_a_reconnecting_tab_rebuilds_from_the_log(pg_url: str) -> None:
    with TestClient(create_app()) as client:
        with client.websocket_connect(f"/ws/{DOC}?client_id=a") as sa:
            Tab(sa, "a").type(0, "hello world")
            sa.send_json({"type": "ops", "ops": []})
            sa.receive_json()

    with TestClient(create_app()) as client:
        with client.websocket_connect(f"/ws/{DOC}?client_id=b") as sb:
            b = Tab(sb, "b")
            assert b.snapshot["ops"] != [], "the snapshot came out of Postgres"
            assert b.text == "hello world"


def test_edits_after_a_restart_append_to_the_same_document(pg_url: str) -> None:
    with TestClient(create_app()) as client:
        with client.websocket_connect(f"/ws/{DOC}?client_id=a") as sa:
            a = Tab(sa, "a")
            a.type(0, "before")
            a.sync()

    with TestClient(create_app()) as client:
        with client.websocket_connect(f"/ws/{DOC}?client_id=b") as sb:
            b = Tab(sb, "b")
            b.type(6, " and after")
            b.sync()
            assert b.text == "before and after"

    # Third lifetime, to prove the second one's writes were durable too.
    with TestClient(create_app()) as client:
        assert client.get(f"/api/doc/{DOC}").json()["text"] == "before and after"


def test_a_replaying_client_heals_operations_the_server_never_stored(pg_url: str) -> None:
    """The mid-session crash hole, and why reconnect is a *mutual* exchange.

    If the server dies after a tab applied an edit locally but before the write
    committed, that operation exists only in that browser. The server's snapshot
    can't contain it and merging can't invent it -- so the client re-sends
    everything it has, and the server absorbs what it's missing.
    """
    # A replica holding edits the server has never seen.
    orphan = RGA("orphan")
    orphan.local_insert(0, "rescued")

    with TestClient(create_app()) as client:
        with client.websocket_connect(f"/ws/{DOC}?client_id=orphan") as socket:
            tab = Tab(socket, "orphan")
            assert tab.text == "", "the server starts out knowing nothing"

            socket.send_json({"type": "ops", "ops": orphan.snapshot_ops()})
            tab.sync()

        assert client.get(f"/api/doc/{DOC}").json()["text"] == "rescued"

    # And it was persisted, not just merged into memory.
    with TestClient(create_app()) as client:
        assert client.get(f"/api/doc/{DOC}").json()["text"] == "rescued"


def test_a_replay_is_idempotent_across_restarts(pg_url: str) -> None:
    """Reconnect healing must not duplicate text, however many times it runs."""
    writer = RGA("writer")
    writer.local_insert(0, "once")

    for _ in range(3):
        with TestClient(create_app()) as client:
            with client.websocket_connect(f"/ws/{DOC}?client_id=writer") as socket:
                tab = Tab(socket, "writer")
                socket.send_json({"type": "ops", "ops": writer.snapshot_ops()})
                tab.sync()
            assert client.get(f"/api/doc/{DOC}").json()["text"] == "once"


def test_concurrent_edits_from_before_and_after_a_restart_merge(pg_url: str) -> None:
    """Two replicas that never met, one of them across a restart boundary."""
    with TestClient(create_app()) as client:
        with client.websocket_connect(f"/ws/{DOC}?client_id=a") as sa:
            a = Tab(sa, "a")
            a.type(0, "hi")
            a.sync()

    # b starts from the persisted log, then edits concurrently with a's replica,
    # which is still holding state from the previous lifetime.
    with TestClient(create_app()) as client:
        with client.websocket_connect(f"/ws/{DOC}?client_id=b") as sb:
            b = Tab(sb, "b")
            assert b.text == "hi"
            b.type(2, " B")
            b.sync()

            # a's edit was made without ever seeing b's.
            a_ops = a.crdt.local_insert(2, " A")
            sb.send_json({"type": "ops", "ops": a_ops})
            b.sync()

            merged = client.get(f"/api/doc/{DOC}").json()["text"]

    assert "A" in merged and "B" in merged, "nothing lost across the restart"

    with TestClient(create_app()) as client:
        assert client.get(f"/api/doc/{DOC}").json()["text"] == merged


def test_documents_stay_separate_across_a_restart(pg_url: str) -> None:
    with TestClient(create_app()) as client:
        with client.websocket_connect("/ws/doc-one?client_id=a") as sa:
            Tab(sa, "a").type(0, "first")
            sa.send_json({"type": "ops", "ops": []})
            sa.receive_json()
        with client.websocket_connect("/ws/doc-two?client_id=b") as sb:
            Tab(sb, "b").type(0, "second")
            sb.send_json({"type": "ops", "ops": []})
            sb.receive_json()

    with TestClient(create_app()) as client:
        assert client.get("/api/doc/doc-one").json()["text"] == "first"
        assert client.get("/api/doc/doc-two").json()["text"] == "second"


def test_an_untouched_document_is_empty_not_missing(pg_url: str) -> None:
    with TestClient(create_app(instance_id="solo")) as client:
        state = client.get("/api/doc/never-written").json()
        assert state == {
            "doc_id": "never-written",
            "text": "",
            "rev": 0,
            "clients": 0,
            "instance": "solo",
        }
