"""Reproduce the Stage 0 lost update from the command line — no browser needed.

Two WebSocket clients open the same doc, agree on a base text, then edit
concurrently without having seen each other's keystroke. Exactly what happens
when you type in two tabs at once.

    uv run uvicorn app.main:app --port 8000     # in one shell
    uv run python scripts/race_demo.py          # in another
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any

import websockets

HTTP_BASE = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"
DOC_ID = "race-demo"


async def take(socket: Any, want: str) -> dict[str, Any]:
    """Read frames until one of type `want` shows up (skipping presence noise)."""
    while True:
        message = json.loads(await socket.recv())
        if message["type"] == want:
            return message


async def send_edit(socket: Any, text: str) -> None:
    await socket.send(json.dumps({"type": "edit", "text": text}))


def server_state() -> dict[str, Any]:
    with urllib.request.urlopen(f"{HTTP_BASE}/api/doc/{DOC_ID}") as response:
        return json.load(response)


async def main() -> int:
    async with (
        websockets.connect(f"{WS_BASE}/ws/{DOC_ID}?client_id=alice") as alice,
        websockets.connect(f"{WS_BASE}/ws/{DOC_ID}?client_id=bob") as bob,
    ):
        await take(alice, "snapshot")
        await take(bob, "snapshot")

        # Establish a base both clients agree on.
        await send_edit(alice, "hi")
        await take(bob, "edit")
        print(f'both clients agree on "hi"            rev={server_state()["rev"]}')

        # The race: both append a character to their own copy of "hi" and send
        # the whole document. Neither has seen the other's version.
        await asyncio.gather(send_edit(alice, "hi Alice"), send_edit(bob, "hi Bob"))

        alice_sees = (await take(alice, "edit"))["text"]
        bob_sees = (await take(bob, "edit"))["text"]

        final = server_state()

    print('alice typed "hi Alice", bob typed "hi Bob"')
    print(f"alice's textarea now reads             {alice_sees!r}")
    print(f"bob's textarea now reads               {bob_sees!r}")
    print(f"server document                        {final['text']!r} (rev {final['rev']})")

    survived = "Alice" if "Alice" in final["text"] else "Bob"
    destroyed = "Bob" if survived == "Alice" else "Alice"
    print()
    print(f"=> {survived}'s edit won. {destroyed}'s edit is gone for good — it was")
    print("   never stored anywhere. Three edits applied, two survive.")

    if alice_sees == bob_sees == final["text"]:
        print("   This run happened to leave all three in agreement, which is luck")
        print("   of timing rather than a guarantee. Run it again.")
    else:
        print()
        print("   And it's worse than lost data: the two clients don't agree with")
        print("   each other OR the server. Each one ends at 'my own last edit,")
        print("   unless a remote frame landed after it' — and that ordering differs")
        print("   per client. Neither can even detect the problem, because the server")
        print("   never tells a client which rev its own edit was assigned.")
        print("   So Stage 0 fails to converge, not just to preserve intent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
