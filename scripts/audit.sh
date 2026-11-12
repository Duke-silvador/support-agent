#!/bin/sh
# Dependency audit for both lockfiles, run inside Docker.
# Usage (from repo root):
#   docker run --rm -v "%cd%:/src:ro" -w /src python:3.12-slim sh scripts/audit.sh
set -eu
python -m pip install --disable-pip-version-check --quiet "pip-audit==2.9.0"
python -m pip_audit --requirement requirements.lock --disable-pip --require-hashes --strict
python -m pip_audit --requirement requirements-dev.lock --disable-pip --require-hashes --strict
