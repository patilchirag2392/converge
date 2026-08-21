"""Tests for the RGA sequence CRDT.

The headline claim is `test_converges_under_every_operation_order`: take a set of
operations produced by three replicas editing concurrently, apply them in every
possible order, and get byte-identical text every time. That is what "converges"
means, and it's the property Stage 0 could not offer.
"""

from __future__ import annotations

import itertools
import json
import random
from pathlib import Path

import pytest

from app.crdt import ROOT, RGA, order_key

VECTORS = Path(__file__).resolve().parents[2] / "shared" / "crdt-vectors.json"


def replica(client_id: str, *ops_lists) -> RGA:
    """A fresh replica with every op from `ops_lists` applied."""
    doc = RGA(client_id)
    for ops in ops_lists:
        for op in ops:
            doc.apply(op)
    return doc


# --------------------------------------------------------------- local editing


def test_insert_builds_text() -> None:
    doc = RGA("a")
    doc.local_insert(0, "hello")
    assert doc.text == "hello"


def test_insert_in_the_middle_lands_between_its_neighbours() -> None:
    """The case that forces siblings to be ordered newest-first.

    After "ab", inserting "c" after "a" leaves "a" with two children, c and b.
    Newest-first gives "acb"; oldest-first would give "abc" -- the character in
    the wrong place.
    """
    doc = RGA("a")
    doc.local_insert(0, "ab")
    doc.local_insert(1, "c")
    assert doc.text == "acb"


def test_repeated_insert_at_the_front_reads_left_to_right() -> None:
    doc = RGA("a")
    for char in "cba":
        doc.local_insert(0, char)
    assert doc.text == "abc"


def test_delete_removes_only_the_named_characters() -> None:
    doc = RGA("a")
    doc.local_insert(0, "hello world")
    doc.local_delete(5, 6)
    assert doc.text == "hello"


def test_delete_then_insert_at_the_seam() -> None:
    doc = RGA("a")
    doc.local_insert(0, "abcd")
    doc.local_delete(1, 2)  # -> "ad"
    doc.local_insert(1, "XY")
    assert doc.text == "aXYd"


def test_local_insert_rejects_an_out_of_range_index() -> None:
    doc = RGA("a")
    doc.local_insert(0, "ab")
    with pytest.raises(IndexError):
        doc.local_insert(99, "x")


# ------------------------------------------------------------------ two replicas


def test_concurrent_inserts_at_the_same_spot_both_survive() -> None:
    """Stage 0's fatal case. Both characters are kept, and both replicas agree."""
    alice, bob = RGA("alice"), RGA("bob")

    base = alice.local_insert(0, "hi")
    for op in base:
        bob.apply(op)
    assert alice.text == bob.text == "hi"

    # Neither has seen the other's keystroke -- the exact Stage 0 race.
    alice_ops = alice.local_insert(2, "A")
    bob_ops = bob.local_insert(2, "B")

    for op in bob_ops:
        alice.apply(op)
    for op in alice_ops:
        bob.apply(op)

    assert alice.text == bob.text
    assert "A" in alice.text and "B" in alice.text
    assert alice.text == "hiBA"  # deterministic, not merely equal


def test_concurrent_word_insertions_do_not_interleave() -> None:
    """Typing a whole word at the same spot keeps each word contiguous.

    This is why local_insert chains each character onto the previous one instead
    of anchoring them all to the same parent.
    """
    alice, bob = RGA("alice"), RGA("bob")
    alice_ops = alice.local_insert(0, "hello")
    bob_ops = bob.local_insert(0, "world")

    for op in bob_ops:
        alice.apply(op)
    for op in alice_ops:
        bob.apply(op)

    assert alice.text == bob.text
    assert alice.text in {"helloworld", "worldhello"}


def test_concurrent_delete_and_insert_after_the_deleted_character() -> None:
    """A tombstone still has to work as an anchor."""
    alice, bob = RGA("alice"), RGA("bob")
    for op in alice.local_insert(0, "abc"):
        bob.apply(op)

    delete_ops = alice.local_delete(1, 1)  # alice deletes "b" -> "ac"
    insert_ops = bob.local_insert(2, "Z")  # bob inserts after "b" -> "abZc"

    for op in insert_ops:
        alice.apply(op)
    for op in delete_ops:
        bob.apply(op)

    assert alice.text == bob.text == "aZc"


