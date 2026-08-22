"""Integration tests for PostgresOpStore, against a real database.

Skipped when Postgres isn't reachable -- see conftest.py. These are the tests
that justify hand-writing the SQL: they check the constraints and the transaction
boundary, not just that Python objects round-trip.
"""

from __future__ import annotations

import asyncpg
import pytest

from app.crdt import RGA
from app.store import PostgresOpStore


async def test_append_then_load_round_trips(pg_pool: asyncpg.Pool) -> None:
    store = PostgresOpStore(pg_pool)
    writer = RGA("writer")

    await store.append_ops("demo", writer.local_insert(0, "hello world"))
    await store.append_ops("demo", writer.local_delete(5, 6))

    rebuilt = RGA("reader")
    for op in await store.load_ops("demo"):
        rebuilt.apply(op)

    assert rebuilt.text == writer.text == "hello"


async def test_load_is_order_independent(pg_pool: asyncpg.Pool) -> None:
    """The load query's ORDER BY is for humans, not for correctness.

    Applying the rows backwards has to produce the same document, because
    `RGA.apply` parks causally-early inserts. Means a missing index here would
    cost speed, never consistency.
    """
    store = PostgresOpStore(pg_pool)
    writer = RGA("writer")
    await store.append_ops("demo", writer.local_insert(0, "the quick brown fox"))

    ops = await store.load_ops("demo")
    forwards, backwards = RGA("f"), RGA("b")
    for op in ops:
        forwards.apply(op)
    for op in reversed(ops):
        backwards.apply(op)

    assert forwards.text == backwards.text == writer.text


async def test_appending_the_same_operations_twice_is_a_no_op(pg_pool: asyncpg.Pool) -> None:
    """ON CONFLICT DO NOTHING is the whole idempotency story."""
    store = PostgresOpStore(pg_pool)
    ops = RGA("writer").local_insert(0, "hi")

    await store.append_ops("demo", ops)
    await store.append_ops("demo", ops)
    await store.append_ops("demo", ops)

    assert await store.count_ops("demo") == 2


async def test_two_clients_deleting_the_same_character_store_one_row(
    pg_pool: asyncpg.Pool,
) -> None:
    """A delete names the character, not the deleter, so both produce the same
    row and the second costs nothing."""
    store = PostgresOpStore(pg_pool)
    alice, bob = RGA("alice"), RGA("bob")
    base = alice.local_insert(0, "abc")
    for op in base:
        bob.apply(op)
    await store.append_ops("demo", base)

    await store.append_ops("demo", alice.local_delete(1, 1))
    await store.append_ops("demo", bob.local_delete(1, 1))

    assert await store.count_ops("demo") == 4  # 3 inserts + 1 delete


async def test_documents_are_isolated(pg_pool: asyncpg.Pool) -> None:
    store = PostgresOpStore(pg_pool)
    await store.append_ops("doc-1", RGA("a").local_insert(0, "one"))
    await store.append_ops("doc-2", RGA("b").local_insert(0, "two"))

    assert await store.count_ops("doc-1") == 3
    assert await store.count_ops("doc-2") == 3
    assert [op["value"] for op in await store.load_ops("doc-1")] == list("one")


async def test_appending_creates_the_document_row(pg_pool: asyncpg.Pool) -> None:
    store = PostgresOpStore(pg_pool)
    await store.append_ops("demo", RGA("a").local_insert(0, "x"))

    row = await pg_pool.fetchrow("SELECT * FROM documents WHERE doc_id = $1", "demo")
    assert row is not None
    assert row["created_at"] is not None


async def test_appending_touches_updated_at(pg_pool: asyncpg.Pool) -> None:
    store = PostgresOpStore(pg_pool)
    writer = RGA("a")
    await store.append_ops("demo", writer.local_insert(0, "x"))
    first = await pg_pool.fetchval("SELECT updated_at FROM documents WHERE doc_id = $1", "demo")

    await store.append_ops("demo", writer.local_insert(1, "y"))
    second = await pg_pool.fetchval("SELECT updated_at FROM documents WHERE doc_id = $1", "demo")

    assert second > first


async def test_a_batch_is_all_or_nothing(pg_pool: asyncpg.Pool) -> None:
    """One transaction per frame.

    The trigger here is a lone UTF-16 surrogate: Python's `len()` says one
    character so `parse_op` accepts it, but it cannot be encoded as UTF-8, so the
    write fails. That's a real (documented) limitation of the textarea diff, and
    it doubles as proof the transaction rolls back -- the valid operations either
    side of it must not survive.
    """
    store = PostgresOpStore(pg_pool)
    writer = RGA("writer")
    good = writer.local_insert(0, "ab")
    poison = [{"kind": "insert", "id": ["writer", 99], "parent": ["", 0], "value": "\ud83d"}]

    with pytest.raises(Exception):
        await store.append_ops("demo", [*good, *poison])

    assert await store.count_ops("demo") == 0, "the whole frame must roll back"


async def test_the_schema_rejects_a_malformed_row(pg_pool: asyncpg.Pool) -> None:
    """The shape invariant is enforced by the database, not only by Python.

    Goes around the store deliberately -- this is testing the CHECK constraint.
    """
    await pg_pool.execute("INSERT INTO documents (doc_id) VALUES ('demo')")

    # An insert with no parent and no value.
    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await pg_pool.execute(
            "INSERT INTO operations (doc_id, kind, client_id, counter)"
            " VALUES ('demo', 'insert', 'a', 1)"
        )

    # A delete that carries a value anyway.
    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await pg_pool.execute(
            "INSERT INTO operations (doc_id, kind, client_id, counter, value)"
            " VALUES ('demo', 'delete', 'a', 2, 'x')"
        )

    # A value longer than one character.
    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await pg_pool.execute(
            "INSERT INTO operations"
            " (doc_id, kind, client_id, counter, parent_client_id, parent_counter, value)"
            " VALUES ('demo', 'delete', 'a', 3, '', 0, 'too long')"
        )

    # A kind that isn't one of ours.
    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await pg_pool.execute(
            "INSERT INTO operations (doc_id, kind, client_id, counter)"
            " VALUES ('demo', 'nonsense', 'a', 4)"
        )


async def test_operations_require_a_known_document(pg_pool: asyncpg.Pool) -> None:
    """The foreign key means orphaned operations can't exist."""
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pg_pool.execute(
            "INSERT INTO operations"
            " (doc_id, kind, client_id, counter, parent_client_id, parent_counter, value)"
            " VALUES ('never-created', 'insert', 'a', 1, '', 0, 'x')"
        )


async def test_deleting_a_document_removes_its_operations(pg_pool: asyncpg.Pool) -> None:
    """ON DELETE CASCADE, so there's one place to clean up."""
    store = PostgresOpStore(pg_pool)
    await store.append_ops("demo", RGA("a").local_insert(0, "hello"))
    assert await store.count_ops("demo") == 5

    await pg_pool.execute("DELETE FROM documents WHERE doc_id = $1", "demo")
    assert await store.count_ops("demo") == 0


async def test_a_large_document_round_trips(pg_pool: asyncpg.Pool) -> None:
    """2,000 characters is 2,000 rows. Worth knowing what that costs."""
    store = PostgresOpStore(pg_pool)
    writer = RGA("writer")
    text = "lorem ipsum dolor sit amet " * 77  # ~2000 chars

    await store.append_ops("demo", writer.local_insert(0, text))

    rebuilt = RGA("reader")
    for op in await store.load_ops("demo"):
        rebuilt.apply(op)
    assert rebuilt.text == text
    assert await store.count_ops("demo") == len(text)
