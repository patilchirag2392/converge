# Resume here

Session handoff for Converge. Written 2026-08-25, updated 2026-08-26 after
Stage 4. Paste this file (or point at it) to bring a fresh session up to speed.

Companion docs: [ROADMAP.md](./ROADMAP.md) is the plan, [NOTES.md](./NOTES.md) is
the per-file decision trail for Stages 2–4, [README.md](./README.md) is how it all
works.

---

## Where things stand

**Stages 0–4 are built and verified. Stage 5 (AWS) is next and untouched.**

| Stage | What | State |
|---|---|---|
| 0 | last-write-wins, edits clobber | done, tagged `stage-0` |
| 1 | hand-written RGA CRDT | done, **not tagged, not merged** |
| 2 | Postgres persistence (op log) | done, **not tagged, not merged** |
| 3 | Redis pub/sub across 2 instances | done and pushed, **not tagged, not merged** |
| 4 | Docker + GitHub Actions | done, **not pushed** — so CI has never run |
| 5 | Deploy to AWS | not started |

Tests: **103 server** (`cd server && uv run pytest`), **43 client**
(`cd client && npm test`). Every commit on `stage-3-redis` passes independently —
verified in a throwaway worktree, not assumed.

### Git topology — read this before doing anything

```
main              e801044   Stage 0 only (+ one demo fix)      [pushed]
stage-1-crdt      f433c0a   Stage 1                            [pushed]
stage-2-postgres  f0a0955   Stage 1 + 2                        [pushed]
stage-3-redis     cf6a626   Stage 1 + 2 + 3                    [pushed]
stage-4-docker-ci (tip)     Stage 1 + 2 + 3 + 4 ← current      [NOT pushed]
tags              stage-0                                      [pushed]
```

**`stage-4-docker-ci` has never been pushed, which means the GitHub Actions
workflow has never run.** That is the single biggest open item: the workflow is
desk-checked and every step was verified by hand against the local stack, but
"goes green" is unproven. Pushing the branch is what proves it.

**Nothing has been merged to `main` since Stage 0.** Each stage branch builds on
the previous one, so `stage-4-docker-ci` carries all four stages' work, well
ahead of `origin/main`. The branch names no longer line up with what's in each.
Four branches are on GitHub; `stage-4-docker-ci` is local only.

