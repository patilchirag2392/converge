# Stage 2 — Postgres persistence · build notes

Interview-prep breadcrumbs, written as I build. Terse on purpose.

---

## The decision: operation log, not materialized snapshot

Three candidates:

| | shape | write cost per keystroke |
|---|---|---|
| **a. operation log** | one row per insert/delete op | O(1) append |
| b. materialized state | one row per character + deleted flag | O(1) upsert |
| c. snapshot blob | one JSONB doc per document | **O(n)** — rewrites the whole doc |

(c) is out immediately: rewriting the entire document on every keypress.

**Chose (a). The reason is specific to this CRDT:** an RGA never discards anything.
An insert becomes a tombstone at worst; a delete is permanent. `snapshot_ops()`
already returns exactly one insert op per node plus one delete op per tombstone —
so the "materialized snapshot" *is* the operation set. Same rows, same size.

The usual argument for materializing (the log is much bigger than the state — true
for counters, LWW registers, anything that overwrites) **collapses here**. Given
equal size, the log wins on writes:

- pure appends, `ON CONFLICT DO NOTHING` — no read-modify-write, no row locks, no
  UPDATE contention when two clients edit the same doc concurrently
- loading is symmetric with what the CRDT already does: rows → op dicts →
  `RGA.apply()`, reusing the existing validation path rather than a second
  deserializer
- free audit trail (`seq`, `created_at`) for no extra work

**Main tradeoff:** unbounded growth, tied to total edit history rather than
document size. It's the same unbounded growth the in-memory tombstones already
have, so the log isn't making it worse — but if tombstone GC ever lands, the
materialized form could shrink where the log can't without pruning rows too. The
tradeoff is deferred, not dodged.

**Nice consequence:** because `apply()` is commutative and causal-safe, the load
query needs no `ORDER BY` for correctness. I added one anyway, for humans reading
`psql` output.

---

## Files

### `server/app/schema.sql`
Two tables: `documents` (one row per doc, timestamps) and `operations` (one row per
CRDT op).
- **PK is `(doc_id, kind, client_id, counter)`** — the op's own identity, which
  makes `ON CONFLICT DO NOTHING` the whole idempotency story. Two clients deleting
  the same character produce the same row, so the second is a no-op for free.
- `kind` + a `CHECK` constraint enforces the shape per kind (inserts must have a
  parent and a 1-character value; deletes must have neither) — invariant lives in
  the DB, not just in Python.
- Extra index on `(doc_id, seq)` so the load query avoids a sort; the PK index
  covers `WHERE doc_id = $1` but not `ORDER BY seq`.
- **Tradeoff:** `IF NOT EXISTS` DDL applied at startup instead of real migrations
  (alembic/sqitch). Fine while the schema only grows; wrong the first time a column
  needs to change.

### `server/app/db.py`
Pool creation + schema application. `DATABASE_URL` env var.
- **Decisions:** asyncpg over an ORM (the point of the stage is seeing the SQL);
  one pool for the process, created in FastAPI's lifespan so it's closed cleanly.
- **Tradeoff:** raw SQL means no compile-time checking of queries — mitigated by
  the store having real integration tests against a real database.

### `server/app/store.py`
All the SQL, as readable module-level constants. `PostgresOpStore` +
`InMemoryOpStore` behind an `OpStore` Protocol.
- **Decisions:** row↔op conversion goes through `crdt.parse_op`, so persistence
  can't accept an operation the CRDT would reject. `append_ops` runs one
  transaction per frame (all ops from one keystroke batch commit together, or none
  do). `executemany` rather than a single `unnest(...)` multi-row insert — chose
  legibility over the last bit of throughput.
- **The Protocol exists for tests, not for pretend database portability.** It lets
  the ~50 fast unit tests run with no database while the store itself is tested
  against real Postgres.
- **Tradeoff:** one round trip per frame (plus a `documents` touch). A busy doc
  would want write batching across frames.

### `server/app/hub.py` (changed)
`document()` and `apply_ops()` are now async, and the Hub owns a store.
- **Persist before applying in memory, and before broadcasting.** If the write
  fails, nothing is applied and the client gets an error frame — the DB is the
  durability boundary, so it must not lag the in-memory replica.
- Per-doc `asyncio.Lock` around loading, so two clients joining at once don't both
  run the load query and build competing replicas.
- Loaded replicas stay cached in memory and serve all reads; Postgres is written
  through and only read on first access.
