"""Document replicas, connection rooms, and the write/propagate paths.

Each document is a full CRDT replica (see crdt.py), so a server converges
alongside the browsers rather than refereeing between them. Postgres holds the
operation log; Redis (see bus.py) carries operations between instances live.

Two orderings in here are load-bearing, both for the same reason -- Postgres is
the source of truth and nothing may be visible that isn't durable:

  1. persist -> apply -> publish, never the reverse
  2. subscribe -> load -> register, so nothing published mid-load is missed

One invariant worth stating: **a document is cached here if and only if this
instance is subscribed to it**, which is if and only if it has at least one local
client. Breaking that is how you get the Stage 3 stale-replica bug -- a cached
copy nobody is feeding updates to.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from .bus import Bus, NullBus, OpMessage
from .crdt import RGA, Op, parse_op
from .store import OpStore

log = logging.getLogger("converge.hub")


class Connection(Protocol):
    """The only part of starlette's WebSocket the Hub actually needs.

    Kept this narrow so tests can hand in a fake instead of a real socket.
    """

    async def send_json(self, data: Any) -> None: ...


class PropagationLatency:
    """Rolling window of observed cross-instance propagation delays, in ms.

    Measured as `now - message.published_at`, so it covers Redis plus both
    instances' handling, but *not* the final hop to the browser. Wall-clock
    based, so it is only meaningful when the two instances' clocks agree.
    scripts/latency_probe.py measures the honest end-to-end number using one
    clock; this exists so a running cluster can report on itself.
    """

    def __init__(self, size: int = 512) -> None:
        self._samples: deque[float] = deque(maxlen=size)

    def record(self, seconds: float) -> None:
        # A negative sample means the clocks disagree; recording it would be
        # worse than dropping it, because it silently flatters the average.
        if seconds >= 0:
            self._samples.append(seconds * 1000.0)

    def summary(self) -> dict[str, float | int]:
        if not self._samples:
            return {"count": 0}
        ordered = sorted(self._samples)
        return {
            "count": len(ordered),
            "p50_ms": round(statistics.median(ordered), 3),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
            "max_ms": round(ordered[-1], 3),
        }


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
    bus: Bus = field(default_factory=NullBus)
    instance_id: str = "single"
    _docs: dict[str, Document] = field(default_factory=dict)
    _rooms: dict[str, dict[str, Connection]] = field(default_factory=dict)
    _load_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    latency: PropagationLatency = field(default_factory=PropagationLatency)

    def __post_init__(self) -> None:
        self.bus.on_message(self.apply_remote_ops)

    async def document(self, doc_id: str, *, cache: bool = True) -> Document:
        """Return the replica, rebuilding it from the operation log on first use.

        The lock matters: two clients opening the same document at once would
        otherwise both run the load query and build competing replicas, and the
        loser's would be silently dropped along with any edits already applied
        to it.

        `cache=False` returns a throwaway replica instead of populating the cache.
        Read-only callers use it, because a cached replica with no subscription
        feeding it is precisely the stale-replica bug: it would answer with
        whatever the document said when it was first read, forever.
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
            if cache:
                self._docs[doc_id] = doc
            log.info(
                "loaded doc=%s ops=%d chars=%d cached=%s", doc_id, len(ops), len(doc.text), cache
            )
            return doc

    async def apply_ops(
        self, doc_id: str, raw_ops: Any, *, author: str = ""
    ) -> tuple[Document, list[Any]]:
        """Persist a batch, merge it, then publish it. Returns the doc and the
        operations that were actually new.

        **Order: persist -> apply -> broadcast locally -> publish.** Every step
        of that sequence is deliberate:

        * *Persist first* so the durable log never lags anything anyone can see.
          If the write fails, nothing is applied, nothing is sent, nothing is
          published, and the caller reports an error -- rather than showing an
          edit that vanishes at the next restart. The cost is that propagation
          latency includes the database commit; publishing first would shave that
          off in exchange for edits that are visible but not durable, which is a
          bad trade for an editor.
        * *Local broadcast before publish* so clients on this instance don't wait
          on a Redis round trip to see their neighbour's typing. Getting this
          backwards is not a correctness bug, but it put the publish latency into
          the same-instance path and made the two indistinguishable when measured.

        Only *new* operations are published, so a client replaying its snapshot
        after a reconnect doesn't get fanned out across the whole cluster.
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

        if fresh:
            await self.broadcast(
                doc_id,
                {"type": "ops", "ops": fresh, "rev": doc.rev, "author": author},
                exclude=author or None,
            )
            await self.bus.publish(
                OpMessage.create(doc_id, fresh, origin=self.instance_id, author=author)
            )
        return doc, fresh

    async def apply_remote_ops(self, message: OpMessage) -> None:
        """Operations from another instance: merge locally and fan out. That's all.

        Explicitly **not** done here:

        * **Not persisted.** The originating instance already committed them
          before publishing, so writing again would be pure duplicate work. (It
          would be *harmless* -- ON CONFLICT DO NOTHING -- but pointless.)
        * **Not republished.** Otherwise every instance would rebroadcast
          everything it hears and the cluster would amplify each edit forever.

        Loop prevention is explicit rather than incidental: we drop messages we
        published ourselves by comparing `origin`. The CRDT's idempotency would
        make a self-delivered op harmless to *state*, but relying on that would
        still mean re-sending operations to local clients that already have them,
        and would pollute the latency numbers. So: prevent the loop where it's
        exact and free, and keep idempotency as the backstop for the duplicates
        we can't prevent -- redelivery, a client reconnecting onto a different
        instance and replaying, two instances both hearing the same op.
        """
        if message.origin == self.instance_id:
            return

        self.latency.record(time.time() - message.published_at)

        doc = self._docs.get(message.doc_id)
        if doc is None:
            # Not serving this document: no local clients, nothing cached. The
            # operation is already durable, so the next load picks it up.
            return

        fresh: list[Any] = []
        for raw in message.ops:
            try:
                if doc.crdt.apply(raw):
                    fresh.append(raw)
            except ValueError:
                log.warning("dropping unusable remote op for doc=%s: %r", message.doc_id, raw)

        if not fresh:
            return

        # `exclude` is belt-and-braces: the author is normally on another
        # instance entirely, but it could be connected here too mid-reconnect.
        await self.broadcast(
            message.doc_id,
            {"type": "ops", "ops": fresh, "rev": doc.rev, "author": message.author},
            exclude=message.author or None,
        )

    async def attach(self, doc_id: str, client_id: str, conn: Connection) -> Document:
        """Take on a client: subscribe, load, then register. In that order.

        **Subscribing before loading is what closes the join race.** Load first
        and any operation published between the query and the subscription is
        lost to this instance -- it isn't in the rows we read, and we weren't
        listening when it went past. Subscribe first and the worst case is
        receiving an operation we also read from Postgres, which is a no-op
        because applying is idempotent.
        """
        await self.bus.subscribe(doc_id)
        doc = await self.document(doc_id)
        self.join(doc_id, client_id, conn)
        return doc

    async def detach(self, doc_id: str, client_id: str) -> None:
        """Drop a client, and if it was the last one, stop serving the document.

        Unsubscribing and evicting together is deliberate: they maintain the
        invariant that a cached replica always has a live subscription feeding
        it. Keep the cache without the subscription and you get Stage 3's
        stale-replica bug; keep the subscription without the cache and the
        instance does work for documents nobody here is reading.

        The cost is re-reading from Postgres if the document is reopened later.
        Worth it -- that's one query against being confidently wrong.
        """
        self.leave(doc_id, client_id)
        if self.client_count(doc_id) == 0:
            self._docs.pop(doc_id, None)
            self._load_locks.pop(doc_id, None)
            await self.bus.unsubscribe(doc_id)

    def join(self, doc_id: str, client_id: str, conn: Connection) -> None:
        """Registration only, no I/O. `attach` is the full path."""
        self._rooms.setdefault(doc_id, {})[client_id] = conn

    def leave(self, doc_id: str, client_id: str) -> None:
        """Deregistration only, no I/O. `detach` is the full path."""
        room = self._rooms.get(doc_id)
        if room is None:
            return
        room.pop(client_id, None)
        if not room:
            del self._rooms[doc_id]

    def client_count(self, doc_id: str) -> int:
        return len(self._rooms.get(doc_id, {}))

    @property
    def document_count(self) -> int:
        """Replicas held in memory. Equals the number of subscriptions, by the
        invariant in this module's docstring."""
        return len(self._docs)

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
                # Socket died between the room lookup and the send. Full detach,
                # not just leave: if that was the last client we must give up the
                # subscription and the cached replica too, or we'd be left
                # holding a copy with nothing feeding it.
                await self.detach(doc_id, client_id)
