"""JagaNVR - recording playback.

Recordings are MKV (browser can't play that), so playback runs ffmpeg once per
requested time window and remuxes to fragmented MP4 with `-c copy` - no
re-encode, no frame decoding. Seeking = start a new session at the target time
(ffmpeg seeks to the nearest keyframe).

Sessions are shared: two people watching the same camera+window share one
ffmpeg process. Sessions die when the last viewer leaves or the stream ends.
"""
from __future__ import annotations

import subprocess
import time
import threading
from pathlib import Path

from . import index
from .recorder import CREATE_NO_WINDOW, find_ffmpeg, find_ffprobe
from .stream import FragmentStream


class PlaybackSession(FragmentStream):
    """Plays [start, end] of a camera's recording, or a single clip file."""

    def __init__(self, key: str, cfg: dict, start: float, end: float,
                 cam_id: str = "", clip_path: Path | None = None):
        super().__init__(key, cfg)
        self.cam_id = cam_id
        # request timestamps and lifecycle method have distinct names
        self.start_at = start
        self.end_at = end
        self.requested_start = start
        self.requested_end = end
        self.clip_path = clip_path
        self.segments: list[str] = []
        self.window_duration = max(0.0, end - start)
        self.source_codec = ""
        self.skipped_segments: list[str] = []
        self.file_start = 0.0
        self._prepared = False
        self._offset = 0.0

    def prepare(self) -> None:
        root = Path(self.cfg["storage"]["root"])
        if self.clip_path is not None:
            if not self.clip_path.is_file():
                self.error = "clip file missing"
                return
            # probe clip codec so browser-incompatible formats transcode
            self.source_codec = self._probe_file(self.clip_path)
            self._prepared = True
            return
        from .export import plan_window
        plan = plan_window(root, self.cam_id, self.start_at, self.end_at, cfg=self.cfg)
        if not plan.get("ok"):
            self.error = plan.get("error", "no footage in that window")
            return
        # A truncated segment (killed muxer, full disk) poisons the concat
        # demuxer: it aborts and the whole window plays nothing. Probe each
        # segment and keep only the ones that actually contain frames.
        usable: list[str] = []
        skipped: list[str] = []
        for seg in plan["segments"]:
            path = root / seg["path"]
            if self._file_usable(path):
                usable.append(seg["path"])
            else:
                skipped.append(seg["path"])
        self.skipped_segments = skipped
        if not usable:
            self.error = ("no playable footage in that window "
                          "(all %d segment(s) are truncated or corrupt)" % len(skipped))
            return
        self.segments = usable
        # offset/duration were computed against the original plan; recompute
        # against the segments we are actually going to feed ffmpeg.
        self._offset = plan["offset"] if not skipped else 0.0
        self.window_duration = plan["duration"]
        # segments written with -use_wallclock_as_timestamps do not start at 0;
        # without this the -t window sits before any real frame
        self.file_start = self._probe_file_start(root / self.segments[0])
        self.source_codec = self._probe_codec()
        self._prepared = True

    def _file_usable(self, path: Path) -> bool:
        """True if the file's tail is intact, i.e. it is not a truncated stub.

        A segment cut short by a killed muxer or a full disk keeps a perfectly
        valid MKV header: ffprobe reports the correct codec AND the correct
        declared duration, so neither header nor nb_frames can detect it. The
        only honest test is to ask ffmpeg to seek near the end and pull a frame
        out - a truncated file reports "File ended prematurely" there, a real
        one returns a frame. That is ~0.15s per segment, cheap enough to run
        per playback request.
        """
        try:
            ffmpeg = find_ffmpeg(self.cfg["ffmpeg"].get("path", ""))
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                 "-ss", "-3", "-i", str(path), "-frames:v", "1",
                 "-f", "null", "-"],
                capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
            if r.returncode != 0:
                return False
            err = r.stderr.decode("utf-8", "replace").lower()
            if "ended prematurely" in err or "invalid data" in err:
                return False
            return True
        except (OSError, subprocess.TimeoutExpired):
            # probe failed outright (locked, mid-write) - do not discard it
            return True

    def _probe_file(self, path: Path) -> str:
        """Probe codec_name of the first video stream in any file."""
        try:
            ffprobe = find_ffprobe(find_ffmpeg(self.cfg["ffmpeg"].get("path", "")))
            if not ffprobe:
                return ""
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of",
                 "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, timeout=8, creationflags=CREATE_NO_WINDOW)
            return r.stdout.decode("utf-8", "replace").strip().splitlines()[0] if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired, IndexError):
            return ""

    def _probe_codec(self) -> str:
        """Probe first selected segment so browser-incompatible codecs transcode."""
        if not self.segments:
            return ""
        root = Path(self.cfg["storage"]["root"])
        return self._probe_file(root / self.segments[0])

    def _probe_file_start(self, path: Path) -> float:
        """First video frame timestamp of a file, in seconds.

        The recorder writes segments with `-use_wallclock_as_timestamps`, so a
        segment does not necessarily begin at 0 - observed values run from 0 up
        to ~98399s. A `-t <window>` measured from zero therefore lands entirely
        before any real data and ffmpeg silently emits just the init segment
        (841 bytes, black screen). Seeding `-ss` with this value puts the
        requested window back inside the data.
        """
        try:
            ffprobe = find_ffprobe(find_ffmpeg(self.cfg["ffmpeg"].get("path", "")))
            if not ffprobe:
                return 0.0
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "packet=pts_time", "-read_intervals", "%+#1",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            if r.returncode != 0:
                return 0.0
            first = r.stdout.decode("utf-8", "replace").strip().splitlines()
            return float(first[0]) if first else 0.0
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return 0.0

    def build_cmd(self) -> list[str]:
        ffmpeg = find_ffmpeg(self.cfg["ffmpeg"].get("path", ""))
        head = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
        seek = 0.0
        dur = self.window_duration

        if self.clip_path is not None:
            inp = ["-i", str(self.clip_path)]
        else:
            root = Path(self.cfg["storage"]["root"])
            if len(self.segments) == 1:
                inp = ["-i", str(root / self.segments[0])]
            else:
                concat = root / index.TMP_DIR / f"pb-{abs(hash(self.key))}.txt"
                concat.parent.mkdir(parents=True, exist_ok=True)
                lines = []
                for rel in self.segments:
                    p = (root / rel).resolve().as_posix().replace("'", "'\\''")
                    lines.append(f"file '{p}'")
                concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
                inp = ["-f", "concat", "-safe", "0", "-i", str(concat)]
            # base the window on the first segment's real start timestamp
            seek = float(getattr(self, "_offset", 0.0)) + self.file_start

        cmd = head + inp
        if seek > 0:
            # -ss before -i: keyframe seek, no decoding of skipped data
            cmd = head + ["-ss", f"{seek:.3f}"] + inp
        if dur:
            cmd += ["-t", f"{dur:.3f}"]

        cmd += ["-map", "0:v:0?", "-an"]
        if self.source_codec and self.source_codec not in ("h264", "avc1", "mjpeg"):
            self.transcoding = True
            fps = max(1, min(int(self.cfg["live"].get("fps", 8)), 25))
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-profile:v", "baseline", "-pix_fmt", "yuv420p",
                    "-g", str(fps * 2), "-bf", "0", "-r", str(fps)]
        else:
            # -g with -c copy: same reason as live.py - a fragment must begin on
            # an IDR or Chrome accepts the data and draws nothing.
            cmd += ["-c:v", "copy", "-g", "50"]
        # no frag_keyframe: it lets a fragment run to the next real keyframe,
        # which on a sparse-GOP recording produces an undecodable fragment and a
        # permanent buffering spinner in the player.
        cmd += ["-flush_packets", "1",
                "-movflags", "empty_moov+default_base_moof+separate_moof",
                "-frag_duration", "1000000",
                "-f", "mp4", "pipe:1"]
        return cmd

    def info(self) -> dict:
        return {
            "key": self.key,
            "cam_id": self.cam_id,
            "start": self.start_at,
            "end": self.end_at,
            "duration": self.window_duration,
            "segments": len(self.segments),
            "skipped_segments": self.skipped_segments,
            "viewers": self.viewers,
            "init_ready": self.init_ready,
            "position_sec": self.position(),
            "eof": self.eof,
            "error": self.error,
        }

    def position(self) -> float:
        """Approximate playhead: fragments delivered x frag_duration."""
        return round(min(self.frag_seq * 1.0, self.window_duration), 1)