- **Tradeoff:** the client applied its own edit optimistically before the server
  did. If the DB write fails, the client is briefly ahead of the server. Healed by
  the snapshot exchange on reconnect rather than by rollback — CRDTs don't roll
  back cleanly, and with idempotent ops re-syncing is cheaper than undoing.

### `server/app/main.py` (changed)
`create_app(store=None)` factory + lifespan.
- **Decisions:** injecting a store beats env-var magic — tests build
  `create_app(InMemoryOpStore())`, uvicorn gets `app = create_app()` which opens a
  real pool. Also kills the old module-level `hub` global, so tests are isolated
  and no longer need unique doc ids.
- Only genuinely-new ops are relayed (`apply_ops` reports which ops changed
  anything), so a reconnect that re-sends a whole snapshot doesn't fan a redundant
  burst out to every other tab.
- **Tradeoff:** `app.state` is stringly-typed; a typo is a runtime error.

### `server/app/crdt.py` (changed)
`apply_parsed` now returns whether the op changed anything.
- Needed so the server can relay only new operations. A parked (causally early)
  insert counts as a change — it's new information worth relaying.
- **Tradeoff:** the TS mirror doesn't have this return value, since the client has
  no use for it. First intentional asymmetry between the two implementations.

### `client/src/useDocSocket.ts` (changed)
On receiving a snapshot, the client now sends its *own* snapshot back.
- **Why:** the server-restart hole. If the server dies after a client applied an
  edit locally but before the DB commit, that op exists only in that browser. The
  server's snapshot won't contain it, and merging can't invent it. So reconnect is
  a **mutual** exchange: both sides send everything they have, both merge.
- Only possible because ops are idempotent and commutative — this would be
  unthinkable in Stage 0, where a stale whole-document write would clobber everyone.
- **Tradeoff:** O(document) upload per reconnect. Real systems exchange version
  vectors first and send only the difference.

### `server/tests/conftest.py`
Two fixtures for the tests that need real Postgres, both **skipping** rather than
failing when it isn't reachable, so `uv run pytest` still works without a database.
- `pg_pool` is async, for tests that call the store directly. `pg_url` is
  **synchronous** — those tests drive the app through `TestClient`, which runs it in
  its own event loop, and an asyncpg pool can't cross loops. So the app has to open
  its own pool in there; the fixture just cleans the database and points
  `DATABASE_URL` at it.
- `TRUNCATE documents CASCADE` between tests — one statement, because the FK
  cascades into `operations`.

### `server/tests/test_store.py`
12 integration tests. The ones that justify hand-writing the SQL:
- the `CHECK` constraints are tested by going *around* the store with raw INSERTs —
  testing the database, not Python
- `ON CONFLICT DO NOTHING` dedup, incl. two clients deleting the same character
  producing one row
- **load order independence**: apply the rows backwards, get the same document.
  Proves the `ORDER BY` is decoration.
- batch atomicity, triggered by a lone UTF-16 surrogate (see limitation below)

### `server/tests/test_persistence.py`
8 restart tests. Each `with TestClient(create_app())` block is one server
lifetime; leaving it closes the pool and discards every replica, so the next block
is a genuinely cold process.
- Includes the mid-session-crash case: a replica holding ops the server never
  stored, replaying them on reconnect.
- **Tradeoff:** can't restart the server *under* a live socket in-process, so the
  real test is the browser one below.

### `server/tests/helpers.py`
`Tab` — a socket plus its own replica — shared by the websocket and persistence
tests. Also holds `sync()`, a barrier that sends a deliberately invalid frame and
waits for the error reply; a single socket is ordered, so that proves everything
earlier was processed. Needed because an author never gets a reply of its own.

---

## Verified

**Automated:** 81 server tests — 31 CRDT, 17 hub, 13 websocket (all four need no
database), 12 store integration, 8 restart — plus 43 client tests.

**Pull the plug, for real** — scripted against a live browser tab:

| step | result |
|---|---|
| typed with server up | in Postgres immediately |
| `pkill uvicorn` | tab flips to `closed`, keeps its text |
| kept typing while down (19 chars) | `queued: 19` pill |
| restarted server | tab reconnects, queue flushes, server text matches the tab |
| killed + restarted again | text comes straight back from Postgres |

No JS errors. The queued-then-flushed path is the one worth re-running by hand —
it's the outbox and the mutual replay working together.

What the rows look like, which is the nicest confirmation the design is right:

```
 seq |  kind  | client_id | counter | p_client | p_ctr | value
   1 | insert | 02bdb4e1  |       1 |          |     0 | w
   2 | insert | 02bdb4e1  |       2 | 02bdb4e1 |     1 | r
   3 | insert | 02bdb4e1  |       3 | 02bdb4e1 |     2 | i
```

The chain is right there — each character's parent is the one before it, which is
why a typed run stays contiguous under concurrent edits.

---

## Limitation found while building this

**Lone UTF-16 surrogates can't be persisted.** Python's `len()` counts one
character so `parse_op` accepts one, but it can't be encoded as UTF-8, so the
write fails and the whole frame rolls back.

Reaching it needs `diffText` to split a surrogate pair, which needs an edit
*inside* an emoji — browsers move the caret over a pair atomically, so normal
typing never does it. Typing an emoji normally is fine: JS string iteration is
code-point aware, so an emoji becomes one node with both code units.

Not fixed here (Stage 1 code, and unreachable by hand), but pinned by
`test_a_batch_is_all_or_nothing`, which uses it as the poison value. Proper fix:
snap `diffText`'s boundaries to code-point edges.

---

# Stage 3 — horizontal scaling with Redis pub/sub · build notes

## Step 1: the break, reproduced before fixing anything

Two instances (`a` on :8001, `b` on :8002) behind nginx on :8080, round-robin,
both on the same Postgres. No Redis. `multi_instance_demo.py` output:

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

**Two distinct failures, one cause — state lives per process:**

1. **The live broadcast never leaves the instance that received it.** `Hub._rooms`
   is a dict in one process; `broadcast()` can only reach sockets in that dict.
2. **A warm instance serves an indefinitely stale replica.** This one I didn't
   expect and it's the better half of the story. Stage 2 loads a document once and
   caches it forever. Instance `a` loaded this doc while it was empty, and nothing
   ever invalidated that. It will keep answering `''` over HTTP for as long as the
   process lives — Redis has to fix the read path, not just the broadcast.

Nothing is *lost*: Postgres has every op, so a cold instance reads it correctly and
a client reconnect heals. The system is stale, not lossy — which is exactly why it's
tempting to ship and exactly why it's unacceptable for a live editor.

**Why not just use sticky sessions?** `ip_hash` in nginx would pin each client to
one instance and make the demo pass. It doesn't solve the problem: two *different*
users still land on different instances and still can't see each other. It also
breaks on any rebalance or instance restart, and it makes scaling uneven. Chose
round-robin deliberately so the real requirement stays visible.

## Files

### `deploy/nginx.conf`
Round-robin over two backends on :8080.
- **The `map $http_upgrade $connection_upgrade` block is the load-bearing part.**
  `Upgrade`/`Connection` are hop-by-hop headers and aren't forwarded by default, so
  without it the WebSocket handshake arrives at the backend stripped and fails as a
  plain HTTP request. Also needs `proxy_http_version 1.1` (default to upstreams is
  1.0, which has no upgrade mechanism).
- `proxy_read_timeout 1h` — an editor socket is idle whenever nobody types, and the
  60s default would drop it mid-session.
- **Tradeoff:** round-robin means a reconnecting client can land on a different
  instance than before. Correct here because replicas are rebuildable, but it's the
  thing sticky sessions exist to avoid.

### `scripts/cluster.sh`
`up` / `down` / `status` for two instances + nginx (+ Redis when `REDIS_URL` is set).
- Runs everything unprivileged: nginx on :8080 with its own prefix under
  `deploy/.run/`, so nothing touches the Homebrew config or launchd.
- `uv run --directory server` so `app` is importable — uvicorn resolves the module
  from cwd, which bit me first try.
- Prints four `/healthz` responses at the end so you can watch round-robin
  alternate before doing anything else.
- **Tradeoff:** a shell script, not a process supervisor. No restart-on-crash, and
  `down` has to pattern-match strays because `uv` spawns uvicorn as a child.

### `server/scripts/multi_instance_demo.py`
Opens sockets until two land on *different* instances, types on one, waits for the
other. Reports whichever outcome it sees, so the same script is the before *and*
after demo. Exit code 1 = didn't propagate.
- Queries each instance directly (`:8001`, `:8002`) **and** rebuilds the document
  straight from Postgres with no server involved — that three-way comparison is
  what makes the stale-cache failure legible rather than confusing.

### `server/app/main.py` (changed)
`create_app(store, instance_id)`; `INSTANCE_ID` env with a random fallback.
- Instance id is reported by `/healthz`, `/api/doc/{id}` **and every snapshot
  frame**, so a browser tab can display which process it's talking to. Without that
  the whole stage is unobservable.
