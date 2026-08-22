"""Persistence for the CRDT operation log.

Every SQL statement in the project is here, as a module-level string you can copy
into psql. Nothing is generated.

We store operations, not text. For an RGA that is also the materialized state --
nothing is ever discarded, so `snapshot_ops()` and "every op ever applied" are the
same set of rows. See NOTES.md for the full argument.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import asyncpg

from .crdt import Delete, Insert, delete_op, insert_op, parse_op
from .crdt import Node as CrdtNode

log = logging.getLogger("converge.store")


# --------------------------------------------------------------------- the SQL

LOAD_OPS_SQL = """
    SELECT kind, client_id, counter, parent_client_id, parent_counter, value
      FROM operations
     WHERE doc_id = $1
     ORDER BY seq
"""
"""ORDER BY is for humans reading psql output, not for correctness: `RGA.apply`
is commutative and parks causally-early inserts, so any order rebuilds the same
document. Worth knowing -- it means a missing index here costs speed, never
consistency."""

ENSURE_DOCUMENT_SQL = """
    INSERT INTO documents (doc_id)
    VALUES ($1)
    ON CONFLICT (doc_id) DO NOTHING
"""

APPEND_OP_SQL = """
    INSERT INTO operations (
        doc_id, kind, client_id, counter, parent_client_id, parent_counter, value
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (doc_id, kind, client_id, counter) DO NOTHING
"""
"""The primary key is the operation's own identity, so the conflict clause is the
entire idempotency story: replaying a frame, or two clients deleting the same
character, costs nothing and corrupts nothing."""

TOUCH_DOCUMENT_SQL = """
    UPDATE documents
       SET updated_at = now()
     WHERE doc_id = $1
"""

COUNT_OPS_SQL = """
    SELECT count(*) FROM operations WHERE doc_id = $1
"""

# Handy in psql, not used by the app:
#
#   -- biggest documents by operation count
#   SELECT doc_id, count(*) AS ops, max(created_at) AS last_edit
#     FROM operations GROUP BY doc_id ORDER BY ops DESC LIMIT 10;
#
#   -- how much of a document is tombstones
#   SELECT count(*) FILTER (WHERE kind = 'insert') AS characters,
#          count(*) FILTER (WHERE kind = 'delete') AS deleted
#     FROM operations WHERE doc_id = 'demo';


# ------------------------------------------------------- rows <-> operations


def row_to_op(row: Any) -> dict[str, Any]:
    """Build the same wire-format dict the WebSocket carries, so loading reuses
    `RGA.apply` and its validation rather than a second deserializer."""
    if row["kind"] == "insert":
        return insert_op(
            CrdtNode(
                id=(row["client_id"], row["counter"]),
                parent=(row["parent_client_id"], row["parent_counter"]),
                value=row["value"],
            )
        )
    return delete_op((row["client_id"], row["counter"]))


def op_to_row(doc_id: str, raw: Any) -> tuple[Any, ...]:
    """Parameters for APPEND_OP_SQL, validated through the CRDT's own parser so
    the database can never hold an operation the CRDT would reject."""
    op = parse_op(raw)
    if isinstance(op, Insert):
        node = op.node
        return (
            doc_id,
            "insert",
            node.id[0],
            node.id[1],
            node.parent[0],
            node.parent[1],
            node.value,
        )
    assert isinstance(op, Delete)
    return (doc_id, "delete", op.id[0], op.id[1], None, None, None)


# ------------------------------------------------------------------- the store


class OpStore(Protocol):
    """Exists so the fast unit tests can run without a database. It is not a
    pretence at database portability -- there is one real implementation."""

    async def load_ops(self, doc_id: str) -> list[dict[str, Any]]: ...

    async def append_ops(self, doc_id: str, raw_ops: list[Any]) -> None: ...


class PostgresOpStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def load_ops(self, doc_id: str) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(LOAD_OPS_SQL, doc_id)
        log.info("loaded doc=%s ops=%d", doc_id, len(rows))
        return [row_to_op(row) for row in rows]

    async def append_ops(self, doc_id: str, raw_ops: list[Any]) -> None:
        """One transaction per frame: every operation from a batch commits, or
        none of them do."""
        rows = [op_to_row(doc_id, raw) for raw in raw_ops]
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(ENSURE_DOCUMENT_SQL, doc_id)
                # executemany rather than one unnest(...) multi-row insert:
                # legibility beats the last bit of throughput at this scale.
                await connection.executemany(APPEND_OP_SQL, rows)
                await connection.execute(TOUCH_DOCUMENT_SQL, doc_id)

    async def count_ops(self, doc_id: str) -> int:
        return await self._pool.fetchval(COUNT_OPS_SQL, doc_id)


class InMemoryOpStore:
    """Test double: same contract and the same *semantics*, minus durability.

    Deliberately keyed by the operation's identity, mirroring the Postgres
    primary key, so a duplicate append is dropped here exactly as
    `ON CONFLICT DO NOTHING` drops it there. A double that dedups differently
    from the real thing would let tests pass on data Postgres rejects.
    """

    def __init__(self) -> None:
        # doc_id -> {op identity: op}. Insertion-ordered, standing in for `seq`.
        self.docs: dict[str, dict[tuple[str, str, int], dict[str, Any]]] = {}

    async def load_ops(self, doc_id: str) -> list[dict[str, Any]]:
        return list(self.docs.get(doc_id, {}).values())

    async def append_ops(self, doc_id: str, raw_ops: list[Any]) -> None:
        # Validate exactly like the real store, so tests can't pass on data
        # Postgres would have rejected.
        rows = [op_to_row(doc_id, raw) for raw in raw_ops]
        ops = self.docs.setdefault(doc_id, {})
        for row, raw in zip(rows, raw_ops):
            _, kind, client_id, counter = row[:4]
            ops.setdefault((kind, client_id, counter), raw)

    async def count_ops(self, doc_id: str) -> int:
        return len(self.docs.get(doc_id, {}))