def test_both_replicas_deleting_the_same_character_is_fine() -> None:
    alice, bob = RGA("alice"), RGA("bob")
    for op in alice.local_insert(0, "abc"):
        bob.apply(op)

    alice_ops = alice.local_delete(1, 1)
    bob_ops = bob.local_delete(1, 1)
    for op in bob_ops:
        alice.apply(op)
    for op in alice_ops:
        bob.apply(op)

    assert alice.text == bob.text == "ac"


# ----------------------------------------------------------------- convergence


def concurrent_edit_scenario() -> tuple[list[dict], str]:
    """Three replicas edit a shared base concurrently. Returns (all ops, text)."""
    alice = RGA("alice")
    base = alice.local_insert(0, "hi")

    bob, carol = replica("bob", base), replica("carol", base)

    ops = list(base)
    ops += alice.local_insert(2, "!")  # alice appends
    ops += bob.local_insert(0, "oh ")  # bob prepends
    ops += carol.local_delete(0, 1)  # carol deletes the "h"

    reference = replica("reference", ops)
    return ops, reference.text


def test_converges_under_every_operation_order() -> None:
    """Every permutation of the same operations produces the same document."""
    ops, expected = concurrent_edit_scenario()
    assert len(ops) <= 8, "keep this small enough to permute exhaustively"

    seen: set[str] = set()
    for order in itertools.permutations(ops):
        seen.add(replica("perm", list(order)).text)

    assert seen == {expected}, f"order-dependent outcomes: {sorted(seen)}"


def test_converges_under_random_orders_on_a_larger_document() -> None:
    alice = RGA("alice")
    base = alice.local_insert(0, "the quick brown fox")
    bob, carol = replica("bob", base), replica("carol", base)

    ops = list(base)
    ops += alice.local_insert(19, " jumps")
    ops += bob.local_insert(0, "SO: ")
    ops += bob.local_delete(4, 4)
    ops += carol.local_insert(10, "very ")
    ops += carol.local_delete(0, 3)

    expected = replica("reference", ops).text
    rng = random.Random(20260821)
    for _ in range(300):
        shuffled = ops[:]
        rng.shuffle(shuffled)
        assert replica("shuffled", shuffled).text == expected


def test_all_three_replicas_agree_after_exchanging_operations() -> None:
    alice = RGA("alice")
    base = alice.local_insert(0, "hi")
    bob, carol = replica("bob", base), replica("carol", base)

    alice_ops = alice.local_insert(2, "!")
    bob_ops = bob.local_insert(0, "oh ")
    carol_ops = carol.local_delete(0, 1)

    for doc, mine in ((alice, alice_ops), (bob, bob_ops), (carol, carol_ops)):
        for ops in (alice_ops, bob_ops, carol_ops):
            if ops is mine:
                continue
            for op in ops:
                doc.apply(op)

    assert alice.text == bob.text == carol.text


# ------------------------------------------------- idempotency & causal safety


def test_applying_the_same_operation_twice_changes_nothing() -> None:
    alice = RGA("alice")
    ops = alice.local_insert(0, "abc")
    ops += alice.local_delete(1, 1)

    once = replica("once", ops)
    twice = replica("twice", ops, ops)
    assert once.text == twice.text == "ac"
    assert once.applied == twice.applied


def test_an_insert_whose_parent_is_missing_is_parked_then_applied() -> None:
    alice = RGA("alice")
    ops = alice.local_insert(0, "abc")

    # Deliberately worst case: children strictly before parents.
    doc = RGA("late")
    for op in reversed(ops):
        doc.apply(op)
    assert doc.text == "abc"


def test_a_delete_can_arrive_before_the_insert_it_refers_to() -> None:
    alice = RGA("alice")
    inserts = alice.local_insert(0, "abc")
    deletes = alice.local_delete(1, 1)

    doc = replica("late", deletes, inserts)
    assert doc.text == "ac"


