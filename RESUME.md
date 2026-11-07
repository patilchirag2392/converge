# Resume here

Session handoff for Converge. Written 2026-08-25, last updated 2026-08-27 at the
end of Stage 5. Paste this file (or point at it) to bring a fresh session up to
speed.

Companion docs: [ROADMAP.md](./ROADMAP.md) is the plan, [NOTES.md](./NOTES.md) is
the per-file decision trail for Stages 2–5, [README.md](./README.md) is how it all
works, [deploy/aws/README.md](./deploy/aws/README.md) is the AWS runbook.

---

## Read this first if you just rebooted

**Everything is committed and pushed.** `stage-5-aws` on GitHub is the current
tip; nothing of value lives only on disk. Local temp files from the last session
(`/tmp`, the scratchpad) are gone and none of them mattered — the one script
worth keeping was moved into `e2e/prod_edge_browser.mjs` and committed.

Three things do **not** survive a reboot and need starting by hand:

```bash
# 1. Docker. Colima is deliberately not registered at login.
colima start                       # ~1 min; images and volumes persist in the VM

# 2. Postgres is a brew service and comes back on its own. Verify:
pg_isready

# 3. Redis on the host is NOT running and does not need to be. It is only for
#    `scripts/cluster.sh` and the 7 Redis integration tests. Start it only if
#    you want those to run instead of skip:
redis-server --port 6379 --daemonize yes --save '' --appendonly no
```

Then confirm you're where you think you are:

```bash
cd ~/Developer/converge
git log --oneline -1               # should be the docs commit for Stage 5
git status                         # should be clean
cd server && uv run pytest -q      # 103 passed (96 + 7 skipped without Redis)
cd ../client && npm test           # 50 passed
```

**Where the last session stopped:** **Stage 5 is live on AWS** and the project is
wrapped up. The EC2 box is normally **stopped** between demos (only its disk
bills, ~$1.60/month). All stages are merged to `main` and tagged `stage-0` to
`stage-5`.

---

## Demoing it

**Live on AWS** — from the repo on your laptop, no SSH needed:

```bash
./deploy/aws/start.sh      # ~2-3 min; waits for /healthz, prints the new URL
./deploy/aws/stop.sh       # when done; the disk and its data stay
```

Open the printed URL in two **new** windows (Cmd+N, type it). Never duplicate a
tab: the client id lives in `sessionStorage`, which a duplicated tab copies.
The hostname changes on every start; nothing needs updating for that.

SSH is only needed to change something on the box, and only works from the IP
in the security group — see the runbook §10 if it times out.

**Locally, the before/after story** (Stage 0 clobbering, then the fix across two
instances): the "Getting running again" section below. Never run the Stage 0
dev server and the compose stack together — both want port 5173.

**Gone for good?** `./deploy/aws/teardown.sh --yes` gets the bill to $0 but
deletes the data, and rebuilding is the full runbook again (~20-30 min).

---

## Where things stand

**All five stages are built, green in CI, merged to `main` and tagged. Stage 5
is live on AWS.**

| Stage | What | State |
|---|---|---|
| 0 | last-write-wins, edits clobber | done, tagged `stage-0` |
| 1 | hand-written RGA CRDT | done, tagged `stage-1` |
| 2 | Postgres persistence (op log) | done, tagged `stage-2` |
| 3 | Redis pub/sub across 2 instances | done, tagged `stage-3` |
| 4 | Docker + GitHub Actions | done, **CI green**, tagged `stage-4` |
| 5 | Deploy to AWS | **live on EC2**, tagged `stage-5` |

Tests: **103 server** (`cd server && uv run pytest`), **50 client**
(`cd client && npm test`). Every commit on `stage-3-redis` passes independently —
verified in a throwaway worktree, not assumed.

### Git topology — read this before doing anything

```
main              (tip)     everything, Stages 0-5             [pushed]
stage-5-aws       (tip)     same commit as main                [pushed]
stage-1-crdt      f433c0a   Stage 1                            [pushed, = tag stage-1]
stage-2-postgres  f0a0955   Stage 1 + 2                        [pushed, = tag stage-2]
stage-3-redis     cf6a626   Stage 1 + 2 + 3                    [pushed, = tag stage-3]
stage-4-docker-ci bbbbfc6   Stage 1 + 2 + 3 + 4                [pushed, = tag stage-4]
tags              stage-0 … stage-5                            [pushed]
```

