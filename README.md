# Converge

A real-time collaborative text editor, built in stages to learn distributed-systems
concepts from the inside out. The sequence CRDT that merges concurrent edits is
hand-written — no Yjs, no Automerge.

**Stack:** React + TypeScript (client) · FastAPI + Python (server) · WebSockets (transport)

Build plan and learning gates: [ROADMAP.md](./ROADMAP.md).

---

## Status

**Stage 1 — the CRDT.** Concurrent edits merge instead of clobbering each other.
Every replica (both browser tabs *and* the server) converges independently, with no
coordination and no agreement on message order.

- [x] Own sequence CRDT so concurrent edits merge cleanly
- [x] Each character has a unique, ordered ID
- [x] Concurrent inserts resolve deterministically on every replica

Stage 0 is preserved at the **`stage-0`** tag — the same app with last-write-wins,
kept so the before/after is demonstrable. `git checkout stage-0` to watch edits get
destroyed, then come back.

---

## Run it

Two shells. Server needs Python 3.11+ (managed by [uv](https://docs.astral.sh/uv/)),
client needs Node 20+.

```bash
# shell 1 — server on :8000
cd server
uv sync
uv run uvicorn app.main:app --reload --port 8000

# shell 2 — client on :5173
cd client
npm install
npm run dev
```

Open <http://localhost:5173/?doc=demo>, then the same URL in a second tab (there's a
button). Type in both at once — both sets of keystrokes survive, and your cursor
stays where you left it.

```bash
cd server && uv run pytest                       # 54 tests
cd client && npm test                            # 43 tests
cd server && uv run python scripts/race_demo.py  # the Stage 0 race, now merging
cd server && uv run python scripts/gen_vectors.py # regenerate shared vectors
curl localhost:8000/api/doc/demo                 # the server's own replica, rendered
```

---

## Architecture

```
┌──────────── Tab A ────────────┐          ┌──────── FastAPI (1 process) ────────┐
│ React + TS                    │   ops    │                                     │
│  <textarea>                   │  ──────► │  Hub                                 │
│  useDocSocket()               │   JSON   │   docs:  {doc_id -> Document}        │
│  RGA replica  ◄───────────────┼──────────┤            Document{ RGA replica }   │
│   crdt.ts                     │          │   conns: {doc_id -> {client_id: ws}} │
└───────────────────────────────┘          │                                      │
┌──────────── Tab B ────────────┐          │  merges the same ops into its own    │
│ RGA replica  ◄────────────────┼─────────►│  replica, then relays them verbatim  │
└───────────────────────────────┘          └─────────────────────────────────────┘
        three replicas, each converging on its own
```

The same algorithm is implemented twice — `server/app/crdt.py` and
`client/src/crdt.ts` — so the server is a real participant rather than a referee.
It can answer "what does this document say" without asking anyone, which Stage 2's
persistence needs, and it means convergence can be tested exhaustively in Python.

The cost is two implementations of one algorithm, which will drift unless something
stops it. That something is `shared/crdt-vectors.json`: generated from the Python
side, asserted by both test suites. If they disagree, both suites fail.

### Wire protocol

| Direction | Message | Meaning |
|---|---|---|
| S→C on connect | `{type:"snapshot", doc_id, ops, rev, clients, client_id}` | the op history, not the text |
| C→S on edit | `{type:"ops", ops}` | operations this tab just performed |
| S→C broadcast | `{type:"ops", ops, rev, author}` | relayed verbatim to everyone *except* the author |
| S→C on join/leave | `{type:"presence", clients}` | room size |
| S→C on bad frame | `{type:"error", detail}` | frame rejected whole, socket stays open |

```jsonc
{"kind": "insert", "id": ["a7f", 12], "parent": ["b31", 4], "value": "x"}
{"kind": "delete", "id": ["a7f", 12]}
```

The unit of change is an operation **naming a character** — never a position, never
the whole document. `rev` counts applied operations and is display-only: nothing
needs an ordering, so nothing depends on it.

---

## How the CRDT works

**Every character gets a permanent id**, `(client_id, counter)`. Unique for all
time, never reused, never changed. This is the whole trick, and it exists because
positions are unusable across replicas:

```
both tabs show:  "cat"
tab A: insert "X" at position 1   ->  "cXat"
tab B: delete position 0          ->  "at"

B applies A's op: insert at position 1 of "at"  ->  "aXt"   ✗ wrong
```

A meant *after the c*. By the time B applied it, position 1 meant something else.
So an insert instead says **"place this character immediately after id `(a,1)`"**,
which stays true no matter what happened nearby. Deletes name an id too.

**Siblings are ordered by id, descending.** When two characters claim the same
parent, every replica sorts them by `(counter, client_id)` and lands on the same
answer alone — no locks, no coordination, no asking the server. That's the
deterministic-ordering guarantee.

Descending isn't cosmetic, it's required. In `"ab"`, inserting `"c"` after `"a"`
leaves `"a"` with two children, `c` and `b`. Newest-first gives `"acb"`;
oldest-first would give `"abc"` — the character in the wrong place.

**Deletes are tombstones.** A character can't actually be removed, because an
operation still in flight may say "insert after that one" and would have nothing to
attach to. Deleted ids are kept in a set and skipped when rendering. Storing them
as a *set of ids* also means a delete can arrive **before** its insert and still be
correct, which makes deletes idempotent and order-independent with no buffering.

**Inserts that arrive early are parked, not dropped.** If an insert names a parent
we haven't seen, it waits in `_pending` until that parent lands, then links —
possibly unblocking others in a chain. This is what lets a snapshot be sent in
arbitrary order, and every new tab exercises the path.

**Counters are Lamport-style**: on applying any operation, a replica raises its
counter to at least what it just saw, so "newer" keeps meaning something across
replicas.

Together: `apply()` is **commutative**, **idempotent** and **causal-safe**, so a
replica converges regardless of what order anything arrives in, or whether it ever
sees its own operations at all.

### The consequence worth noticing

In Stage 0, the server excluding an edit's author from the broadcast is precisely
what caused permanent divergence — every client saw a different message order with
no way to reconcile. That exclusion is now completely harmless. Operations commute,
so it no longer matters what order anyone applies them in, or that a tab never
hears its own work back.

The bug wasn't patched. **The category of bug stopped existing.**

---

## What Stage 1 fixes, measured

Same scenario that destroyed data in Stage 0, verified against a live server and in
two real browser tabs:

| | Stage 0 | Stage 1 |
|---|---|---|
| concurrent edits at the same spot | one silently destroyed | both survive |
| tab A / tab B / server agree | no — `BBBBBBBB` / `AAAAAAAA` / `BBBBBBBB` | yes — all `BBBAAAXYZhello world!!` |
| caret during a remote append | jumped 2 → 14 (end of doc) | stays at 2 |
| caret when the remote insert is *before* it | destroyed | shifts 2 → 5, correctly |
| bandwidth per keystroke | whole document | one operation |
| editing while disconnected | keystrokes lost | queued, merged on reconnect |

**Cursors are anchored to character ids too.** Offsets are meaningless across a
merge, so before applying remote operations the client notes the *identity* of the
character to the left of the caret, then finds where that character ended up. If
the same batch deleted it, it walks left to the nearest survivor. Same trick as the
text, applied to the cursor.

**Editing offline now works**, and it's a consequence rather than a feature: since
operations commute and are idempotent, holding them in an outbox and sending them
later merges exactly as well as sending them immediately. On reconnect the incoming
snapshot is *merged* into the existing replica rather than replacing it.

---

## Known limitations

Being able to state these is worth more than hiding them.

- **Backward typing interleaves.** RGA's real wart, asserted in
  `test_known_limitation_backward_typing_can_interleave` rather than papered over.
  Typing a word forwards chains each character onto the last, so concurrent words
  stay contiguous. But typing right-to-left anchors every character to the *same*
  parent, and two replicas doing that concurrently interleave character by
  character (`"abc"` + `"xyz"` → `"xaybzc"`). Still converges; just not what either
  user meant. Fugue and Yjs's YATA add anchoring rules that fix it.
- **Tombstones grow forever.** Deleted characters are never reclaimed. Real systems
  garbage-collect them once every replica has acknowledged the deletion.
- **Rendering is O(n).** Every keystroke walks the whole tree to produce the text
  and the visible-id list. Fine for documents this size, wrong for a large one — a
  real implementation keeps an incrementally maintained index.
- **One contiguous change per event.** The textarea diff assumes a single edited
  region, which is true for typing, backspace, paste and select-and-replace, but
  would be wrong for multi-cursor editing.
- **Astral characters split.** The diff works in UTF-16 code units, so an emoji
  becomes two nodes. Text round-trips fine; deleting half of one would not.
- **Selections collapse.** Restoring the caret after a merge collapses a selection
  to a single point.

### What would break if…

- **…you ran two server instances?** Each has its own `Hub`, so clients on
  different instances never exchange operations at all. Stage 3.
- **…the process restarted?** Every document is gone. Stage 2.
- **…a client sent one malformed operation in a batch?** The whole frame is
  rejected before any of it is applied (`Hub.apply_ops` parses everything first),
  so you never get a half-merged batch.
- **…operations arrived out of order, duplicated, or not at all?** Out of order and
  duplicated are both fine by construction. *Never* is not: this relies on the
  WebSocket delivering everything eventually. A dropped operation is a permanent
  hole, which is why a real system numbers operations per client and lets a peer
  ask for a range it's missing.
- **…the handler task is cancelled instead of disconnected?** The `finally` block's
  presence broadcast is skipped — an `await` in a cancelled task doesn't complete.
  State cleanup (`hub.leave`) is synchronous and still runs.

---

## Stage 1 learn-gate

The three questions to be able to answer without looking:

1. **Why does last-write-wins fail?** See the Stage 0 section of the `stage-0` tag's
   README — it loses data *and* diverges, for two separate reasons.
2. **Why does every character need a unique id?** The `"cat"` example above.
3. **How do two clients inserting at the same position end up in the same order?**
   Both characters claim the same parent; every replica sorts siblings by id
   descending, which is a total order, so all of them compute the same sequence
   with no coordination.

---

## Layout

```
shared/crdt-vectors.json   cases both test suites assert, pinning the two implementations
server/
  app/crdt.py              the CRDT: ids, insert-after, sibling order, tombstones, buffering
  app/hub.py               documents (each a replica) + connection rooms
  app/main.py              FastAPI app, WebSocket endpoint, wire protocol
  scripts/race_demo.py     the Stage 0 race, now merging cleanly
  scripts/gen_vectors.py   regenerates the shared vectors
  tests/                   54 tests, incl. every permutation of a concurrent edit set
client/
  src/crdt.ts              mirror of crdt.py
  src/diff.ts              <textarea> value -> operations
  src/useDocSocket.ts      socket, caret anchoring, offline outbox
  src/App.tsx              editor, status pills, merged-operation log
  src/crdt.test.ts         43 tests, incl. the shared vectors
```
