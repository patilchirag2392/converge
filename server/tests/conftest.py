"""Fixtures for the tests that need a real Postgres.

Everything else in the suite runs against `InMemoryOpStore` and needs no
database. These fixtures skip (rather than fail) when Postgres isn't reachable,
so `uv run pytest` still works on a machine without it.

    createdb converge_test        # one-time setup
    TEST_DATABASE_URL=... uv run pytest    # or point it elsewhere
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import asyncpg
import pytest

from app.db import SCHEMA_PATH

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql:///converge_test")

UNREACHABLE = (OSError, asyncpg.PostgresError, asyncpg.exceptions._base.PostgresError)


async def _prepare(url: str) -> None:
    """Apply the schema and empty the tables. TRUNCATE on `documents` cascades
    into `operations` via the foreign key."""
    connection = await asyncpg.connect(dsn=url)
    try:
        await connection.execute(SCHEMA_PATH.read_text())
        await connection.execute("TRUNCATE documents CASCADE")
    finally:
        await connection.close()


@pytest.fixture
async def pg_pool() -> AsyncIterator[asyncpg.Pool]:
    """A clean pool, for tests that talk to the store directly."""
    try:
        await _prepare(TEST_DATABASE_URL)
        pool = await asyncpg.create_pool(dsn=TEST_DATABASE_URL, min_size=1, max_size=5)
    except UNREACHABLE as exc:
        pytest.skip(f"no Postgres at {TEST_DATABASE_URL}: {exc}")

    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def pg_url(monkeypatch: pytest.MonkeyPatch) -> str:
    """Clean database, with DATABASE_URL pointed at it.

    Synchronous on purpose. These tests drive the app through `TestClient`, which
    runs it in its own event loop -- so the app has to open its own pool in that
    loop. A pool created out here could not be used in there.
    """
    try:
        asyncio.run(_prepare(TEST_DATABASE_URL))
    except UNREACHABLE as exc:
        pytest.skip(f"no Postgres at {TEST_DATABASE_URL}: {exc}")

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    return TEST_DATABASE_URL
