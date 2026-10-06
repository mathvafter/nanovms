# NanoVMS

Turn any spare computer into a security-camera recorder. No cloud account, no subscription, no database — just install, open the web page it prints, paste your camera's address, and it starts recording.

```bash
git clone https://github.com/mathvafter/nanovms.git
cd nanovms
./setup.sh        # checks python + ffmpeg, creates config, runs self-tests
./start.sh serve  # prints the address to open, e.g. http://192.168.1.50:1900
```

Open that address in a browser → **Setup tab** → paste a camera RTSP URL → **Test URL** → **Add**. Done. Live view, recording, playback, and codec fixes are all buttons in that page — nothing to edit by hand.

What you need: **Python 3.10+**, **ffmpeg**, and any camera that speaks **RTSP** (Hikvision, Dahua, Reolink, Tapo, ONVIF…). Recording is stream-copy (`ffmpeg -c copy`), so even weak hardware records several cameras at ~2% CPU each.

Your camera passwords live in `config.json` on your machine only — that file is never committed to git.

Works on **Linux**, **Windows**, **macOS**. Want it always-on after reboot? See [DEPLOY.md](DEPLOY.md) (one systemd service file included).

---

## Requirements

- Python 3.10+ (stdlib only — no pip install)
- ffmpeg + ffprobe on `PATH` (or set `ffmpeg.path` in `config.json`)

## Quick start

```bash
git clone https://github.com/mathvafter/nanovms.git && cd nanovms && ./setup.sh
```

`setup.sh` checks python + ffmpeg, creates `config.json` from the template, and
runs the test suite. It never overwrites an existing `config.json`.

Then start the server:

```bash
./start.sh serve
```

It prints the address to open, port included:

```
NanoVMS listening on http://0.0.0.0:1900
  Reachable at: http://192.168.1.50:1900
```

**Everything else happens in the browser.** Open that address, go to the
**Setup** tab, paste a camera RTSP URL, press **Test URL** to confirm the camera
answers, then **Add**. Live view, recording, playback, retention and codec
settings are all in the UI — you never need to edit `config.json` by hand.

`config.json` is still what gets written (it holds your camera credentials and
is git-ignored, so every machine keeps its own), but it is created for you and
editable from the browser.

## Manual install (no git)

```bash
# 1 — copy the example config
cp config.example.json config.json

# 2 — start (the bundled "test" camera uses a lavfi synthetic source)
python run.py          # Linux / macOS / git-bash
start.bat             # Windows

# 3 — open the address it prints
```

The web UI can add, edit and delete every camera, so step 2 needs no editing.
Prefer to seed a config from a file? `config.example.json` is the shared,
credential-free template.

## CLI

```
python run.py                   # start server (default port 1900)
python run.py --port 9090       # different port
python run.py check             # environment / ffmpeg check
python run.py test <url>        # probe a camera URL before adding it
python run.py add "Front gate" rtsp://user:pass@192.168.1.50:554/stream
python run.py stats             # storage summary
python run.py sweep --dry       # preview retention cleanup
python run.py sweep             # run cleanup
```

## Security

**There is no authentication.** Any client that can reach the port can read
`/api/config` (which returns your camera RTSP URLs, passwords included), walk
the filesystem via `/api/fs/browse`, delete recordings, and stop the server via
`/api/shutdown`. The server also binds `0.0.0.0` by default, so it is reachable
from every device on the network.

That is a deliberate trade-off: an NVR you cannot open from your phone is not
much use, and putting an auth system in front of every route adds a lot of
surface for something that mostly runs on a private home LAN. It does print a
warning at startup.

On a shared or untrusted network, pick one:

```sh
# 1. bind localhost only, and reach it through a tunnel
#    (set "host": "127.0.0.1" in config.json)
ssh -L 1900:127.0.0.1:1900 you@nanovms-host

# 2. bind localhost only, reachable over a VPN (Tailscale, WireGuard)
```

A reverse proxy with TLS and a password in front of the port also works, and is
the usual answer if you need to expose it to the internet at all.

## Camera URL formats

| Brand | URL pattern |
|-------|-------------|
| Hikvision (main) | `rtsp://user:pass@ip:554/Streaming/Channels/101` |
| Hikvision (sub) | `rtsp://user:pass@ip:554/Streaming/Channels/102` |
| Dahua | `rtsp://user:pass@ip:554/cam/realmonitor?channel=1&subtype=0` |
| Reolink | `rtsp://user:pass@ip:554/h264Preview_01_main` |
| Generic ONVIF | `rtsp://user:pass@ip:554/stream1` |
| Test (no camera) | `lavfi:testsrc=size=1280x720:rate=25` |

## config.json reference

