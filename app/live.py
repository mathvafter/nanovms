"""NanoVMS - on-demand live view.

Cost design (why this is built the way it is):
  * Nothing runs until a human opens the stream, and it tears itself down after
    `live.idle_timeout_sec` with zero viewers.
  * `-c:v copy` means ffmpeg only demuxes/remuxes: no decode, no encode. On a
    Celeron N3050 that is a few percent of one core per camera, not 60-150%.
  * Only cameras whose codec browsers cannot decode (HEVC/H.265, MPEG-4 ASP)
    fall back to libx264 ultrafast - flagged per camera in the UI so Jack knows
    which stream is the expensive one.
  * `mjpeg` endpoint is the universal fallback (works in any browser, no MSE).

Transport: fragmented MP4 boxes over HTTP -> browser MSE SourceBuffer.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from .recorder import CREATE_NO_WINDOW, find_ffmpeg, find_ffprobe, build_input
from .stream import FragmentStream

BROWSER_OK = ("h264", "mjpeg", "avc1")
# audio codecs a browser SourceBuffer accepts inside an MP4 stream
BROWSER_AUDIO_OK = ("aac", "opus")


class LiveSession(FragmentStream):
    def __init__(self, cam: dict, cfg: dict):
        super().__init__(f"live:{cam['id']}", cfg)
        self.cam = cam
        self.cam_id = cam["id"]
        self.codec = "h264"
        self.audio_codec = ""
        self.audio_dropped = ""
        self.width = 0
        self.height = 0
        # _av_broken is decided by on_progress() from the muxer's own output
        self._av_broken = bool(cam.get("_audio_unusable"))
        self._restart_pending = False

    # -- prep -------------------------------------------------------------- #

    def prepare(self) -> None:
        if not self.probe():
            self.error = "camera unreachable (ffprobe failed) - check URL, credentials, network"

    def _probe_streams(self, timeout: float) -> dict:
        """Probe video + audio stream properties in one ffprobe pass.

        Parses JSON, not `default=nw=1` lines: ffprobe emits fields grouped per
        stream but order within a stream is not guaranteed, so line parsing put
        `aac` in the video slot and made the UI think live was audio-only.
        """
        ffmpeg = find_ffmpeg(self.cfg["ffmpeg"].get("path", ""))
        ffprobe = find_ffprobe(ffmpeg)
        out: dict = {}
        if not ffprobe:
            return out
        url = self.cam["url"]
        if url.lower().startswith("lavfi:"):
            cmd = [ffprobe, "-v", "error", "-f", "lavfi"]
        else:
            cmd = [ffprobe, "-v", "error"]
            if url.lower().startswith(("rtsp://", "rtsps://")):
                cmd += ["-rtsp_transport", self._transport()]
        cmd += ["-show_entries", "stream=codec_type,codec_name,width,height",
                "-of", "json",
                url[6:].strip() if url.lower().startswith("lavfi:") else url]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               creationflags=CREATE_NO_WINDOW)
        except (subprocess.TimeoutExpired, OSError):
            return out
        if r.returncode != 0:
            return out
        try:
            data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        except (ValueError, TypeError):
            return out
        for s in data.get("streams") or []:
            ctype = s.get("codec_type")
            if not ctype or ctype in out:
                continue                    # first stream of each type wins
            info = {"codec_name": s.get("codec_name") or ""}
            if ctype == "video":
                info["width"] = int(s.get("width") or 0)
                info["height"] = int(s.get("height") or 0)
            out[ctype] = info
        return out

    def probe(self, timeout: float = 12.0) -> bool:
        info = self._probe_streams(timeout)
        v = info.get("video") or {}
        a = info.get("audio") or {}
        self.codec = v.get("codec_name") or self.codec
        self.audio_codec = a.get("codec_name") or ""
        self.width = v.get("width", 0)
        self.height = v.get("height", 0)
        return bool(self.width)

    def _av_mux_is_broken(self) -> bool:
        """True when muxing audio alongside video yields no video at all.

        The EZVIZ C6N is the case in point: its AAC track's PTS runs ~34h
        ahead of the video's, so ffmpeg's interleaver keeps emitting audio
        fragments (track 2) and produces no video fragments (track 1) - a live
        stream with sound and a black picture. Measured on cam2, 37 KB of
        fragments contained 35,621 B of audio and 0 B of video.

        Note: the primary fix for cam2 is NOT this detector. The camera's 1080p
        main stream is simply too heavy for the Pi target, so cam2 transcodes
        (live_passthrough: false), which also sidesteps the mux-starve path.
        This detector stays guarded: it watches the muxer's own output without
        opening an extra RTSP connection, so it is safe to leave active.
        """
        return self._av_broken

    def on_progress(self, seen_bytes: int, media: dict[int, int]) -> None:
        if self._av_broken or self.cam.get("_audio_unusable"):
            return
        if not self.audio_codec or self.viewers < 1:
            return
        if seen_bytes < 150_000:
            return
        # a fair sample: an A/V stream legitimately opens with audio-only
        # fragments, so only judge once enough VIDEO could have arrived. A
        # starved one still shows nothing by the time the buffer is full.
        if media.get(1, 0) > 32_768:
            self.cam["_audio_ok"] = True          # video is flowing; stop watching
            return
        if sum(media.values()) < 200_000:
            return
        self.cam["_audio_unusable"] = True
        self._av_broken = True
        self.audio_dropped = ("camera audio is unusable (A/V mux starves video) "
                              "- serving video only")
        self._restart_pending = True

    def _transport(self) -> str:
        return str(self.cam.get("transport") or
                   self.cfg["ffmpeg"].get("rtsp_transport") or "tcp")

    # -- command ----------------------------------------------------------- #

    def build_cmd(self) -> list[str]:
        ffmpeg = find_ffmpeg(self.cfg["ffmpeg"].get("path", ""))
        lc = self.cfg["live"]
        cam = self.cam
        want_audio = bool(cam.get("live_audio", True))
        if want_audio and self._av_mux_is_broken():
            want_audio = False
            self.audio_codec = ""
            self.audio_dropped = ("camera audio is unusable (A/V mux produces no "
                                  "frames) - video only")
        passthrough = bool(cam.get("live_passthrough", True)) and self.codec in BROWSER_OK
        if not self.width and not self.probe():
            passthrough = False
        self.transcoding = not passthrough

        # Some cameras (the EZVIZ C6N) push absolute wallclock PTS on the
        # wire: video samples land ~34h into the timeline while the muxer
        # writes an init whose start_time is 0. MSE then buffers happily and
        # draws nothing, because every sample sits far past the element's
        # playback head. -copyts keeps ffmpeg's original stamps; the mp4
        # muxer only rebases them when they are absent.
        if cam.get("live_rebase_ts", True):
            cmd_ts = ["-fflags", "+genpts", "-start_at_zero"]
        else:
            cmd_ts = []

        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
        cmd += build_input(cam["url"], loop=bool(cam.get("loop")),
                           transport=self._transport(),
                           rw_timeout_ms=int(self.cfg["ffmpeg"].get("rw_timeout_ms", 15_000_000)),
                           live_source=True)
        cmd += ["-map", "0:v:0"]
        cmd += cmd_ts

        # ---- audio: include it only when a browser can actually decode it ----
        if not self.audio_dropped:
            self.audio_dropped = ""
        keep_audio = False
        if want_audio and self.audio_codec:
            if self.audio_codec in BROWSER_AUDIO_OK:
                keep_audio = True
            elif cam.get("live_audio_transcode", True):
                keep_audio = True                  # re-encode to AAC below
            else:
                self.audio_dropped = f"{self.audio_codec} (not browser-playable)"
        elif want_audio:
            self.audio_dropped = "camera sends no audio"
        if keep_audio:
            cmd += ["-map", "0:a:0?"]
        else:
            cmd += ["-an"]

        if passthrough:
            # -g forces an IDR at least this often even with -c copy, so every
            # fragment boundary is independently decodable. Without it FFmpeg
            # keeps the source GOP (a 1080p EZVIZ sub-stream can have keyframes
            # tens of seconds apart), and combined with -movflags frag_keyframe
            # that yields fragments that contain no keyframe at all. Chrome then
            # accepts the appendBuffer, reports readyState 4 with the right
            # resolution, and shows a permanently black picture with audio.
            # Re-encoding is not needed: -force_key_frames is a container-level
            # hint the bitstream honours at the next keyframe opportunity, and
            # -g is the cheap, always-honoured equivalent for stream copy.
            gop = int(lc.get("gop_sec", 2) or 2) * 25
            cmd += ["-c:v", "copy", "-g", str(max(1, gop))]
        else:
            fps = max(1, min(int(lc.get("fps", 8)), 25))
            # GOP must come from live.gop_sec, NOT a hardcoded fps*2. MSE needs
            # every fragment to begin on an IDR, so the GOP must divide the
            # fragment duration: a 2s GOP against 1s fragments makes every other
            # fragment start mid-GOP, and the browser freezes on one frame with
            # readyState 4 and no error. Clamp to frag_ms so it can never exceed
            # one fragment.
            gop_sec = max(0.1, float(lc.get("gop_sec", 2) or 2))
            frag_ms = max(100, int(lc.get("frag_ms", 1000) or 1000))
            gop_ms = min(int(round(gop_sec * 1000)), frag_ms)
            gop = max(1, int(round(gop_ms * fps / 1000.0)))
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-profile:v", "baseline", "-pix_fmt", "yuv420p",
                    "-g", str(gop), "-bf", "0", "-r", str(fps)]
            if lc.get("max_width"):
                cmd += ["-vf", f"scale='min({int(lc['max_width'])},iw)':-2"]

        if keep_audio:
            if self.audio_codec in BROWSER_AUDIO_OK:
                cmd += ["-c:a", "copy"]
            else:
                # browsers accept AAC/Opus in MP4; G.711 must be re-encoded
                cmd += ["-c:a", "aac", "-b:a", "64k", "-ac", "1"]

        # frag_duration alone (no frag_keyframe) cuts every fragment exactly on
        # the -g interval, so each one starts on an IDR. frag_keyframe would let
        # FFmpeg stretch a fragment to the next real keyframe, which is what
        # produced undecodable fragments on sparse-GOP cameras.
        cmd += ["-flush_packets", "1",
                "-movflags", "empty_moov+default_base_moof+separate_moof",
                "-frag_duration", str(int(lc.get("frag_ms", 1000) or 1000) * 1000),
                "-f", "mp4", "pipe:1"]
        return cmd

    def info(self) -> dict:
        err = self.error
        # A camera that caps concurrent RTSP clients (both of Jack's do, at 2)
        # refuses the live connection while its recorder already holds one, so
        # ffmpeg starts, gets no data, and dies silently. Every symptom the UI
        # can show is identical to a dead camera, so name the likely cause
        # instead of leaving a permanently black tile with no explanation.
        if not err and self._stop.is_set() and not self.init_ready and self.viewers:
            err = ("no video from camera - it may be at its connection limit "
                   "(a recorder is using the other slot)")
        return {
            "cam_id": self.cam_id,
            "active": bool(self.init_ready) and not self.eof and not self._stop.is_set(),
            "codec": self.codec,
            "audio_codec": self.audio_codec,
            "audio_dropped": self.audio_dropped,
            "width": self.width,
            "height": self.height,
            "transcoding": self.transcoding,
            "viewers": self.viewers,
            "init_ready": self.init_ready,
            "uptime_sec": int(time.time() - self.started_at) if self.started_at else 0,
            "error": err,
        }


class LiveManager:
    """Keeps at most `live.max_concurrent` live sessions alive."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._sessions: dict[str, LiveSession] = {}
        self._lock = threading.RLock()

    def sync(self, cfg: dict | None = None) -> None:
        if cfg:
            self.cfg = cfg

    def acquire(self, cam_id: str) -> LiveSession | None:
        cam = next((c for c in self.cfg["cameras"]
                    if c["id"] == cam_id and c.get("enabled") and c.get("url")), None)
        if not cam:
            return None
        with self._lock:
            self._evict_dead()
            s = self._sessions.get(cam_id)
            # a session that saw the mux starve its video asks to be rebuilt
            # without audio; do it here, off the reader thread
            if s is not None and s._restart_pending:
                s.stop()
                self._sessions.pop(cam_id, None)
                s = None
            if s is None or (s.eof and not s.info()["active"]):
                self._enforce_limit(cam_id)
                s = LiveSession(cam, self.cfg)
                if cam.get("_audio_unusable"):
                    s.audio_dropped = ("camera audio is unusable (A/V mux starves "
                                       "video) - serving video only")
                self._sessions[cam_id] = s
                s.acquire()
                s.start()
            else:
                s.acquire()
            return s

    def _evict_dead(self) -> None:
        for cid, s in list(self._sessions.items()):
            if s.eof and s.viewers <= 0:
                s.stop()
                self._sessions.pop(cid, None)

    def _enforce_limit(self, incoming: str) -> None:
        """Close the idlest idle session when we are at the concurrency cap."""
        limit = max(1, int(self.cfg["live"].get("max_concurrent", 2)))
        sessions = [(cid, s) for cid, s in self._sessions.items() if cid != incoming]
        active = [t for t in sessions if t[1].viewers > 0]
        if len(active) < limit:
            return
        victim = min(active, key=lambda t: t[1].last_access)
        victim[1].stop()
        self._sessions.pop(victim[0], None)

    def release(self, cam_id: str) -> None:
        with self._lock:
            s = self._sessions.get(cam_id)
            if s:
                s.release()

    def stop(self, cam_id: str) -> None:
        with self._lock:
            s = self._sessions.pop(cam_id, None)
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
            self._evict_dead()
            return [s.info() for s in self._sessions.values()]


# --------------------------------------------------------------------------- #
# one-shot snapshot: cheapest possible "live" (single frame, no session)
# --------------------------------------------------------------------------- #

def snapshot(cam: dict, cfg: dict, timeout: float = 15.0) -> bytes | None:
    ffmpeg = find_ffmpeg(cfg["ffmpeg"].get("path", ""))
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    cmd += build_input(cam["url"], loop=bool(cam.get("loop")),
                       transport=str(cam.get("transport") or
                                     cfg["ffmpeg"].get("rtsp_transport", "tcp")),
                       rw_timeout_ms=int(cfg["ffmpeg"].get("rw_timeout_ms", 15_000_000)),
                       live_source=True)
    cmd += ["-frames:v", "1", "-q:v", "4", "-f", "image2", "pipe:1"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout if r.stdout[:2] == b"\xff\xd8" else None
