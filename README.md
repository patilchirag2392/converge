# Converge

A real-time collaborative text editor, built in stages to learn distributed-systems
concepts from the inside out. The sequence CRDT that merges concurrent edits is
hand-written — no Yjs, no Automerge.

**Stack:** React + TypeScript (client) · FastAPI + Python (server) · WebSockets (transport)

Build plan and learning gates: [ROADMAP.md](./ROADMAP.md).

---

## Status

**Stage 2 — Postgres persistence.** Documents survive a restart. The server keeps
the CRDT's operation log in Postgres, rebuilds a replica from it on first access,
and writes through before acknowledging an edit.

- [x] In-memory state replaced with Postgres, so docs survive a restart
- [x] Schema designed here, queries hand-written (no ORM)
- [x] Load a doc's state on connect, persist on change

Earlier stages are tagged: **`stage-0`** (last-write-wins, edits clobber each
other) and **`stage-1`** (the CRDT, in memory only). `git checkout stage-0` to
watch edits get destroyed.

Build notes for this stage, including the persistence decision and its tradeoffs:
[NOTES.md](./NOTES.md).

## Run it

Three things: Postgres 14+, Python 3.11+ (via [uv](https://docs.astral.sh/uv/)),
Node 20+.

```bash
# one-time
brew install postgresql@16 && brew services start postgresql@16
createdb converge
createdb converge_test        # only needed to run the database tests

# shell 1 — server on :8000
cd server
uv sync
uv run uvicorn app.main:app --reload --port 8000

# shell 2 — client on :5173
cd client
npm install
npm run dev
```

The server reads `DATABASE_URL`, defaulting to `postgresql:///converge` (local
socket, your OS user — what a Homebrew Postgres gives you with no setup). It
applies `server/app/schema.sql` on every boot; every statement is `IF NOT EXISTS`.

Open <http://localhost:5173/?doc=demo>, then the same URL in a second tab.

```bash
cd server && uv run pytest                       # 81 tests; database ones skip if no PG
cd client && npm test                            # 43 tests
cd server && uv run python scripts/race_demo.py  # the Stage 0 race, now merging
curl localhost:8000/api/doc/demo                 # the server's replica, rendered
psql converge -c 'SELECT * FROM operations LIMIT 5'
```

### Pulling the plug

The test worth doing by hand, because it exercises the client too:

1. Type something at <http://localhost:5173/?doc=demo>.
2. Kill the server: `pkill -f 'uvicorn app.main:app'`. The status pill flips to
   `closed` — your text stays on screen, because the tab holds its own replica.
3. **Keep typing.** A `queued: n` pill appears; those operations are buffered.
4. Restart the server. The tab reconnects on its own, the queue flushes, and
   everything you typed while it was down lands in Postgres.
5. `curl localhost:8000/api/doc/demo` — all of it is there, and still there after
   another restart.

Step 3 is the interesting one. Editing offline works because operations commute
and are idempotent, so sending them late merges exactly as well as sending them
immediately.

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
└───────────────────────────────┘          └──────────────────┬──────────────────┘
        three replicas, each                    writes through │ before acking
        converging on its own                                  ▼
                                             ┌─────────────────────────────────┐
                                             │ Postgres: operations (op log)   │
                                             │  one row per insert/delete      │
                                             └─────────────────────────────────┘
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

## How persistence works

### Operation log, not materialized snapshot

The decision worth defending. Three options:

| | shape | write cost per keystroke |
|---|---|---|
| **operation log** | one row per insert/delete | O(1) append |
| materialized state | one row per character + deleted flag | O(1) upsert |
| snapshot blob | one JSONB document per doc | **O(n)** — rewrites everything |

The blob is out immediately. Between the other two, **the reason to pick the log is
specific to this CRDT: an RGA never discards anything.** An insert becomes a
tombstone at worst; a delete is permanent. `snapshot_ops()` already returns exactly
one insert op per node plus one delete op per tombstone — so the "materialized
snapshot" *is* the operation set. Same rows, same size.

The usual argument for materializing (the log grows much larger than the state —
true for counters, LWW registers, anything that overwrites) **collapses here**.
Given equal size, the log wins on writes: pure appends with `ON CONFLICT DO
NOTHING`, no read-modify-write, no row locks, no UPDATE contention when two clients
edit the same document. Loading reuses `RGA.apply` rather than needing a second
deserializer, and `seq`/`created_at` give an audit trail for free.

**The tradeoff:** growth is tied to total edit history, not document size. That is
the same unbounded growth the in-memory tombstones already have, so the log isn't
making things worse — but if tombstone GC ever lands, the materialized form could
shrink where the log can't without pruning rows too.

### The schema

Two tables, in `server/app/schema.sql`. The interesting parts:

**`PRIMARY KEY (doc_id, kind, client_id, counter)`** — the operation's own
identity. That one line is the entire idempotency story: replaying a frame is free,
and two clients deleting the same character generate the *identical* row, so the
second one costs nothing. `client_id`/`counter` name the character the operation is
about, not the client that sent it.

**A `CHECK` constraint per kind** — an insert must carry a parent and exactly one
character; a delete must carry neither. The invariant lives in the database, not
only in Python, and `test_store.py` tests it with raw INSERTs that go around the
application entirely.

**`ON DELETE CASCADE`** from `operations` to `documents`, so cleanup has one place
to happen.

**An index on `(doc_id, seq)`** — the primary key already covers `WHERE doc_id =
$1`, but not the `ORDER BY seq` the load query uses, which would otherwise sort.

That `ORDER BY` is decoration: `RGA.apply` is commutative and parks causally-early
inserts, so any order rebuilds the same document. `test_load_is_order_independent`
applies the rows backwards to prove it. Which means a missing index here would cost
speed, never consistency.

### Write path

Operations are **persisted before they are applied in memory, and before they are
broadcast.** Postgres is the durability boundary, so it must never lag the replica
— otherwise the server would serve an edit that vanishes at the next restart. If
the write fails, nothing is applied and the client gets an `error` frame.

One transaction per frame: every operation from a keystroke batch commits, or none
does. The frame is fully parsed before anything is written, so one malformed
operation rejects the batch instead of half-merging it.

### Read path

A replica is rebuilt from the log on first access and then cached in memory, where
it serves every read. Concurrent first-opens are serialised by a per-document
`asyncio.Lock` — otherwise two clients joining at once would both run the load
query, build competing replicas, and one would be silently dropped along with any
edits already applied to it.

### The mid-session crash

There's a hole that persistence alone doesn't close. If the server dies after a tab
applied an edit locally but before the write committed, that operation exists *only
in that browser*. The server's snapshot can't contain it and merging can't invent
it.

So reconnect is a **mutual exchange**: the server sends its snapshot, and the
client sends its own back. Both merge. The server drops everything it already knows
and relays only the genuine gaps, so nothing is duplicated and no other tab sees a
redundant burst.

This is only thinkable because operations are idempotent and commutative. The same
move in Stage 0 — a client re-uploading its whole document on reconnect — would
have clobbered every other tab.

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
- **The operation log grows forever.** One row per character ever typed, plus one
  per deletion. A 2,000-character document is 2,000 rows; a year of editing is
  however many keystrokes that took. Needs the same tombstone GC the in-memory
  replica needs.
- **Schema changes have no migration path.** `schema.sql` is applied with
  `IF NOT EXISTS` on every boot. Fine while the schema only grows, wrong the first
  time a column has to change — that's when this wants alembic or sqitch.
- **Every replica is cached in memory forever.** Nothing evicts a document once
  loaded, so a server that has served many documents holds them all.
- **Lone UTF-16 surrogates can't be persisted.** Python's `len()` says one
  character so the CRDT accepts one, but it can't be encoded as UTF-8 and the write
  fails (rolling the frame back correctly). Reaching it requires an edit *inside* an
  emoji, which browsers don't let a caret do — typing an emoji normally is fine,
  since JS string iteration is code-point aware. Fix is to snap `diffText`'s
  boundaries to code-point edges.

### What would break if…

- **…you ran two server instances?** Each has its own `Hub`, so clients on
  different instances never exchange operations at all. Stage 3.
- **…the process restarted?** Nothing is lost. The replica is rebuilt from the
  operation log, and a live tab reconnects and re-syncs on its own.
- **…Postgres was down?** Reads of an already-loaded document keep working from
  memory, but every edit is rejected with an `error` frame rather than accepted and
  dropped, and a document nobody has opened yet can't be loaded at all. The client
  keeps its own replica and its outbox, so nothing a user typed is lost — it just
  can't be acknowledged.
- **…two servers pointed at the same database?** They'd both persist correctly and
  neither would see the other's operations until a client reconnected, because
  broadcasts don't cross processes. That's exactly Stage 3.
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

## Learn-gates

**Stage 1 — the CRDT.** Three questions to answer without looking:

1. **Why does last-write-wins fail?** See the `stage-0` tag's README — it loses data
   *and* diverges, for two separate reasons.
2. **Why does every character need a unique id?** The `"cat"` example above.
3. **How do two clients inserting at the same position end up in the same order?**
   Both characters claim the same parent; every replica sorts siblings by id
   descending, which is a total order, so all of them compute the same sequence with
   no coordination.

**Stage 2 — persistence.** Explain the schema and why it stores what it stores, in
that shape. The load-bearing parts: why the primary key is the operation's identity
(idempotency for free), why an operation log rather than materialized state (for
*this* CRDT they're the same rows, so appends win), why the load query's `ORDER BY`
doesn't matter, and why writes happen before broadcasts.

## Layout

```
NOTES.md                   per-file build notes and decisions for Stage 2
shared/crdt-vectors.json   cases both test suites assert, pinning the two implementations
server/
  app/crdt.py              the CRDT: ids, insert-after, sibling order, tombstones, buffering
  app/schema.sql           the two tables, the constraints, the index
  app/store.py             every SQL statement in the project, as readable constants
  app/db.py                connection pool + schema application
  app/hub.py               replicas (loaded from Postgres) + connection rooms
  app/main.py              create_app(), WebSocket endpoint, wire protocol
  scripts/race_demo.py     the Stage 0 race, now merging cleanly
  scripts/gen_vectors.py   regenerates the shared vectors
  tests/test_crdt.py       31 tests, incl. every permutation of a concurrent edit set
  tests/test_store.py      12 tests against real Postgres, incl. the CHECK constraints
  tests/test_persistence.py 8 restart tests, each block a separate server lifetime
client/
  src/crdt.ts              mirror of crdt.py
  src/diff.ts              <textarea> value -> operations
  src/useDocSocket.ts      socket, caret anchoring, offline outbox, reconnect replay
  src/App.tsx              editor, status pills, merged-operation log
  src/crdt.test.ts         43 tests, incl. the shared vectors
```