- **Tradeoff:** a random id when unset is friendly for one-off runs but means log
  lines from an unconfigured instance aren't stable across restarts.

## Step 2: the fix — Redis pub/sub

Same script, same cluster, `REDIS_URL` now set:

```
  alice -> instance 'b'
  bob   -> instance 'a'
alice typed "typed on one instance"
  alice sees: 'typed on one instance'
  bob sees:   'typed on one instance'
=> PROPAGATED in 14.1 ms across instances.

what each instance thinks the document says:
  instance a (:8001): 'typed on one instance'
  instance b (:8002): 'typed on one instance'
```

Both Step 1 failures gone: the live broadcast crosses processes, **and** the
warm instance is no longer stale, because it applies incoming ops to its cached
replica.

### The decision: what travels over Redis

**The same operation dicts that travel over the WebSocket**, in a thin envelope
(`doc_id`, `ops`, `origin`, `author`, `published_at`).

The argument is about what kind of channel Redis pub/sub *is*: fire-and-forget,
at-most-once, no replay, no ordering guarantee between publishers, and anything
published while a subscriber is disconnected is gone. That is a weak channel.

An op-based CRDT is the ideal payload for exactly that channel — ops are
commutative (unordered delivery is fine), idempotent (duplicate delivery is
fine), and self-contained (no prior context needed). **The properties the channel
can't provide are the properties the payload doesn't need.**

The alternative would be a "document X changed, go re-read Postgres"
notification, which is what you'd be forced into if you were shipping *state*
rather than operations: correctness would need ordering the channel can't give,
so you'd fall back on the database as the tiebreak — an extra query per keystroke
and a thundering herd on every edit. Sending whole documents was never on the
table; that's Stage 0 with more steps.

Zero translation is the other win: one format, one validator (`parse_op`), no
second schema to keep in sync with the wire protocol.

**Tradeoff:** a dropped message is silently lost. Survivable only because
Postgres has every op, so a reconnect or cold load heals any gap. Redis is a
*latency optimisation over the durable log*, not a second source of truth — and
that's the sentence that justifies the whole design.

### Ordering: persist → apply → broadcast locally → publish

Persist first, always. If the write fails, nothing is applied, nothing is sent,
nothing is published, and the client gets an error — rather than an edit that is
visible on three machines and gone after a restart. Cost: propagation latency
includes the Postgres commit. Publishing first would shave that off in exchange
for visible-but-not-durable edits, which is a bad trade for an editor.

**Local broadcast before publish** — and I got this backwards first. Publishing
inside `apply_ops` before `main.py`'s local broadcast meant clients on the
*originating* instance waited for the Redis round trip too. Not a correctness
bug, and invisible by inspection — but the latency probe showed same-instance and
cross-instance identical to two decimal places (1.50 ms vs 1.50 ms), which is not
a plausible measurement. After moving the local fan-out ahead of the publish:
same-instance dropped to 1.22 ms and the difference became real. **The metric
found the flaw**, which is the argument for measuring rather than asserting.

### Loop prevention: prevented, not just tolerated

The loop is client → A → Redis → A. Handled by an explicit `origin ==
instance_id` check that drops our own messages.

Asked directly: am I relying on CRDT idempotency, or preventing duplicates? **Both,
deliberately, at different layers.**

- *Prevented* where it's exact and free: self-delivery. Idempotency would make it
  harmless to state, but we'd still re-send ops to local clients that already
  have them, and it would pollute the latency window with a ~0 ms self-sample.
- *Tolerated* everywhere else, because pub/sub gives no delivery guarantees and
  correctness must not depend on perfect dedup: redelivery, a client reconnecting
  onto a different instance and replaying its snapshot, two instances both
  hearing the same op. `test_a_duplicate_delivery_is_harmless` pins that.

Remote ops are also **not re-persisted** (the origin committed them before
publishing; writing again is pointless, though `ON CONFLICT` makes it harmless)
and **not republished** (otherwise every instance rebroadcasts everything it
hears and the cluster amplifies each edit without bound).

### The measured number

`scripts/latency_probe.py`, 60 samples, one process holding all three sockets so
there is a single clock and no skew question:

| | p50 | p95 | max |
|---|---|---|---|
| same-instance | 1.22 ms | 1.53 ms | 1.77 ms |
| cross-instance | 1.56 ms | 1.86 ms | 2.14 ms |