def test_a_deep_chain_does_not_blow_the_stack() -> None:
    """Typing n characters builds a chain n deep, so the walk must be iterative."""
    alice = RGA("alice")
    ops = alice.local_insert(0, "x" * 5000)
    doc = replica("deep", ops)
    assert doc.text == "x" * 5000


def test_snapshot_ops_rebuild_the_document() -> None:
    alice = RGA("alice")
    alice.local_insert(0, "hello world")
    alice.local_delete(5, 6)

    rebuilt = replica("joiner", alice.snapshot_ops())
    assert rebuilt.text == alice.text == "hello"


def test_counter_advances_past_ids_seen_from_other_replicas() -> None:
    """Lamport-style clock, so "newer" stays meaningful across replicas."""
    alice, bob = RGA("alice"), RGA("bob")
    for _ in range(5):
        bob.local_insert(0, "x")

    for op in bob.snapshot_ops():
        alice.apply(op)

    new_ops = alice.local_insert(0, "y")
    assert new_ops[0]["id"][1] > 5


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "bad",
    [
        "not an object",
        {"kind": "nonsense", "id": ["a", 1]},
        {"kind": "insert", "id": ["a", 1], "parent": ["", 0], "value": "too long"},
        {"kind": "insert", "id": ["a", 1], "parent": ["", 0]},  # no value
        {"kind": "insert", "id": "a:1", "parent": ["", 0], "value": "x"},
        {"kind": "insert", "id": ["a", "1"], "parent": ["", 0], "value": "x"},
        {"kind": "delete", "id": ["a"]},
        {"kind": "delete"},
    ],
)
def test_malformed_operations_are_rejected(bad: object) -> None:
    doc = RGA("a")
    with pytest.raises(ValueError):
        doc.apply(bad)


def test_a_rejected_operation_leaves_the_document_untouched() -> None:
    doc = RGA("a")
    doc.local_insert(0, "hi")
    with pytest.raises(ValueError):
        doc.apply({"kind": "insert", "id": ["a", 9], "parent": ["", 0], "value": ""})
    assert doc.text == "hi"


def test_order_key_is_a_total_order() -> None:
    ids = [("a", 1), ("b", 1), ("a", 2), ("b", 2), ("", 0)]
    keys = [order_key(i) for i in ids]
    assert len(set(keys)) == len(keys)
    assert order_key(("a", 2)) > order_key(("b", 1))  # counter dominates
    assert order_key(("b", 1)) > order_key(("a", 1))  # client id breaks ties
    assert ROOT == ("", 0)


# ------------------------------------------------------- documented limitation


def test_known_limitation_backward_typing_can_interleave() -> None:
    """RGA's real wart, asserted rather than hidden.

    Typing a word *forwards* chains each character onto the last, so concurrent
    words stay contiguous (see the test above). But typing right-to-left -- each
    new character inserted at the same offset -- anchors every character to the
    same parent. Two replicas doing that concurrently interleave character by
    character, because siblings sort by id and the ids alternate.

    Both replicas still converge; the result is just not what either user meant.
    Fugue and Yjs's YATA solve this with extra anchoring rules. Documented here
    as the honest limitation of the algorithm rather than pretended away.
    """
    alice, bob = RGA("alice"), RGA("bob")

    alice_ops: list[dict] = []
    for char in "cba":  # types "abc" by repeatedly inserting at the front
        alice_ops += alice.local_insert(0, char)
    bob_ops: list[dict] = []
    for char in "zyx":  # types "xyz" the same way
        bob_ops += bob.local_insert(0, char)

    for op in bob_ops:
        alice.apply(op)
    for op in alice_ops:
        bob.apply(op)

    assert alice.text == bob.text, "still converges, which is the point"
    assert alice.text == "xaybzc", "...but the two words are interleaved"


# ---------------------------------------------- cross-language shared vectors


def test_shared_vectors_match() -> None:
    """The same vectors are asserted by client/src/crdt.test.ts.

    Two implementations of one algorithm will drift unless something pins them
    together; this is that something.
    """
    vectors = json.loads(VECTORS.read_text())
    for case in vectors["cases"]:
        doc = RGA("vector-runner")
        for op in case["ops"]:
            doc.apply(op)
        assert doc.text == case["text"], case["name"]
