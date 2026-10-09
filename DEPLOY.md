# Deploying JagaNVR on Linux (systemd)

Run JagaNVR 24/7 with auto-start after reboot. Any Debian/Ubuntu machine works.

## 1. Copy the project

```sh
# on the server
sudo useradd -r -m -d /opt/jaganvr jaganvr 2>/dev/null || true
sudo mkdir -p /opt/jaganvr
# copy the jaganvr/ directory contents here (rsync -a --exclude __pycache__)
sudo chown -R jaganvr:jaganvr /opt/jaganvr
```

Only 307 KB, pure Python stdlib. No pip install, no Node, no database.

## 2. ffmpeg

```sh
sudo apt install -y ffmpeg
ffmpeg -version | head -1        # need 6.x or newer
```

## 3. Storage

Recordings should NOT go on a small root partition. Point them at a disk with room:

```sh
sudo mkdir -p /srv/jaganvr
sudo chown jaganvr:jaganvr /srv/jaganvr
```

Then edit `/opt/jaganvr/config.json`:

```json
"storage": { "root": "/srv/jaganvr" }
```

If you use /home instead, uncomment the matching `ReadWritePaths=` line in
`jaganvr.service` — `ProtectHome=read-only` blocks writes there by default.

## 4. Config

```sh
cd /opt/jaganvr
sudo -u jaganvr ./start.sh check          # verifies ffmpeg + paths
```

In `config.json`:
- `storage.root` — the path from step 3
- each camera's `url` — rtsp://user:pass@ip:554/... **never commit this file**
- per-camera `live_mode` (also in the browser: camera row ▸ live): `auto` = stream-copy when the browser can decode, transcode otherwise; `copy` = always copy (cheapest); `x264` = always transcode (fixes black tiles); `mjpeg` = universal fallback.
  If a live tile stays black, set that camera's live mode to `x264` or `mjpeg` in the browser — no config edit needed.

## 5. Service

```sh
sudo cp /opt/jaganvr/jaganvr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jaganvr
systemctl status jaganvr
journalctl -u jaganvr -f
```

The unit uses `Restart=always`, so the NVR comes back after power loss or reboot.

## Verifying the stop path

`systemctl stop` sends SIGTERM. If that is mishandled, ffmpeg is orphaned and
the in-progress segment loses its MKV trailer, so playback reports it corrupt
forever. Check it once on the server:

```sh
systemctl stop jaganvr
sleep 3
pgrep -a ffmpeg || echo "no ffmpeg left behind - correct"
systemctl start jaganvr
```

If ffmpeg is still running, do **not** use `systemctl kill -s SIGKILL`; that
signal cannot be caught and the same truncation happens. The unit already sets
`KillSignal=SIGTERM` and `TimeoutStopSec=30` for this reason.

## CPU budget

Recording is always `-c copy` (no decode/encode). Live view transcodes only for
cameras that need it. On weak 2-core hardware, two 1080p transcodes at once is
too much — if live view feels heavy, set
`live.max_concurrent: 1` so only one view is ever transcoded, or point a
camera's live mode to `x264` (or point it at a sub-stream URL like `stream=1`).

## Before going live

- [ ] Use a dedicated camera password (the RTSP URL holds it in plain text).
- [ ] Confirm the camera RTSP URLs are reachable from this machine, not just your PC.
- [ ] `sudo -u jaganvr ./start.sh test 'rtsp://...'` to probe each camera.
- [ ] Run the suite here too: `python3 test_jaganvr.py`. The
      SIGTERM end-to-end leg only runs on POSIX, so it will actually execute
      here rather than being skipped.
