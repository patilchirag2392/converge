"""In-memory document replicas, connection rooms, and the write path to Postgres.

Each document is a full CRDT replica (see crdt.py), so the server converges
alongside the browsers rather than refereeing between them. Postgres holds the
operation log; the in-memory replica serves every read and is loaded once, on
first access.

Still a single process: one uvicorn worker means one event loop and one copy of
each replica, so no locking beyond the per-document load lock below. Stage 3 is
what breaks that assumption.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .crdt import RGA, Op, parse_op
from .store import OpStore

log = logging.getLogger("converge.hub")


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
    store: OpStore
    _docs: dict[str, Document] = field(default_factory=dict)
    _rooms: dict[str, dict[str, Connection]] = field(default_factory=dict)
    _load_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    async def document(self, doc_id: str) -> Document:
        """Return the replica, rebuilding it from the operation log on first use.

        The lock matters: two clients opening the same document at once would
        otherwise both run the load query and build competing replicas, and the
        loser's would be silently dropped along with any edits already applied
        to it.
        """
        existing = self._docs.get(doc_id)
        if existing is not None:
            return existing

        lock = self._load_locks.setdefault(doc_id, asyncio.Lock())
        async with lock:
            # Someone may have finished loading while we waited for the lock.
            existing = self._docs.get(doc_id)
            if existing is not None:
                return existing

            crdt = RGA(f"server:{doc_id}")
            ops = await self.store.load_ops(doc_id)
            for op in ops:
                crdt.apply(op)

            doc = Document(doc_id=doc_id, crdt=crdt)
            self._docs[doc_id] = doc
            log.info("loaded doc=%s ops=%d chars=%d", doc_id, len(ops), len(doc.text))
            return doc

    async def apply_ops(self, doc_id: str, raw_ops: Any) -> tuple[Document, list[Any]]:
        """Persist a batch, then merge it. Returns the doc and the *new* ops.

        Order is deliberate: Postgres is written first, so the durable log can
        never lag the in-memory replica. If the write fails nothing is applied and
        the caller reports an error, rather than serving an edit that would vanish
        on the next restart.

        The returned list holds only operations that told us something new, so a
        client re-sending its whole snapshot on reconnect doesn't cause a
        redundant broadcast storm.
        """
        if not isinstance(raw_ops, list) or not raw_ops:
            raise ValueError("ops must be a non-empty list")
        if len(raw_ops) > 5_000:
            raise ValueError("too many operations in one frame")

        # Parse everything before touching anything: one malformed operation
        # rejects the whole frame instead of leaving a half-merged batch.
        parsed: list[Op] = [parse_op(raw) for raw in raw_ops]

        doc = await self.document(doc_id)
        await self.store.append_ops(doc_id, raw_ops)

        fresh = [
            raw for raw, op in zip(raw_ops, parsed) if doc.crdt.apply_parsed(op)
        ]
        return doc, fresh

    def join(self, doc_id: str, client_id: str, conn: Connection) -> None:
        self._rooms.setdefault(doc_id, {})[client_id] = conn

    def leave(self, doc_id: str, client_id: str) -> None:
        room = self._rooms.get(doc_id)
        if room is None:
            return
        room.pop(client_id, None)
        if not room:
            del self._rooms[doc_id]
            # The replica stays in memory after the last client leaves: it's
            # already loaded, and Postgres has the durable copy either way.

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
