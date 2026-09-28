# NanoVMS

Lightweight NVR (network video recorder) — a Shinobi replacement for resource-constrained hardware.

**Why not Shinobi?** Shinobi runs Node.js + per-stream transcoding + a JS motion engine. On a Celeron N3050 (4 GB RAM) that is 60–150 % CPU per stream. NanoVMS records with `ffmpeg -c copy` (stream copy, zero decode/encode), so the same hardware records 3–4 cameras at ~2 % CPU.

---

## Requirements

- Python 3.10+ (stdlib only — no pip install)
- ffmpeg + ffprobe on `PATH` (or set `ffmpeg.path` in `config.json`)

## Quick start

```bash
# fresh machine, one command:
git clone <this-repo> nanovms && cd nanovms && ./setup.sh

# setup.sh checks python + ffmpeg, creates config.json from the template,
# and runs the test suite. It never overwrites an existing config.json.

# then edit config.json (camera urls, storage.root) and run:
./start.sh serve
# -> http://<this-machine-ip>:8080
```

Prefer to do it by hand?

```bash
# 1 — copy the example config
cp config.example.json config.json

# 2 — edit: set real camera URLs, storage path, retention
# (the "test" camera with a lavfi synthetic source works out of the box)

# 3 — start
python run.py          # Linux / macOS / git-bash
start.bat             # Windows

# 4 — open browser
http://localhost:8080
```

`config.json` holds your camera credentials and is git-ignored, so every machine
keeps its own. Share the settings you want to be common by editing
`config.example.json` instead.

## CLI

```
python run.py                   # start server (default port 8080)
python run.py --port 9090       # different port
python run.py check             # environment / ffmpeg check
python run.py test <url>        # probe a camera URL before adding it
python run.py add "Front gate" rtsp://user:pass@192.168.1.50:554/stream
python run.py stats             # storage summary
python run.py sweep --dry       # preview retention cleanup
python run.py sweep             # run cleanup
```

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
  "server":  { "host": "0.0.0.0", "port": 8080 },
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
      "enabled": true,
      "record": true,
      "audio": false,
      "transport": "",       // "" = inherit ffmpeg.rtsp_transport
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

## Deployment on Debian/CasaOS

```bash
# as a systemd service
cat > /etc/systemd/system/nanovms.service << 'EOF'
[Unit]
Description=NanoVMS
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/nanovms
ExecStart=/usr/bin/python3 run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now nanovms
```

## Testing

```bash
python test_nanovms.py     # 90 tests, no camera or network required
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
