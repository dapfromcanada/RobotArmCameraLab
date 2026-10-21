#!/usr/bin/env bash
# Install camera_lab into the existing uv venv WS/environments/camera_lab.
#
# Idempotent: safe to re-run. It does NOT create the venv (workspace convention:
# the venv already exists, created with `uv venv --python 3.12`).
#
#   1. install the pinned lock (requirements.lock)
#   2. install camera_lab itself in editable mode (--no-deps)
#   3. install the 4 vendor SDK wheels from WS/SDK/ by absolute path
#   4. print versions
#
# numpy must stay 1.x (eBUS wheels require numpy<2). The final check fails the
# script if anything pulled numpy to 2.x.
set -euo pipefail

WS="/mnt/workspaces/robotic_cell"
APP="${WS}/apps/camera_lab"
VENV="${WS}/environments/camera_lab"
PY="${VENV}/bin/python"

ARENA_WHL="${WS}/SDK/lucid_arena/ArenaSDK_Linux_x64/OutputDirectory/Linux/x64Release/ArenaPy/arena_api-2.10.0-py3-none-any.whl"
EBUS_DIR="${WS}/SDK/jai_ebus"
EBUS_WHLS=(
    "${EBUS_DIR}/ebus_base-7.0.1-7536_jai-py312-none-manylinux2014_x86_64.whl"
    "${EBUS_DIR}/ebus_edge-7.0.1-7536_jai-py312-none-manylinux2014_x86_64.whl"
    "${EBUS_DIR}/ebus_receive-7.0.1-7536_jai-py312-none-manylinux2014_x86_64.whl"
)

export UV_LINK_MODE=copy

log() { echo "[setup_env] $*" >&2; }

# --- preconditions --------------------------------------------------------
if [[ ! -x "${PY}" ]]; then
    log "ERROR: venv python not found at ${PY}"
    log "Create it first: uv venv --python 3.12 ${VENV}"
    exit 1
fi
case "$("${PY}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" in
    3.12) ;;
    *) log "ERROR: ${PY} is not Python 3.12 (eBUS wheels need ==3.12.*)"; exit 1 ;;
esac
for whl in "${ARENA_WHL}" "${EBUS_WHLS[@]}"; do
    [[ -f "${whl}" ]] || { log "ERROR: vendor wheel missing: ${whl}"; exit 1; }
done

# --- 1. pinned dependencies ----------------------------------------------
log "Installing pinned dependencies from requirements.lock"
uv pip install --python "${PY}" -r "${APP}/requirements.lock"

# --- 2. camera_lab (editable) --------------------------------------------
log "Installing camera_lab (editable, --no-deps)"
uv pip install --python "${PY}" --no-deps -e "${APP}"

# --- 3. vendor wheels (local only) ---------------------------------------
# --no-deps: their only dependency is numpy (already pinned by the lock);
# this keeps the resolver from ever touching numpy here.
log "Installing vendor SDK wheels from ${WS}/SDK/"
uv pip install --python "${PY}" --no-deps --no-index "${ARENA_WHL}" "${EBUS_WHLS[@]}"

# --- 4. verify + versions --------------------------------------------------
log "Checking installed environment"
uv pip check --python "${PY}" >&2
"${PY}" - <<'EOF'
import sys
from importlib.metadata import version

import numpy

if not numpy.__version__.startswith("1."):
    sys.exit(f"numpy {numpy.__version__} installed; must be 1.x (eBUS needs numpy<2)")

pkgs = [
    "camera-lab", "numpy", "opencv-python-headless", "tifffile", "pydantic", "PyYAML",
    "mcp", "typer", "PySide6-Essentials", "structlog", "pytest", "pytest-qt",
    "pytest-cov", "ruff", "mypy", "pip-audit", "arena_api", "ebus_base", "ebus_edge",
    "ebus_receive",
]
print(f"Python {sys.version.split()[0]}  ({sys.executable})")
for p in pkgs:
    print(f"  {p:24s} {version(p)}")
EOF
log "Done."
