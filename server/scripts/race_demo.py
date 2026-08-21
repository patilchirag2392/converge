"""Run the Stage 0 race against the Stage 1 CRDT — no browser needed.

Two clients open the same doc, agree on a base text, then edit concurrently
without having seen each other's keystroke. In Stage 0 one edit was destroyed and
the clients ended up disagreeing with each other and the server. Now both edits
survive and all three replicas agree.

    uv run uvicorn app.main:app --port 8000     # in one shell
    uv run python scripts/race_demo.py          # in another
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

import websockets

# Run as a script from server/, so put server/ on the path before importing app.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crdt import RGA  # noqa: E402

HTTP_BASE = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"
DOC_ID = "race-demo"


class Client:
    """A stand-in for a browser tab: its own replica plus a socket."""

    def __init__(self, name: str, socket: Any) -> None:
        self.name = name
        self.socket = socket
        self.crdt = RGA(name)

    async def take(self, want: str) -> dict[str, Any]:
        """Read frames until one of type `want` shows up, merging any ops."""
        while True:
            message = json.loads(await self.socket.recv())
            if message["type"] in {"ops", "snapshot"}:
                for op in message["ops"]:
                    self.crdt.apply(op)
            if message["type"] == want:
                return message

    async def type_at(self, index: int, text: str) -> None:
        ops = self.crdt.local_insert(index, text)
        await self.socket.send(json.dumps({"type": "ops", "ops": ops}))

    @property
    def text(self) -> str:
        return self.crdt.text


def server_state() -> dict[str, Any]:
    with urllib.request.urlopen(f"{HTTP_BASE}/api/doc/{DOC_ID}") as response:
        return json.load(response)


async def main() -> int:
    async with (
        websockets.connect(f"{WS_BASE}/ws/{DOC_ID}?client_id=alice") as sa,
        websockets.connect(f"{WS_BASE}/ws/{DOC_ID}?client_id=bob") as sb,
    ):
        alice, bob = Client("alice", sa), Client("bob", sb)
        await alice.take("snapshot")
        await bob.take("snapshot")

        # Establish a base both clients agree on.
        await alice.type_at(0, "hi")
        await bob.take("ops")
        print(f'both clients agree on "hi"            rev={server_state()["rev"]}')

        # The race: both append to their own copy of "hi", neither having seen
        # the other. Identical setup to the Stage 0 demo.
        await asyncio.gather(alice.type_at(2, " Alice"), bob.type_at(2, " Bob"))
        await alice.take("ops")
        await bob.take("ops")

        final = server_state()

    print('alice typed " Alice", bob typed " Bob"')
    print(f"alice's textarea now reads             {alice.text!r}")
    print(f"bob's textarea now reads               {bob.text!r}")
    print(f"server document                       {final['text']!r} (rev {final['rev']})")
    print()

    kept_both = "Alice" in final["text"] and "Bob" in final["text"]
    agreed = alice.text == bob.text == final["text"]

    print(f"both edits survived?   {'YES' if kept_both else 'NO'}")
    print(f"all three agree?       {'YES' if agreed else 'NO'}")
    if kept_both and agreed:
        print()
        print("   Nothing was lost and nothing had to be resolved. The server")
        print("   merged operations instead of picking a winner, and each replica")
        print("   reached the same answer on its own -- including bob, who never")
        print("   heard its own operations back. In Stage 0 that exclusion was")
        print("   exactly what caused permanent divergence.")
        return 0

    print("\n   UNEXPECTED: the CRDT should have preserved both edits and converged.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
