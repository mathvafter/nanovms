#!/usr/bin/env bash
# JagaNVR launcher (Linux / macOS / git-bash on Windows)
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || { echo "python not found"; exit 1; }

# create a venv once (stdlib only: this is just to keep the system python clean)
if [ ! -d .venv ] && [ "${JAGANVR_NO_VENV:-0}" != "1" ]; then
  echo "creating .venv ..."
  "$PY" -m venv .venv 2>/dev/null || true
fi
if [ -x .venv/bin/python ]; then PY=".venv/bin/python"; fi

exec "$PY" run.py "$@"
