# Converge — Build & Learning Roadmap

A real-time collaborative editor (Google-Docs/Figma-style). Named for the core guarantee
you're implementing: CRDTs *converge* — every replica settles to identical state.

The point isn't just to finish it — it's to understand every layer well enough to defend
it in an interview. **Rule: don't check off a stage until you can explain it back without looking.**

Stack baseline: **React + TypeScript** (client), **FastAPI / Python** (server),
**WebSockets** (transport). New skills get added stage by stage below.

---

## The one rule that makes this project impressive

The sync/conflict-resolution logic must be **yours**. The moment a library (Yjs,
Convex reactive sync, Automerge) does the merging, this collapses into "an app that
wraps a library" — the same critique as an LLM-wrapper project. Your own CRDT = standout.
Someone else's = filler.

---

## Git / GitHub workflow (do this from Stage 0)

One branch per stage, merged when the stage's learn-gate passes. Your commit history
then reads like this roadmap — which is exactly what a reviewer browsing your repo wants.

- **Init now:** `git init`, create the repo on GitHub, first commit = the Stage 0 scaffold.
- **Branch per stage:** `git checkout -b stage-1-crdt`, work, then merge to `main` (or open
  a PR to yourself — PRs on your own repo look deliberate and let you write up what each stage did).
- **Commit in small, meaningful chunks** — not one giant "done stage 3" commit. Each commit
  should be one coherent change with a clear message. This is the habit real teams expect,
  and commit count is a defensible resume metric if you ever want it.
- **Tag each finished stage:** `git tag stage-0`, `git tag stage-1` … easy to jump back and
  demo the "broken" last-write-wins version vs. the CRDT version in an interview.
- **README from day one:** short description, the stack, and a line on what works so far.
  Update it as you go. A live, evolving README is a signal by itself.
- **`.gitignore`** for `node_modules`, `__pycache__`, `.env`, build artifacts — get this
  right at Stage 0 so you never commit junk or secrets.

> Interview tie-in: keeping the Stage 0 tag means you can literally show the moment the
> problem appears and the moment your CRDT fixes it. That before/after is your best story.

---

## Stages

### Stage 0 — Foundation (existing skills, deliberately)
- [ ] React/TS client with a shared text area
- [ ] FastAPI WebSocket server, single instance, document held in memory
- [ ] Last-write-wins: edits from any client overwrite the doc
- [ ] Open two browser tabs on the same doc
- [ ] `git init` + push to GitHub + `.gitignore` + short README; tag `stage-0`

**Learn-gate:** Type in both tabs at once. Watch edits clobber each other.
Be able to say *why* it breaks (concurrent writes race, last one wins, edits get lost).

**New resume skill:** none — this is the familiar base so the new work stands out.

---

### Stage 1 — The CRDT (hard concept, not a new tool)
- [ ] Implement your own sequence CRDT so concurrent edits merge cleanly
- [ ] Each character gets a unique, ordered ID
- [ ] Concurrent inserts resolve deterministically on every client

**Learn-gate:** Explain (a) why last-write-wins fails, (b) why every character needs a
unique ID, (c) how two clients inserting at the same position end up in the same order.

**New resume skill:** CRDTs / conflict resolution / distributed data structures

> Learning tip: build Stage 0 → feel the problem → build Stage 1 → feel the fix.
> That before/after *is* your best interview answer. Don't skip the pain of Stage 0.

---

### Stage 2 — Persistence with PostgreSQL (NEW: real SQL)
- [ ] Replace in-memory state with Postgres so docs survive a restart
- [ ] Design the schema yourself; write actual SQL (not just an ORM black box)
- [ ] Load a doc's state on connect, persist on change

**Learn-gate:** Explain your schema and why you store what you store (and in what shape).

**New resume skill:** PostgreSQL / SQL

> Skip Convex here. Its built-in reactive sync would hide the exact logic that makes
> this project yours. Postgres = plain persistence, sync stays your code.

---

### Stage 3 — Horizontal scaling with Redis pub/sub (NEW — best interview story)
- [ ] Run two server instances behind an Nginx load balancer
- [ ] Observe the break: clients on different instances can't see each other's edits
- [ ] Add Redis pub/sub so edits broadcast across all instances
- [ ] (Bonus) Measure real edit-propagation latency across instances — a defensible number

**Learn-gate:** Explain why two instances break sync, and how pub/sub fixes it.

**New resume skills:** Redis, load balancing, Nginx, horizontal scaling

> **This is a natural strong stopping point.** Stop here and you already have a killer
> project. Stages 4–5 add trending ops skills — do them for the resume breadth.

---

### Stage 4 — Containerize + CI/CD (NEW: Docker, GitHub Actions)
- [x] Dockerfile per service
- [x] docker-compose for one-command local spin-up
- [x] GitHub Actions: run tests + build on every push

**Learn-gate:** Explain what your Dockerfile does line by line, and what your pipeline runs.

**New resume skills:** Docker, CI/CD

---

### Stage 5 — Deploy to cloud (NEW: AWS)
- [ ] Deploy to AWS — start simple (EC2 + docker-compose)
- [ ] (Upgrade) Move to ECS for the stronger resume line
- [ ] Publicly reachable, shareable URL

**Learn-gate:** Trace a request from browser → load balancer → container → and back.

**New resume skill:** AWS / cloud deployment

---

## Deliberately NOT in scope (and why — this is good interview signal)

- **Kubernetes** — a bazooka on a fly for a two-instance app. Mostly buzzword value,
  steep cliff. "I chose not to add K8s because the scale didn't justify it" is *stronger*
  signal than a shallow K8s line.
- **Go/Java backend rewrite** — real value, but a new language on top of a new CRDT on
  top of new infra is too much at once. Save it as the *next* project.
- **Rich-text (tables, images, formatting)** — UI busywork, zero systems signal.

---

## How to use Claude Code so you actually learn (not just autopilot)

You want to answer for this in interviews, so drive the tool like a tutor, not a vending machine:

- **Build one stage at a time.** Don't let it scaffold all five stages at once.
- **Before writing code each stage, ask it to explain the approach and the tradeoffs**
  — then have it build.
- **After each stage, close the file and explain it back out loud.** If you can't, you
  haven't earned the checkbox. Ask Claude Code to quiz you on the learn-gate questions.
- **Interrogate the CRDT hardest.** Frontend you can move fast on; the CRDT you learn
  line by line — what each field is, why the ordering works, what happens on concurrent edits.
- **Ask "what would break if…"** for each layer. That's the question an interviewer asks.

---

## Honest resume bullets this produces (no invented metrics)

- Built a real-time collaborative editor supporting concurrent multi-user editing,
  implementing a sequence CRDT for conflict-free merging of simultaneous edits
- Scaled the WebSocket backend across multiple instances using Redis pub/sub and an
  Nginx load balancer for cross-instance edit propagation
- Containerized the stack with Docker and built a GitHub Actions CI pipeline running
  automated tests on each push
- Deployed the system to AWS (EC2/ECS)

> Defend-it rule: a skill goes on the resume only once you've used it deeply enough to
> explain it. Go deep on a few, don't sprinkle ten shallow buzzwords.