Each stage branch builds on the previous one, so `main` was fast-forwarded —
no merge commits, history unchanged. The tags are the way to jump to any stage
(`git checkout stage-0` for the "before" demo).

**CI is green on `stage-4-docker-ci`** (run 33083297367): 103 server tests with
**zero skips**, 43 client tests, all three images built, the stack came up and an
edit crossed instances in 8.1 ms. One fix was needed after the first push —
`astral-sh/setup-uv@v10` does not resolve, because that action publishes moving
major aliases only up to v7; it is pinned to `v10.0.1` now.

**`stage-5-aws` is pushed and CI is green on it too** (run 33103039392): 103
server tests with zero skips, 43 client, all three images built, stack up, edit
crossed instances in 9.8 ms. So `bootstrap.sh` can clone it and the deploy is
unblocked.
Latest green run, after the deploy fixes: 42329559401 (103 server, 50 client,
stack up).

**Merged and tagged without the learn-gates.** The roadmap gates each merge on
explaining the stage without looking. That was not done here; the decision was
to catch up in one go and do the understanding separately — by rebuilding the
project from scratch in a new repo, which is the learn-gate done properly.

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

### The production topology, locally

The Stage 5 overlay, which is worth running before deploying because it is the
same thing the box runs — one open port, nginx serving the bundle *and* proxying
the API.

```bash
cd ~/Developer/converge
export PUBLIC_PORT=8081 POSTGRES_PASSWORD=whatever-you-like
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --wait

# everything on ONE port: the app, the API, the socket
open http://localhost:8081/?doc=demo
cd server && uv run python scripts/verify_deployment.py http://localhost:8081
cd ../e2e && node prod_edge_browser.mjs http://localhost:8081

docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```

**Gotcha, hit for real:** if the `pgdata` volume already exists from a
non-overlay run, the prod overlay's different `POSTGRES_PASSWORD` produces
`InvalidPasswordError` and the servers never go healthy — Postgres only runs
`initdb` (which creates the role) on a **first, empty** volume. `down -v` first,
or reuse the old password. Documented in the runbook's troubleshooting.

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

1. **No learn-gate done for Stages 1–5.** Merged and tagged anyway (see Git
   topology). The questions are in the README under *Learn-gates*.
2. **`verify_deployment.py` has not been run against the live URL.** The
   deployment was confirmed by hand — two browser windows syncing at the
   public address, and again after a stop/start at the new hostname — but the
   scripted four-check acceptance test only ran against the local prod
   overlay. With the box started: `cd server && uv run python
   scripts/verify_deployment.py "http://<hostname from start.sh>"`.
3. **Presence is per-instance** (found at the end of Stage 3). The `clients: n`
   pill counts only sockets on the instance you're connected to, so two tabs on
   different instances each show `clients: 1`. Visibly wrong. Needs presence in
   Redis with its own expiry/liveness design.
4. **Duplicated tabs share a client id.** `sessionStorage` is copied into a
   duplicated tab, so both windows become the same CRDT replica and can mint
   colliding character ids. Demos avoid it by opening fresh windows; the fix is
   an id per page load (in memory, not `sessionStorage`).
5. **No HTTPS.** Needs a domain (Let's Encrypt won't issue for
   `*.amazonaws.com`) and a certificate on the edge nginx; runbook §11. The
   client already picks `wss://` on an https page.
6. **A Redis restart reports a false error** (found during Stage 4, filed not
   fixed). The first edit after Redis is replaced gets
   `"could not persist operations"` — which is untrue: the ops *are* in Postgres
   and local clients *did* see them; only the cross-instance publish failed. Two
   defects: no publish retry on the bus, and one exception handler covering two
   failure modes with opposite consequences. Deterministic repro and full
   diagnosis in NOTES.md. Doesn't affect a clean-state `compose up`.
