"""Postgres connection pool and schema setup.

asyncpg rather than an ORM on purpose: the point of this stage is that the SQL is
visible and mine. Every query in the project lives in store.py as a readable
string; nothing is generated.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import asyncpg

log = logging.getLogger("converge.db")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_DATABASE_URL = "postgresql:///converge"


def database_url() -> str:
    """`postgresql:///converge` uses the local socket and the OS user, which is
    what a Homebrew Postgres gives you with no setup."""
    return os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


async def create_pool(url: str | None = None, **kwargs: object) -> asyncpg.Pool:
    """One pool per process, opened in the app's lifespan and closed with it."""
    target = url or database_url()
    log.info("connecting to %s", target)
    pool = await asyncpg.create_pool(dsn=target, min_size=1, max_size=10, **kwargs)
    if pool is None:  # pragma: no cover - asyncpg only returns None on bad args
        raise RuntimeError(f"could not create a connection pool for {target}")
    return pool


async def apply_schema(pool: asyncpg.Pool) -> None:
    """Run schema.sql. Every statement is IF NOT EXISTS, so this is safe on every
    boot -- see the note at the top of schema.sql about migrations."""
    statements = SCHEMA_PATH.read_text()
    async with pool.acquire() as connection:
        await connection.execute(statements)
    log.info("schema applied")
