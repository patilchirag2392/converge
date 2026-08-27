"""Is a deployed Converge actually working? Checks only the public origin.

Written for Stage 5. The existing demo scripts assume they can reach the
instances directly on :8001/:8002, which is exactly what production closes off
-- so this asserts the same properties through the one public port a browser
has, and nothing else.

    # against a local prod-overlay stack
    uv run python scripts/verify_deployment.py http://localhost:8081

    # against the EC2 box
    uv run python scripts/verify_deployment.py http://<public-dns>

Exit status is the point: 0 if every check passed, 1 otherwise, so it can gate
a deploy. Each check prints what it proves, because a green tick nobody
understands is not evidence.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from urllib.parse import urlparse

import websockets

TIMEOUT = 10.0
PROPAGATION_TIMEOUT = 10.0
"""Generous on purpose: this may be crossing the public internet to another
continent, not a loopback interface. A slow pass is still a pass; the latency
number is reported separately rather than being an assertion."""

HEALTH_PROBES = 10
"""Round-robin needs enough samples to see both instances. With 2 backends,
10 probes miss one only if the balancer is genuinely not distributing."""


class Report:
    """Collects results so one failure doesn't hide the rest."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}")
        if detail:
            for line in detail.splitlines():
                print(f"         {line}")
        if not ok:
            self.failures.append(name)
        return ok

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"         {text}")


async def fetch_json(session: object, url: str) -> dict:
    """Plain stdlib HTTP in a thread -- avoids adding an async HTTP client to
    the runtime dependencies just for this script."""
    import urllib.request

    def get() -> dict:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return json.load(response)

    return await asyncio.to_thread(get)


def ws_url_for(base: str, doc_id: str, client_id: str) -> str:
    """http -> ws, https -> wss, preserving host and port.

    The scheme mapping is the thing people get wrong when they add TLS: a
    browser refuses ws:// from an https:// page as mixed content, so an
    https base URL *must* produce wss://.
    """
    parsed = urlparse(base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws/{doc_id}?client_id={client_id}"


async def snapshot_of(socket: object) -> dict:
    while True:
        message = json.loads(await socket.recv())
        if message["type"] == "snapshot":
            return message


async def main(base: str) -> int:
    base = base.rstrip("/")
    doc_id = f"verify-{int(time.time())}"
    report = Report()

    print(f"\nVerifying {base}")
    print(f"document: {doc_id}\n")

    # --- 1. Is the frontend being served at all? ---------------------------
    print("1. the public origin serves the client bundle")
    import urllib.request

    def get_index() -> tuple[int, str, int]:
        request = urllib.request.Request(base + "/")
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            return response.status, response.headers.get("content-type", ""), len(body)

    try:
        status, content_type, size = await asyncio.to_thread(get_index)
        report.check(
            status == 200 and "html" in content_type,
            "GET / returns HTML",
            f"HTTP {status}, {content_type}, {size} bytes",
        )
    except Exception as exc:
        report.check(False, "GET / returns HTML", f"{type(exc).__name__}: {exc}")

    # --- 2. Does the API answer on the same origin? ------------------------
    print("\n2. the API answers on that same origin (so: no CORS, one open port)")
    instances: list[str] = []
    try:
        for _ in range(HEALTH_PROBES):
            health = await fetch_json(None, f"{base}/healthz")
            instances.append(health["instance"])
        distinct = sorted(set(instances))
        report.check(
            len(distinct) >= 2,
            "the load balancer reaches more than one instance",
            f"{HEALTH_PROBES} probes hit: {', '.join(distinct)}\n"
            f"sequence: {' '.join(instances)}",
        )
    except Exception as exc:
        report.check(False, "GET /healthz", f"{type(exc).__name__}: {exc}")
        distinct = []

    # --- 3. The actual claim: do two clients on different instances sync? --
    print("\n3. two clients land on different instances and see each other's edits")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    from app.crdt import RGA  # noqa: E402  -- after sys.path is set

    kept: dict[str, tuple[object, RGA]] = {}
    sockets: list[object] = []
    try:
        for attempt in range(12):
            socket = await websockets.connect(
                ws_url_for(base, doc_id, f"v{attempt}"), open_timeout=TIMEOUT
            )
            snapshot = await asyncio.wait_for(snapshot_of(socket), timeout=TIMEOUT)
            instance = snapshot["instance"]
            if instance in kept:
                await socket.close()
                continue
            replica = RGA(f"v{attempt}")
            for op in snapshot["ops"]:
                replica.apply(op)
            kept[instance] = (socket, replica)
            sockets.append(socket)
            if len(kept) == 2:
                break

        if not report.check(
            len(kept) == 2,
            "landed two WebSocket clients on two different instances",
            f"instances: {', '.join(sorted(kept))}",
        ):
            raise RuntimeError("cannot test propagation without two instances")

        (alice_instance, (alice, alice_crdt)), (bob_instance, (bob, bob_crdt)) = (
            kept.items()
        )
        text = "hello from the internet"
        started = time.monotonic()
        await alice.send(json.dumps({"type": "ops", "ops": alice_crdt.local_insert(0, text)}))

        elapsed: float | None = None
        deadline = started + PROPAGATION_TIMEOUT
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(bob.recv(), timeout=deadline - time.monotonic())
            except (TimeoutError, asyncio.TimeoutError):
                break
            message = json.loads(raw)
            if message["type"] == "error":
                report.note(f"server replied with an error frame: {message}")
                break
            if message["type"] == "ops":
                for op in message["ops"]:
                    bob_crdt.apply(op)
                elapsed = time.monotonic() - started
                break

        report.check(
            elapsed is not None and bob_crdt.text == text,
            f"an edit on instance {alice_instance!r} reached instance {bob_instance!r}",
            (
                f"propagated in {elapsed * 1000:.0f} ms (includes the round trip "
                f"from wherever this script is running)\n"
                f"instance {alice_instance!r} sees: {alice_crdt.text!r}\n"
                f"instance {bob_instance!r} sees: {bob_crdt.text!r}"
                if elapsed is not None
                else f"nothing arrived within {PROPAGATION_TIMEOUT:.0f}s\n"
                f"instance {bob_instance!r} sees: {bob_crdt.text!r}"
            ),
        )
    except Exception as exc:
        report.check(False, "cross-instance propagation", f"{type(exc).__name__}: {exc}")
    finally:
        for socket in sockets:
            try:
                await socket.close()
            except Exception:
                pass

    # --- 4. Did it survive the socket closing -- i.e. is it persisted? -----
    print("\n4. the edit is durable, not just in memory")
    try:
        state = await fetch_json(None, f"{base}/api/doc/{doc_id}")
        report.check(
            state["text"] == "hello from the internet",
            "GET /api/doc returns the text after both sockets closed",
            f"served by instance {state['instance']!r}: {state['text']!r}\n"
            "read back from Postgres, since detaching evicts the cached replica",
        )
    except Exception as exc:
        report.check(False, "GET /api/doc", f"{type(exc).__name__}: {exc}")

    # --- summary ----------------------------------------------------------
    print()
    if report.failures:
        print(f"FAILED -- {len(report.failures)} check(s): {', '.join(report.failures)}")
        print("\n`docker compose logs` on the box is the next place to look.")
        return 1
    print("All checks passed. The deployment is live and syncing across instances.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
