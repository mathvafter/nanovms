"""NanoVMS - shared fragmented-MP4 streamer.

Both live view and recording playback need the same thing: run ffmpeg once,
chop its stdout into MP4 fragments, and hand fragments to any number of
HTTP/JS clients. This base class holds that machinery so live.py and
playback.py only supply a command line.

Cost model: with `-c:v copy` ffmpeg never decodes or encodes, so a stream costs
roughly one demux + one mux per camera - a few percent of one core on a Celeron.
"""
from __future__ import annotations

import collections
import os
import subprocess
import threading
import time

from .recorder import CREATE_NO_WINDOW, IS_WIN

BOX_HEADER = 8
INIT_TYPES = ("ftyp", "moov", "free", "skip", "styp")
PAYLOAD_TYPES = ("mdat", "free", "skip", "styp")


def split_boxes(buf: bytearray) -> list[tuple[str, bytes]]:
    """Pop every COMPLETE leading top-level MP4 box out of `buf`.

    Returns [(type, raw_bytes), ...]; an incomplete tail stays in `buf` for the
    next read. The raw bytes travel with the box, so callers never re-slice a
    buffer that has already been consumed.
    """
    out: list[tuple[str, bytes]] = []
    pos = 0
    n = len(buf)
    while True:
        if n - pos < BOX_HEADER:
            break
        size = int.from_bytes(buf[pos:pos + 4], "big")
        btype = bytes(buf[pos + 4:pos + 8]).decode("latin-1", "replace")
        header = BOX_HEADER
        if size == 1:
            if n - pos < 16:
                break
            size = int.from_bytes(buf[pos + 8:pos + 16], "big")
            header = 16
        elif size == 0:
            break                       # extends to EOF: not valid mid-stream
        if size < header:
            pos += 1                    # corrupt size field: resync one byte
            continue
        if n - pos < size:
            break                       # need more data
        out.append((btype, bytes(buf[pos:pos + size])))
        pos += size
    if pos:
        del buf[:pos]
    return out


def moof_track_id(raw: bytes) -> int:
    """track_ID out of a moof's tfhd box; 0 when there isn't one.

    Needed because an mdat's bytes say nothing about WHICH track they belong
    to: a stream that muxes a stalled audio track alongside video still grows
    its mdats steadily, so counting payload alone cannot tell "audio is starving
    video out" from "everything is fine".
    """
    p = raw.find(b"tfhd", 4)
    if p < 0 or p + 12 > len(raw):
        return 0
    return int.from_bytes(raw[p + 8:p + 12], "big")


