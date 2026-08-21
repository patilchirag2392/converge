"""End-to-end tests over a real WebSocket, including the Stage 0 learn-gate
encoded as an executable claim: concurrent edits lose data.

Each test uses its own doc id, because `app.main.hub` is module-level state that
lives for the whole test session.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_snapshot_is_sent_on_connect() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/t-snap?client_id=a") as a:
        assert a.receive_json() == {
            "type": "snapshot",
            "doc_id": "t-snap",
            "text": "",
            "rev": 0,
            "clients": 1,
            "client_id": "a",
        }


def test_second_client_receives_existing_text() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws/t-join?client_id=a") as a:
            a.receive_json()
            a.send_json({"type": "edit", "text": "already here"})

            with client.websocket_connect("/ws/t-join?client_id=b") as b:
                snapshot = b.receive_json()
                assert snapshot["text"] == "already here"
                assert snapshot["rev"] == 1
                assert snapshot["clients"] == 2
                # a is told someone joined
                assert a.receive_json() == {"type": "presence", "clients": 2}


def test_edit_is_broadcast_to_others_but_not_the_author() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws/t-bcast?client_id=a") as a:
            a.receive_json()
            with client.websocket_connect("/ws/t-bcast?client_id=b") as b:
                b.receive_json()
                a.receive_json()  # presence

                a.send_json({"type": "edit", "text": "typed by a"})
                assert b.receive_json() == {
                    "type": "edit",
                    "text": "typed by a",
                    "rev": 1,
                    "author": "a",
                }

                # If the author were echoed to, this would deadlock waiting on a
                # message that should never come; instead the doc round-trips clean.
                a.send_json({"type": "edit", "text": "typed by a again"})
                assert b.receive_json()["rev"] == 2


def test_concurrent_edits_lose_data() -> None:
    """THE Stage 0 demonstration.

    Both clients hold "hi", both type one character, both send the whole
    document. Three edits reach the server; only one of the two concurrent ones
    survives anywhere. Whichever wins depends on arrival order, so this asserts
    the order-independent fact: exactly one edit was destroyed.
    """
    with TestClient(app) as client:
        with client.websocket_connect("/ws/t-race?client_id=a") as a:
            a.receive_json()
            with client.websocket_connect("/ws/t-race?client_id=b") as b:
                b.receive_json()
                a.receive_json()  # presence

                # Shared base state that both clients now agree on.
                a.send_json({"type": "edit", "text": "hi"})
                assert b.receive_json()["text"] == "hi"

                # Neither client has seen the other's keystroke yet — this is
                # the whole point. Both are editing revision 1.
                a.send_json({"type": "edit", "text": "hi A"})
                b.send_json({"type": "edit", "text": "hi B"})

                # Each side is handed the other's full document.
                assert b.receive_json()["text"] == "hi A"
                assert a.receive_json()["text"] == "hi B"

        state = client.get("/api/doc/t-race").json()

    assert state["rev"] == 3, "three edits were applied..."
    assert ("A" in state["text"]) != ("B" in state["text"]), (
        "...but only one of the two concurrent edits survived. Nothing merged "
        f"them: the document is {state['text']!r}, not 'hi AB'."
    )


def test_clients_can_diverge_permanently() -> None:
    """The nastier half of the Stage 0 bug: replicas don't even agree.

    Because the server never echoes an edit to its author, each client's final
    state is "my own last keystroke, unless a remote frame landed after it".
    Those two orderings differ, and nothing ever reconciles them: the clients
    sit at different text *and* different revs until someone types again.

    A client cannot fix this on its own — it is never told the rev its own edit
    was assigned, so it has no way to know whether an incoming rev is newer or
    older than what it is already showing.
    """
    with TestClient(app) as client:
        with client.websocket_connect("/ws/t-div?client_id=a") as a:
            a.receive_json()
            with client.websocket_connect("/ws/t-div?client_id=b") as b:
                b.receive_json()
                a.receive_json()  # presence

                a.send_json({"type": "edit", "text": "from a"})  # rev 1
                b_inbound = b.receive_json()
                b.send_json({"type": "edit", "text": "from b"})  # rev 2
                a_inbound = a.receive_json()

                server = client.get("/api/doc/t-div").json()

    # a applied the newer frame, so a happens to match the server.
    assert (a_inbound["text"], a_inbound["rev"]) == ("from b", 2)
    assert server["text"] == "from b" and server["rev"] == 2

    # b applied rev 1 *after* typing what became rev 2, so b is showing older
    # text at an older rev than the server, and stays there.
    assert (b_inbound["text"], b_inbound["rev"]) == ("from a", 1)
    assert b_inbound["text"] != server["text"], "b is out of sync with the server"


def test_documents_are_isolated_by_id() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws/t-iso-1?client_id=a") as a:
            a.receive_json()
            with client.websocket_connect("/ws/t-iso-2?client_id=b") as b:
                b.receive_json()
                a.send_json({"type": "edit", "text": "only in doc 1"})

        assert client.get("/api/doc/t-iso-1").json()["text"] == "only in doc 1"
        assert client.get("/api/doc/t-iso-2").json()["text"] == ""


def test_presence_drops_when_a_client_disconnects() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws/t-leave?client_id=a") as a:
            a.receive_json()
            with client.websocket_connect("/ws/t-leave?client_id=b") as b:
                b.receive_json()
                assert a.receive_json()["clients"] == 2

                # Close explicitly rather than letting the context manager do
                # it: `WebSocketTestSession.__exit__` *cancels* the server task,
                # so the handler's finally-block would be torn down mid-await
                # and never send this. `close()` delivers a real
                # websocket.disconnect, which is what uvicorn does in prod.
                b.close()
                assert a.receive_json() == {"type": "presence", "clients": 1}

        assert client.get("/api/doc/t-leave").json()["clients"] == 0


def test_malformed_frames_are_rejected_without_killing_the_socket() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/t-bad?client_id=a") as a:
        a.receive_json()

        a.send_text("this is not json")
        assert a.receive_json() == {"type": "error", "detail": "malformed JSON"}

        a.send_json({"type": "edit"})  # missing text
        assert a.receive_json()["type"] == "error"

        a.send_json({"type": "edit", "text": 42})  # wrong type
        assert a.receive_json()["type"] == "error"

        # Still usable afterwards.
        a.send_json({"type": "edit", "text": "fine"})
        assert client.get("/api/doc/t-bad").json()["text"] == "fine"


def test_healthz() -> None:
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
