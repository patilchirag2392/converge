#!/usr/bin/env bash
#
# Run a Converge cluster: two server instances behind nginx on :8080.
#
#   scripts/cluster.sh up        two instances + nginx  (+ Redis if REDIS_URL is set)
#   scripts/cluster.sh down      stop everything it started
#   scripts/cluster.sh status    what's listening, and which instance answers
#
# Stage 3 uses this twice: first without Redis to watch cross-instance edits
# fail, then with Redis to watch them work. Set REDIS_URL to enable pub/sub:
#
#   REDIS_URL=redis://localhost:6379 scripts/cluster.sh up
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/deploy/.run"
NGINX_PREFIX="$RUN/nginx"

INSTANCES=("a:8001" "b:8002")

ensure_dirs() {
    mkdir -p "$RUN" "$NGINX_PREFIX/logs"
    # nginx creates these lazily but errors if it cannot; make them up front.
    mkdir -p "$NGINX_PREFIX"/{client_body_temp,proxy_temp,fastcgi_temp,uwsgi_temp,scgi_temp}
}

wait_for() {
    local url="$1" name="$2" tries=60
    while ((tries--)); do
        curl -fsS -m 1 "$url" >/dev/null 2>&1 && return 0
        sleep 0.25
    done
    echo "  !! $name never came up — see $RUN/*.log" >&2
    return 1
}

up() {
    ensure_dirs

    if ! pg_isready -q; then
        echo "!! Postgres isn't accepting connections. brew services start postgresql@16" >&2
        exit 1
    fi

    if [[ -n "${REDIS_URL:-}" ]]; then
        if ! redis-cli -u "$REDIS_URL" ping >/dev/null 2>&1; then
            echo "starting redis..."
            redis-server --port 6379 --daemonize yes --save '' --appendonly no
            sleep 0.5
        fi
        redis-cli -u "$REDIS_URL" ping >/dev/null 2>&1 \
            && echo "redis:    $REDIS_URL (pub/sub ENABLED)" \
            || { echo "!! could not reach redis at $REDIS_URL" >&2; exit 1; }
    else
        echo "redis:    not configured (pub/sub DISABLED — cross-instance edits will not propagate)"
    fi

    for spec in "${INSTANCES[@]}"; do
        local name="${spec%%:*}" port="${spec##*:}"
        # --directory so `app` is importable; uvicorn resolves it from cwd.
        INSTANCE_ID="$name" nohup uv run --directory "$ROOT/server" \
            uvicorn app.main:app --host 127.0.0.1 --port "$port" \
            >"$RUN/instance-$name.log" 2>&1 &
        echo $! >"$RUN/instance-$name.pid"
    done

    for spec in "${INSTANCES[@]}"; do
        local name="${spec%%:*}" port="${spec##*:}"
        wait_for "http://127.0.0.1:$port/healthz" "instance $name" || exit 1
        echo "instance: $name on :$port"
    done

    nginx -p "$NGINX_PREFIX" -c "$ROOT/deploy/nginx.conf"
    wait_for "http://127.0.0.1:8080/healthz" "nginx" || exit 1

    echo
    echo "load balancer: http://localhost:8080"
    echo "client:        cd client && VITE_WS_URL=ws://localhost:8080 npm run dev"
    echo
    echo "which instance am I hitting?  (round-robin, so this alternates)"
    for _ in 1 2 3 4; do curl -s http://localhost:8080/healthz; echo; done
}

down() {
    nginx -p "$NGINX_PREFIX" -c "$ROOT/deploy/nginx.conf" -s quit 2>/dev/null \
        && echo "nginx stopped"
    for spec in "${INSTANCES[@]}"; do
        local name="${spec%%:*}"
        local pidfile="$RUN/instance-$name.pid"
        [[ -f "$pidfile" ]] || continue
        pkill -P "$(cat "$pidfile")" 2>/dev/null
        kill "$(cat "$pidfile")" 2>/dev/null && echo "instance $name stopped"
        rm -f "$pidfile"
    done
    # uv spawns uvicorn as a child, so catch strays by command line too.
    pkill -f "uvicorn app.main:app --host 127.0.0.1 --port 800" 2>/dev/null
    echo "(redis left running; stop with: redis-cli shutdown nosave)"
}

status() {
    for port in 8001 8002 8080; do
        printf "  :%s  " "$port"
        curl -fsS -m 1 "http://127.0.0.1:$port/healthz" 2>/dev/null || echo "not listening"
        echo
    done
    printf "  redis "
    redis-cli ping 2>/dev/null || echo "not running"
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    status) status ;;
    *) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ; exit 1 ;;
esac
