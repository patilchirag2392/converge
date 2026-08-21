"""In-memory document store and connection registry.

Each document is a full CRDT replica (see crdt.py), not a string. The server is
therefore a real participant that converges alongside the browsers rather than a
referee deciding who wins -- there is nothing left to decide.

Everything here still lives in one process's memory. That is fine only because a
single uvicorn worker means one event loop, one copy of the state, and therefore
no locking. Stage 2 makes it durable; Stage 3 makes it work across instances.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from .crdt import RGA, Op, parse_op


class Connection(Protocol):
    """The only part of starlette's WebSocket the Hub actually needs.

    Kept this narrow so tests can hand in a fake instead of a real socket.
    """

    async def send_json(self, data: Any) -> None: ...


@dataclass
class Document:
    doc_id: str
    crdt: RGA

    @property
    def text(self) -> str:
        return self.crdt.text

    @property
    def rev(self) -> int:
        """Operations applied. Display only -- unlike Stage 0's rev, nothing
        about correctness depends on it, because nothing needs ordering."""
        return self.crdt.applied


@dataclass
class Hub:
    _docs: dict[str, Document] = field(default_factory=dict)
    _rooms: dict[str, dict[str, Connection]] = field(default_factory=dict)

    def document(self, doc_id: str) -> Document:
        """Return the doc, creating an empty replica the first time it's opened."""
        doc = self._docs.get(doc_id)
        if doc is None:
            doc = self._docs[doc_id] = Document(doc_id=doc_id, crdt=RGA(f"server:{doc_id}"))
        return doc

    def apply_ops(self, doc_id: str, raw_ops: Any) -> Document:
        """Merge a batch of operations into the document's replica.

        Every operation is parsed before any is applied, so one malformed entry
        rejects the whole frame instead of leaving a half-merged batch. Beyond
        that there is no conflict handling and nothing to reject on the merge
        itself: operations commute, so any batch from any client in any order is
        simply absorbed.
        """
        if not isinstance(raw_ops, list) or not raw_ops:
            raise ValueError("ops must be a non-empty list")
        if len(raw_ops) > 5_000:
            raise ValueError("too many operations in one frame")

        parsed: list[Op] = [parse_op(raw) for raw in raw_ops]
        doc = self.document(doc_id)
        for op in parsed:
            doc.crdt.apply_parsed(op)
        return doc

    def join(self, doc_id: str, client_id: str, conn: Connection) -> Document:
        self._rooms.setdefault(doc_id, {})[client_id] = conn
        return self.document(doc_id)

    def leave(self, doc_id: str, client_id: str) -> None:
        room = self._rooms.get(doc_id)
        if room is None:
            return
        room.pop(client_id, None)
        if not room:
            # The Document deliberately outlives its last reader, so you can
            # close both tabs and reopen to find your text. It dies with the
            # process though -- Stage 2 is what makes it durable.
            del self._rooms[doc_id]

    def client_count(self, doc_id: str) -> int:
        return len(self._rooms.get(doc_id, {}))

    async def broadcast(
        self, doc_id: str, message: dict[str, Any], *, exclude: str | None = None
    ) -> None:
        """Fan a message out to every client in the room except ``exclude``.

        The author is skipped because it already applied its own operations
        locally. In Stage 0 that exclusion was the direct cause of permanent
        divergence: every client saw a different message order and there was no
        way to reconcile. It is now completely harmless -- operations commute, so
        it doesn't matter what order anyone applies them in, or that a client
        never sees its own. The bug wasn't patched; the category stopped existing.
        """
        room = self._rooms.get(doc_id, {})
        targets = [(cid, conn) for cid, conn in room.items() if cid != exclude]
        if not targets:
            return

        results = await asyncio.gather(
            *(conn.send_json(message) for _, conn in targets),
            return_exceptions=True,
        )
        for (client_id, _), result in zip(targets, results):
            if isinstance(result, BaseException):
                # Socket died between the room lookup and the send. Drop it;
                # the endpoint's finally-block handles the normal case.
                self.leave(doc_id, client_id)
