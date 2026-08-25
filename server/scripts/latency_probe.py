"""Measure real edit-propagation latency, same-instance vs across instances.

Why it's built this way: a single process holds all three sockets and uses one
`time.monotonic()` clock, so there is no clock-skew question. The server's
/metrics endpoint reports its own view using wall clocks on two instances, which
is fine on one machine and unreliable across hosts -- this is the number to quote.

What each figure includes, end to end:

  same-instance   client -> nginx -> instance X -> Postgres commit
                         -> local broadcast -> client on X
  cross-instance  the same, plus: Redis publish -> instance Y
                         -> broadcast -> client on Y

Both include the Postgres write, because operations are persisted before they are
published. So the *difference* isolates the cost of the Redis hop, and the
cross-instance figure is what a user on another instance actually waits.

    scripts/cluster.sh up   (with REDIS_URL set)
    uv run python scripts/latency_probe.py [samples]
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crdt import RGA  # noqa: E402

BALANCER_WS = "ws://localhost:8080"
DOC_ID = f"latency-{int(time.time())}"
WARMUP = 5


class Probe:
    def __init__(self, name: str, socket: Any, instance: str) -> None:
        self.name = name
        self.socket = socket
        self.instance = instance
        self.crdt = RGA(name)

    async def send_one_char(self, index: int) -> None:
        ops = self.crdt.local_insert(index, "x")
        await self.socket.send(json.dumps({"type": "ops", "ops": ops}))

    async def next_ops(self, timeout: float = 5.0) -> None:
        """Wait for the next ops frame, ignoring presence chatter."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{self.name} received no ops frame")
            raw = await asyncio.wait_for(self.socket.recv(), timeout=remaining)
            message = json.loads(raw)
            if message["type"] == "ops":
                for op in message["ops"]:
                    self.crdt.apply(op)
                return


async def snapshot(socket: Any) -> dict[str, Any]:
    while True:
        message = json.loads(await socket.recv())
        if message["type"] == "snapshot":
            return message


async def open_probes() -> tuple[Probe, Probe, Probe, list[Any]]:
    """Get two probes on one instance and a third on another.

    nginx round-robins per connection, so we just keep connecting until the
    buckets are filled.
    """
    by_instance: dict[str, list[Probe]] = {}
    sockets: list[Any] = []

    for attempt in range(24):
        socket = await websockets.connect(f"{BALANCER_WS}/ws/{DOC_ID}?client_id=p{attempt}")
        snap = await snapshot(socket)
        instance = snap["instance"]
        probe = Probe(f"p{attempt}", socket, instance)

        bucket = by_instance.setdefault(instance, [])
        # Need two on one instance and one on another; drop anything surplus so
        # extra sockets don't sit there absorbing broadcasts.
        if len(bucket) >= 2:
            await socket.close()
            continue
        bucket.append(probe)
        sockets.append(socket)

        pair = next((v for v in by_instance.values() if len(v) >= 2), None)
        other = next((v for v in by_instance.values() if len(v) == 1 and v is not pair), None)
        if pair and other:
            sender, local = pair[0], pair[1]
            return sender, local, other[0], sockets

    for socket in sockets:
        await socket.close()
    raise SystemExit(
        "could not place probes on two different instances — is the cluster up "
        "with two backends? (scripts/cluster.sh status)"
    )


def summarise(label: str, samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    stats = {
        "n": len(ordered),
        "p50": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "min": ordered[0],
        "max": ordered[-1],
    }
    print(
        f"  {label:16} n={stats['n']:<4} "
        f"p50={stats['p50']:6.2f} ms   p95={stats['p95']:6.2f} ms   "
        f"min={stats['min']:6.2f}   max={stats['max']:6.2f}"
    )
    return stats


async def main() -> int:
    samples = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    sender, local, remote, sockets = await open_probes()

    print(f"doc: {DOC_ID}")
    print(f"  sender          -> instance {sender.instance!r}")
    print(f"  local receiver  -> instance {local.instance!r}  (same process)")
    print(f"  remote receiver -> instance {remote.instance!r}  (via Redis)")
    print(f"\n{samples} samples (+{WARMUP} discarded warm-up)...\n")

    local_ms: list[float] = []
    remote_ms: list[float] = []

    for i in range(samples + WARMUP):
        started = time.monotonic()
        await sender.send_one_char(i)

        local_at, remote_at = None, None

        async def watch_local() -> None:
            nonlocal local_at
            await local.next_ops()
            local_at = time.monotonic()

        async def watch_remote() -> None:
            nonlocal remote_at
            await remote.next_ops()
            remote_at = time.monotonic()

        await asyncio.gather(watch_local(), watch_remote())
        assert local_at is not None and remote_at is not None

        if i >= WARMUP:
            local_ms.append((local_at - started) * 1000.0)
            remote_ms.append((remote_at - started) * 1000.0)

    for socket in sockets:
        await socket.close()

    print("edit propagation, measured end to end on one clock:")
    local_stats = summarise("same-instance", local_ms)
    remote_stats = summarise("cross-instance", remote_ms)

    overhead = remote_stats["p50"] - local_stats["p50"]
    print(
        f"\n  the Redis hop costs about {overhead:.2f} ms at p50 "
        f"({remote_stats['p50']:.2f} - {local_stats['p50']:.2f})."
    )
    print("  Both figures include the Postgres commit, since operations are")
    print("  persisted before they are published.")

    if sender.crdt.text != remote.crdt.text:
        print("\n  !! sender and remote receiver disagree — that's a bug, not a metric")
        return 1
    print(f"\n  all replicas agree on {len(sender.crdt.text)} characters.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
