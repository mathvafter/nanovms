#!/usr/bin/env bash
# First-run setup for a fresh machine. Idempotent: safe to re-run.
#
#   git clone <your-repo> nanovms && cd nanovms && ./setup.sh
#
# Creates config.json from the template if absent, verifies ffmpeg, and runs
# the test suite. It never overwrites an existing config.json.
set -euo pipefail
cd "$(dirname "$0")"

ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '  \033[31mfail\033[0m %s\n' "$*" >&2; exit 1; }

echo "== python =="
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || die "python 3 not found (apt install python3)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "need python 3.9 or newer"
ok "$("$PY" -V 2>&1)"

echo "== ffmpeg =="
if command -v ffmpeg >/dev/null 2>&1; then
  ok "$(ffmpeg -version 2>/dev/null | head -1)"
else
  warn "ffmpeg not found - NanoVMS cannot record without it"
  warn "  Debian/Ubuntu:  sudo apt install -y ffmpeg"
  warn "  Fedora:         sudo dnf install ffmpeg"
  warn "  macOS:          brew install ffmpeg"
  warn "  Windows:        winget install Gyan.FFmpeg"
  die "install ffmpeg, then re-run ./setup.sh"
fi

echo "== config =="
if [ -f config.json ]; then
  ok "config.json already exists (left untouched)"
else
  cp config.example.json config.json
  ok "created config.json from config.example.json"
  echo "     -> edit it and set each camera's rtsp url and storage.root"
fi

echo "== storage =="
ROOT=$("$PY" - <<'EOF' 2>/dev/null || echo ""
import json
try:
    print(json.load(open("config.json", encoding="utf-8"))["storage"]["root"])
except Exception:
    print("")
EOF
)
if [ -n "$ROOT" ] && [ -d "$ROOT" ]; then
  ok "storage root exists: $ROOT"
elif [ -n "$ROOT" ]; then
  warn "storage root '$ROOT' does not exist - mkdir it and chown to the service user"
fi

echo "== tests =="
if "$PY" test_nanovms.py; then
  ok "test suite passed"
else
  die "tests failed - fix before deploying"
fi

cat <<'EOF'

Setup complete. Next:

  1. edit config.json  - camera rtsp urls, storage.root
  2. probe a camera:   ./start.sh test 'rtsp://user:pass@ip:554/path'
  3. run it:           ./start.sh serve
  4. browse to         http://<this-machine-ip>:1900

Running as a service on Debian? See DEPLOY.md and nanovms.service.
EOF
