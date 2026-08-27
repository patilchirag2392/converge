# End-to-end checks (real browsers)

These drive actual Chromium tabs against a running stack. Every browser-level
claim in the README and NOTES.md came from one of these — the caret measurements,
the offline-typing survival, the cross-instance propagation.

Deliberately **not** part of `npm test`: they need a running server, a database,
and a real browser, so they're a manual verification tool rather than unit tests.

CI does **not** run these. It runs `scripts/multi_instance_demo.py` against the
compose stack instead — same claim (an edit crosses instances), asserted with a
WebSocket client rather than a browser, and no Playwright download on the runner.
Putting these in CI would need a browser image and would trade several minutes
per run for coverage of the React layer that vitest already covers. Worth
revisiting if a rendering bug ever ships.

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
| `two_instances_browser.mjs` | `scripts/cluster.sh up` with `REDIS_URL`, client on `VITE_WS_URL=ws://localhost:8080` — **or just `docker compose up`**, which serves the client on :5173 and the balancer on :8080 already | forces two tabs onto *different* instances, then checks edits cross both ways and all four replicas agree |

| `prod_edge_browser.mjs` | any **single-origin** deployment: the Stage 5 prod overlay, or the EC2 box | two tabs on different instances through one public port, and — uniquely — that the bundle's *baked-in* `VITE_WS_URL` is right |

```bash
node prod_edge_browser.mjs http://localhost:8081          # local prod overlay
node prod_edge_browser.mjs http://<ec2-public-dns>        # the deployed box
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
- `stage5-prod-edge.png` — the production topology: one public port, nginx
  serving the bundle *and* proxying the API, two tabs on different instances
- `stage4-containerized.png` — the same two-instance behaviour, but every part of
  the stack (client, balancer, both servers, Postgres, Redis) in a container.
  Taken from a `two_instances_browser.mjs` run against `docker compose up`.
  The `clients: 1` pill is the known per-instance presence bug, still visible.

Useful for a portfolio README later; harmless to delete if not.
