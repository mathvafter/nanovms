#!/usr/bin/env bash
# First-run setup for a fresh machine. Idempotent: safe to re-run.
#
#   git clone <your-repo> jaganvr && cd jaganvr && ./setup.sh
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
  warn "ffmpeg not found - JagaNVR cannot record without it"
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
if "$PY" test_jaganvr.py; then
  ok "test suite passed"
else
  die "tests failed - fix before deploying"
fi

# Print the address the user will actually type, not a <placeholder>: a
# first-time install should never have to work out their own LAN IP.
IP=$("$PY" - <<'PYEOF'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("10.255.255.255", 1))
    print(s.getsockname()[0])
except Exception:
    print("127.0.0.1")
finally:
    s.close()
PYEOF
)
PORT=$("$PY" -c "import json;print(json.load(open('config.json'))['server']['port'])" 2>/dev/null || echo 1900)

cat <<EOF

Setup complete. Next:

  1. run it:        ./start.sh serve
  2. open:          http://$IP:$PORT

Then, in the browser: Setup tab -> paste a camera RTSP url -> Test url -> Add.
Live view, recording, playback and codec settings are all done in the UI.
No need to edit config.json by hand.

Running as a service on Debian? See DEPLOY.md and jaganvr.service.
EOF
