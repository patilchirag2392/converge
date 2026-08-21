"""A hand-written sequence CRDT: RGA (Replicated Growable Array), a.k.a. a causal tree.

The Stage 0 protocol shipped *state* ("the document is now this string"), and two
of those arriving at once cannot both be true. This ships *operations* ("insert
this character after that one", "delete that one"), and two of those can both be
applied. That is the entire idea; everything below is the bookkeeping that makes
it airtight.

Three properties make `apply` safe to call with anything, in any order:

  commutative  ops can arrive in any order and land in the same final state
  idempotent   applying the same op twice is a no-op
  causal-safe  an insert whose parent hasn't arrived yet is parked, not dropped

Together those mean a replica needs no coordination, no locks, and no agreement
on message order to end up byte-identical to every other replica.

Mirrored in client/src/crdt.ts -- the two must stay in step, and
tests/test_crdt.py plus client/src/crdt.test.ts share vectors from
shared/crdt-vectors.json to enforce that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

# An identifier is (client_id, counter): unique for all time, never reused, and
# never changed once assigned. This is the whole trick -- an operation names a
# *character* rather than a *position*, because positions shift under you and
# identities don't.
Id = tuple[str, int]

# Sentinel for "the very beginning of the document". Not a real character.
ROOT: Id = ("", 0)


def order_key(node_id: Id) -> tuple[int, str]:
    """Sort key for siblings; they are ordered by this, descending.

    Doing two jobs at once:

    1. It's a *total* order over ids, so every replica independently computes the
       same sequence with no coordination. That's the deterministic-ordering
       guarantee, and it works because ids are unique and comparable.

    2. Descending order puts a newly inserted child *before* its parent's older
       children. That isn't cosmetic -- it's required. In "ab", inserting "c"
       after "a" gives "a" two children, c and b. Newest-first yields "acb";
       oldest-first would yield "abc" and drop the character in the wrong place.
    """
    client_id, counter = node_id
    return (counter, client_id)


@dataclass
class Node:
    """One character, permanently identified, permanently attached to a parent."""

    id: Id
    parent: Id
    value: str


def to_id(raw: Any) -> Id:
    """Parse an id off the wire. JSON has no tuples, so ids travel as [str, int]."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError("id must be [client_id, counter]")
    client_id, counter = raw
    if not isinstance(client_id, str) or isinstance(counter, bool) or not isinstance(counter, int):
        raise ValueError("id must be [string, integer]")
    return (client_id, counter)


@dataclass(frozen=True)
class Insert:
    node: Node


@dataclass(frozen=True)
class Delete:
    id: Id


Op = Insert | Delete


def parse_op(raw: Any) -> Op:
    """Validate one operation off the wire, without touching any document.

    Split out from `apply` so a batch can be rejected whole: the server parses
    every operation in a frame before applying any of them, and a client that
    sends one bad operation doesn't get a half-applied batch.
    """
    if not isinstance(raw, dict):
        raise ValueError("operation must be an object")

    kind = raw.get("kind")
    if kind == "insert":
        value = raw.get("value")
        if not isinstance(value, str) or len(value) != 1:
            raise ValueError("insert.value must be exactly one character")
        return Insert(Node(id=to_id(raw.get("id")), parent=to_id(raw.get("parent")), value=value))
    if kind == "delete":
        return Delete(to_id(raw.get("id")))
    raise ValueError(f"unknown operation kind: {kind!r}")


def insert_op(node: Node) -> dict[str, Any]:
    return {
        "kind": "insert",
        "id": list(node.id),
        "parent": list(node.parent),
        "value": node.value,
    }


def delete_op(node_id: Id) -> dict[str, Any]:
    return {"kind": "delete", "id": list(node_id)}