class FragmentStream:
    """One ffmpeg process -> an init segment + a rolling deque of fragments."""

    max_fragments = 8

    def __init__(self, key: str, cfg: dict):
        self.key = key
        self.cfg = cfg
        self.init: bytes = b""
        self._init_boxes: set[str] = set()
        self.fragments: collections.deque[bytes] = collections.deque(maxlen=self.max_fragments)
        self.frag_seq = 0
        self.cond = threading.Condition()
        self.viewers = 0
        self.last_access = time.time()
        self.started_at = 0.0
        self.ended_at = 0.0
        self.error = ""
        self.eof = False
        self.transcoding = False
        self._proc: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._started = threading.Event()

    # -- to be provided by subclasses -------------------------------------- #

    def build_cmd(self) -> list[str]:
        raise NotImplementedError

    def prepare(self) -> None:
        """Optional one-shot prep (e.g. probing the camera) before spawning."""

    def info(self) -> dict:
        return {"key": self.key}

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        if self._started.is_set():
            return
        try:
            self.prepare()
        except Exception as e:
            self.error = f"prepare failed: {e}"
        self._started.set()
        self.started_at = time.time()
        self._threads = [
            threading.Thread(target=self._reader, name=f"strm-{self.key}", daemon=True),
            threading.Thread(target=self._reaper, name=f"reap-{self.key}", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        with self.cond:
            self.cond.notify_all()
        # The reader thread calls Popen, so stop() can land while a spawn is still
        # in flight and self._proc is still None. Give the reader a moment to
        # publish the process, then kill it - otherwise ffmpeg starts AFTER the
        # stop flag is set and runs forever, orphaned, still holding one of the
        # camera's 2 RTSP slots. Each camera allows only 2 concurrent clients, so
        # that leak starves the next live view and looks exactly like a dead
        # camera.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            p = self._proc
            if p is None:
                if not any(t.is_alive() for t in self._threads):
                    break
                time.sleep(0.05)
                continue
            if p.poll() is not None:
                break
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=1.0)
            except Exception:
                pass
            break
        p = self._proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
        self._proc = None

    @property
    def alive(self) -> bool:
        return bool(not self._stop.is_set() and
                    (self._proc is None or self._proc.poll() is None))

    def touch(self) -> None:
        """Record HUMAN interest in this stream.

        Only ever call this from a consumer path (an HTTP request pulling
        fragments, acquire/release). The ffmpeg reader must NOT: a live
        transcode emits data forever, so touching on every read made
        `last_access` permanently fresh and the idle reaper could never fire -
        a transcoding session with zero viewers then held its RTSP slot (and
        its CPU) until the process was killed. Each camera allows only 2
        concurrent RTSP clients, so a leaked live session starves the next
        one and looks exactly like a dead camera.
        """
        self.last_access = time.time()

    # -- reader ------------------------------------------------------------ #

    def _reader(self) -> None:
        try:
            cmd = self.build_cmd()
        except Exception as e:
            self.error = f"command build failed: {e}"
            self._finish()
            return
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                bufsize=0, start_new_session=(not IS_WIN))
        except Exception as e:
            self.error = f"ffmpeg spawn failed: {e}"
            self._finish()
            return

        # stop() may have landed while the command was being built. Now that the
        # process exists, honour it immediately instead of streaming into a void.
        if self._stop.is_set():
            try:
                self._proc.kill()
            except Exception:
                pass
            self._finish()
            return

        err_tail: collections.deque[str] = collections.deque(maxlen=20)

        def drain():
            try:
                for raw in self._proc.stderr:      # type: ignore[union-attr]
                    s = raw.decode("utf-8", "replace").strip()
                    if s:
                        err_tail.append(s)
                        self.touch()
            except Exception:
                pass

        threading.Thread(target=drain, daemon=True).start()

        buf = bytearray()
        pending: list[bytes] = []
        pending_tid = 0
        seen_bytes = 0
        media: dict[int, int] = {}
        try:
            while not self._stop.is_set():
                chunk = self._proc.stdout.read(65536)   # type: ignore[union-attr]
                if not chunk:
                    break
                buf += chunk
                seen_bytes += len(chunk)
                for btype, raw in split_boxes(buf):
                    if btype == "moof":
                        pending = [raw]
                        pending_tid = moof_track_id(raw)
                    elif pending and btype in PAYLOAD_TYPES:
                        pending.append(raw)
                        # payload box size minus its own header is real media,
                        # attributed to the track this fragment's moof declares
                        media[pending_tid] = media.get(pending_tid, 0) + max(0, len(raw) - BOX_HEADER)
                        self._push(b"".join(pending))
                        pending = []
                    else:
                        self._add_init_box(btype, raw)
                # let a subclass react to a stalled mux (see note_mux_starved)
                self.on_progress(seen_bytes, media)
        except Exception as e:
            self.error = f"read error: {e}"
        finally:
            tail = " / ".join(list(err_tail)[-2:])
            if tail and not self.error:
                self.error = tail[:300]
            rc = self._proc.poll() if self._proc else None
            if rc not in (0, None) and not self.error:
                self.error = f"ffmpeg exit {rc}"
            self._finish()

    def on_progress(self, seen_bytes: int, media: dict[int, int]) -> None:
        """Per-track mux progress hook. Default: observe nothing.

        Kept because a stream that emits fragments but no decodable frames is
        a real failure mode worth detecting later - but deliberately does not
        act on it here. An earlier version dropped audio and rebuilt the
        session when track 1 stayed empty, and that misfired: a normal A/V
        stream's first fragments are often audio-only, so track 1 legitimately
        reads 0 while video is fine, and the session got torn down mid-play.
        """
        return

    def _finish(self) -> None:
        self.eof = True
        self.ended_at = time.time()
        with self.cond:
            self.cond.notify_all()

    def _push(self, raw: bytes) -> None:
        with self.cond:
            self.frag_seq += 1
            self.fragments.append(raw)
            self.cond.notify_all()

    @property
    def init_ready(self) -> bool:
        """A valid MSE init segment has both file type and movie metadata."""
        return "ftyp" in self._init_boxes and "moov" in self._init_boxes

    def _add_init_box(self, btype: str, raw: bytes) -> None:
        """Accumulate ftyp + moov; do not publish until both are present."""
        if btype not in INIT_TYPES or btype in self._init_boxes:
            return
        self._init_boxes.add(btype)
        self.init += raw

    # -- consumer API ------------------------------------------------------ #

    def wait_fragment(self, cursor: int, timeout: float = 20.0):
        """Block until a fragment newer than `cursor` exists.

        Returns (fragment_bytes, next_cursor) or (None, cursor) on timeout/EOF.
        """
        deadline = time.time() + timeout
        with self.cond:
            while not self._stop.is_set():
                if self.frag_seq > cursor:
                    backlog = min(self.frag_seq - cursor, len(self.fragments))
                    raw = self.fragments[len(self.fragments) - backlog]
                    self.last_access = time.time()
                    return raw, self.frag_seq - backlog + 1
                if self.eof:
                    return None, cursor
                left = deadline - time.time()
                if left <= 0:
                    return None, cursor
                self.cond.wait(timeout=min(left, 0.5))
        return None, cursor

    def first_cursor(self) -> int:
        with self.cond:
            return max(0, self.frag_seq - len(self.fragments))

    def _reaper(self) -> None:
        idle = float(self.cfg["live"].get("idle_timeout_sec", 45))
        while not self._stop.is_set():
            if self._stop.wait(5):
                break
            if self.viewers <= 0 and time.time() - self.last_access > idle:
                self.stop()
                return

    # -- session bookkeeping ----------------------------------------------- #

    def acquire(self) -> "FragmentStream":
        self.viewers += 1
        self.touch()
        return self

    def release(self) -> None:
        self.viewers = max(0, self.viewers - 1)
        self.touch()
