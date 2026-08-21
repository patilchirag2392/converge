"""Unit tests for the Hub. These pin down the Stage 0 semantics, including the
data loss — when Stage 1 lands, `test_last_write_wins_destroys_the_earlier_edit`
is the test that should start failing.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.hub import Hub


class FakeConnection:
    """Records what the Hub tried to send it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)


class BrokenConnection:
    async def send_json(self, data: Any) -> None:
        raise RuntimeError("socket is gone")


def test_document_is_created_empty_on_first_open() -> None:
    hub = Hub()
    doc = hub.document("demo")
    assert doc.text == ""
    assert doc.rev == 0


def test_apply_edit_replaces_text_and_bumps_rev() -> None:
    hub = Hub()
    hub.apply_edit("demo", "hello")
    doc = hub.apply_edit("demo", "hello world")
    assert doc.text == "hello world"
    assert doc.rev == 2


def test_last_write_wins_destroys_the_earlier_edit() -> None:
    """Two clients edit the same base text; only the second survives."""
    hub = Hub()
    hub.apply_edit("demo", "hi")

    # Both clients read "hi", then type into their own copy.
    hub.apply_edit("demo", "hi A")  # client A
    doc = hub.apply_edit("demo", "hi B")  # client B, based on the same "hi"

    assert doc.text == "hi B"
    assert "A" not in doc.text  # A's keystroke is gone from the only replica


def test_document_survives_the_last_client_leaving() -> None:
    hub = Hub()
    hub.join("demo", "a", FakeConnection())
    hub.apply_edit("demo", "persisted in memory")
    hub.leave("demo", "a")

    assert hub.client_count("demo") == 0
    assert hub.document("demo").text == "persisted in memory"


async def test_broadcast_skips_the_author() -> None:
    hub = Hub()
    author, other = FakeConnection(), FakeConnection()
    hub.join("demo", "author", author)
    hub.join("demo", "other", other)

    await hub.broadcast("demo", {"type": "edit", "text": "x"}, exclude="author")

    assert author.sent == []
    assert other.sent == [{"type": "edit", "text": "x"}]


async def test_broadcast_does_not_leak_into_other_documents() -> None:
    hub = Hub()
    here, elsewhere = FakeConnection(), FakeConnection()
    hub.join("doc-1", "a", here)
    hub.join("doc-2", "b", elsewhere)

    await hub.broadcast("doc-1", {"type": "edit", "text": "x"})

    assert len(here.sent) == 1
    assert elsewhere.sent == []


async def test_broadcast_drops_a_dead_connection() -> None:
    hub = Hub()
    hub.join("demo", "dead", BrokenConnection())
    hub.join("demo", "alive", FakeConnection())

    await hub.broadcast("demo", {"type": "presence", "clients": 2})

    assert hub.client_count("demo") == 1


@pytest.mark.parametrize("text", ["", "unicode: 日本語 🎉", "x" * 10_000])
def test_apply_edit_accepts_any_string(text: str) -> None:
    assert hub_text_after(text) == text


def hub_text_after(text: str) -> str:
    hub = Hub()
    return hub.apply_edit("demo", text).text