class RGA:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.applied = 0
        """Count of operations that have actually taken effect. Display only --
        nothing about correctness depends on it, which is the difference from
        Stage 0's `rev`."""

        self._nodes: dict[Id, Node] = {}
        self._children: dict[Id, list[Id]] = {}
        self._deleted: set[Id] = set()
        self._pending: dict[Id, list[Node]] = {}
        self._counter = 0

    # ------------------------------------------------------------------ reading

    def _walk(self) -> Iterator[Node]:
        """Depth-first walk of the tree, yielding visible nodes in document order.

        A node's children come immediately after it, siblings in `order_key`
        order. Iterative on purpose: typing n characters in a row builds a chain
        n deep, so recursion would blow the stack on any real document.
        """
        stack: list[Id] = list(reversed(self._children.get(ROOT, [])))
        while stack:
            node_id = stack.pop()
            if node_id not in self._deleted:
                yield self._nodes[node_id]
            children = self._children.get(node_id)
            if children:
                stack.extend(reversed(children))

    @property
    def text(self) -> str:
        return "".join(node.value for node in self._walk())

    def visible_ids(self) -> list[Id]:
        """Ids of the characters a user can see, in order. Index i of this list
        is offset i in `text`."""
        return [node.id for node in self._walk()]

    # ------------------------------------------- applying operations (any order)

    def apply(self, raw: Any) -> None:
        """Apply one operation from anywhere -- remote, replayed, or duplicated.

        Raises ValueError on a malformed operation; never raises on a
        well-formed one, no matter what state we're in or what order it arrives.
        """
        self.apply_parsed(parse_op(raw))

    def apply_parsed(self, op: Op) -> None:
        if isinstance(op, Insert):
            self._apply_insert(op.node)
        else:
            self._apply_delete(op.id)

    def _knows(self, node_id: Id) -> bool:
        return node_id == ROOT or node_id in self._nodes

    def _apply_insert(self, node: Node) -> None:
        if node.id in self._nodes:
            return  # already applied; ops are idempotent

        # Lamport-style clock: never hand out an id whose counter is lower than
        # one we've already seen, so "newer" stays meaningful across replicas.
        self._counter = max(self._counter, node.id[1])

        if not self._knows(node.parent):
            # Causally not ready: we haven't seen the character this one attaches
            # to. Park it -- do NOT drop it -- and link it when the parent lands.
            self._pending.setdefault(node.parent, []).append(node)
            return

        # Linking a node can unblock others that arrived before it, and those can
        # unblock more. Worklist rather than recursion, for the same stack reason
        # as _walk.
        ready = [node]
        while ready:
            current = ready.pop()
            if current.id in self._nodes:
                continue
            self._counter = max(self._counter, current.id[1])
            self._link(current)
            ready.extend(self._pending.pop(current.id, []))

    def _link(self, node: Node) -> None:
        self._nodes[node.id] = node
        siblings = self._children.setdefault(node.parent, [])
        siblings.append(node.id)
        siblings.sort(key=order_key, reverse=True)
        self.applied += 1

    def _apply_delete(self, node_id: Id) -> None:
        """Tombstone: mark invisible, keep the id.

        The character can't actually be removed, because an operation still in
        flight may say "insert after that one" and would otherwise have nothing
        to attach to.

        Recording deletions as a *set of ids* also means a delete can arrive
        before the matching insert and still be correct -- the character is
        simply invisible the moment it shows up. That makes deletes idempotent
        and order-independent for free, with no buffering needed.
        """
        if node_id in self._deleted:
            return
        self._deleted.add(node_id)
        self.applied += 1

    # -------------------------------------------------------------- local edits

    def _next_id(self) -> Id:
        self._counter += 1
        return (self.client_id, self._counter)

    def local_insert(self, index: int, value: str) -> list[dict[str, Any]]:
        """Insert `value` at visible offset `index`; returns ops to broadcast."""
        visible = self.visible_ids()
        if not 0 <= index <= len(visible):
            raise IndexError(f"insert index {index} outside 0..{len(visible)}")

        parent = visible[index - 1] if index > 0 else ROOT
        ops: list[dict[str, Any]] = []
        for char in value:
            node = Node(id=self._next_id(), parent=parent, value=char)
            self._apply_insert(node)
            ops.append(insert_op(node))
            # Chain each character onto the previous one rather than all onto the
            # same parent. A pasted or typed run then stays contiguous instead of
            # being interleaved by a concurrent insert at the same spot.
            parent = node.id
        return ops

    def local_delete(self, index: int, length: int) -> list[dict[str, Any]]:
        """Delete `length` visible characters from `index`; returns ops."""
        visible = self.visible_ids()
        ops: list[dict[str, Any]] = []
        for node_id in visible[index : index + length]:
            self._apply_delete(node_id)
            ops.append(delete_op(node_id))
        return ops

    # ----------------------------------------------------------------- snapshot

    def snapshot_ops(self) -> list[dict[str, Any]]:
        """Every operation needed to rebuild this document from empty.

        Emitted in dict order, which is *not* causal order -- parents can follow
        their children. That's deliberate: the receiver buffers anything early,
        so a snapshot needs no sorting, and every join exercises that path.
        """
        ops = [insert_op(node) for node in self._nodes.values()]
        ops.extend(delete_op(node_id) for node_id in self._deleted)
        return ops
