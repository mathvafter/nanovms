"""NanoVMS - HTTP entrypoint. stdlib ThreadingHTTPServer + JSON API + static UI."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config as cfgmod
from . import export, index, live, playback
from .recorder import CREATE_NO_WINDOW, find_ffmpeg, IS_WIN, RecorderManager

STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_BODY = 2 * 1024 * 1024


class App:
    """Shared server state - one instance for the whole process."""

    def __init__(self, cfg: dict, config_path: Path | None = None):
        self.cfg = cfg
        self.config_path = Path(config_path) if config_path else cfgmod.CONFIG_PATH
        self.lock = threading.RLock()
        self.started = time.time()
        self.dirty = threading.Event()
        self.recorders = RecorderManager(cfg)
        self.live = live.LiveManager(cfg)
        self.playback = playback.PlaybackManager(cfg)
        self._sweeper: threading.Thread | None = None
        self._last_sweep = 0.0

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        self.recorders.sync()
        self._sweeper = threading.Thread(target=self._sweep_loop, daemon=True,
                                         name="sweeper")
        self._sweeper.start()

    def shutdown(self) -> None:
        self.live.stop_all()
        self.playback.stop_all()
        self.recorders.stop_all()

    def apply_config(self, new_cfg: dict, persist: bool = True) -> None:
        # A settings PUT carries only the keys the form knows about. Replacing
        # the config wholesale would silently delete every camera when the body
        # omits "cameras" - which is exactly what happens if anyone edits a
        # GET /api/config response and PUTs it back. Cameras are managed
        # exclusively through /api/cameras, so a config save must never touch
        # them; an explicit "cameras" key is still honoured.
        body = dict(new_cfg or {})
        with self.lock:
            if "cameras" not in body:
                body["cameras"] = self.cfg.get("cameras", [])
            self.cfg = cfgmod.normalize(body)
            if persist:
                cfgmod.save(self.cfg, self.config_path)
        self.recorders.sync(self.cfg)
        self.live.sync(self.cfg)
        self.playback.sync(self.cfg)

    def save_config(self) -> None:
        with self.lock:
            cfgmod.save(self.cfg, self.config_path)

    def _sweep_loop(self) -> None:
        while True:
            try:
                every = 300
                time.sleep(every)
                if time.time() - self._last_sweep < 60:
                    continue
                self._last_sweep = time.time()
                res = index.sweep(self.cfg)
                # Retention deleting footage silently looks like a playback bug,
                # so always report what went and which rule triggered it.
                if res.get("removed"):
                    reasons = {}
                    for it in res.get("items") or []:
                        reasons[it.get("reason", "?")] = reasons.get(it.get("reason", "?"), 0) + 1
                    kinds = ", ".join(f"{k}x{v}" for k, v in reasons.items())
                    print(f"[sweep] deleted {res['removed']} item(s), "
                          f"freed {res['freed']} bytes ({kinds})", flush=True)
            except Exception as e:
                print(f"[sweep] error: {e}", flush=True)

    # -- helpers ----------------------------------------------------------- #

    def camera(self, cam_id: str) -> dict | None:
        return next((c for c in self.cfg["cameras"] if c["id"] == cam_id), None)

    def sysinfo(self) -> dict:
        info = {"python": None, "ffmpeg": "", "ffmpeg_ok": False, "host": socket.gethostname(),
                "uptime_sec": int(time.time() - self.started), "os": os.name}
        try:
            import sys
            info["python"] = sys.version.split()[0]
        except Exception:
            pass
        try:
            info["ffmpeg"] = find_ffmpeg(self.cfg["ffmpeg"].get("path", ""))
            info["ffmpeg_ok"] = True
        except FileNotFoundError:
            info["ffmpeg"] = ""
        info["cpu_count"] = os.cpu_count()
        try:
            if hasattr(os, "getloadavg"):
                info["loadavg"] = [round(x, 2) for x in os.getloadavg()]
        except Exception:
            pass
        return info


# --------------------------------------------------------------------------- #

def _int(v, default, lo=None, hi=None):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _float(v, default, lo=None, hi=None):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


class Handler(BaseHTTPRequestHandler):
    server_version = "NanoVMS/1.0"
    protocol_version = "HTTP/1.1"
    app: App = None  # type: ignore[assignment]

    # -- plumbing ---------------------------------------------------------- #

    def log_message(self, fmt, *args):
        pass  # quiet: we are a recorder, not a web server log generator

    def _send(self, code: int, body: bytes, ctype: str = "application/octet-stream",
              extra: dict | None = None, head_only: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code: int = 200, head_only: bool = False):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", head_only=head_only)

    def _err(self, code: int, msg: str, head_only: bool = False):
        self._json({"ok": False, "error": msg}, code, head_only=head_only)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _q(self) -> dict:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q, keep_blank_values=True).items()}

    # -- verbs ------------------------------------------------------------- #

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    # -- router ------------------------------------------------------------ #

    def _route(self, method: str):
        app = self.app
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        head = method == "HEAD"
        try:
            if not path.startswith("/api/"):
                return self._static(path, head)

            if path == "/api/status" and method in ("GET", "HEAD"):
                return self._json({
                    "ok": True,
                    "version": "1.0",
                    "sys": app.sysinfo(),
                    "storage": index.storage_stats(app.cfg),
                    "recorders": app.recorders.status(),
                    "live": app.live.status(),
                }, head_only=head)

            if path == "/api/cameras":
                if method in ("GET", "HEAD"):
                    cams = []
                    rstatus = {r["cam_id"]: r for r in app.recorders.status()}
                    for c in app.cfg["cameras"]:
                        d = dict(c)
                        d["recorder"] = rstatus.get(c["id"], {})
                        cams.append(d)
                    return self._json({"ok": True, "cameras": cams}, head_only=head)
                if method == "POST":
                    if len(app.cfg["cameras"]) >= 32:
                        return self._err(400, "camera limit reached (32)")
                    body = self._body()
                    cam = cfgmod.normalize_camera(body, len(app.cfg["cameras"]))
                    if not cam["url"]:
                        return self._err(400, "url is required")
                    base, n = cam["id"], 2
                    while app.camera(cam["id"]):
                        cam["id"] = f"{base}-{n}"
                        n += 1
                    with app.lock:
                        app.cfg["cameras"].append(cam)
                    app.save_config()
                    app.recorders.sync(app.cfg)
                    app.live.sync(app.cfg)
                    return self._json({"ok": True, "camera": cam})

            m = re.match(r"^/api/cameras/([\w.-]+)$", path)
            if m:
                cid = m.group(1)
                cam = app.camera(cid)
                if not cam:
                    return self._err(404, "no such camera")
                if method in ("GET", "HEAD"):
                    return self._json({"ok": True, "camera": cam}, head_only=head)
                if method in ("PUT", "PATCH"):
                    body = self._body()
                    merged = cfgmod.normalize_camera({**cam, **body}, 0)
                    merged["id"] = cid
                    with app.lock:
                        i = app.cfg["cameras"].index(cam)
                        app.cfg["cameras"][i] = merged
                    app.save_config()
                    app.recorders.sync(app.cfg)
                    app.live.sync(app.cfg)
                    return self._json({"ok": True, "camera": merged})
                if method == "DELETE":
                    with app.lock:
                        app.cfg["cameras"] = [c for c in app.cfg["cameras"] if c["id"] != cid]
                    app.save_config()
                    app.recorders.sync(app.cfg)
                    app.live.stop(cid)
                    return self._json({"ok": True, "deleted": cid,
                                       "note": "recordings on disk were kept"})

            m = re.match(r"^/api/cameras/([\w.-]+)/motion$", path)
            if m and method == "POST":
                cid = m.group(1)
                act = (self._body().get("action") or "").lower()
                if act == "start":
                    ok = app.recorders.start(cid)
                elif act == "stop":
                    app.recorders.stop(cid)
                    ok = True
                elif act == "restart":
                    ok = app.recorders.restart(cid)
                else:
                    return self._err(400, "action must be start|stop|restart")
                return self._json({"ok": ok, "recorders": app.recorders.status()})

            if path == "/api/config":
                if method in ("GET", "HEAD"):
                    return self._json({"ok": True, "config": app.cfg}, head_only=head)
                if method in ("PUT", "POST"):
                    body = self._body()
                    app.apply_config(body)
                    return self._json({"ok": True, "config": app.cfg})

            if path == "/api/config/reset" and method == "POST":
                app.apply_config({})
                return self._json({"ok": True, "config": app.cfg})

            if path == "/api/validate-path" and method in ("GET", "POST"):
                p = (self._body().get("path") or self._q().get("path") or "").strip()
                if not p:
                    return self._json({"ok": False, "exists": False, "error": "no path given"})
                resolved = os.path.abspath(p)
                exists = os.path.isdir(resolved)
                writable = False
                if exists:
                    try:
                        test = os.path.join(resolved, ".nanovms_write_test")
                        open(test, "w").close()
                        os.unlink(test)
                        writable = True
                    except OSError:
                        pass
                return self._json({"ok": True, "path": resolved,
                                   "exists": exists, "writable": writable})

            if path == "/api/fs/drives" and method in ("GET", "HEAD"):
                # List available drive roots so the picker can jump to D:, E:, ...
                if not IS_WIN:
                    return self._json({"ok": True, "drives": [{"name": "/", "path": "/",
                                                                "label": "/", "free_gb": 0}]})
                drives = []
                try:
                    import ctypes
                    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
                    free_bytes = ctypes.c_ulonglong(0)
                    sectors = ctypes.c_ulong(0)
                    bytes_per_sector = ctypes.c_ulong(0)
                    free_clusters = ctypes.c_ulong(0)
                    total_clusters = ctypes.c_ulong(0)
                    for i in range(26):
                        if not (bitmask >> i) & 1:
                            continue
                        letter = chr(ord("A") + i) + ":\\"
                        if not os.path.isdir(letter):
                            continue
                        free_gb = 0.0
                        if ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                                ctypes.c_wchar_p(letter),
                                ctypes.byref(free_bytes), None, None):
                            free_gb = round(free_bytes.value / 1024 ** 3, 1)
                        drives.append({"name": chr(ord("A") + i) + ":",
                                       "path": letter,
                                       "label": letter,
                                       "free_gb": free_gb})
                except Exception:
                    return self._json({"ok": True, "drives": []})
                return self._json({"ok": True, "drives": drives})

            if path == "/api/fs/browse" and method in ("GET", "HEAD"):
                # Server-side folder picker. Lists subdirectories only - no file
                # contents and no file names are ever returned.
                raw = self._q().get("path") or ""
                raw = os.path.abspath(os.path.expanduser(raw)) if raw.strip() else os.path.abspath(".")
                # "/" means "start at C:\" on Windows, "/" elsewhere
                if raw in ("/", "\\", "*", "root"):
                    raw = os.path.abspath("C:\\") if IS_WIN else "/"
                if not os.path.isdir(raw):
                    return self._err(400, "not a folder: " + raw)
                try:
                    names = sorted(os.listdir(raw), key=str.lower)
                except OSError as e:
                    return self._err(403, f"cannot read folder: {e.strerror}")
                dirs = []
                for n in names:
                    if n.startswith(".") or n in ("$RECYCLE.BIN", "System Volume Information"):
                        continue
                    try:
                        if os.path.isdir(os.path.join(raw, n)):
                            dirs.append(n)
                    except OSError:
                        continue
                # "Up" from a drive root stays put; ".." from anywhere else walks up.
                parent = os.path.dirname(raw)
                if parent == raw:
                    parent = ""                       # already at a drive/filesystem root
                return self._json({"ok": True, "path": raw, "parent": parent,
                                   "dirs": dirs[:500], "truncated": len(dirs) > 500})

            if path == "/api/fs/mkdir" and method == "POST":
                raw = (self._body().get("path") or "").strip()
                if not raw:
                    return self._err(400, "path required")
                target = os.path.abspath(os.path.expanduser(raw))
                try:
                    os.makedirs(target, exist_ok=True)
                except OSError as e:
                    return self._err(409, f"could not create folder: {e.strerror}")
                if not os.access(target, os.W_OK):
                    return self._err(403, "folder created but is not writable")
                return self._json({"ok": True, "path": target})

            if path in ("/api/segments", "/api/recordings") and method in ("GET", "HEAD"):
                q = self._q()
                cid = q.get("cam", "")
                if not app.camera(cid):
                    return self._err(404, "no such camera")
                if q.get("days"):
                    return self._json({"ok": True, "cam": cid,
                                       "days": index.list_days(Path(app.cfg["storage"]["root"]), cid,
                                                              limit=_int(q.get("days"), 14, 1, 365))},
                                      head_only=head)
                segs = index.list_segments(Path(app.cfg["storage"]["root"]), cid,
                                           day=q.get("day") or None,
                                           limit=_int(q.get("limit"), 2000, 1, 20000),
                                           cfg=app.cfg)
                return self._json({"ok": True, "cam": cid, "count": len(segs),
                                   "segments": segs}, head_only=head)

            if path == "/api/clips":
                root = Path(app.cfg["storage"]["root"])
                if method in ("GET", "HEAD"):
                    return self._json({"ok": True,
                                       "clips": index.list_clips(root, self._q().get("cam") or None)},
                                      head_only=head)
                if method == "POST":
                    b = self._body()
                    cid = b.get("cam") or b.get("cam_id")
                    if not app.camera(cid):
                        return self._err(404, "no such camera")
                    start = index.parse_ts(b.get("start"))
                    end = index.parse_ts(b.get("end"))
                    if start is None or end is None:
                        return self._err(400, "start/end required (epoch or 'YYYY-mm-dd HH:MM:SS')")
                    res = export.export_to_clip(app.cfg, cid, start, end,
                                                name=b.get("name", ""),
                                                precise=bool(b.get("precise")),
                                                audio=bool(b.get("audio")))
                    if not res.get("ok"):
                        return self._err(409, res.get("error", "export failed"))
                    return self._json(res)

            m = re.match(r"^/api/clips/([\w.-]+)$", path)
            if m:
                cid = m.group(1)
                root = Path(app.cfg["storage"]["root"])
                if method in ("GET", "HEAD"):
                    c = index.get_clip(root, cid)
                    return self._json({"ok": True, "clip": c}, head_only=head) if c \
                        else self._err(404, "no such clip")
                if method == "DELETE":
                    ok = index.delete_clip(root, cid)
                    return self._json({"ok": ok})

            if path == "/api/storage" and method in ("GET", "HEAD"):
                return self._json({"ok": True, **index.storage_stats(app.cfg)}, head_only=head)

            if path == "/api/sweep/why" and method in ("GET", "HEAD"):
                # explain what the sweeper would delete right now, and why
                st = app.cfg["storage"]
                try:
                    du = shutil.disk_usage(str(Path(app.cfg["storage"]["root"])))
                    pct = round(du.used / du.total * 100, 1)
                except OSError:
                    du, pct = None, 0.0
                return self._json({
                    "ok": True,
                    "disk_percent": pct,
                    "max_usage_percent": st.get("max_usage_percent"),
                    "keep_free_gb": st.get("keep_free_gb"),
                    "retention_days": st.get("retention_days"),
                    "free_gb": round(du.free / 1024 ** 3, 2) if du else 0,
                    "over_percent": bool(pct > float(st.get("max_usage_percent", 85))),
                    "over_space": bool(du and du.free < float(st.get("keep_free_gb", 10)) * 1024 ** 3),
                    "sweep_interval_sec": 300,
                })

            if path == "/api/sweep" and method in ("GET", "POST"):
                dry = self._q().get("dry") in ("1", "true") or \
                    bool(self._body().get("dry_run")) if method == "POST" else \
                    self._q().get("dry") in ("1", "true")
                return self._json({"ok": True, **index.sweep(app.cfg, dry_run=bool(dry))})

            if path == "/api/test" and method == "POST":
                b = self._body()
                url = (b.get("url") or "").strip()
                if not url:
                    cam = app.camera(b.get("cam", ""))
                    url = cam["url"] if cam else ""
                if not url:
                    return self._err(400, "url required")
                if not re.match(r"^(rtsp|rtsps|http|https|rtmp|srt|udp|file)://|^lavfi:", url, re.I):
                    return self._err(400, "url must be rtsp:// rtsps:// http(s):// rtmp:// file:// or lavfi:<graph>")
                return self._json(_probe(url, app.cfg,
                                         transport=b.get("transport") or "tcp",
                                         timeout=_float(b.get("timeout"), 12, 3, 60)))

            # ---- live streaming ------------------------------------------- #

            if path == "/api/live" and method in ("GET", "HEAD"):
                st = app.live.status()
                return self._json({"ok": True, "sessions": st,
                                   "max_concurrent": app.cfg["live"]["max_concurrent"]},
                                  head_only=head)

            # ---- recordings playback -------------------------------------- #

            if path == "/api/play" and method in ("GET", "HEAD"):
                q = self._q()
                cid = q.get("cam", "")
                if not app.camera(cid):
                    return self._err(404, "no such camera")
                start = index.parse_ts(q.get("start"))
                end = index.parse_ts(q.get("end"))
                if start is None:
                    return self._err(400, "start required")
                if end is None:
                    end = start + 300
                s = app.playback.open(cid, start, end)
                return self._json({"ok": True, "session": s.info(),
                                   "init_url": f"/api/play/init.mp4?s={int(start)}&e={int(end)}&cam={cid}",
                                   "frag_url": f"/api/play/frag.mp4?s={int(start)}&e={int(end)}&cam={cid}"},
                                  head_only=head)

            if path == "/api/play/init.mp4" and method in ("GET", "HEAD"):
                return self._pb_init(head)
            if path == "/api/play/frag.mp4" and method in ("GET", "HEAD"):
                return self._pb_frag(head)
            if path == "/api/play/close" and method == "POST":
                q = self._q()
                cid, s0 = q.get("cam", ""), index.parse_ts(q.get("start"))
                if cid and s0 is not None:
                    app.playback.stop(f"pb:{cid}:{int(s0)}")
                return self._json({"ok": True})

            if path == "/api/clip/play/init.mp4" and method in ("GET", "HEAD"):
                return self._pb_init(head, clip_id=self._q().get("id", ""))
            if path == "/api/clip/play/frag.mp4" and method in ("GET", "HEAD"):
                return self._pb_frag(head, clip_id=self._q().get("id", ""))

            m = re.match(r"^/api/live/([\w.-]+)/init\.mp4$", path)
            if m and method in ("GET", "HEAD"):
                return self._live_init(m.group(1), head)

            m = re.match(r"^/api/live/([\w.-]+)/frag\.mp4$", path)
            if m and method in ("GET", "HEAD"):
                return self._live_frag(m.group(1), head)

            m = re.match(r"^/api/live/([\w.-]+)/mjpeg$", path)
            if m and method in ("GET", "HEAD"):
                return self._mjpeg(m.group(1), head)

            m = re.match(r"^/api/snapshot/([\w.-]+)\.jpg$", path)
            if m and method in ("GET", "HEAD"):
                cam = app.camera(m.group(1))
                if not cam:
                    return self._err(404, "no such camera")
                img = live.snapshot(cam, app.cfg)
                if not img:
                    return self._err(504, "camera did not return a frame")
                return self._send(200, img, "image/jpeg",
                                  {"Cache-Control": "no-store"}, head_only=head)

            if re.match(r"^/api/live/[\w.-]+/stop$", path) and method == "POST":
                cid = path.split("/")[3]
                app.live.stop(cid)
                return self._json({"ok": True})

            # ---- downloads ------------------------------------------------ #

            if path == "/api/file" and method in ("GET", "HEAD"):
                rel = self._q().get("p", "")
                p = export.download_segment(app.cfg, rel)
                if not p:
                    return self._err(404, "file not found")
                return self._file(p, head)
            if path == "/api/clip-file" and method in ("GET", "HEAD"):
                p = index.clip_file(Path(app.cfg["storage"]["root"]), self._q().get("id", ""))
                if not p:
                    return self._err(404, "clip not found")
                return self._file(p, head)
            if path == "/api/frame" and method in ("GET", "HEAD"):
                return self._frame(head)

            if path == "/api/shutdown" and method == "POST":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

            return self._err(404, f"no route {method} {path}")
        except ValueError as e:
            return self._err(400, str(e))
        except BrokenPipeError:
            return
        except Exception as e:
            return self._err(500, f"{type(e).__name__}: {e}")

    # -- static ------------------------------------------------------------ #

    def _static(self, path: str, head: bool):
        if path in ("/", "/index.html"):
            path = "/index.html"
        rel = path.lstrip("/")
        if ".." in rel or rel.startswith("/"):
            return self._err(403, "bad path")
        f = (STATIC / rel).resolve()
        try:
            f.relative_to(STATIC.resolve())
        except ValueError:
            return self._err(403, "bad path")
        if not f.is_file():
            # SPA-ish fallback for unknown non-api paths
            f = STATIC / "index.html"
            if not f.is_file():
                return self._err(404, "not found")
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if f.suffix == ".js":
            ctype = "application/javascript"
        body = f.read_bytes()
        return self._send(200, body, ctype, {"Cache-Control": "no-cache"}, head_only=head)

    # -- file serving with Range (video scrubbing) ------------------------- #

    def _file(self, p: Path, head: bool):
        size = p.stat().st_size
        rng = self.headers.get("Range")
        ctype = mimetypes.guess_type(str(p))[0] or "video/x-matroska"
        if p.suffix == ".mkv":
            ctype = "video/x-matroska"
        elif p.suffix == ".mp4":
            ctype = "video/mp4"

        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                s = int(m.group(1)) if m.group(1) else 0
                e = int(m.group(2)) if m.group(2) else size - 1
                s = max(0, min(s, size - 1))
                e = max(s, min(e, size - 1))
                length = e - s + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {s}-{e}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if head:
                    return
                try:
                    with p.open("rb") as fh:
                        fh.seek(s)
                        left = length
                        while left > 0:
                            chunk = fh.read(min(262144, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head:
            return
        try:
            with p.open("rb") as fh:
                shutil.copyfileobj(fh, self.wfile, 262144)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- live + playback endpoints ----------------------------------------- #

    def _stream_session(self, s, head: bool, what: str):
        """Wait for a session's init segment, then serve it."""
        app = self.app
        deadline = time.time() + 25
        while time.time() < deadline and not s.init_ready:
            if s.error and not s.init_ready:
                return self._err(504, s.error)
            if s.eof and not s.init_ready:
                return self._err(404 if what == "playback" else 502,
                                 s.error or f"{what} produced no stream")
            time.sleep(0.15)
        if not s.init_ready:
            return self._err(504, s.error or "timed out waiting for complete stream header")
        s.touch()
        extra = {"X-NanoVMS-Session": s.key,
                 "Access-Control-Allow-Origin": "*"}
        codec = getattr(s, "codec", "")
        if codec:
            extra["X-NanoVMS-Codec"] = codec
            extra["X-NanoVMS-Transcode"] = "1" if s.transcoding else "0"
            extra["X-NanoVMS-Resolution"] = f"{getattr(s, 'width', 0)}x{getattr(s, 'height', 0)}"
        acodec = getattr(s, "audio_codec", "")
        if acodec:
            extra["X-NanoVMS-Audio-Codec"] = acodec
        return self._send(200, s.init, "video/mp4", extra, head_only=head)

    def _forward_fragments(self, s, head: bool, cursor_default: int = 0):
        """Long-poll a session's fragment deque and return the queued bytes."""
        if head:
            return self._send(200, b"", "video/mp4")
        s.touch()
        cursor = _int(self._q().get("c"), cursor_default if cursor_default else s.first_cursor(),
                      0, 2 ** 31)
        buf = bytearray()
        deadline = time.time() + 20
        eof = False
        while time.time() < deadline:
            frag, cursor = s.wait_fragment(cursor, timeout=3.0)
            if frag is None:
                eof = s.eof
                break
            buf += frag
            if len(buf) >= 200_000:
                break
        extra = {"Access-Control-Allow-Origin": "*",
                 "X-NanoVMS-Cursor": str(cursor),
                 "X-NanoVMS-EOF": "1" if eof else "0"}
        if not buf:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            return
        return self._send(200, bytes(buf), "video/mp4", extra)

    def _live_init(self, cid: str, head: bool):
        app = self.app
        s = app.live.acquire(cid)
        if not s:
            return self._err(404, "no such camera")
        try:
            return self._stream_session(s, head, "live")
        finally:
            app.live.release(cid)

    def _live_frag(self, cid: str, head: bool):
        app = self.app
        s = app.live.acquire(cid)
        if not s:
            return self._err(404, "no such camera")
        try:
            return self._forward_fragments(s, head)
        finally:
            app.live.release(cid)

    def _pb_session(self, clip_id: str = ""):
        app = self.app
        if clip_id:
            p = index.clip_file(Path(app.cfg["storage"]["root"]), clip_id)
            if not p:
                return None, None
            return app.playback.open_clip(clip_id, p), f"clip:{clip_id}"
        q = self._q()
        cid = q.get("cam", "")
        start = index.parse_ts(q.get("s"))
        end = index.parse_ts(q.get("e")) or (start + 300) if start is not None else None
        if not app.camera(cid) or start is None:
            return None, None
        return app.playback.open(cid, start, end), f"pb:{cid}:{int(start)}"

    def _pb_init(self, head: bool, clip_id: str = ""):
        s, _ = self._pb_session(clip_id)
        if not s:
            return self._err(404, "no such camera/clip or bad start time")
        s.acquire()
        try:
            return self._stream_session(s, head, "playback")
        finally:
            s.release()

    def _pb_frag(self, head: bool, clip_id: str = ""):
        s, _ = self._pb_session(clip_id)
        if not s:
            return self._err(404, "no such camera/clip or bad start time")
        s.acquire()
        try:
            return self._forward_fragments(s, head)
        finally:
            s.release()

    def _mjpeg(self, cid: str, head: bool):
        app = self.app
        cam = app.camera(cid)
        if not cam:
            return self._err(404, "no such camera")
        lc = app.cfg["live"]
        fps = _int(self._q().get("fps"), int(lc.get("fps", 8)), 1, 25)
        width = _int(self._q().get("w"), 640, 160, 1920)
        boundary = "nanovmsframe"

        with app.live._lock:                      # count as a live viewer
            pass

        ffmpeg = find_ffmpeg(app.cfg["ffmpeg"].get("path", ""))
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
        if cam.get("loop"):
            cmd += ["-stream_loop", "-1", "-re"]
        if cam["url"].lower().startswith("rtsp://"):
            cmd += ["-rtsp_transport", str(cam.get("transport") or
                                           app.cfg["ffmpeg"].get("rtsp_transport", "tcp"))]
        cmd += ["-rw_timeout", str(app.cfg["ffmpeg"].get("rw_timeout_ms", 15_000_000)),
                "-i", cam["url"], "-an",
                "-vf", f"fps={fps},scale='min({width},iw)':-2",
                "-c:v", "mjpeg", "-q:v", str(_int(lc.get("jpeg_quality"), 7, 2, 20)),
                "-f", "mpjpeg", "pipe:1"]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 creationflags=CREATE_NO_WINDOW, bufsize=0,
                                 start_new_session=(not IS_WIN))
        except Exception as e:
            return self._err(500, f"ffmpeg failed: {e}")

        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if head:
            p.kill()
            return
        buf = bytearray()
        try:
            while True:
                chunk = p.stdout.read(65536)      # type: ignore[union-attr]
                if not chunk:
                    break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                    if s < 0 or e < 0:
                        if len(buf) > 4_000_000:
                            buf.clear()
                        break
                    jpg = bytes(buf[s:e + 2])
                    del buf[:e + 2]
                    self.wfile.write(f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                                     f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                p.kill()
            except Exception:
                pass

    def _frame(self, head: bool):
        """Single JPEG frame from a recorded segment at a given time."""
        app = self.app
        q = self._q()
        cid = q.get("cam", "")
        ts = index.parse_ts(q.get("t"))
        if not app.camera(cid) or ts is None:
            return self._err(400, "cam and t required")
        got = index.resolve_segment(Path(app.cfg["storage"]["root"]), cid, ts)
        if not got:
            return self._err(404, "no footage at that time")
        path, off = got
        ffmpeg = find_ffmpeg(app.cfg["ffmpeg"].get("path", ""))
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-ss", f"{off:.3f}", "-i", str(path), "-frames:v", "1",
               "-vf", "scale='min(480,iw)':-2", "-q:v", "6", "-f", "image2", "pipe:1"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=25,
                               creationflags=CREATE_NO_WINDOW)
        except (subprocess.TimeoutExpired, OSError):
            return self._err(504, "frame extraction timed out")
        if r.stdout[:2] != b"\xff\xd8":
            return self._err(500, "no frame produced")
        return self._send(200, r.stdout, "image/jpeg", {"Cache-Control": "max-age=60"},
                          head_only=head)