class PlaybackManager:
    def __init__(self, cfg: dict, max_sessions: int = 3):
        self.cfg = cfg
        self.max_sessions = max_sessions
        self._sessions: dict[str, PlaybackSession] = {}
        self._lock = threading.RLock()

    def sync(self, cfg: dict | None = None) -> None:
        if cfg:
            self.cfg = cfg

    def _gc(self) -> None:
        for k, s in list(self._sessions.items()):
            if (s.eof and s.viewers <= 0) or (s._stop.is_set() and s.viewers <= 0):
                s.stop()
                self._sessions.pop(k, None)
        # if still over budget, drop the oldest idle-but-open session
        while len(self._sessions) > self.max_sessions:
            idle = [(k, s) for k, s in self._sessions.items() if s.viewers <= 0]
            if not idle:
                break
            k, s = min(idle, key=lambda t: t[1].last_access)
            s.stop()
            self._sessions.pop(k, None)

    def open(self, cam_id: str, start: float, end: float) -> PlaybackSession:
        """Get (or start) a session for this camera + window."""
        with self._lock:
            self._gc()
            key = f"pb:{cam_id}:{int(start)}"
            s = self._sessions.get(key)
            if s is None or (s.eof and s.viewers <= 0):
                s = PlaybackSession(key, self.cfg, start, end, cam_id=cam_id)
                self._sessions[key] = s
                super(PlaybackSession, s).start()
            return s

    def open_clip(self, clip_id: str, path: Path) -> PlaybackSession:
        with self._lock:
            self._gc()
            key = f"clip:{clip_id}"
            s = self._sessions.get(key)
            if s is None or (s.eof and s.viewers <= 0):
                s = PlaybackSession(key, self.cfg, 0.0, 0.0, clip_path=path)
                self._sessions[key] = s
                super(PlaybackSession, s).start()
            return s

    def get(self, key: str) -> PlaybackSession | None:
        with self._lock:
            return self._sessions.get(key)

    def stop(self, key: str) -> None:
        with self._lock:
            s = self._sessions.pop(key, None)
        if s:
            s.stop()

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.stop()

    def status(self) -> list[dict]:
        with self._lock:
            self._gc()
            return [s.info() for s in self._sessions.values()]
