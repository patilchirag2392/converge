"""Fixtures for the tests that need a real Postgres.

Everything else in the suite runs against `InMemoryOpStore` and needs no
database. These fixtures skip (rather than fail) when Postgres isn't reachable,
so `uv run pytest` still works on a machine without it.

    createdb converge_test        # one-time setup
    TEST_DATABASE_URL=... uv run pytest    # or point it elsewhere

Skipping is right on a laptop and wrong in CI. CI provisions Postgres and Redis
as service containers specifically so the integration tests run -- if they
skipped there, the suite would go green while never touching a database, which
is worse than no CI. Set CONVERGE_REQUIRE_SERVICES=1 to turn every skip below
into a failure; the workflow does exactly that.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any, NoReturn

import asyncpg
import pytest

from app.db import SCHEMA_PATH

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql:///converge_test")

UNREACHABLE = (OSError, asyncpg.PostgresError, asyncpg.exceptions._base.PostgresError)

REQUIRE_SERVICES = os.environ.get("CONVERGE_REQUIRE_SERVICES", "") not in ("", "0", "false")


def _unavailable(reason: str) -> NoReturn:
    """Skip on a dev machine, fail loudly where the service was promised."""
    if REQUIRE_SERVICES:
        # pytrace=False: the fixture's own traceback says nothing useful; the
        # reason string is the whole message.
        pytest.fail(f"{reason} (CONVERGE_REQUIRE_SERVICES is set)", pytrace=False)
    pytest.skip(reason)


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
        _unavailable(f"no Postgres at {TEST_DATABASE_URL}: {exc}")

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
        _unavailable(f"no Postgres at {TEST_DATABASE_URL}: {exc}")

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    return TEST_DATABASE_URL


REDIS_TEST_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
async def redis_bus_factory() -> AsyncIterator[Any]:
    """Builds started RedisBus instances and closes them afterwards.

    Skips when Redis isn't reachable. Uses database 15 -- pub/sub isn't actually
    scoped by database in Redis, but it keeps any future key use out of the way.
    """
    from app.bus import RedisBus

    probe = RedisBus(REDIS_TEST_URL)
    try:
        await probe.start()
    except Exception as exc:  # ConnectionError, OSError, ...
        await probe.close()
        _unavailable(f"no Redis at {REDIS_TEST_URL}: {exc}")
    await probe.close()

    created: list[Any] = []

    async def make(handler: Any = None) -> Any:
        bus = RedisBus(REDIS_TEST_URL)
        if handler is not None:
            bus.on_message(handler)
        await bus.start()
        created.append(bus)
        return bus

    try:
        yield make
    finally:
        for bus in created:
            await bus.close()
