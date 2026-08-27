# syntax=docker/dockerfile:1
#
# Converge server -- FastAPI + uvicorn, two-stage build.
#
#   builder   has uv, resolves the lock file, creates /app/.venv
#   runtime   has the venv and the source, and nothing else
#
# The split exists because *installing* dependencies needs tooling that
# *running* them does not: uv itself (~35 MB), the wheel cache, and a C
# toolchain for any package without a prebuilt wheel. None of that is a runtime
# dependency, and every byte of it is extra attack surface in a shipped image.
# See NOTES.md for the size numbers.

# 3.11 on purpose: it is the version the local .venv and uv.lock were resolved
# against, so dev, CI and the image all run the same interpreter. A newer base
# would be a different resolution than the one the tests passed on.
ARG PYTHON_VERSION=3.11
# Pinned to the version used locally (`uv --version`), so the resolver that
# reads uv.lock here is the one that wrote it.
ARG UV_VERSION=0.12.3

# uv comes from its own published image rather than `pip install uv`: it is a
# single static binary, so getting it is a file copy rather than a dependency
# resolution that could itself drift. Naming it as a stage (rather than
# inlining the ref in COPY --from) is what lets the ARG above apply.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------- builder ----
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    # Precompile .pyc at build time. Costs a few seconds here and saves the
    # compile on every cold start -- worth it because the runtime user cannot
    # write to the venv to cache them itself.
    UV_COMPILE_BYTECODE=1 \
    # The cache mount below is a different filesystem from the venv, and uv's
    # default hardlink strategy cannot span filesystems. Without this it falls
    # back with a warning on every build.
    UV_LINK_MODE=copy \
    # Never silently download a second interpreter: use the one in this image,
    # or fail loudly. Otherwise a mismatched pin here would produce an image
    # whose python is not the base image's python.
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Only the lock file and manifest, deliberately *before* the source. Docker
# caches per layer, so editing app/main.py reuses this layer and skips the
# install entirely; touching pyproject.toml is what invalidates it.
COPY pyproject.toml uv.lock ./

# --frozen: install exactly the lock file, and fail if it disagrees with
#   pyproject.toml. Never re-resolve during a build -- a build that can pick new
#   versions is a build that can break without a code change.
# --no-dev: pytest, httpx and friends stay out of the image. They are CI's job.
#
# The cache mount keeps uv's wheel cache across builds without landing in a
# layer, so a rebuild is fast but the image does not carry the cache.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------- runtime ----
FROM python:${PYTHON_VERSION}-slim AS runtime

# Activating the venv by PATH rather than sourcing an activate script: there is
# no shell in the CMD (see the exec-form CMD below), so `activate` would never
# run. Prepending its bin/ is what actually makes `uvicorn` resolve.
ENV PATH="/app/.venv/bin:$PATH" \
    # Logs must reach `docker compose logs` immediately. Without this, stdout is
    # block-buffered when not a tty and a crash can lose the traceback that
    # explains it.
    PYTHONUNBUFFERED=1 \
    # The bytecode is already compiled (UV_COMPILE_BYTECODE above) and the
    # runtime user cannot write to the venv anyway, so writing .pyc would only
    # produce failed-write noise.
    PYTHONDONTWRITEBYTECODE=1

# Non-root. A container escape starts from whatever the process runs as, and
# nothing here needs root: the port is 8000 (>1024, no privileged bind) and the
# app writes no files. Fixed uid so a bind-mounted volume has predictable
# ownership rather than depending on image build order.
RUN useradd --create-home --uid 10001 converge

WORKDIR /app

# The venv is copied to the *same absolute path* it was built at. A virtualenv
# records its own location in pyvenv.cfg and in console-script shebangs, so
# relocating it to /opt or /usr/src would produce a venv whose `uvicorn` points
# at a python that isn't there.
COPY --from=builder --chown=converge:converge /app/.venv /app/.venv

# Source last: it is the layer that changes on almost every build, so it goes
# after everything expensive. app/ includes schema.sql, which db.apply_schema
# reads at startup relative to its own file.
COPY --chown=converge:converge app ./app

USER converge

EXPOSE 8000

# Healthcheck via python, not curl: the slim base has no curl and installing one
# would add ~10 MB plus its own CVE stream to every image, purely so the
# healthcheck could be one line shorter. The venv's python is already here.
#
# start-period covers the boot window -- connecting to Postgres and Redis, with
# retries (see app/db.py) -- so a slow database start is not counted as a
# failure. compose reads this to gate `depends_on: service_healthy`.
HEALTHCHECK --interval=5s --timeout=3s --start-period=45s --retries=5 \
    CMD ["python", "-c", "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

# Exec form (JSON array), so uvicorn is PID 1 and receives SIGTERM directly.
# Shell form would make /bin/sh PID 1, which does not forward signals to its
# child -- every `docker compose down` would then wait out the 10s timeout and
# SIGKILL, skipping the lifespan shutdown that closes the pool and the bus.
#
# --host 0.0.0.0 because 127.0.0.1 inside a container is only reachable from
# inside that container; nginx would never connect.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
