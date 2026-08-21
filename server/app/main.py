"""Converge Stage 1 — FastAPI app: one WebSocket room per document.

Wire protocol (all JSON text frames):

    client -> server   {"type": "ops", "ops": [<operation>, ...]}

    server -> client   {"type": "snapshot", "doc_id", "ops", "rev", "clients", "client_id"}
                       {"type": "ops",      "ops", "rev", "author"}
                       {"type": "presence", "clients"}
                       {"type": "error",    "detail"}

    operation          {"kind": "insert", "id": [client, n], "parent": [client, n], "value": "x"}
                       {"kind": "delete", "id": [client, n]}

The unit of change is now an *operation naming a character*, never a position and
never the whole document. That is the entire difference from Stage 0, and it is
why the server no longer has to resolve anything: it merges, and merging always
succeeds.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .hub import Hub

log = logging.getLogger("converge")

app = FastAPI(title="Converge", version="0.2.0")

# Only needed for the plain HTTP debug routes below; WebSocket handshakes are
# not subject to CORS. The Vite dev server runs on 5173.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

hub = Hub()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/doc/{doc_id}")
async def read_doc(doc_id: str) -> dict[str, object]:
    """The server's own replica, rendered: ``curl localhost:8000/api/doc/demo``.

    Worth appreciating that this endpoint can exist at all -- the server holds a
    real CRDT replica, so it can answer "what does the document say" without
    asking any client.
    """
    doc = hub.document(doc_id)
    return {
        "doc_id": doc.doc_id,
        "text": doc.text,
        "rev": doc.rev,
        "clients": hub.client_count(doc_id),
    }


@app.websocket("/ws/{doc_id}")
async def doc_socket(
    websocket: WebSocket, doc_id: str, client_id: str | None = None
) -> None:
    await websocket.accept()
    client_id = client_id or uuid4().hex[:8]
    doc = hub.join(doc_id, client_id, websocket)
    log.info("join doc=%s client=%s clients=%d", doc_id, client_id, hub.client_count(doc_id))

    # A new tab gets the operation history rather than the text, so it builds its
    # own replica and can merge concurrent edits from the moment it connects.
    await websocket.send_json(
        {
            "type": "snapshot",
            "doc_id": doc_id,
            "ops": doc.crdt.snapshot_ops(),
            "rev": doc.rev,
            "clients": hub.client_count(doc_id),
            "client_id": client_id,
        }
    )
    await hub.broadcast(
        doc_id, {"type": "presence", "clients": hub.client_count(doc_id)}, exclude=client_id
    )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "malformed JSON"})
                continue

            if not isinstance(message, dict) or message.get("type") != "ops":
                await websocket.send_json(
                    {"type": "error", "detail": "expected {type: 'ops', ops: [...]}"}
                )
                continue

            try:
                doc = hub.apply_ops(doc_id, message.get("ops"))
            except ValueError as exc:
                await websocket.send_json({"type": "error", "detail": str(exc)})
                continue

            # Relay verbatim. The server does not rewrite, reorder or renumber
            # anything -- every replica merges the same operations independently.
            await hub.broadcast(
                doc_id,
                {"type": "ops", "ops": message["ops"], "rev": doc.rev, "author": client_id},
                exclude=client_id,
            )
    except WebSocketDisconnect:
        pass
    finally:
        hub.leave(doc_id, client_id)
        log.info("leave doc=%s client=%s clients=%d", doc_id, client_id, hub.client_count(doc_id))
        await hub.broadcast(doc_id, {"type": "presence", "clients": hub.client_count(doc_id)})
