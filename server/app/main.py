"""Converge Stage 2 — FastAPI app: one WebSocket room per document, Postgres-backed.

Wire protocol (all JSON text frames):

    client -> server   {"type": "ops", "ops": [<operation>, ...]}

    server -> client   {"type": "snapshot", "doc_id", "ops", "rev", "clients", "client_id"}
                       {"type": "ops",      "ops", "rev", "author"}
                       {"type": "presence", "clients"}
                       {"type": "error",    "detail"}

    operation          {"kind": "insert", "id": [client, n], "parent": [client, n], "value": "x"}
                       {"kind": "delete", "id": [client, n]}

The unit of change is an operation *naming a character* -- never a position, never
the whole document. Operations are persisted before they are broadcast, so a
restart can only ever lose work that was never acknowledged.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .bus import Bus, NullBus, RedisBus, redis_url
from .db import apply_schema, create_pool
from .hub import Hub
from .store import OpStore, PostgresOpStore

log = logging.getLogger("converge")


def default_instance_id() -> str:
    """Which process this is. Set INSTANCE_ID when running a cluster.

    Reported by /healthz and in every snapshot frame, so you can see which
    instance a browser tab actually landed on -- which is the whole point of
    Stage 3, both for watching it break and for watching pub/sub fix it.
    """
    return os.environ.get("INSTANCE_ID") or f"i-{uuid4().hex[:6]}"


def create_app(
    store: OpStore | None = None,
    instance_id: str | None = None,
    bus: Bus | None = None,
) -> FastAPI:
    """Build the app. Pass a store to use it; otherwise a real pool is opened.

    Injecting beats reading an env var to decide: tests build
    `create_app(InMemoryOpStore())` and get complete isolation, while uvicorn gets
    `app = create_app()` below and talks to Postgres. It also means there's no
    module-level Hub, so no state leaks between tests.
    """

    instance = instance_id or default_instance_id()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if store is not None:
            app.state.hub = Hub(
                store=store, bus=bus or NullBus(), instance_id=instance
            )
            yield
            return

        pool = await create_pool()
        await apply_schema(pool)

        # No REDIS_URL means single-instance mode: a NullBus, because there is
        # nowhere for an operation to propagate to. The app is identical
        # otherwise, which is what makes the Stage 3 before/after a config change
        # rather than a different codebase.
        url = redis_url()
        live_bus: Bus
        if url is None:
            log.warning("REDIS_URL unset — no cross-instance propagation")
            live_bus = NullBus()
        else:
            redis_bus = RedisBus(url)
            await redis_bus.start()
            live_bus = redis_bus
            log.info("instance %s on bus %s", instance, url)

        app.state.hub = Hub(store=PostgresOpStore(pool), bus=live_bus, instance_id=instance)
        try:
            yield
        finally:
            await live_bus.close()
            await pool.close()

    app = FastAPI(title="Converge", version="0.4.0", lifespan=lifespan)

    # Only needed for the plain HTTP debug routes; WebSocket handshakes are not
    # subject to CORS. The Vite dev server runs on 5173.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "instance": instance}

    @app.get("/metrics")
    async def metrics(request: Request) -> dict[str, object]:
        """Cross-instance propagation delay, as observed by *this* instance.

        Measured from the publisher's `published_at` to the moment this process
        handled the message, so it covers Redis and both instances but not the
        final hop to the browser. Wall-clock based: only meaningful when the
        instances' clocks agree. scripts/latency_probe.py is the honest
        end-to-end measurement.
        """
        hub: Hub = request.app.state.hub
        return {
            "instance": instance,
            "documents_cached": hub.document_count,
            "cross_instance_propagation": hub.latency.summary(),
        }

    @app.get("/api/doc/{doc_id}")
    async def read_doc(doc_id: str, request: Request) -> dict[str, object]:
        """The server's own replica, rendered, loading it from Postgres if this
        process hasn't seen the document yet: ``curl localhost:8000/api/doc/demo``."""
        hub: Hub = request.app.state.hub
        # cache=False: reading must never leave behind a replica that no
        # subscription is feeding. See Hub.document.
        doc = await hub.document(doc_id, cache=False)
        return {
            "doc_id": doc.doc_id,
            "text": doc.text,
            "rev": doc.rev,
            "clients": hub.client_count(doc_id),
            "instance": instance,
        }

    @app.websocket("/ws/{doc_id}")
    async def doc_socket(
        websocket: WebSocket, doc_id: str, client_id: str | None = None
    ) -> None:
        hub: Hub = websocket.app.state.hub
        await websocket.accept()
        client_id = client_id or uuid4().hex[:8]

        # attach() subscribes to this document's channel, *then* loads it, then
        # registers the socket. The order closes the join race -- see Hub.attach.
        doc = await hub.attach(doc_id, client_id, websocket)
        log.info("join doc=%s client=%s clients=%d", doc_id, client_id, hub.client_count(doc_id))

        # A new tab gets the operation history rather than the text, so it builds
        # its own replica and can merge concurrent edits from the moment it
        # connects. After a restart this history comes straight from Postgres.
        await websocket.send_json(
            {
                "type": "snapshot",
                "doc_id": doc_id,
                "ops": doc.crdt.snapshot_ops(),
                "rev": doc.rev,
                "clients": hub.client_count(doc_id),
                "client_id": client_id,
                "instance": instance,
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
                    doc, fresh = await hub.apply_ops(
                        doc_id, message.get("ops"), author=client_id
                    )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
                    continue
                except Exception:
                    # A failed write must not be reported as accepted, or the
                    # edit would disappear at the next restart.
                    log.exception("could not persist ops for doc=%s", doc_id)
                    await websocket.send_json(
                        {"type": "error", "detail": "could not persist operations"}
                    )
                    continue

                # apply_ops owns the whole ordering -- persist, apply, fan out
                # locally, then publish to the cluster -- so there is nothing to
                # do here. `fresh` is empty when a frame was entirely known
                # already, typically a client replaying its snapshot.
                del fresh
        except WebSocketDisconnect:
            pass
        finally:
            await hub.detach(doc_id, client_id)
            log.info(
                "leave doc=%s client=%s clients=%d", doc_id, client_id, hub.client_count(doc_id)
            )
            await hub.broadcast(doc_id, {"type": "presence", "clients": hub.client_count(doc_id)})

    return app


app = create_app()
