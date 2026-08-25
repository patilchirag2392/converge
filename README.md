# Converge

A real-time collaborative text editor, built in stages to learn distributed-systems
concepts from the inside out. The sequence CRDT that merges concurrent edits is
hand-written — no Yjs, no Automerge.

**Stack:** React + TypeScript (client) · FastAPI + Python (server) · WebSockets (transport)

Build plan and learning gates: [ROADMAP.md](./ROADMAP.md).

---

## Status

**Stage 3 — horizontal scaling with Redis pub/sub.** Multiple server instances
behind an nginx load balancer. Each instance holds its own WebSocket connections
and its own CRDT replica; Redis carries operations between them so a client on one
instance sees edits made on another.

- [x] Two instances behind an Nginx load balancer
- [x] Observed the break: clients on different instances couldn't see each other
- [x] Redis pub/sub so edits broadcast across all instances
- [x] Measured real cross-instance propagation latency

**Measured, not estimated** (`scripts/latency_probe.py`, 60 samples, one process
and one clock so there's no skew):

| | p50 | p95 | max |
|---|---|---|---|
| same-instance | 1.22 ms | 1.53 ms | 1.77 ms |
| cross-instance | 1.56 ms | 1.86 ms | 2.14 ms |

The Redis hop costs **~0.34 ms at p50**. Both figures include the Postgres commit,
since operations are persisted before they're published.

Earlier stages are tagged: **`stage-0`** (last-write-wins, edits clobber each
other), **`stage-1`** (the CRDT), **`stage-2`** (Postgres persistence).

Build notes and the decisions behind each stage: [NOTES.md](./NOTES.md).

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

## Running a cluster

```bash
# two instances + nginx on :8080, with pub/sub
REDIS_URL=redis://localhost:6379 scripts/cluster.sh up

# and the client pointed at the load balancer instead of one instance
cd client && VITE_WS_URL=ws://localhost:8080 npm run dev
```

`scripts/cluster.sh status` shows what's listening; `down` stops it. Everything
runs unprivileged — nginx on :8080 with its own prefix under `deploy/.run/`.

Open two tabs on <http://localhost:5173/?doc=demo> and check the `instance:` pill:
round-robin means they usually land on **different** processes. Type in one and it
appears in the other, having crossed through Redis.

### Watching it break first

The interesting part of this stage is the failure. Start the cluster *without*
Redis:

```bash
scripts/cluster.sh up                                  # no REDIS_URL
cd server && uv run python scripts/multi_instance_demo.py
```

```
  alice -> instance 'b'
  bob   -> instance 'a'
alice typed "typed on one instance"
  alice sees: 'typed on one instance'
  bob sees:   ''
=> DID NOT PROPAGATE within 3s.

what each instance thinks the document says:
  instance a (:8001): ''
  instance b (:8002): 'typed on one instance'
straight from Postgres:     'typed on one instance'
```

**Two failures, one cause — state lives per process:**

1. The live broadcast never leaves the instance that received it. `Hub._rooms` is
   a dict in one process, so `broadcast()` can only reach sockets in that dict.
2. A warm instance serves an indefinitely stale replica. Instance `a` loaded the
   document while it was empty and nothing ever invalidated that, so it will keep
   answering `''` for as long as the process lives.

Nothing is *lost* — Postgres has every operation, so a cold instance reads it
correctly and a reconnect heals. The system is **stale, not lossy**, which is
exactly why it's tempting to ship and exactly why it's unacceptable for a live
editor.

Then set `REDIS_URL` and run the identical script: it propagates, and both
instances agree.

**Why not sticky sessions?** `ip_hash` in nginx would pin each client to one
instance and make that demo pass. It doesn't fix anything: two *different* users
still land on different instances and still can't see each other. It also breaks
on any rebalance or restart. Round-robin is deliberate so the real requirement
stays visible.

---

## Architecture

```
                          ┌─────────────┐
   browser tabs ─────────►│    nginx    │  round-robin, :8080
                          └──┬───────┬──┘
             ┌───────────────┘       └───────────────┐
             ▼                                      ▼
   ┌──────────────────────┐              ┌──────────────────────┐
   │  instance a  :8001   │              │  instance b  :8002   │
   │  Hub                 │              │  Hub                 │
   │   its own sockets    │              │   its own sockets    │
   │   its own replicas   │              │   its own replicas   │
   └─────┬──────────┬─────┘              └─────┬──────────┬─────┘
         │          │   subscribe / publish    │          │
         │          └──────────┐   ┌───────────┘          │
         │                     ▼   ▼                      │
         │            ┌──────────────────────┐            │
         │            │ Redis pub/sub        │            │
         │            │ converge:ops:{doc}   │            │
         │            └──────────────────────┘            │
         │                                                │
         │   persist BEFORE publishing                    │
         └────────────────┐              ┌────────────────┘
                          ▼              ▼
                  ┌──────────────────────────────┐
                  │ Postgres: operations         │  the source of truth
                  └──────────────────────────────┘
```

Every browser tab, and every server instance, holds a full CRDT replica. Redis is
a *live delivery layer over the durable log* — never a second source of truth. If
it drops a message, Postgres still has the operation and the next reconnect or
cold load heals the gap.

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

## How cross-instance propagation works

### What travels over Redis

**The same operation dicts that travel over the WebSocket**, in a thin envelope:

```jsonc
{"doc_id": "demo",
 "ops": [{"kind": "insert", "id": ["a7f", 12], "parent": ["b31", 4], "value": "x"}],
 "origin": "a",              // publishing instance, used to drop our own messages
 "author": "a7f",            // client that made the edit
 "published_at": 1787604469.812}
```

The argument for that choice is about what kind of channel Redis pub/sub *is*:
fire-and-forget, at-most-once, no replay, no ordering guarantee between
publishers, and anything published while a subscriber is disconnected is gone.
That's a weak channel.

An op-based CRDT is the ideal payload for exactly that channel. Operations are
commutative, so unordered delivery is fine; idempotent, so duplicate delivery is
fine; self-contained, so a subscriber needs no prior context. **The guarantees the
channel can't give are the guarantees the payload doesn't need.**

The alternative — a "document X changed, go re-read Postgres" notification — is
what you'd be forced into if you shipped *state*, because correctness would need
ordering the channel can't provide. That costs a query per keystroke and a
thundering herd on every edit.

**One channel per document** (`converge:ops:{doc_id}`), so an instance isn't woken
by traffic for documents it isn't serving.

### Ordering: persist → apply → broadcast locally → publish

Every step is deliberate:

- **Persist first.** If the write fails, nothing is applied, sent, or published,
  and the client gets an error — rather than an edit that's visible on three
  machines and gone after a restart. Cost: propagation includes the Postgres
  commit.
- **Local broadcast before publish**, so clients on the originating instance don't
  wait on a Redis round trip to see their neighbour's typing.

That second one was wrong at first, and the *metric* caught it: same-instance and
cross-instance came out identical to two decimal places, which isn't a plausible
measurement. Moving the local fan-out ahead of the publish dropped same-instance
from 1.50 ms to 1.22 ms and made the comparison mean something.

### Not looping, not double-applying

The loop to worry about is client → instance A → Redis → instance A. Every message
carries the publishing instance's id, and a receiver drops its own.

Both prevention *and* tolerance, at different layers, on purpose:

- **Prevented** where it's exact and free — self-delivery. Idempotency would make
  it harmless to state, but we'd still re-send operations to local clients that
  already have them, and it would pollute the latency window with ~0 ms samples.
- **Tolerated** everywhere else, because pub/sub gives no delivery guarantees and
  correctness must never depend on perfect dedup: redelivery, a client reconnecting
  onto a different instance and replaying its snapshot, two instances hearing the
  same operation.

Operations arriving from Redis are **not re-persisted** (the origin committed them
before publishing) and **not republished** (otherwise every instance rebroadcasts
what it hears and the cluster amplifies each edit without bound).

### The invariant that keeps replicas honest

**A document is cached in a process if and only if that process is subscribed to
it, which is if and only if it has at least one local client.** `attach`
subscribes then loads then registers; `detach` unsubscribes and evicts together.

Subscribing *before* loading closes the join race: load first and an operation
published between the query and the subscription is lost to that instance. Doing
it in this order, the worst case is receiving an operation that was also in the
rows we read — a no-op, because applying is idempotent.

Break that invariant and you get the Stage 3 stale-replica bug back: a cached copy
with nothing feeding it.

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
- **Presence is per-instance.** The `clients: n` pill counts sockets on *this*
  instance only, so two tabs on different instances each show `clients: 1`. Needs
  presence in Redis (a key per document, or periodic per-instance count
  broadcasts) with its own expiry and liveness design.
- **A dropped Redis message is silently lost.** Pub/sub is at-most-once with no
  replay, so an instance disconnected from Redis misses everything published in
  the gap. Survivable only because Postgres has every operation — a reconnect or
  cold load heals it. A stricter design would use Redis Streams with consumer
  groups and acknowledgements.
- **`/metrics` uses wall clocks on two machines.** Meaningful on one host,
  unreliable across hosts. `scripts/latency_probe.py` is the trustworthy
  measurement because it uses a single clock.
- **Cluster orchestration is a shell script.** No supervision, no restart-on-crash,
  and `down` pattern-matches stray processes. Stage 4's Docker Compose replaces it.
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

**Stage 3 — scaling.** Explain why two instances break sync and how pub/sub fixes
it. The load-bearing parts: that broadcast state is per process (and that the
*stale cached replica* is the same bug in the read path), why operations are the
right thing to put on a weak channel, why persistence happens before publishing,
how the self-echo loop is prevented, and what the measured latency figure does and
doesn't include.

**Stage 2 — persistence.** Explain the schema and why it stores what it stores, in
that shape. The load-bearing parts: why the primary key is the operation's identity
(idempotency for free), why an operation log rather than materialized state (for
*this* CRDT they're the same rows, so appends win), why the load query's `ORDER BY`
doesn't matter, and why writes happen before broadcasts.

## Layout

```
NOTES.md                   per-file build notes and decisions for Stage 2
shared/crdt-vectors.json   cases both test suites assert, pinning the two implementations
deploy/nginx.conf          load balancer: round-robin, WebSocket upgrade headers
scripts/cluster.sh         two instances + nginx (+ Redis), up / down / status
server/
  app/crdt.py              the CRDT: ids, insert-after, sibling order, tombstones, buffering
  app/bus.py               Redis pub/sub: the envelope, per-document channels, the reader
  app/schema.sql           the two tables, the constraints, the index
  app/store.py             every SQL statement in the project, as readable constants
  app/db.py                connection pool + schema application
  app/hub.py               replicas (loaded from Postgres) + connection rooms
  app/main.py              create_app(), WebSocket endpoint, wire protocol
  scripts/race_demo.py     the Stage 0 race, now merging cleanly
  scripts/multi_instance_demo.py  does an edit cross instances? reports either answer
  scripts/latency_probe.py conversation-ready latency numbers, measured on one clock
  scripts/gen_vectors.py   regenerates the shared vectors
  tests/test_crdt.py       31 tests, incl. every permutation of a concurrent edit set
  tests/test_store.py      12 tests against real Postgres, incl. the CHECK constraints
  tests/test_persistence.py 8 restart tests, each block a separate server lifetime
  tests/test_cluster.py    15 cross-instance tests on an in-process bus
  tests/test_redis_bus.py  7 integration tests against real Redis
client/
  src/crdt.ts              mirror of crdt.py
  src/diff.ts              <textarea> value -> operations
  src/useDocSocket.ts      socket, caret anchoring, offline outbox, reconnect replay
  src/App.tsx              editor, status pills, merged-operation log
  src/crdt.test.ts         43 tests, incl. the shared vectors
```