```json
{
  "server":  { "host": "0.0.0.0", "port": 1900 },
  "storage": {
    "root": "recordings",
    "segment_minutes": 5,
    "retention_days": 7,
    "max_usage_percent": 85,
    "keep_free_gb": 5,
    "clips_retention_days": 14
  },
  "ffmpeg": {
    "path": "",              // "" = autodetect on PATH
    "rtsp_transport": "tcp", // tcp | udp | http
    "stall_timeout_sec": 30,
    "restart_backoff_min": 2,
    "restart_backoff_max": 60
  },
  "live": {
    "max_concurrent": 2,     // max live streams open at once
    "idle_timeout_sec": 45,
    "fps": 8,                // transcode FPS (only used for HEVC cameras)
    "max_width": 1280
  },
  "cameras": [
    {
      "id": "cam1",
      "name": "Front gate",
      "url": "rtsp://...",
      "enabled": true,       // untick "on" in the GUI to disable without deleting
      "record": true,
      "audio": false,
      "transport": "",       // "" = inherit ffmpeg.rtsp_transport (per-camera override in the row)
      "live_mode": "auto",   // auto | copy | x264 | mjpeg - per-camera dropdown in the GUI
      "encode": false,        // true = re-encode on record (MJPEG/rawvideo cameras)
      "segment_minutes": 0,  // 0 = inherit storage.segment_minutes
      "encode": false,       // true for MJPEG/rawvideo sources
      "encode_fps": 10
    }
  ]
}
```

## REST API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | Server status, recorder states, live sessions, storage |
| GET | `/api/cameras` | List cameras with recorder state |
| POST | `/api/cameras` | Add camera `{name, url, record, audio, transport}` |
| GET/PUT/DELETE | `/api/cameras/<id>` | Get / update / remove camera |
| POST | `/api/cameras/<id>/motion` | `{action: start\|stop\|restart}` recorder |
| GET/PUT | `/api/config` | Read / update full config |
| GET | `/api/segments?cam=<id>&day=YYYY-MM-DD` | List recording segments |
| GET | `/api/segments?cam=<id>&days=1` | Calendar rollup (days with footage) |
| GET | `/api/clips` | List clips |
| POST | `/api/clips` | Export clip `{cam, start, end, name, precise, audio}` |
| DELETE | `/api/clips/<id>` | Delete clip |
| GET | `/api/clip-file?id=<id>` | Download clip file |
| GET | `/api/live/<id>/init.mp4` | Live stream init segment (MSE) |
| GET | `/api/live/<id>/frag.mp4?c=<cursor>` | Live fragment (long-poll) |
| GET | `/api/live/<id>/mjpeg` | MJPEG stream (universal fallback) |
| GET | `/api/snapshot/<id>.jpg` | Single JPEG snapshot |
| GET | `/api/play/init.mp4?cam=<id>&s=<epoch>&e=<epoch>` | Playback init |
| GET | `/api/play/frag.mp4?cam=<id>&s=<epoch>&e=<epoch>&c=<cursor>` | Playback fragment |
| GET | `/api/frame?cam=<id>&t=<epoch>` | Single frame from recordings |
| GET | `/api/storage` | Disk usage + per-camera stats |
| GET/POST | `/api/sweep?dry=1` | Run retention cleanup |
| POST | `/api/test` | Probe a URL `{url, transport, timeout}` |
| POST | `/api/shutdown` | Graceful shutdown |

## Live view: how it works (zero CPU path)

```
camera (H.264 RTSP)
  → ffmpeg -c:v copy -movflags frag_keyframe+empty_moov
  → fragmented MP4 boxes on stdout
  → NanoVMS splits at moof boundaries
  → browser long-polls /api/live/<id>/frag.mp4
  → MSE SourceBuffer.appendBuffer() per fragment
```

If the camera streams H.264 or MJPEG: ffmpeg only demuxes+remuxes, no pixel touched.
If the camera streams HEVC/H.265: NanoVMS transcodes to H.264 baseline with `-preset ultrafast`. This is the only expensive path.

## Deployment on Debian (systemd)

See [DEPLOY.md](DEPLOY.md) — copy the project to `/opt/nanovms`, point
`storage.root` at a disk with room, copy the included `nanovms.service` to
`/etc/systemd/system/`, then `systemctl enable --now nanovms`.
`Restart=always` brings it back after power loss or reboot.

## Testing

```bash
python test_nanovms.py     # full suite, no camera or network required
```

## File layout

```
nanovms/
├── run.py              CLI + entry point
├── config.json         your config (copy from config.example.json)
├── config.example.json template with comments
├── test_nanovms.py     test suite (90 tests)
├── start.sh / .bat     shell launchers
├── app/
│   ├── config.py       config load/save/normalize
│   ├── recorder.py     ffmpeg supervisor (one per camera)
│   ├── stream.py       shared fragmented-MP4 pipe reader
│   ├── live.py         on-demand live sessions
│   ├── playback.py     recording playback sessions
│   ├── index.py        filesystem index + clips + retention sweep
│   ├── export.py       clip rendering (stream copy by default)
│   └── server.py       ThreadingHTTPServer + JSON API
└── static/
    ├── index.html
    ├── style.css
    └── app.js          vanilla JS, no build step
```
