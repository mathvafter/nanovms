"""NanoVMS - one recorder thread per camera, supervising an ffmpeg segment muxer.

Resource strategy (the whole point):
  * `-c copy` stream copy: ffmpeg does NO transcoding, so CPU stays near zero.
  * One ffmpeg process per *recording* camera only. No worker per stream type,
    no browser-side analysis, no motion engine.
  * `-progress pipe:1` drives a stall watchdog: a frozen RTSP stream is killed
    and respawned instead of hanging forever holding buffers.
  * Exponential backoff with jitter so a dead camera cannot spin the CPU.
  * Live view is NOT started here - see live.py, on-demand only.
"""
from __future__ import annotations

import collections
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

FNAME_FMT = "%Y-%m-%d_%H-%M-%S"
FNAME_RE_LEN = 19  # len("2026-01-01_00-00-00")


# --------------------------------------------------------------------------- #
# ffmpeg discovery
# --------------------------------------------------------------------------- #

_ffmpeg_cache: dict[str, str] = {}


def find_ffmpeg(hint: str = "") -> str:
    """Resolve ffmpeg. `hint` may be an explicit path or empty for autodetect."""
    key = hint or "__auto__"
    if key in _ffmpeg_cache:
        return _ffmpeg_cache[key]
    cands: list[str] = []
    if hint:
        cands.append(hint)
    cands.append(shutil.which("ffmpeg") or "")
    cands += [
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
        str(Path.home() / "bin" / "ffmpeg"),
        "C:/ffmpeg/bin/ffmpeg.exe",
        "C:/Program Files/ffmpeg/bin/ffmpeg.exe",
    ]
    for c in cands:
        if not c:
            continue
        p = shutil.which(c) or (c if os.path.isfile(c) else None)
        if p:
            _ffmpeg_cache[key] = p
            return p
    raise FileNotFoundError(
        "ffmpeg not found. Install it (apt install ffmpeg / winget install Gyan.FFmpeg) "
        "or set ffmpeg.path in config.json"
    )


def find_ffprobe(ffmpeg_path: str) -> str:
    p = Path(ffmpeg_path)
    cand = p.with_name("ffprobe" + (".exe" if IS_WIN else ""))
    if cand.is_file():
        return str(cand)
    return shutil.which("ffprobe") or ""


NET_PROTO = ("rtsp://", "rtsps://", "http://", "https://", "rtmp://", "udp://",
             "tcp://", "srt://", "rtp://")


def redact_rtsp_credentials(text: str) -> str:
    """Remove user:password pairs from FFmpeg error text before exposing it."""
    if not text:
        return ""
    return re.sub(r"(rtsps?://[^:/@\s]+:)[^@\s]+@", r"\1[REDACTED]@", text,
                  flags=re.IGNORECASE)


def build_input(url: str, loop: bool = False, transport: str = "tcp",
                rw_timeout_ms: int = 15_000_000, live_source: bool = True) -> list[str]:
    """ffmpeg input arguments for a camera URL.

    Supports a synthetic `lavfi:<filtergraph>` source so the whole pipeline can
    be exercised without a camera (see config.example.json).
    `-timeout` is only valid on network protocols, so it is added for those
    only - passing it to a plain file input makes ffmpeg refuse to start.
    """
    u = (url or "").strip()
    low = u.lower()

    if low.startswith("lavfi:"):
        src = u[6:].strip()
        args = ["-f", "lavfi"]
        if live_source:
            args += ["-re"]
        args += ["-i", src]
        return args

    args: list[str] = []
    if loop:
        args += ["-stream_loop", "-1", "-re"]
    if low.startswith(("rtsp://", "rtsps://")):
        args += ["-rtsp_transport", transport or "tcp"]
    if any(low.startswith(p) for p in NET_PROTO):
        # FFmpeg 9 RTSP demuxer uses -timeout. -rw_timeout is rejected as
        # "Option not found" on current builds, preventing every camera opening.
        args += ["-timeout", str(int(rw_timeout_ms))]
    if live_source and not loop and "://" not in u:
        # local file used as a stand-in source: throttle to real time
        args += ["-re"]
    args += ["-i", u]
    return args


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