7. ~~`VITE_WS_URL` baked into the client image~~ **Fixed after the first real
   deploy.** Production now builds with `VITE_WS_URL=same-origin`, and the
   client derives the socket URL from the page's own address (`wss://` on
   https). A stop/start's new hostname needs no `.env` edit and no rebuild;
   `deploy/aws/stop.sh` / `start.sh` pause and resume the box from the laptop.
   Simpler than the `config.js` entrypoint once planned, because nginx already
   serves page and socket on one origin. Dev and the base compose stack still
   take an explicit URL, since there the socket is on a different port.
8. **`scripts/cluster.sh` survives on purpose.** compose replaced it for
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

**Stage 5 — one EC2 box, and the ports closed twice.** The security group is one
layer; `docker-compose.prod.yml` not publishing the ports is the other. Both,
because a published container port doesn't respect the host firewall the way
you'd expect — Docker writes its own iptables DNAT rules — so "the security
group will catch it" is a habit worth not forming. 443 is deliberately **closed**:
nothing is listening on it, and an open port with no service is surface with no
function.

**Stage 5 — compose MERGES `ports` across `-f` files.** An overlay that just
doesn't mention `ports` leaves Postgres published to the internet. It takes
`!reset []`, or `!override [...]` where a service both drops the inherited value
and adds one. Needs Compose v2.24+, which `bootstrap.sh` checks rather than
assumes.

**Stage 5 — no RDS, knowingly.** The data lives in a Docker volume on the
instance's root EBS volume, so terminate destroys it (stop doesn't). What that
gives up: backups/PITR, a data lifecycle independent of compute, multi-AZ
failover. Why it's still right: the failure this project explores is CRDT
convergence, which one Postgres exercises identically, and RDS would more than
double the bill. The line that matters: *with real users, RDS would come before
any further scaling work.*

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
- **Compose MERGES `ports` across `-f` files.** An overlay that omits `ports`
  inherits the base file's — which in production means Postgres published to the
  internet. Needs `!reset []`, or `!override [...]` where a service both drops
  the inherited value and adds one. Always read `compose config` rather than
  trusting the overlay. Requires Compose v2.24+.
- **Postgres `initdb` runs only on a first, empty volume.** Changing
  `POSTGRES_PASSWORD` later changes what the servers send, not what the database
  expects, and you get `InvalidPasswordError` with no obvious cause.
- **A `HEALTHCHECK` cannot reference a build `ARG`.** It has to be re-exported as
  an `ENV` (the shell form expands env vars at run time). Otherwise the prod lb
  image probes the wrong port, never goes healthy, and deadlocks behind its own
  `depends_on` — which presents as a hang, not an error.
- **`.gitignore` negations need to cover every template.** `.env.*` was silently
  ignoring `deploy/aws/.env.production.example`, which `bootstrap.sh` copies on
  the box — so the deploy would have failed with "no such file", a long way from
  the cause. `git check-ignore -v <path>` is the check.
- **GitHub Actions major-version aliases are not guaranteed.** `setup-uv`
  publishes moving tags only up to `v7`, so `@v10` does not resolve even though
  `v10.0.1` is current. Checking the latest *release* is not the same as checking
  the alias exists. Pin exact releases in CI.
- **`crypto.randomUUID()` does not exist on a plain-HTTP public origin.** Browsers
  only expose it in a secure context (HTTPS or `localhost`). Every local check ran
  on `localhost`, so the first real EC2 deploy rendered a blank page
  (`crypto.randomUUID is not a function`). Client ids now come from
  `crypto.getRandomValues`. Anything browser-side has to be checked on a
  non-localhost HTTP origin before it ships here.

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

---

## Session log — Stages 4 and 5 (2026-08-25 → 27)

Kept because the *findings* matter more than the diffs, and a fresh session can't
reconstruct them from the code.

**Stage 4 — Docker + CI.** Built, verified, pushed, CI green, tagged `stage-4`.

- Docker wasn't installed. Chose **Colima** (CLI-only Linux VM) over Docker
  Desktop: no GUI, no licence question, nothing at login. Also had to clean a
  dead Docker Desktop install out of `~/.docker` — `credsStore: "desktop"` with
  no such binary breaks `docker pull` on its own, and 14 plugin symlinks pointed
  into a deleted `/Applications/Docker.app`. Backup at
  `~/.docker/config.json.bak-pre-colima`.