**The Redis hop costs ~0.34 ms at p50.** Both figures include the Postgres
commit, so the difference isolates Redis. Independently corroborated by
`/metrics`, which measures the publish→remote-handling leg from the servers' own
clocks and reports p50 0.27–0.39 ms. Two different methods agreeing is the reason
to trust either.

## Files

### `server/app/bus.py`
`OpMessage` envelope + `Bus` protocol with four implementations: `RedisBus`,
`NullBus` (single instance), `InProcessCluster`/`InProcessBus` (tests).
- **One channel per document** (`converge:ops:{doc_id}`), not one global channel:
  an instance shouldn't be woken by traffic for documents it isn't serving. Costs
  subscription churn as rooms open and close.
- One reader task drains the pubsub for the whole process, and **never lets an
  exception kill the loop** — one malformed message must not stop delivery for
  every other document on the instance.
- Subscribes to a keepalive channel at startup because redis-py's `get_message`
  needs at least one subscription to have a connection, and an instance starts
  with no documents open.
- `InProcessCluster` delivers to *every* bus **including the publisher's own**, on
  purpose, so the loop-prevention path is exercised by tests rather than only in
  production.
- **Tradeoff:** `Bus` has four implementations, which is a lot of surface for one
  behaviour. Justified by the tests running with no Redis at all.

### `server/app/hub.py` (changed)
Gains `bus`, `instance_id`, `attach`/`detach`, `apply_remote_ops`, and a latency
window.
- **`attach` subscribes *before* loading.** Load first and an op published between
  the query and the subscription is lost to this instance — not in the rows we
  read, and we weren't listening. Subscribe first and the worst case is an op we
  also read from Postgres, which is a no-op.
- **Invariant: a document is cached iff this instance is subscribed to it, iff it
  has ≥1 local client.** `detach` unsubscribes and evicts together. This is the
  fix for Step 1's second failure — a cached copy with nothing feeding it *is* the
  stale-replica bug. It also retires Stage 2's "replicas cached forever" wart.
- `document(cache=False)` for read-only callers, so a `curl` can't leave behind an
  unsubscribed replica that answers stale forever.
- **Tradeoff:** reopening a document re-reads Postgres. One query against being
  confidently wrong.

### `server/app/main.py` (changed)
Lifespan builds a `RedisBus` when `REDIS_URL` is set, `NullBus` otherwise; adds
`/metrics`.
- Single-instance and clustered are **the same code with different config**, which
  is what made the Step 1 → Step 2 comparison a config change rather than a
  different branch.
- The endpoint no longer broadcasts; `apply_ops` owns the whole ordering so it
  lives in one documented place.
- **Tradeoff:** `/metrics` is wall-clock based and so only trustworthy when the
  instances' clocks agree. Documented in the handler and in the probe.

### `server/tests/test_cluster.py` / `test_redis_bus.py`
15 + 7 tests. Cluster semantics run on the in-process bus (fast, one event loop,
deterministic); the Redis wiring gets its own integration file that skips when
Redis is absent.
- Loop prevention is asserted *observably*: after an edit, the origin instance's
  latency window is empty and the remote's has exactly one sample.
- **Tradeoff:** no test drives two `TestClient`s over one bus. Each `TestClient`
  runs the app in its own event loop, so an in-process bus would have to await a
  handler across loops. The real cross-process path is covered by the scripts
  against the live cluster instead.

### `server/scripts/latency_probe.py`
Two probes on one instance, one on another, single `time.monotonic()` clock.
- Reports same-instance and cross-instance separately so the difference isolates
  Redis, and states what each figure includes.
- 5 warm-up samples discarded.
- **Tradeoff:** measures a loopback cluster on an idle machine. Real numbers need
  real hosts and real load; this is a floor, not a capacity claim.

### Client (`protocol.ts`, `useDocSocket.ts`, `App.tsx`)
Snapshot carries `instance`; the UI shows it as a pill.
- Without it the stage is unobservable — you can't tell whether two tabs are on
  the same process, so you can't tell whether propagation is being tested at all.

## Known limitation found at the end

**Presence is per-instance now.** The `clients: n` pill counts sockets on *this*
instance only, so two tabs on different instances both show `clients: 1`. Visible
in the Stage 3 screenshot and wrong on its face.

Not fixed here — it needs presence in Redis (a key per document with the union of
connected clients, or a periodic broadcast of per-instance counts) which is its
own design with its own expiry/liveness problem. Filed rather than hidden.
