# JagaNVR

Record your security cameras on your own computer. No cloud, no subscription, no monthly fee.

You install it, open a web page, paste your camera address, and it records 24/7.

## What you need

1. A computer that stays on (old laptop, mini PC, anything)
2. **Python 3.10 or newer**
3. **ffmpeg**
4. A camera with an **RTSP address** (Hikvision, Dahua, Reolink, Tapo, ONVIF - almost all IP cameras have this)

Recording uses stream-copy, so even a weak computer records several cameras at very low CPU.

## Step 1: Install Python + ffmpeg

**Debian / Ubuntu:**
```bash
sudo apt install -y python3 ffmpeg
```

**Windows (PowerShell):**
```powershell
winget install Python.Python.3 Gyan.FFmpeg
```

**macOS:**
```bash
brew install python ffmpeg
```

Check they exist:
```bash
python3 --version
ffmpeg -version | head -1
```

## Step 2: Download JagaNVR

```bash
git clone https://github.com/mathvafter/jaganvr.git
cd jaganvr
```

No git? Click **Code -> Download ZIP** on GitHub, unzip, open the folder.

## Step 3: Run setup

```bash
./setup.sh
```

Windows: double-click `start.bat`, or run `start.bat` in cmd.

This checks Python + ffmpeg, creates your `config.json`, and runs a self-test. It never deletes anything.

## Step 4: Start it

```bash
./start.sh serve
```

Windows: `start.bat`

It prints an address like:

```
JagaNVR listening on http://0.0.0.0:1900
  Reachable at: http://192.168.1.50:1900
```

Open the **Reachable at** address in your browser (phone works too, as long as you are on the same WiFi).

## Step 5: Add your camera

1. Open the web page -> click the **Setup** tab
2. Paste your camera RTSP address, for example:
   `rtsp://user:pass@192.168.1.100:554/Streaming/Channels/101`
3. Click **Test URL** - it should say OK
4. Click **Add**

Done. It starts recording immediately. Live view, playback, and clips are on the same page.

Your camera password is stored only in `config.json` on your computer. That file is never uploaded to git.

## Camera addresses (RTSP)

Don't know yours? Try the pattern for your brand (`user`, `pass` and `ip` are yours):

| Brand | Try this |
|-------|----------|
| Hikvision main stream | `rtsp://user:pass@ip:554/Streaming/Channels/101` |
| Hikvision sub stream | `rtsp://user:pass@ip:554/Streaming/Channels/102` |
| Dahua | `rtsp://user:pass@ip:554/cam/realmonitor?channel=1&subtype=0` |
| Reolink | `rtsp://user:pass@ip:554/h264Preview_01_main` |
| Generic ONVIF | `rtsp://user:pass@ip:554/stream1` |
| No camera yet (test) | `lavfi:testsrc=size=1280x720:rate=25` |

Still unsure? The camera maker's app or web page usually shows the RTSP URL, or search "`<your camera model>` RTSP URL".

## Black screen? One-click fix

Some cameras send HEVC/H.265 which browsers can't play. Fix without touching any file:

1. Find your camera row in the **Setup** tab
2. Change its **live** dropdown to **x264**
3. The picture appears in a few seconds

If still black, try **mjpeg**.

## Keep it running after reboot

**Linux (systemd)** — copy to `/opt`, point storage at a disk, install the service:

```bash
sudo useradd -r -m -d /opt/jaganvr jaganvr 2>/dev/null || true
sudo mkdir -p /opt/jaganvr /srv/jaganvr
sudo cp -r . /opt/jaganvr/ && sudo chown -R jaganvr:jaganvr /opt/jaganvr /srv/jaganvr
# in /opt/jaganvr/config.json set: "storage": { "root": "/srv/jaganvr" }
sudo cp /opt/jaganvr/jaganvr.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now jaganvr
systemctl status jaganvr
```

`Restart=always` brings it back after power loss. Check the log anytime: `journalctl -u jaganvr -f`.

- **Windows:** put `start.bat` in Startup, or run it as a scheduled task.
- **macOS:** use `launchd`, or just leave the terminal open.

## Safety note

There is no login page. Anyone on your home network who can open the address can watch and change settings. Fine on home WiFi - do NOT expose the port directly to the internet. To reach it from outside, use a VPN (Tailscale, WireGuard) or an SSH tunnel.

## Want more?

<details>
<summary>Command line shortcuts</summary>

```bash
python3 run.py                   # start (default port 1900)
python3 run.py --port 9090       # different port
python3 run.py check             # check python + ffmpeg
python3 run.py test "rtsp://..." # test a camera address before adding it
python3 run.py stats             # how much disk recordings use
python3 run.py sweep --dry       # preview old-file cleanup
python3 run.py sweep             # run cleanup
```
</details>

<details>
<summary>Settings reference (config.json)</summary>

You never need to edit this by hand - the web UI does it. But this is what it holds:

- `server.host / port` - where it listens (default port 1900)
- `storage.root` - folder for recordings (default `recordings`)
- `storage.retention_days` - auto-delete footage older than this (default 7)
- `storage.max_usage_percent / keep_free_gb` - stop before the disk is full
- `live.max_concurrent` - how many live views at once (set to 1 on weak PCs)
- per camera: `url`, `enabled`, `record`, `audio`, `transport`, `live_mode` (auto/copy/x264/mjpeg), `encode`

</details>

<details>
<summary>For developers: tests, API, file layout</summary>

```bash
python3 test_jaganvr.py     # full suite, no camera needed
```

Main API routes: `GET /api/status`, `GET/POST /api/cameras`, `GET/PUT/DELETE /api/cameras/<id>`, `GET /api/segments?cam=<id>&day=YYYY-MM-DD`, `GET/POST /api/clips`, `GET /api/live/<id>/mjpeg`, `GET /api/snapshot/<id>.jpg`, `POST /api/test`, `POST /api/shutdown`.

```
jaganvr/
+-- run.py              start + CLI
+-- config.json         your settings (created for you, never committed)
+-- config.example.json template
+-- test_jaganvr.py     self-tests
+-- start.sh / start.bat launchers
+-- app/                server, recorder, live, playback, clips
+-- static/             web page (no build step)
```
</details>