- **The multi-stage argument is not `--no-dev`.** A single-stage image that
  genuinely `rm`s `uv` and the wheel cache is still 51 MB bigger, because layers
  are additive. Measured: server 287 vs 419 (naive) vs 338 (careful); client 76
  vs 502.
- **`depends_on` does not wait for Postgres to accept connections**, and
  `service_healthy` still isn't sufficient — a socket-based `pg_isready` reports
  READY during `initdb`. Hence healthchecks (TCP-forced) *and* app-level retry.
- **CI failed on the first push**: `astral-sh/setup-uv@v10` doesn't resolve.
  Pinned to `v10.0.1`. Second run green with substance — 103 tests, **zero
  skips**, propagation asserted in CI.
- **Found a real bug, filed rather than fixed.** The first edit after Redis is
  replaced is reported to the client as `"could not persist operations"` — which
  is false; the ops are durable and local clients saw them, only the
  cross-instance publish failed. My first hypothesis (a subscribe-confirmation
  race) was **wrong** — tested it, 0/300 misses. Deterministic repro and the real
  diagnosis are in NOTES.md. Still open, item 6 in loose ends.

**Stage 5 — AWS config.** Written, locally verified, pushed, CI green. **Nothing
provisioned.**

- **The ports-merge trap**, caught by reading `compose config` before writing the
  overlay: a production overlay that just omits `ports` leaves Postgres published
  to the internet. Fixed with `!reset`/`!override`; effective published ports on
  the whole stack is now exactly **1**.
- **443 is deliberately closed.** Nothing listens on it; an open port with no
  service is surface with no function. Opens in the same change as a certificate.
- **Single-box durability**: stop is safe, terminate destroys the data
  (`DeleteOnTermination=true`). RDS would buy a data lifecycle independent of
  compute — the honest line is *with real users, RDS comes before more scaling
  work*.
- **Verified locally**: prod overlay healthy in 20 s, both required-variable
  guards firing, `verify_deployment.py` 4/4 with 5 ms propagation, two real
  browser tabs on different instances through the single public port, and
  Docker's apt repo confirmed on `ubuntu:24.04`/`linux/amd64`.
- **Not verified**: every `aws` command, the AMI SSM path, free-tier status,
  current prices. Said so in the runbook's opening paragraph rather than implying
  otherwise.

---

## Session log — the first real deploy (2026-11)

The runbook was run against a real account for the first time. What it found:

- **A stale `~/.ssh/converge-key.pem`** from an earlier attempt made the
  key-pair redirect fail with zsh `permission denied` (mode 400). The `aws` call
  never ran, so nothing was half-created.
- **SSH timed out** because the source IP no longer matched the port-22 rule.
  Fixed by replacing the rule; the runbook §10 has the commands.
- **Blank page** — `crypto.randomUUID is not a function`. It only exists in a
  secure context; the EC2 page is plain HTTP. Every earlier check ran on
  localhost. Fixed with `crypto.getRandomValues`.
- **Restarts were a chore**: each stop/start gave a new hostname, baked into the
  bundle, so SSH + `.env` edit + rebuild. Fixed by building production with
  `VITE_WS_URL=same-origin`; added `start.sh`/`stop.sh`.
- **The `t3.micro` survived the client build** with the 2 GiB of swap
  `bootstrap.sh` adds — one of the two "expect friction" unknowns, resolved.
- **CI flaked once** on the stack job: Docker could not bind 55432 on the
  runner (`address already in use`). A re-run passed. 55432/56379 sit inside
  Linux's ephemeral port range, so it can recur; moving them below 32768 would
  end it.
- **Locally**, the demo hit the `InvalidPasswordError` trap again (an old
  `pgdata` volume from a prod-overlay run) — `down -v` fixed it.

Then: `main` fast-forwarded to the tip, tags `stage-1`, `stage-2`, `stage-3` and
`stage-5` added. The project is wrapped up here; the learning continues in a
from-scratch rebuild.

