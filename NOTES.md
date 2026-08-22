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
