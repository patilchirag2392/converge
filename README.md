# Converge

A real-time collaborative text editor, built in stages to learn distributed-systems
concepts from the inside out. The sync and conflict-resolution logic is hand-written —
no Yjs, no Automerge.

**Stack:** React + TypeScript (client) · FastAPI + Python (server) · WebSockets (transport)

Build plan and learning gates: [ROADMAP.md](./ROADMAP.md).

---

## Status

**Stage 0 — foundation, deliberately broken.** A single-instance server holds each
document in memory and resolves nothing: the newest write replaces the whole document.
Two tabs on the same doc will destroy each other's edits. That is the point — Stage 1
replaces this with a CRDT, and keeping the `stage-0` tag means the before/after is
demonstrable.

- [x] React/TS client with a shared textarea
- [x] FastAPI WebSocket server, single instance, document in memory
- [x] Last-write-wins: edits from any client overwrite the doc
- [x] Two browser tabs on the same doc
- [x] `.gitignore`, README, `stage-0` tag

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

Open <http://localhost:5173/?doc=demo>, then open the same URL in a second tab (there's
a button for it). The `doc` query param is the room — change it for a separate document.

Set `VITE_WS_URL` (see `client/.env.example`) if the server isn't on `localhost:8000`.

```bash
cd server && uv run pytest              # 19 tests
cd server && uv run python scripts/race_demo.py   # reproduce the data loss in the terminal
curl localhost:8000/api/doc/demo        # peek at server-side state
```

---

## Architecture

```
┌──────────── Tab A ────────────┐          ┌──────── FastAPI (1 process) ────────┐
│ React + TS                    │          │                                     │
│  <textarea>                   │  WS      │  Hub                                 │
│  useDocSocket()  ────────────────────────►   docs:  {doc_id -> Document}        │
│   • local state is authoritative         │            Document{text, rev}       │
│     while you type            │  JSON    │   conns: {doc_id -> {client_id: ws}} │
└───────────────────────────────┘◄─────────┤                                      │
┌──────────── Tab B ────────────┐          │  apply_edit() = self.text = text     │
│ same component, same doc_id   │◄────────►│              ^^^^^^^^^^^^^^^^^^^^    │
└───────────────────────────────┘          │              the entire "algorithm"  │
                                           └─────────────────────────────────────┘
```

Server state is two dicts in process memory (`server/app/hub.py`). No database, no
locks — one uvicorn worker means one event loop and one copy of the state, so every
edit is serialized for free. That free serialization is exactly the assumption two
instances behind a load balancer will break in Stage 3.

One WebSocket per tab: `ws://localhost:8000/ws/{doc_id}?client_id=...`. The doc id
forms the room; `client_id` is per-tab (sessionStorage) so the server can avoid echoing
your own edit back at you.

### Wire protocol

| Direction | Message | Meaning |
|---|---|---|
| S→C on connect | `{type:"snapshot", doc_id, text, rev, clients, client_id}` | full current doc |
| C→S per keystroke | `{type:"edit", text}` | "the doc is now this string" |
| S→C broadcast | `{type:"edit", text, rev, author}` | to everyone in the room *except* the author |
| S→C on join/leave | `{type:"presence", clients}` | room size |
| S→C on bad frame | `{type:"error", detail}` | frame ignored, socket stays open |

The unit of change is **the entire document**, not an operation. Nothing tells the
server which revision the client was editing, so it cannot detect a concurrent edit
even in principle — there is nothing to compare. `rev` is a monotonic counter for
display only; wiring it into a conflict check would be conflict resolution.

---

## What breaks, and why (Stage 0 learn-gate)

Type in both tabs at the same time. Three separate failures show up:

**1. Lost updates.** Both tabs hold `"hi"`. Both append a character and send the whole
document. The server applies them in arrival order, last one wins, and the loser's
keystroke is gone from every replica — it was never stored anywhere. Verified by
`test_concurrent_edits_lose_data`.

**2. Replicas diverge — and stay diverged.** The subtler failure, and the more
interesting one. Because the server never echoes an edit back to its author, each
client ends at *"my own last keystroke, unless a remote frame arrived after it"* —
and those orderings differ per client. Observed in a real browser:

```
tab A textarea : "BBBBBBBB"   rev 4
tab B textarea : "AAAAAAAA"   rev 3
server document: "BBBBBBBB"   rev 4
```

Tab B sits at older text and an older rev than the server, indefinitely, until someone
types again. A client can't fix this alone: it is never told the rev its own edit was
assigned, so it has no way to tell whether an incoming rev is newer or older than what
it's already showing. Watch the two `rev:` pills disagree — that's divergence on screen.
Verified by `test_clients_can_diverge_permanently`.

So the project's namesake guarantee fails here twice over: the replicas neither
preserve intent *nor* converge.

**3. The caret teleports.** Applying a remote edit replaces `textarea.value` wholesale,
which destroys the selection and snaps your cursor to the end mid-word. Measured in the
browser: caret at offset 2 → offset 14 after one remote edit. A consequence of shipping
whole documents rather than operations, and it disappears once edits are ops.

Also: bandwidth is O(document) per keystroke. Fine for a toy, absurd for a real doc.

Three things left out on purpose to keep the failure visible: no debounce on send
(debouncing shrinks the race window and hides the bug), no queue or retry for keystrokes
typed while disconnected (they're simply dropped), and no server-side echo suppression
beyond skipping the author.

### What would break if…

- **…you ran two server instances?** Each gets its own `Hub`, so clients on different
  instances never see each other at all. Stage 3.
- **…the process restarted?** Every document is gone. Stage 2.
- **…a client's socket dies mid-broadcast?** `Hub.broadcast` catches the failed send
  and evicts that connection, so one dead tab can't break the fan-out.
- **…the handler task is cancelled instead of disconnected?** The `finally` block's
  presence broadcast is skipped — an `await` in a cancelled task doesn't complete.
  State cleanup (`hub.leave`) is synchronous and still runs. This is also why
  `test_presence_drops_when_a_client_disconnects` calls `close()` explicitly:
  starlette's test client *cancels* the server task on context exit, where real uvicorn
  delivers a `websocket.disconnect`.
- **…one client is very slow?** `broadcast` gathers all sends concurrently, but still
  awaits the slowest before handling the next edit. A real system needs per-client
  outbound queues.

---

## Layout

```
server/
  app/hub.py            in-memory documents + connection rooms (the LWW "algorithm")
  app/main.py           FastAPI app, WebSocket endpoint, wire protocol
  scripts/race_demo.py  reproduce the lost update from the CLI
  tests/                19 tests, including the data loss as an executable claim
client/
  src/protocol.ts       message types, mirrors main.py
  src/useDocSocket.ts   the socket, reconnect, and local-vs-remote state
  src/App.tsx           editor, status pills, inbound-edit log
```