@dataclass
class RecorderState:
    cam_id: str
    running: bool = False
    state: str = "stopped"      # stopped | starting | recording | backoff | error
    pid: int = 0
    started_at: float = 0.0
    last_progress: float = 0.0
    restarts: int = 0
    last_error: str = ""
    bytes_estimate: float = 0.0
    log: collections.deque = field(default_factory=lambda: collections.deque(maxlen=120))

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "cam_id": self.cam_id,
            "running": self.running,
            "state": self.state,
            "pid": self.pid,
            "uptime_sec": int(now - self.started_at) if self.running and self.started_at else 0,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "log": list(self.log)[-40:],
        }


# --------------------------------------------------------------------------- #
# recorder
# --------------------------------------------------------------------------- #

class Recorder:
    """Supervises one ffmpeg process that writes time-stamped segments."""

    def __init__(self, cam: dict, cfg: dict, stall_cb=None, on_segment=None):
        self.cam = cam
        self.cfg = cfg
        self.cam_id = cam["id"]
        self.out_dir = Path(cfg["storage"]["root"]) / self.cam_id
        self.st = RecorderState(cam_id=self.cam_id)
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=60)
        self._on_segment = on_segment or stall_cb  # called(cam_id) whenever new files appear
        self._known_files: set[str] = set()

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"rec-{self.cam_id}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 8.0) -> None:
        self._stop.set()
        self._kill_proc(graceful=True)      # close the current segment cleanly
        if self._thread:
            self._thread.join(timeout=timeout)
        self.st.running = False
        self.st.state = "stopped"
        self.st.pid = 0

    def restart(self) -> None:
        self.stop()
        self.start()

    def restart_with(self, cfg: dict) -> None:
        """Re-read config and restart ffmpeg at the updated storage path."""
        self.stop()
        self.cfg = cfg
        self.out_dir = Path(cfg["storage"]["root"]) / self.cam_id
        self.start()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- internals ---------------------------------------------------------- #

    def _log(self, msg: str) -> None:
        self.st.log.append(time.strftime("%H:%M:%S ") + msg)

    def _kill_proc(self, graceful: bool = False) -> None:
        """Stop the ffmpeg muxer.

        `graceful` matters: a hard kill (taskkill /F, SIGKILL) leaves the
        in-progress segment with no MKV trailer, so it reads as corrupt
        forever after. Stopping the server or restarting a recorder must not
        cost the user a segment.

        On Windows `Popen.terminate()` maps to TerminateProcess, which is just
        as brutal as a kill and skips ffmpeg's cleanup, so we instead close
        ffmpeg's stdin and give it a moment to notice EOF and close the file
        itself - the same trick `q` over stdin. Only if that fails do we
        hard-kill.
        """
        p = self._proc
        if not p or p.poll() is not None:
            return
        if graceful:
            try:
                if p.stdin is not None:
                    # ffmpeg treats 'q' on stdin as "finish and close the
                    # current output cleanly" - this is what finalises the MKV
                    # trailer. Closing the pipe alone is honoured less
                    # consistently across builds, so send q then close.
                    try:
                        p.stdin.write(b"q")
                        p.stdin.flush()
                    except (OSError, ValueError):
                        pass
                    p.stdin.close()
                else:
                    p.terminate()
                p.wait(timeout=10)
                self._proc = None
                return
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass                         # fell through to the hard kill
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=10)
            else:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass
        self._proc = None

    def _build_cmd(self, ffmpeg: str) -> list[str]:
        cam, cfg = self.cam, self.cfg
        fcfg = cfg["ffmpeg"]
        seg_min = int(cam.get("segment_minutes") or 0) or int(cfg["storage"]["segment_minutes"])

        # no -nostdin: the graceful-shutdown path writes "q" to ffmpeg's stdin
        # so it finalises the segment it is writing. -nostdin would ignore it
        # and force every stop to be a hard kill, truncating the segment.
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning",
               "-progress", "pipe:1", "-nostats"]

        cmd += build_input(cam["url"], loop=bool(cam.get("loop")),
                           transport=str(cam.get("transport") or fcfg.get("rtsp_transport") or "tcp"),
                           rw_timeout_ms=int(fcfg.get("rw_timeout_ms", 15_000_000)),
                           live_source=True)
        if not cam.get("loop"):
            cmd += ["-use_wallclock_as_timestamps", "1",
                    "-analyzeduration", str(int(fcfg.get("analyze_duration", 2)) * 1_000_000),
                    "-probesize", str(fcfg.get("probe_size", 2_000_000))]

        cmd += ["-map", "0:v:0"]
        if cam.get("audio"):
            cmd += ["-map", "0:a:0?"]
        if cam.get("encode"):
            fps = max(1, min(int(cam.get("encode_fps") or 10), 30))
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                    "-pix_fmt", "yuv420p", "-g", str(fps * 2), "-r", str(fps),
                    "-c:a", "aac", "-b:a", "64k"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-sn", "-dn"]

        self.out_dir.mkdir(parents=True, exist_ok=True)
        cmd += [
            "-f", "segment",
            "-segment_time", str(int(seg_min * 60)),
            "-segment_format", "matroska",
            "-reset_timestamps", "1",
            "-strftime", "1",
            str(self.out_dir / f"{FNAME_FMT}.mkv"),
        ]
        return cmd

    def _pump_stdout(self, p: subprocess.Popen) -> None:
        """Parse -progress key=value lines to feed the stall watchdog."""
        try:
            for raw in p.stdout:  # type: ignore[union-attr]
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                key, _, val = line.partition("=")
                if key == "out_time_ms":
                    try:
                        self.st.bytes_estimate = max(self.st.bytes_estimate, int(val) / 1e6)
                    except ValueError:
                        pass
                if key in ("out_time_ms", "out_time_us", "progress", "frame", "packet"):
                    self.st.last_progress = time.time()
                if key == "progress" and val in ("end", "continue"):
                    self.st.state = "recording" if self.st.running else self.st.state
        except Exception:
            pass

    def _pump_stderr(self, p: subprocess.Popen) -> None:
        try:
            for raw in p.stderr:  # type: ignore[union-attr]
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr_tail.append(line)
        except Exception:
            pass

    def _scan_new_segments(self) -> None:
        try:
            with os.scandir(self.out_dir) as it:
                for e in it:
                    if e.name.endswith(".mkv") and e.name not in self._known_files:
                        # only count files that have stopped growing (closed segment)
                        try:
                            if time.time() - e.stat().st_mtime > 3:
                                self._known_files.add(e.name)
                                self._on_segment and self._on_segment(self.cam_id)
                        except OSError:
                            pass
        except FileNotFoundError:
            pass

    def _run(self) -> None:
        fcfg = self.cfg["ffmpeg"]
        backoff_min = float(fcfg.get("restart_backoff_min", 2))
        backoff_max = float(fcfg.get("restart_backoff_max", 60))
        stall_timeout = float(fcfg.get("stall_timeout_sec", 30))
        backoff = backoff_min

        try:
            ffmpeg = find_ffmpeg(fcfg.get("path", ""))
        except FileNotFoundError as e:
            self.st.state = "error"
            self.st.last_error = str(e)
            self._log(f"FATAL {e}")
            return

        self.st.running = True
        first = True

        while not self._stop.is_set():
            if not first:
                self.st.state = "backoff"
                delay = backoff + random.uniform(0, backoff * 0.3)
                self._log(f"restart in {delay:.1f}s (restart #{self.st.restarts})")
                if self._stop.wait(delay):
                    break
            first = False

            if not shutil.disk_usage(str(self.out_dir.parent)).free and False:
                pass  # placeholder: storage guard lives in index.sweep()

            cmd = self._build_cmd(ffmpeg)
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    # a real pipe, not DEVNULL: closing it is how we ask ffmpeg
                    # to shut down cleanly and finalise the current segment
                    stdin=subprocess.PIPE,
                    creationflags=CREATE_NO_WINDOW,
                    start_new_session=(not IS_WIN),
                )
            except Exception as e:
                self.st.state = "error"
                self.st.last_error = f"spawn failed: {e}"
                self._log(self.st.last_error)
                backoff = min(backoff * 2, backoff_max)
                continue

            self.st.pid = self._proc.pid
            self.st.started_at = time.time()
            self.st.last_progress = time.time()
            self.st.state = "recording"
            self.st.last_error = ""
            self._stderr_tail.clear()
            self._log(f"ffmpeg pid={self._proc.pid} seg={self.cam.get('segment_minutes') or self.cfg['storage']['segment_minutes']}m")

            t_out = threading.Thread(target=self._pump_stdout, args=(self._proc,), daemon=True)
            t_err = threading.Thread(target=self._pump_stderr, args=(self._proc,), daemon=True)
            t_out.start()
            t_err.start()

            rc = None
            while not self._stop.is_set():
                rc = self._proc.poll()
                if rc is not None:
                    break
                idle = time.time() - self.st.last_progress
                if idle > stall_timeout:
                    self.st.last_error = f"stalled {int(idle)}s - no data from stream"
                    self._log(self.st.last_error)
                    self._kill_proc()
                    rc = -1
                    break
                self._scan_new_segments()
                time.sleep(0.5)

            if self._stop.is_set():
                self._kill_proc(graceful=True)   # clean shutdown: close the file
                break

            self._kill_proc()
            self._scan_new_segments()

            uptime = time.time() - self.st.started_at
            raw_err = " / ".join(list(self._stderr_tail)[-2:]) if self._stderr_tail else f"exit={rc}"
            err = redact_rtsp_credentials(raw_err)
            self.st.last_error = err[:400]
            self.st.restarts += 1
            self._log(f"exit rc={rc} after {uptime:.0f}s: {err[:200]}")

            if uptime > 60:
                backoff = backoff_min      # healthy run -> reset backoff
            else:
                backoff = min(max(backoff * 2, backoff_min), backoff_max)

        self.st.running = False
        self.st.state = "stopped"
        self.st.pid = 0


