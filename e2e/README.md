# End-to-end checks (real browsers)

These drive actual Chromium tabs against a running stack. Every browser-level
claim in the README and NOTES.md came from one of these — the caret measurements,
the offline-typing survival, the cross-instance propagation.

Deliberately **not** part of `npm test`: they need a running server, a database,
and a real browser, so they're a manual verification tool rather than unit tests.
Stage 4's CI could run them in a container if that seems worth it.

## Setup

Playwright browsers are already cached on this machine. Only the driver is needed:

```bash
cd e2e
npm init -y && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm i playwright-core
```

The Chromium path defaults to this machine's Playwright cache; override with
`CHROME_PATH=/path/to/chrome` elsewhere (a normal Chrome install works).

## The scripts

| script | needs | proves |
|---|---|---|
| `two_tabs_stage1.mjs` | single instance on :8000, client on :5173 | concurrent edits merge; caret holds at 2 on a remote append and shifts 2→5 when the insert is before it |
| `pull_the_plug.mjs` | single instance + Postgres | kills the server mid-session, types while it's down (`queued: n`), restarts, and confirms the offline keystrokes reach Postgres and survive a second restart |
| `two_instances_browser.mjs` | `scripts/cluster.sh up` with `REDIS_URL`, client on `VITE_WS_URL=ws://localhost:8080` | forces two tabs onto *different* instances, then checks edits cross both ways and all four replicas agree |

```bash
node two_tabs_stage1.mjs
node pull_the_plug.mjs            # restarts the server itself, via pkill + spawn
node two_instances_browser.mjs
```

Each prints a readable report and writes a screenshot. `pull_the_plug.mjs` starts
and stops uvicorn on its own, so don't run it against a server you care about.

## screenshots/

One per stage, kept as visual evidence of the progression:

- `stage0-tab-a.png` — last-write-wins, inbound edits replacing the whole document
- `stage1-tab-a.png` — CRDT merging, cursor preserved
- `stage2-after-restart.png` — text recovered from Postgres after a restart
- `stage3-tab-a.png` — the `instance:` pill, showing which process the tab hit

Useful for a portfolio README later; harmless to delete if not.
