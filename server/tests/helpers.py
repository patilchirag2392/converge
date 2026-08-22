"""Shared test helpers."""

from __future__ import annotations

from typing import Any

from app.crdt import RGA


class Tab:
    """A stand-in for a browser tab: its own CRDT replica plus a socket."""

    def __init__(self, session: Any, client_id: str) -> None:
        self.session = session
        self.crdt = RGA(client_id)
        self.snapshot = session.receive_json()
        assert self.snapshot["type"] == "snapshot"
        for op in self.snapshot["ops"]:
            self.crdt.apply(op)

    @property
    def text(self) -> str:
        return self.crdt.text

    def type(self, index: int, text: str) -> list[dict[str, Any]]:
        ops = self.crdt.local_insert(index, text)
        self.session.send_json({"type": "ops", "ops": ops})
        return ops

    def erase(self, index: int, length: int) -> list[dict[str, Any]]:
        ops = self.crdt.local_delete(index, length)
        self.session.send_json({"type": "ops", "ops": ops})
        return ops

    def replay(self) -> None:
        """Re-send everything this replica knows, as the real client does after a
        reconnect. Heals operations the server lost to a crash."""
        self.session.send_json({"type": "ops", "ops": self.crdt.snapshot_ops()})

    def receive_ops(self) -> dict[str, Any]:
        """Read until an ops frame arrives, merging it. Skips presence noise."""
        while True:
            message = self.session.receive_json()
            if message["type"] == "ops":
                for op in message["ops"]:
                    self.crdt.apply(op)
                return message

    def sync(self) -> None:
        """Barrier: block until the server has processed everything sent so far.

        A single socket is ordered, so an intentionally invalid frame gives us a
        reply we can wait on -- by the time the error arrives, every earlier frame
        has been handled. Needed because the author gets no reply of its own.
        """
        self.session.send_json({"type": "ops", "ops": []})
        assert self.session.receive_json()["type"] == "error"
