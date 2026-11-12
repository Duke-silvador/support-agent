# Convenience targets (Linux/macOS/CI). On Windows use the same docker commands
# directly, or run under WSL/Git Bash.
.PHONY: up down reset logs migrate test test-all evals lint typecheck lock audit scan sbom

# The lockfiles are generated with uv (0.13.x):
#   uv pip compile pyproject.toml --generate-hashes --python-version 3.12 -o requirements.lock
#   uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 \
#       -o requirements-dev.lock
# `make lock` uses the uv binary on PATH (or UV=/path/to/uv).
LOCK_PY ?= uv

# Scanner images are pinned by tag *and* digest, so `make scan`/`make sbom` run
# exactly the reviewed tool even if a tag is moved upstream.
TRIVY_IMAGE ?= aquasec/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb
SYFT_IMAGE ?= anchore/syft:v1.57.0@sha256:3c70e6d5979364cefdc22b51617a78fd264574d3bd6b4f84a351204147025e2a

up:
	docker compose up -d --build

down:
	docker compose down

reset:
	docker compose down -v

logs:
	docker compose logs -f app

migrate:
	docker compose run --rm migrate

test:
	docker compose --profile test run --rm test pytest -q -m "not integration"

test-all:
	# Uses the test service command: alembic upgrade head && pytest -q (with
	# RUN_INTEGRATION=1, so integration tests actually run).
	docker compose --profile test run --rm test

evals:
	python -m evals.runner

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy

# Regenerate both hashed locks from pyproject.toml. `--upgrade` is required:
# without it uv treats the existing lock as a preference and silently keeps
# old pins (including known-vulnerable ones) even after pyproject changes.
lock:
	$(LOCK_PY) pip compile pyproject.toml --generate-hashes --python-version 3.12 \
		--upgrade -o requirements.lock
	$(LOCK_PY) pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 \
		--upgrade -o requirements-dev.lock

# Blocking dependency audit against the pinned lockfiles (runs in Docker, so no
# local pip-audit install is needed).
audit:
	docker run --rm -v "$(PWD)":/src:ro -w /src python:3.12-slim sh scripts/audit.sh

# Local equivalent of the CI image job (needs Docker).
scan:
	docker build -t support-agent:local .
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
		$(TRIVY_IMAGE) image --scanners vuln --timeout 20m --exit-code 1 --ignore-unfixed \
		--severity HIGH,CRITICAL support-agent:local

sbom:
	docker build -t support-agent:local .
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
		-v "$(PWD)":/out $(SYFT_IMAGE) \
		scan support-agent:local -o spdx-json=/out/sbom.spdx.json -o cyclonedx-json=/out/sbom.cdx.json
