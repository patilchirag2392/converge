"""Do two clients on *different* instances see each other's edits?

Run the cluster first (scripts/cluster.sh up), then this. It reports whichever
answer it observes, so the same script tells the Stage 3 story twice:

    without Redis   -> edits do NOT propagate live; the instances are islands
    with Redis      -> edits propagate, and you get a rough latency figure

Usage:
    uv run python scripts/multi_instance_demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crdt import RGA  # noqa: E402
from app.db import create_pool  # noqa: E402
from app.store import PostgresOpStore  # noqa: E402

BALANCER_HTTP = "http://localhost:8080"
BALANCER_WS = "ws://localhost:8080"
DOC_ID = f"multi-{int(time.time())}"
PROPAGATION_TIMEOUT = 3.0


class Client:
    """One browser tab: a socket, its own replica, and the instance it landed on."""

    def __init__(self, name: str, socket: Any, snapshot: dict[str, Any]) -> None:
        self.name = name
        self.socket = socket
        self.instance = snapshot["instance"]
        self.crdt = RGA(name)
        for op in snapshot["ops"]:
            self.crdt.apply(op)

    @property
    def text(self) -> str:
        return self.crdt.text

    async def type_at(self, index: int, text: str) -> None:
        ops = self.crdt.local_insert(index, text)
        await self.socket.send(json.dumps({"type": "ops", "ops": ops}))

    async def wait_for_ops(self, timeout: float) -> float | None:
        """Seconds until an ops frame arrives, or None if it never does."""
        started = time.monotonic()
        deadline = started + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(self.socket.recv(), timeout=remaining)
            except (TimeoutError, asyncio.TimeoutError):
                return None
            message = json.loads(raw)
            if message["type"] == "ops":
                for op in message["ops"]:
                    self.crdt.apply(op)
                return time.monotonic() - started


async def snapshot_of(socket: Any) -> dict[str, Any]:
    while True:
        message = json.loads(await socket.recv())
        if message["type"] == "snapshot":
            return message


async def connect_to_two_instances() -> tuple[Client, Client, list[Any]]:
    """Open sockets until two of them are on different instances.

    nginx round-robins per connection, so this normally takes two attempts; the
    loop just makes the script robust to whichever slot we start on.
    """
    kept: dict[str, Client] = {}
    sockets: list[Any] = []

    for attempt in range(12):
        socket = await websockets.connect(f"{BALANCER_WS}/ws/{DOC_ID}?client_id=c{attempt}")
        snapshot = await snapshot_of(socket)
        instance = snapshot["instance"]
        if instance in kept:
            await socket.close()
            continue
        kept[instance] = Client(f"c{attempt}", socket, snapshot)
        sockets.append(socket)
        if len(kept) == 2:
            break

    if len(kept) < 2:
        raise SystemExit(
            "could not land on two different instances — is the cluster running "
            "with two backends? (scripts/cluster.sh status)"
        )

    first, second = kept.values()
    return first, second, sockets


def server_state() -> dict[str, Any]:
    with urllib.request.urlopen(f"{BALANCER_HTTP}/api/doc/{DOC_ID}") as response:
        return json.load(response)


async def text_in_postgres() -> str:
    """Rebuild the document from the operation log with no server involved."""
    pool = await create_pool()
    try:
        replica = RGA("psql")
        for op in await PostgresOpStore(pool).load_ops(DOC_ID):
            replica.apply(op)
        return replica.text
    finally:
        await pool.close()


async def main() -> int:
    alice, bob, sockets = await connect_to_two_instances()

    print(f"doc: {DOC_ID}")
    print(f"  alice -> instance {alice.instance!r}")
    print(f"  bob   -> instance {bob.instance!r}")
    print("  (different processes, same Postgres)\n")

    text = "typed on one instance"
    await alice.type_at(0, text)
    elapsed = await bob.wait_for_ops(PROPAGATION_TIMEOUT)

    print(f'alice typed "{text}"')
    print(f"  alice sees: {alice.text!r}")
    print(f"  bob sees:   {bob.text!r}")

    propagated = elapsed is not None
    if propagated:
        print(f"\n=> PROPAGATED in {elapsed * 1000:.1f} ms across instances.")
        print("   The op went alice -> instance A -> Redis -> instance B -> bob.")
    else:
        print(f"\n=> DID NOT PROPAGATE within {PROPAGATION_TIMEOUT:.0f}s.")
        print("   Each instance only broadcasts to the sockets in its own process,")
        print("   and there is nothing carrying operations between processes.")
        print("   bob will not see this edit until he reconnects.")

    for socket in sockets:
        await socket.close()

    # Each instance keeps its own in-memory replica, loaded once and cached. Ask
    # them separately -- the difference is the same bug in the HTTP path.
    print("\nwhat each instance thinks the document says:")
    for name, port in (("a", 8001), ("b", 8002)):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/doc/{DOC_ID}") as response:
                state = json.load(response)
            print(f"  instance {name} (:{port}): {state['text']!r}")
        except OSError as exc:
            print(f"  instance {name} (:{port}): unreachable ({exc})")

    # And the durable truth, read straight out of Postgres with no server involved.
    print(f"\nstraight from Postgres:     {await text_in_postgres()!r}")

    if not propagated:
        print(
            "\n   Two separate failures, one cause -- state lives per process:\n"
            "     * the live broadcast never left the instance that received it\n"
            "     * the other instance is still serving a replica it loaded while\n"
            "       the document was empty, and nothing ever invalidated it\n"
            "   Nothing was *lost*: Postgres has every operation, so a cold\n"
            "   instance reads it correctly. It just isn't live, and a warm\n"
            "   instance can be indefinitely stale."
        )

    return 0 if propagated else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