# --------------------------------------------------------------------------- #

def _probe(url: str, cfg: dict, transport: str = "tcp", timeout: float = 12.0) -> dict:
    """ffprobe a URL and report what a camera would give us."""
    ffmpeg = find_ffmpeg(cfg["ffmpeg"].get("path", ""))
    from .recorder import find_ffprobe
    ffprobe = find_ffprobe(ffmpeg)
    if not ffprobe:
        return {"ok": False, "error": "ffprobe not found next to ffmpeg"}

    synthetic = url.lower().startswith("lavfi:")
    target = url[6:].strip() if synthetic else url
    cmd = [ffprobe, "-v", "error"]
    if synthetic:
        cmd += ["-f", "lavfi"]
    elif target.lower().startswith("rtsp://"):
        cmd += ["-rtsp_transport", transport]
    cmd += ["-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate",
            "-show_entries", "format=format_name,duration,bit_rate",
            "-of", "json", target]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout:.0f}s - check URL/credentials/network"}
    except OSError as e:
        return {"ok": False, "error": f"ffprobe start failed: {e}"}
    elapsed = round(time.time() - t0, 2)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        return {"ok": False, "error": err[-1] if err else "probe failed", "elapsed": elapsed}
    try:
        data = json.loads(r.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "unparseable ffprobe output", "elapsed": elapsed}

    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        return {"ok": False, "error": "no video stream found", "elapsed": elapsed}
    fmt = (data.get("format") or {})
    try:
        vbr = int(fmt.get("bit_rate") or 0)
    except (TypeError, ValueError):
        vbr = 0
    fps = v.get("r_frame_rate") or ""
    fps_txt = ""
    if "/" in fps:
        try:
            n, d = fps.split("/")
            fps_txt = f"{int(n) / max(int(d), 1):.1f}"
        except (ValueError, ZeroDivisionError):
            fps_txt = fps
    daily_gb = round(vbr / 8 / 1024 ** 3 * 86400, 2) if vbr else 0
    return {
        "ok": True,
        "elapsed": elapsed,
        "video": {"codec": v.get("codec_name"), "width": v.get("width"),
                  "height": v.get("height"), "fps": fps_txt},
        "audio": {"codec": a.get("codec_name")} if a else None,
        "container": fmt.get("format_name", ""),
        "bitrate_kbps": round(vbr / 1000) if vbr else 0,
        "est_gb_per_day": daily_gb,
        "browser_playable": v.get("codec_name") in ("h264", "mjpeg"),
        "note": ("live view can stream-copy (no CPU cost)" if v.get("codec_name") in ("h264", "mjpeg")
                 else f"{v.get('codec_name')} is not browser-playable: live view will transcode"),
    }


def serve(cfg: dict, config_path: Path | None = None):
    app = App(cfg, config_path)
    Handler.app = app
    host = cfg["server"]["host"]
    port = int(cfg["server"]["port"])
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    app.start()
    return app, httpd
