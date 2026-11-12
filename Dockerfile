# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# ---------- base: digest-pinned Python + Debian security updates ----------
# Base image pinned by tag *and* digest for reproducible, tamper-evident builds.
# Bump procedure: `docker buildx imagetools inspect python:3.12-slim`, copy the
# new digest, rebuild, run `make scan`.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base

# Apply Debian security updates published after the base image was built.
# Trivy treats HIGH/CRITICAL with an available fix as blocking (the scan runs
# with `--ignore-unfixed`, so "no fix yet" does not excuse these). The digest
# pin fixes the baseline; this is the patch step, and `make scan` gates it.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# ---------- deps: install the hashed runtime lock into a venv ----------
FROM base AS deps

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.lock ./
# `--require-hashes` makes the lock authoritative: every installed artifact is
# pinned to a version *and* a hash. `--only-binary :all:` refuses sdists, so the
# image build never executes an sdist's build backend.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --require-hashes --only-binary :all: -r requirements.lock

# The application is imported from source (`PYTHONPATH=/app/src`), so the image
# does not need to rebuild the project or pull build backends from the network.

# ---------- test: runtime + dev tooling (used by CI and local pytest) ----------
FROM deps AS test
COPY requirements-dev.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --require-hashes --only-binary :all: -r requirements-dev.lock
WORKDIR /app
ENV PYTHONPATH=/app/src
# The pytest configuration (asyncio_mode=auto, markers, testpaths) lives here.
# Without it, async integration tests are not collected in the image.
COPY pyproject.toml ./
COPY src ./src
COPY tests ./tests
COPY evals ./evals
COPY alembic.ini ./
COPY migrations ./migrations
CMD ["pytest"]

# ---------- runtime: minimal, non-root ----------
FROM base AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

USER app
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

CMD ["uvicorn", "support_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