That's a deliberate consequence of the roadmap's rule ("merge when the stage's
learn-gate passes") plus the fact that **no learn-gate has been done for Stages
1, 2, 3 or 4**. Decide whether to keep gating or catch up — see loose ends below.

---

## Getting running again

### The easy way, as of Stage 4

```bash
cd ~/Developer/converge
colima start                       # the VM does NOT run at login
docker compose up --build          # six containers, ~17s to healthy
```

Then <http://localhost:5173/?doc=demo> in two tabs. `docker compose down` to
stop, `down -v` to also drop the Postgres volume.

**Docker is installed now** (it wasn't when Stage 4 started): Colima 0.10.3 plus
the docker CLI, buildx and compose plugins, all via Homebrew — no Docker Desktop.
Nothing registers at login, matching how Redis and nginx were already handled.
`colima status` / `colima stop`.

Ports under compose: 5173 client · 8080 nginx · 8001/8002 the two instances ·
**55432** Postgres · **56379** Redis. The last two are deliberately not the
default ports, so they can't collide with — or be confused for — the Homebrew
Postgres and Redis.

### The host-process way, still the best loop for editing code

`--reload`, real debuggers, no rebuild. Everything is installed via Homebrew.
Postgres runs as a launchd service; Redis and nginx do **not** (started ad-hoc
by the cluster script, deliberately, so nothing was registered permanently).

```bash
cd ~/Developer/converge

# 1. Postgres (should already be running at login)
pg_isready || brew services start postgresql@16

# 2. single instance, no cluster — simplest for CRDT/persistence work
cd server && uv run uvicorn app.main:app --reload --port 8000
cd client && npm run dev                       # http://localhost:5173/?doc=demo

# 3. OR the full Stage 3 cluster: 2 instances + nginx + Redis
REDIS_URL=redis://localhost:6379 scripts/cluster.sh up
cd client && VITE_WS_URL=ws://localhost:8080 npm run dev
scripts/cluster.sh status     # what's listening
scripts/cluster.sh down       # stop instances + nginx (leaves Redis up)
```

**Ports:** 5173 client · 8000 single instance · 8001/8002 cluster instances ·
8080 nginx · 5432 Postgres · 6379 Redis.

**Databases:** `converge` (dev), `converge_test` (tests; truncated per test).
Create with `createdb` if missing.

**Env vars:** `DATABASE_URL` (default `postgresql:///converge`), `REDIS_URL`
(unset = single-instance mode, no pub/sub), `INSTANCE_ID`, `VITE_WS_URL`,
`TEST_DATABASE_URL`, `TEST_REDIS_URL`, and from Stage 4
`CONVERGE_CONNECT_TIMEOUT` (startup retry budget, default 30s), `LOG_LEVEL`,
`CONVERGE_REQUIRE_SERVICES` (turns test skips into failures — CI sets it).
All of them, with defaults, are in `.env.example`.

### Demo scripts, all real

```bash
cd server
uv run python scripts/race_demo.py            # the Stage 0 race, now merging
uv run python scripts/multi_instance_demo.py  # does an edit cross instances?
uv run python scripts/latency_probe.py 60     # measured propagation latency
uv run python scripts/gen_vectors.py          # regenerate shared CRDT vectors
```

`multi_instance_demo.py` reports whichever outcome it observes, so it's the
before *and* after demo — exit 1 without Redis, 0 with it.

**Browser-level checks live in `e2e/`** (see its README): concurrent editing and
caret behaviour, killing the server mid-session, and two tabs forced onto
different instances. They need a running stack and a real browser, so they're
manual rather than part of `npm test`. `e2e/screenshots/` has one image per stage
as visual evidence of the progression.

---

## Loose ends, roughly in priority order

1. **No learn-gate done for Stages 1, 2, 3 or 4**, and no `stage-1`…`stage-4` tags. The roadmap gates merging on being able to explain the stage
   without looking. Either do the gates and merge+tag each, or decide to catch up
   in one go. The questions are in the README under *Learn-gates*.
2. **`main` is three stages behind.** The longer this drifts, the less the history
   reads like the roadmap — which was the point of the branch-per-stage rule.
3. **Presence is per-instance** (found at the end of Stage 3). The `clients: n`
   pill counts only sockets on the instance you're connected to, so two tabs on
   different instances each show `clients: 1`. Visibly wrong. Needs presence in
   Redis with its own expiry/liveness design.
4. **The CI workflow has never run.** `stage-4-docker-ci` isn't pushed. Every
   step was verified by hand locally — pytest under `CONVERGE_REQUIRE_SERVICES=1`,
   the round-robin shell snippet verbatim, `compose build`, `compose up --wait`,
   the demo — but GitHub Actions can't be run locally, so the workflow as a whole
   is unproven. Push the branch and watch it.
5. **A Redis restart reports a false error** (found during Stage 4, filed not
   fixed). The first edit after Redis is replaced gets
   `"could not persist operations"` — which is untrue: the ops *are* in Postgres
   and local clients *did* see them; only the cross-instance publish failed. Two
   defects: no publish retry on the bus, and one exception handler covering two
   failure modes with opposite consequences. Deterministic repro and full
   diagnosis in NOTES.md. Doesn't affect a clean-state `compose up`.
6. **`VITE_WS_URL` is baked into the client image at build time** (Vite inlines
   `import.meta.env.VITE_*`), so the client image is environment-specific. This
   needs solving *before* Stage 5 puts it behind a real hostname — runtime
   injection via an entrypoint that writes a `config.js` the app reads.
7. **`scripts/cluster.sh` survives on purpose.** compose replaced it for
   everything except running the stack as bare host processes, which is still
   the fastest loop for code changes. It's still a shell script with no
   supervision and a `down` that pattern-matches strays.

---

## Decisions already made (don't re-litigate these)

**Stage 1 — RGA (insert-after) over fractional indexing.** Each character has a
permanent id `(client_id, counter)`; an insert names the character it follows.
Siblings sort by id **descending** — required, not cosmetic: in `"ab"`, inserting
`"c"` after `"a"` must give `"acb"`, and oldest-first would give `"abc"`.
Tombstones for deletes, causal buffering for early inserts, Lamport-style counters.
`apply()` is commutative + idempotent + causal-safe.

The CRDT is implemented **twice** (`server/app/crdt.py`, `client/src/crdt.ts`) so
the server is a real replica, not a referee. `shared/crdt-vectors.json` is
generated from Python and asserted by both test suites so they can't drift.

**Stage 2 — operation log, not materialized snapshot.** The reason is specific to
this CRDT: an RGA never discards anything, so `snapshot_ops()` *is* the op set —
same rows, same size. The usual "log >> state" argument collapses, so appends win
(`ON CONFLICT DO NOTHING`, no read-modify-write, no row locks). Primary key is the
operation's own identity, which is the whole idempotency story.

**Stage 3 — raw ops over Redis, one channel per document.** Pub/sub is
fire-and-forget, at-most-once, unordered, no replay. An op-based CRDT is the ideal
payload for exactly that channel, because the guarantees it can't give are the ones
ops don't need. Redis is a *latency layer over the durable log*, never a second
source of truth.

**Ordering, everywhere: persist → apply → broadcast locally → publish.** Postgres
first so nothing visible is undurable. Local broadcast before publish so local
clients don't wait on Redis — I had that backwards at first and the latency probe
caught it (both paths measured identical, which isn't plausible).

**Loop prevention: both layers, deliberately.** Self-echo is *prevented* with an
explicit `origin` check (exact and free). Everything else is *tolerated* via
idempotency, because pub/sub gives no delivery guarantees and correctness must not
depend on perfect dedup.

**Invariant worth protecting:** a document is cached in a process **iff** that
process is subscribed to it **iff** it has ≥1 local client. Break it and the
Stage 3 stale-replica bug comes back.

**Stage 4 — healthchecks *and* retry-on-connect, both deliberately.** Bare
`depends_on` does not wait for Postgres to accept connections; it waits for the
container to be *started*. `condition: service_healthy` does wait, and is used
everywhere — but it isn't sufficient, for three reasons: a healthcheck can lie
(socket-based `pg_isready` reports READY during `initdb`, so the compose file
forces TCP with `-h 127.0.0.1`), healthy at t=0 says nothing about t=1, and
`depends_on` doesn't exist outside compose. Healthchecks make the common case
fast and the logs clean; the app's retry loop is the half that's actually
correct and the half that still exists on ECS.

**Stage 4 — multi-stage because layers are additive, not because of `--no-dev`.**
A single-stage image that genuinely `rm`s `uv` and the wheel cache is still 51 MB
bigger than the multi-stage one, because a file added in one layer and deleted in
a later one is still shipped. Multi-stage works because the final stage starts
clean and receives only what a `COPY --from` names. Measured numbers in NOTES.md.

**Stage 4 — two explicit server services, not `deploy: replicas: 2`.** Replicas
can't have distinct `INSTANCE_ID`s, and without those the stage is unobservable.

### The measured number (real, one clock, no skew)

`scripts/latency_probe.py`, 60 samples:

| | p50 | p95 | max |
|---|---|---|---|
| same-instance | 1.22 ms | 1.53 ms | 1.77 ms |
| cross-instance | 1.56 ms | 1.86 ms | 2.14 ms |

Redis hop ≈ **0.34 ms at p50**. Both include the Postgres commit. Corroborated
independently by `/metrics` (0.27–0.39 ms for the publish→remote-handling leg).

---

## Known limitations (all documented in README)

- **Backward typing interleaves** — RGA's real wart, asserted in a test rather
  than hidden. `"abc"` + `"xyz"` typed right-to-left → `"xaybzc"`. Converges, just
  not what either user meant. Fugue/YATA fix it.
- Tombstones and the op log **grow forever**; no GC.
- Rendering is **O(n)** per keystroke.
- **Presence is per-instance** (see loose ends).
- A **dropped Redis message is silently lost** — survivable only because Postgres
  has every op. Redis Streams + consumer groups would be the stricter design.
- `/metrics` uses **wall clocks on two machines**; trustworthy only on one host.
- **Lone UTF-16 surrogates can't be persisted** — needs an edit *inside* an emoji,
  which browsers don't allow, so unreachable by hand. Pinned by a test that uses it
  as a poison value. Fix: snap `diffText` to code-point boundaries.
- `schema.sql` is applied with `IF NOT EXISTS`, **no migrations**. Fine while the
  schema only grows.

---

## Environment gotchas (each of these cost time)

- **`uv run --directory server`** when launching uvicorn from the repo root, or
  `app` isn't importable — uvicorn resolves the module from cwd.
- **Starlette's `TestClient` *cancels* the server task** when a websocket context
  exits, so a handler's `finally` block never completes its `await`. Call
  `session.close()` explicitly to get a real `websocket.disconnect`. This is why
  `Tab.sync()` exists as a test barrier.
- **nginx needs the `map $http_upgrade $connection_upgrade` block and
  `proxy_http_version 1.1`** or the WebSocket handshake arrives stripped and fails
  as plain HTTP. Also `proxy_read_timeout 1h`, since an editor socket is idle
  whenever nobody types.
- **Round-robin is deliberate** in `deploy/nginx.conf`. `ip_hash` would make the
  Stage 3 demo pass without fixing anything.
- **Don't verify commits with `git stash` in a loop** — I made that mistake, left
  five stashes behind, and reverted a file. Use
  `git worktree add --detach` plus the existing venv's python
  (`server/.venv/bin/python -m pytest`) instead.
- Postgres is a **brew service**; Redis and nginx are started **ad-hoc** by
  `scripts/cluster.sh`, so nothing extra runs at login. Colima is the same —
  `colima start` by hand, nothing at login.
- **`~/.docker` held a dead Docker Desktop install** (`credsStore: "desktop"`,
  `currentContext: "desktop-linux"`, and 14 dangling plugin symlinks into a
  deleted `/Applications/Docker.app`). `credsStore` pointing at a missing
  binary breaks `docker pull` on its own. Cleaned up; the original is at
  `~/.docker/config.json.bak-pre-colima`. `cliPluginsExtraDirs` now points at
  `/opt/homebrew/lib/docker/cli-plugins` so `docker compose` and `docker buildx`
  resolve as subcommands.
- **A virtualenv can't be relocated between build stages.** It records its own
  path in `pyvenv.cfg` and in console-script shebangs, so the Dockerfile copies
  it to the same `/app/.venv` it was built at.
- **Use exec-form `CMD`.** Shell form makes `/bin/sh` PID 1, and sh doesn't
  forward SIGTERM, so `compose down` waits out the grace period and SIGKILLs —
  skipping the lifespan shutdown. `Started server process [1]` is the check.
- **`docker compose logs` shows nothing from the app unless logging is
  configured.** uvicorn configures its own loggers and leaves the root alone.

---

## How the collaboration has been working

Worth preserving, because it's produced good results:

- **Explain the approach and tradeoffs before writing code**, then build. The
  roadmap asks for this explicitly and it's caught real design questions (the
  algorithm choice in Stage 1, the log-vs-snapshot decision in Stage 2).
- **One stage at a time.** No scaffolding ahead.
- **`NOTES.md` gets updated while building**, not after — a short entry per file:
  what it does, 2–3 key decisions, the one main tradeoff.
- **Small, meaningful commits**, each green on its own, with the reasoning in the
  message body rather than the docs only.
- **Verify, don't assert.** Every claim in these docs came from a run: real browser
  tabs via Playwright, real `pkill` of the server mid-session, real measurements.
  Two independent methods where a number matters.
- **Test before pushing.** Ask first.
- **Limitations get filed, not hidden** — including ones found by accident, and
  including mistakes made along the way.
