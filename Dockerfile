# deltaflow API image. Three stages: a shared builder that resolves the locked
# dependency graph, a lean `runtime` for deployment, and a `dev` target that
# keeps uv and an editable install so bind-mounted source hot-reloads.
#
# The dependency solve is cached independently of the app code: an edit under
# src/deltaflow/ reuses the dependency layer and only re-runs the fast project
# install.

# ---- builder: resolve deps, build+install the deltaflow wheel into /app/.venv ----
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder

# Byte-compile on install (faster cold start) and copy rather than hardlink out
# of the uv cache mount (the cache lives on a different filesystem than
# /app/.venv).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer: third-party deps only, from the locked graph, with no
# project code present. Bind-mounting the manifests (instead of COPY) keeps them
# out of this layer, so its cache key is purely pyproject.toml + uv.lock.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project

# Project layer: bring in the source and install deltaflow itself as a wheel
# (--no-editable, so the venv is self-contained and /app/src can be dropped at
# runtime). Only this layer is invalidated by a code change.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- dev: uv retained, project installed editable, source expected as a mount ----
FROM builder AS dev

# Editable (no --no-editable) so /app/.venv resolves the package through
# /app/src, which docker compose bind-mounts from the host. Dev dependencies are
# included so `pytest` runs inside the container too.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/app

EXPOSE 8000

# Runs as root deliberately: a bind-mounted source tree owned by the host user
# is otherwise unreadable, and this target is for local development only.
#
# uvicorn directly rather than `deltaflow serve --reload`, only so that
# --reload-dir can be pinned to the mounted tree. Without it the watcher walks
# all of /app including .venv, which is thousands of files to poll (see
# WATCHFILES_FORCE_POLLING in docker-compose.yml) for no benefit. Startup
# migration lives in the app's lifespan, so this is otherwise identical.
CMD ["uvicorn", "deltaflow.api:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--reload", "--reload-dir", "/app/src"]

# ---- runtime: interpreter + built venv, no uv, no source tree ----
FROM python:3.13-slim-trixie AS runtime

# Run as non-root. The SQLite file lives under /data, which is a volume owned by
# this user; config arrives through DELTAFLOW_* environment variables.
RUN groupadd --system deltaflow \
    && useradd --system --gid deltaflow --home-dir /app deltaflow \
    && mkdir -p /data \
    && chown deltaflow:deltaflow /data

COPY --from=builder --chown=deltaflow:deltaflow /app/.venv /app/.venv

# Put the venv on PATH so the `deltaflow` console script resolves without
# activation. The default database lands in the /data volume rather than the
# container's writable layer, where it would vanish on every redeploy.
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/app \
    DELTAFLOW_DATABASE_URL=sqlite:////data/deltaflow.db

USER deltaflow
WORKDIR /app
VOLUME ["/data"]

EXPOSE 8000

# /healthz is unauthenticated and does not touch the database, so it reports
# "the process is serving" rather than "the whole system is well" -- which is
# what a restart decision should be based on.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"]

# Schema migration happens on startup by default (DELTAFLOW_AUTO_MIGRATE); set
# it false and run `deltaflow migrate` as a deploy step if several replicas
# could ever start at once and race each other.
ENTRYPOINT ["deltaflow"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
