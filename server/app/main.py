"""Converge Stage 0 — FastAPI app: one WebSocket room per document.

Wire protocol (all JSON text frames):

    client -> server   {"type": "edit", "text": "<the entire document>"}

    server -> client   {"type": "snapshot", "doc_id", "text", "rev", "clients", "client_id"}
                       {"type": "edit",     "text", "rev", "author"}
                       {"type": "presence", "clients"}
                       {"type": "error",    "detail"}

The unit of change is the whole document, not an operation. That is the Stage 0
bug in one sentence.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .hub import Hub

log = logging.getLogger("converge")

app = FastAPI(title="Converge", version="0.1.0")

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
    """Peek at server-side state without a browser: ``curl localhost:8000/api/doc/demo``."""
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

    await websocket.send_json(
        {
            "type": "snapshot",
            "doc_id": doc_id,
            "text": doc.text,
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

            if (
                not isinstance(message, dict)
                or message.get("type") != "edit"
                or not isinstance(message.get("text"), str)
            ):
                await websocket.send_json(
                    {"type": "error", "detail": "expected {type: 'edit', text: string}"}
                )
                continue

            # The entire conflict-resolution strategy, such as it is.
            doc = hub.apply_edit(doc_id, message["text"])
            await hub.broadcast(
                doc_id,
                {"type": "edit", "text": doc.text, "rev": doc.rev, "author": client_id},
                exclude=client_id,
            )
    except WebSocketDisconnect:
        pass
    finally:
        hub.leave(doc_id, client_id)
        log.info("leave doc=%s client=%s clients=%d", doc_id, client_id, hub.client_count(doc_id))
        await hub.broadcast(doc_id, {"type": "presence", "clients": hub.client_count(doc_id)})
