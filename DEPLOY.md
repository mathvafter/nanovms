# Deploying NanoVMS to the NUC (Debian)

Target: Intel NUC5CPYH, Celeron N3050, Debian, root filesystem ~40 GB with the
rest on /home. This is the machine NanoVMS is for; it was developed and tested on
a Windows PC first.

## 1. Copy the project

```sh
# on the NUC
sudo useradd -r -m -d /opt/nanovms nanovms 2>/dev/null || true
sudo mkdir -p /opt/nanovms
# copy the nanovms/ directory contents here (rsync -a --exclude __pycache__)
sudo chown -R nanovms:nanovms /opt/nanovms
```

Only 307 KB, pure Python stdlib. No pip install, no Node, no database.

## 2. ffmpeg

```sh
sudo apt install -y ffmpeg
ffmpeg -version | head -1        # need 6.x or newer
```

## 3. Storage

Recordings must NOT go on the 40 GB root partition. Point them at the big disk:

```sh
sudo mkdir -p /srv/nanovms
sudo chown nanovms:nanovms /srv/nanovms
```

Then edit `/opt/nanovms/config.json`:

```json
"storage": { "root": "/srv/nanovms" }
```

If you use /home instead, uncomment the matching `ReadWritePaths=` line in
`nanovms.service` — `ProtectHome=read-only` blocks writes there by default.

## 4. Config

```sh
cd /opt/nanovms
sudo -u nanovms ./start.sh check          # verifies ffmpeg + paths
```

In `config.json`:
- `storage.root` — the path from step 3
- each camera's `url` — rtsp://user:pass@ip:554/... **never commit this file**
- per-camera `live_mode` (also in the browser: camera row ▸ live): `auto` = stream-copy when the browser can decode, transcode otherwise; `copy` = always copy (cheapest); `x264` = always transcode (fixes black tiles); `mjpeg` = universal fallback.
  If a live tile stays black, set that camera's live mode to `x264` or `mjpeg` in the browser — no config edit needed.

## 5. Service

```sh
sudo cp /opt/nanovms/nanovms.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nanovms
systemctl status nanovms
journalctl -u nanovms -f
```

## Verifying the stop path

`systemctl stop` sends SIGTERM. If that is mishandled, ffmpeg is orphaned and
the in-progress segment loses its MKV trailer, so playback reports it corrupt
forever. Check it once on the NUC:

```sh
systemctl stop nanovms
sleep 3
pgrep -a ffmpeg || echo "no ffmpeg left behind - correct"
systemctl start nanovms
```

If ffmpeg is still running, do **not** use `systemctl kill -s SIGKILL`; that
signal cannot be caught and the same truncation happens. The unit already sets
`KillSignal=SIGTERM` and `TimeoutStopSec=30` for this reason.

## CPU budget

Recording is always `-c copy` (no decode/encode). Live view transcodes only for
cameras that need it. Two 1080p transcodes will not fit a 2-core N3050
comfortably — if live view of both cameras feels heavy, set
`live.max_concurrent: 1` so only one view is ever transcoded, or point a
camera's live mode to `x264` (or point it at a sub-stream URL like `stream=1`).

## Before going live

- [ ] Rotate the camera password. It has appeared in screenshots and terminal
      output during development on the PC.
- [ ] Confirm the camera RTSP URLs are reachable from the NUC, not just the PC.
- [ ] `sudo -u nanovms ./start.sh test 'rtsp://...'` to probe each camera.
- [ ] Run the suite on the NUC too: `python3 test_nanovms.py`. The
      SIGTERM end-to-end leg only runs on POSIX, so it will actually execute
      here rather than being skipped.
