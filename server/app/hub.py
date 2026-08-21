"""In-memory document store and connection registry.

Stage 0 has no conflict resolution on purpose. ``apply_edit`` replaces the whole
document with whatever the most recent writer sent, so two people typing at once
will destroy each other's work. See ROADMAP.md — Stage 1 is where that gets fixed.

Everything here lives in one process's memory. That is fine only because a single
uvicorn worker means one event loop, one copy of the state, and therefore no
locking: each message handler runs to completion before the next one starts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol


class Connection(Protocol):
    """The only part of starlette's WebSocket the Hub actually needs.

    Kept this narrow so tests can hand in a fake instead of a real socket.
    """

    async def send_json(self, data: Any) -> None: ...


@dataclass
class Document:
    doc_id: str
    text: str = ""
    rev: int = 0
    """Monotonic edit counter. Observability only — nothing checks it before
    applying an edit, which is exactly why edits can be lost."""


@dataclass
class Hub:
    _docs: dict[str, Document] = field(default_factory=dict)
    _rooms: dict[str, dict[str, Connection]] = field(default_factory=dict)

    def document(self, doc_id: str) -> Document:
        """Return the doc, creating an empty one the first time it's opened."""
        doc = self._docs.get(doc_id)
        if doc is None:
            doc = self._docs[doc_id] = Document(doc_id=doc_id)
        return doc

    def apply_edit(self, doc_id: str, text: str) -> Document:
        """Last write wins: the new text becomes the document.

        The caller never tells us which revision they were editing, so we cannot
        detect a concurrent edit even in principle — there is nothing to compare.
        Whatever the previous writer typed is simply gone.
        """
        doc = self.document(doc_id)
        doc.text = text
        doc.rev += 1
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
            # process though — Stage 2 is what makes it durable.
            del self._rooms[doc_id]

    def client_count(self, doc_id: str) -> int:
        return len(self._rooms.get(doc_id, {}))

    async def broadcast(
        self, doc_id: str, message: dict[str, Any], *, exclude: str | None = None
    ) -> None:
        """Fan a message out to every client in the room except ``exclude``.

        The author is excluded because they already applied the edit locally;
        echoing it back would stomp their caret for no reason.
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
