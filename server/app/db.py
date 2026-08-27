"""Postgres connection pool and schema setup.

asyncpg rather than an ORM on purpose: the point of this stage is that the SQL is
visible and mine. Every query in the project lives in store.py as a readable
string; nothing is generated.

Stage 4 added the retry loop in `create_pool`. Under docker-compose the server
and the database start together, and "the container is running" is not the same
event as "Postgres is accepting connections" -- see NOTES.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import asyncpg

log = logging.getLogger("converge.db")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_DATABASE_URL = "postgresql:///converge"

# Failures that mean "not ready yet" rather than "misconfigured".
#
# The distinction is the whole point of the loop. OSError covers a refused
# connection and a service name that DNS cannot resolve yet -- both normal for a
# few seconds after `docker compose up`. CannotConnectNowError is Postgres
# itself answering "the database system is starting up", which is what a fresh
# container says while it runs initdb.
#
# Deliberately NOT here: InvalidPasswordError, InvalidCatalogNameError
# (database does not exist), InvalidAuthorizationSpecificationError. Those are
# wrong config, not slow config, and retrying them for 30 seconds would turn an
# instant, readable error into a timeout that says nothing.
TRANSIENT_CONNECT_ERRORS = (
    OSError,
    asyncio.TimeoutError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
)

# One fixed key, shared by every instance. Any bigint would do; this is ASCII
# "Conv" so it is recognisable in pg_locks while debugging.
SCHEMA_LOCK_KEY = 0x436F6E76


def database_url() -> str:
    """`postgresql:///converge` uses the local socket and the OS user, which is
    what a Homebrew Postgres gives you with no setup. Under compose this is set
    to a full `postgresql://user:pass@postgres:5432/db` URL instead."""
    return os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


async def create_pool(
    url: str | None = None, *, wait: float = 0.0, **kwargs: object
) -> asyncpg.Pool:
    """One pool per process, opened in the app's lifespan and closed with it.

    `wait` is how many seconds to keep retrying a *transient* connection
    failure before giving up, with exponential backoff. It defaults to 0 --
    fail on the first attempt -- so tests and scripts keep their existing
    fail-fast behaviour and nothing hangs for half a minute on a typo. Only
    main.py raises it, from CONVERGE_CONNECT_TIMEOUT.
    """
    target = url or database_url()
    deadline = time.monotonic() + wait
    delay = 0.25
    attempt = 0

    while True:
        attempt += 1
        try:
            pool = await asyncpg.create_pool(dsn=target, min_size=1, max_size=10, **kwargs)
            if attempt > 1:
                log.info("connected to %s after %d attempts", target, attempt)
            else:
                log.info("connecting to %s", target)
            if pool is None:  # pragma: no cover - asyncpg only returns None on bad args
                raise RuntimeError(f"could not create a connection pool for {target}")
            return pool
        except TRANSIENT_CONNECT_ERRORS as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            log.warning(
                "postgres not ready (attempt %d, %.0fs left): %s", attempt, remaining, exc
            )
            await asyncio.sleep(min(delay, remaining))
            # Backoff, capped: a database that takes a minute to come up should
            # not be probed 240 times, but a database that takes two seconds
            # should not cost a full 2s poll interval either.
            delay = min(delay * 2, 2.0)


async def apply_schema(pool: asyncpg.Pool) -> None:
    """Run schema.sql. Every statement is IF NOT EXISTS, so this is safe on every
    boot -- see the note at the top of schema.sql about migrations.

    Held under a session-wide advisory lock because IF NOT EXISTS is *not* a
    concurrency primitive: two instances running `CREATE TABLE IF NOT EXISTS`
    at the same instant can both see the table missing, both try to create it,
    and one gets a duplicate-key error on pg_type. That race went from unlikely
    to routine in Stage 4 -- compose releases server-a and server-b the moment
    Postgres reports healthy, so they boot within milliseconds of each other
    against an empty database. The lock costs one round trip per boot and turns
    a startup crash into a wait.
    """
    statements = SCHEMA_PATH.read_text()
    async with pool.acquire() as connection:
        async with connection.transaction():
            # ..._xact_lock releases on commit, so there is no unlock to leak if
            # this raises partway through.
            await connection.execute("SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK_KEY)
            await connection.execute(statements)
    log.info("schema applied")