# --------------------------------------------------------------------------- #
# manager
# --------------------------------------------------------------------------- #

class RecorderManager:
    def __init__(self, cfg: dict, on_segment=None):
        self.cfg = cfg
        self._recs: dict[str, Recorder] = {}
        self._lock = threading.RLock()
        self._on_segment = on_segment

    def sync(self, cfg: dict | None = None) -> None:
        """Start recorders for wanted cameras, stop the rest. Safe to call often."""
        if cfg:
            self.cfg = cfg
        with self._lock:
            want: dict[str, dict] = {
                c["id"]: c for c in self.cfg["cameras"]
                if c.get("enabled") and c.get("record") and c.get("url")
            }
            for cid in list(self._recs):
                if cid not in want:
                    self._recs.pop(cid).stop()
            for cid, cam in want.items():
                rec = self._recs.get(cid)
                if rec is None:
                    self._recs[cid] = Recorder(cam, self.cfg, on_segment=self._on_segment)
                    self._recs[cid].start()
                else:
                    if rec.cam != cam:          # config changed -> restart with new args
                        rec.cam = cam
                        rec.restart()
                    elif rec.cfg.get("storage", {}).get("root") != self.cfg.get("storage", {}).get("root"):
                        rec.restart_with(self.cfg)  # storage.root changed -> restart at new path
                    elif not rec.alive:
                        rec.start()

    def stop_all(self) -> None:
        with self._lock:
            for r in self._recs.values():
                r.stop()
            self._recs.clear()

    def stop(self, cam_id: str) -> None:
        with self._lock:
            r = self._recs.pop(cam_id, None)
        if r:
            r.stop()

    def start(self, cam_id: str) -> bool:
        cam = next((c for c in self.cfg["cameras"] if c["id"] == cam_id), None)
        if not cam or not cam.get("url"):
            return False
        with self._lock:
            if cam_id in self._recs and self._recs[cam_id].alive:
                return True
            self._recs[cam_id] = Recorder(cam, self.cfg, on_segment=self._on_segment)
            self._recs[cam_id].start()
        return True

    def restart(self, cam_id: str) -> bool:
        with self._lock:
            r = self._recs.get(cam_id)
        if r:
            r.restart()
            return True
        return self.start(cam_id)

    def stop_recording(self, cam_id: str) -> None:
        """Stop the process but leave the camera eligible for sync() again."""
        self.stop(cam_id)

    def status(self) -> list[dict]:
        with self._lock:
            recs = dict(self._recs)
        out = []
        for cam in self.cfg["cameras"]:
            cid = cam["id"]
            if cid in recs:
                out.append(recs[cid].st.snapshot())
            else:
                out.append(RecorderState(cam_id=cid).snapshot())
        return out

    def recording_count(self) -> int:
        """How many recorder threads are actually supervising ffmpeg."""
        with self._lock:
            return sum(1 for r in self._recs.values() if r.alive and r.st.running)

    def recorder(self, cam_id: str) -> Recorder | None:
        with self._lock:
            return self._recs.get(cam_id)
