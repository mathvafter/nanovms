"""JagaNVR - filesystem index. No database.

Layout under storage.root:
    <cam_id>/YYYY-mm-dd_HH-MM-SS.mkv     recording segments (ffmpeg writes these)
    _clips/<id>.mkv                      manual exports (self-contained)
    _clips/<id>.json                     clip metadata
    _index/segments.json                 optional cache, rebuilt on demand

The filename IS the index: ffmpeg's strftime pattern encodes start time.
Segment duration comes from the sibling file's start time (or config fallback).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .recorder import CREATE_NO_WINDOW

STAMP_FMT = "%Y-%m-%d_%H-%M-%S"
STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:\.\w+)?$")
CLIP_DIR = "_clips"
TMP_DIR = "_tmp"


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def parse_stamp(name: str) -> float | None:
    m = STAMP_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), STAMP_FMT).timestamp()
    except ValueError:
        return None


def fmt_stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime(STAMP_FMT)


def fmt_ts(ts: float, human: bool = True) -> str:
    if not human:
        return f"{ts:.3f}"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(value) -> float | None:
    """Accept epoch seconds, ISO-8601, or 'YYYY-mm-dd HH:MM:SS'."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s[:19], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# segment listing
# --------------------------------------------------------------------------- #

def cam_dir(root: Path, cam_id: str) -> Path:
    return Path(root) / cam_id


def _segment_entries(d: Path) -> list[tuple[float, Path]]:
    out: list[tuple[float, Path]] = []
    try:
        with os.scandir(d) as it:
            for e in it:
                if not e.is_file():
                    continue
                ts = parse_stamp(e.name)
                if ts is None:
                    continue
                out.append((ts, Path(e.path)))
    except FileNotFoundError:
        return []
    out.sort(key=lambda t: t[0])
    return out


def _real_durations(d: Path, ents: list[tuple[float, Path]]) -> dict[Path, float]:
    """Map each segment file to its true video length, in seconds.

    A segment's end time is normally taken from the next segment's start, which
    is only correct when both files are intact. If a segment was cut short (a
    killed muxer, a full disk) the next file still starts on time, so the index
    silently hands the short file a span far longer than its contents - and
    seeking into that phantom tail yields an empty clip with no error at all.

    ffprobe is the authority here, but it is too slow to run over a whole day on
    every request. So: measure once, cache the answer in a sidecar next to the
    recording, and prefer a real duration whenever we have one.
    """
    out: dict[Path, float] = {}
    for ts, p in ents:
        try:
            dur = _cached_duration(p)
        except OSError:
            continue
        if dur is not None:
            out[p] = dur
    return out


def live_segment(cfg: dict, cam_id: str) -> Path | None:
    """The segment file a recorder is writing right now, if any.

    It is incomplete by definition, so its measured duration means nothing and
    the index must fall back to the mtime rule for it.
    """
    for cam in cfg.get("cameras", []):
        if cam.get("id") != cam_id:
            continue
        st = cam.get("record", cam) if isinstance(cam.get("record"), dict) else cam
        seg_min = float(st.get("segment_minutes", 5) or 5)
        span = max(60.0, seg_min * 60.0)
        try:
            ents = _segment_entries(cam_dir(Path(cfg["storage"]["root"]), cam_id))
        except OSError:
            return None
        if not ents:
            return None
        ts, p = ents[-1]
        # written recently and not yet rolled over => still open
        return p if (time.time() - ts) < span * 1.2 else None
    return None


def dur_cache_path(p: Path) -> Path | None:
    """Sidecar that caches a segment's measured duration, if one is possible."""
    try:
        return p.with_name(p.name + ".dur")
    except (ValueError, OSError):
        return None


def _cached_duration(p: Path) -> float | None:
    """Video length of a segment file, from a sidecar cache or ffprobe.

    None = could not measure (never cache that), float = the real length, 0.0 =
    ffprobe read the file and found no duration, i.e. a stub.
    """
    cache = dur_cache_path(p)
    if cache is not None:
        try:
            return float(cache.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
    dur = probe_duration(p)
    if dur is None:
        return None
    if dur > 0 and cache is not None:
        try:
            cache.write_text(f"{dur:.3f}", encoding="utf-8")
        except OSError:
            pass
    return dur


def probe_duration(p: Path, ffprobe: str = "") -> float | None:
    """Real length of a media file in seconds.

    Measures last-minus-first packet timestamp, and deliberately NOT
    `format.duration`: segments written with -use_wallclock_as_timestamps keep
    their absolute wall clock as PTS, so `format.duration` reports ~98,000s for a
    5-minute file, which would make the index advertise 27 hours of footage that
    does not exist.

    Packets rather than frames because -show_entries frame= has to decode every
    frame (11s on a 1080p segment); packet timestamps are read from container
    indexes and cost ~0.1s, which matters on a Celeron.

    Returns None when no answer is available at all (ffprobe missing, file gone,
    command failed) and 0.0 when ffprobe ran but the file has no packets at all -
    a stub left behind by a killed muxer.
    """
    if not ffprobe:
        ffprobe = _find_ffprobe()
    if not ffprobe:
        return None
    base = [ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time", "-of", "csv=p=0"]
    try:
        first = subprocess.run(base + ["-read_intervals", "%+#1", str(p)],
                               capture_output=True, timeout=15,
                               creationflags=CREATE_NO_WINDOW)
        if first.returncode != 0:
            return None
        f0 = _first_pts(first.stdout)
        if f0 is None:
            return 0.0                      # ran fine, no packets at all
        last = subprocess.run(base + [str(p)], capture_output=True, timeout=30,
                              creationflags=CREATE_NO_WINDOW)
        if last.returncode != 0:
            return None
        f1 = _last_pts(last.stdout)
        if f1 is None:
            return None
        return max(0.0, f1 - f0)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _first_pts(out: bytes) -> float | None:
    for line in out.decode("ascii", "replace").splitlines():
        v = _pts_value(line)
        if v is not None:
            return v
    return None


def _last_pts(out: bytes) -> float | None:
    v = None
    for line in out.decode("ascii", "replace").splitlines():
        got = _pts_value(line)
        if got is not None:
            v = got
    return v


def _pts_value(line: str) -> float | None:
    line = line.strip().rstrip(",")
    if not line or line in ("N/A", "nan"):
        return None
    try:
        return float(line)
    except ValueError:
        return None


def _find_ffprobe() -> str:
    """Locate ffprobe next to a known ffmpeg, else on PATH."""
    from .recorder import find_ffmpeg, find_ffprobe
    try:
        p = find_ffprobe(find_ffmpeg(""))
    except (FileNotFoundError, ValueError, OSError):
        return ""
    return p or ""


def list_segments(root: Path, cam_id: str, day: str | None = None,
                  limit: int = 2000, cfg: dict | None = None) -> list[dict]:
    """Segments for a camera (optionally one YYYY-MM-DD), oldest first.

    Each entry: start, end, duration_sec, size, path (relative to root), name.
    End time is the next segment's start, but never beyond the file's own real
    duration - otherwise a short file advertises hours of footage it does not
    contain, and playback/export into that span returns nothing.
    """
    d = cam_dir(root, cam_id)
    ent = _segment_entries(d)
    if day:
        ent = [(t, p) for t, p in ent if day_key(t) == day]
    ent = ent[-limit:] if limit else ent

    # Real lengths first, so a short file cannot inherit a long span from the
    # next segment's name. The in-progress segment is still being written, so
    # its ffprobe duration is meaningless right now - it keeps the mtime rule.
    real = _real_durations(d, ent)
    # the segment ffmpeg is writing right now; it is expected to be incomplete
    live = live_segment(cfg, cam_id) if cfg else None

    out: list[dict] = []
    for i, (ts, p) in enumerate(ent):
        try:
            st = p.stat()
        except OSError:
            continue
        if i + 1 < len(ent):
            end = ent[i + 1][0]
        else:
            # closed segments have mtime >= last write; use it when sane
            end = max(ts + 1.0, min(st.st_mtime, ts + 3600.0))
        dur = real.get(p)
        if dur is not None and p != live:
            # A measured length is the ceiling, whatever the index would have
            # guessed from the next file's name. dur == 0 means ffprobe could not
            # read a duration at all (a file cut so short its header never got a
            # valid one); such a file holds almost nothing, so collapse it to a
            # 1s sliver rather than let it advertise hours of phantom footage.
            end = min(end, ts + dur if dur > 0 else ts + 1.0)
        out.append({
            "cam_id": cam_id,
            "name": p.name,
            "path": p.relative_to(root).as_posix(),
            "start": ts,
            "end": max(end, ts),
            "duration_sec": round(max(end - ts, 0.0), 2),
            "size": st.st_size,
            "start_h": fmt_ts(ts),
            "end_h": fmt_ts(max(end, ts)),
        })
    return out


def list_days(root: Path, cam_id: str, limit: int = 0) -> list[dict]:
    """Per-day rollup for the date picker, newest first.

    `limit` caps how many days are returned (0 = all), so the UI can offer a
    bounded set of recent days without walking the whole archive.
    """
    ent = _segment_entries(cam_dir(root, cam_id))
    agg: dict[str, dict] = {}
    for ts, p in ent:
        k = day_key(ts)
        try:
            size = p.stat().st_size
        except OSError:
            continue
        a = agg.setdefault(k, {"day": k, "segments": 0, "size": 0, "first": ts, "last": ts})
        a["segments"] += 1
        a["size"] += size
        a["first"] = min(a["first"], ts)
        a["last"] = max(a["last"], ts)
    out = sorted(agg.values(), key=lambda a: a["day"], reverse=True)
    if limit > 0:
        out = out[:limit]
    for a in out:
        a["first_h"] = fmt_ts(a["first"])
        a["last_h"] = fmt_ts(a["last"])
    return out


def find_segment_at(root: Path, cam_id: str, ts: float,
                    cfg: dict | None = None) -> dict | None:
    """Which segment covers epoch `ts`?"""
    segs = list_segments(root, cam_id, cfg=cfg)
    if not segs:
        return None
    if ts < segs[0]["start"]:
        return segs[0]
    for s in segs:
        if s["start"] <= ts < s["end"]:
            return s
    return segs[-1] if ts >= segs[-1]["start"] else None


def resolve_segment(root: Path, cam_id: str, ts: float) -> tuple[Path, float] | None:
    """Return (absolute segment path, offset_seconds_into_segment) for a time."""
    s = find_segment_at(root, cam_id, ts)
    if not s:
        return None
    path = Path(root) / s["path"]
    if not path.is_file():
        return None
    off = max(0.0, min(ts - s["start"], max(s["duration_sec"] - 0.05, 0.0)))
    return path, off


# --------------------------------------------------------------------------- #
# clips
# --------------------------------------------------------------------------- #

def clips_dir(root: Path) -> Path:
    d = Path(root) / CLIP_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_clip(root: Path, cam_id: str, start: float, end: float,
              name: str = "", src_path: Path | None = None) -> dict:
    """Persist a clip. `src_path` = already-rendered file to adopt; otherwise the
    caller renders into clip_target() and then calls this to register it."""
    cid = uuid.uuid4().hex[:12]
    meta = {
        "id": cid,
        "cam_id": cam_id,
        "name": name or f"{cam_id} {fmt_ts(start)}",
        "start": start,
        "end": end,
        "duration_sec": round(max(end - start, 0.0), 2),
        "created": time.time(),
        "file": "",
        "size": 0,
    }
    if src_path and Path(src_path).is_file():
        dst = clips_dir(root) / f"{cid}.mkv"
        if Path(src_path).resolve() != dst.resolve():
            shutil.move(str(src_path), str(dst))
        meta["file"] = dst.relative_to(root).as_posix()
        meta["size"] = dst.stat().st_size
        meta["stamp"] = parse_stamp(dst.name) or meta["start"]
    (clips_dir(root) / f"{cid}.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def clip_target(root: Path) -> Path:
    """Temp output path for a clip being rendered; register it right after."""
    d = Path(root) / TMP_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"clip-{uuid.uuid4().hex[:12]}.mkv"


def list_clips(root: Path, cam_id: str | None = None) -> list[dict]:
    d = clips_dir(root)
    out: list[dict] = []
    for j in sorted(d.glob("*.json")):
        try:
            m = json.loads(j.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        f = Path(root) / m.get("file", "")
        if not m.get("file") or not f.is_file():
            continue  # file gone -> hide (sweep cleans the json)
        m["size"] = f.stat().st_size
        m["start_h"] = fmt_ts(m.get("start", 0))
        m["end_h"] = fmt_ts(m.get("end", 0))
        if cam_id and m.get("cam_id") != cam_id:
            continue
        out.append(m)
    out.sort(key=lambda m: m.get("start", 0), reverse=True)
    return out


def get_clip(root: Path, clip_id: str) -> dict | None:
    j = clips_dir(root) / f"{clip_id}.json"
    if not j.is_file():
        return None
    try:
        m = json.loads(j.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    f = Path(root) / m.get("file", "")
    if not m.get("file") or not f.is_file():
        return None
    m["size"] = f.stat().st_size
    return m


def clip_file(root: Path, clip_id: str) -> Path | None:
    m = get_clip(root, clip_id)
    if not m:
        return None
    return Path(root) / m["file"]


def delete_clip(root: Path, clip_id: str) -> bool:
    m = get_clip(root, clip_id)
    if not m:
        # orphaned metadata
        j = clips_dir(root) / f"{clip_id}.json"
        if j.is_file():
            j.unlink()
            return True
        return False
    f = Path(root) / m["file"]
    try:
        f.unlink(missing_ok=True)
    except OSError:
        pass
    (clips_dir(root) / f"{clip_id}.json").unlink(missing_ok=True)
    return True


# --------------------------------------------------------------------------- #
# storage: stats + retention sweep
# --------------------------------------------------------------------------- #

def dir_size(d: Path) -> tuple[int, int]:
    total = 0
    n = 0
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        total += e.stat().st_size
                        n += 1
                except OSError:
                    pass
    except FileNotFoundError:
        pass
    return total, n


def storage_stats(cfg: dict) -> dict:
    root = Path(cfg["storage"]["root"])
    cams = []
    grand = 0
    for cam in cfg["cameras"]:
        size, n = dir_size(cam_dir(root, cam["id"]))
        grand += size
        cams.append({"cam_id": cam["id"], "name": cam["name"], "bytes": size,
                     "segments": n, "days": len(list_days(root, cam["id"]))})
    clip_bytes, clip_n = dir_size(clips_dir(root))
    grand += clip_bytes
    try:
        du = shutil.disk_usage(str(root if root.exists() else root.parent))
        disk = {"total": du.total, "used": du.used, "free": du.free,
                "percent": round(du.used / du.total * 100, 1)}
    except OSError:
        disk = {"total": 0, "used": 0, "free": 0, "percent": 0}
    return {
        "root": str(root),
        "bytes": grand,
        "cameras": cams,
        "clips": {"bytes": clip_bytes, "count": clip_n},
        "disk": disk,
    }


def sweep(cfg: dict, dry_run: bool = False) -> dict:
    """Retention + free-space guard. Deletes oldest segments first.

    Order of enforcement:
      1. age: drop segment files older than retention_days
      2. age: drop clips older than clips_retention_days
      3. space: while disk usage > max_usage_percent, delete oldest segments
      4. space: while free < keep_free_gb, delete oldest segments
      5. orphans: clip json without file, tmp files older than 1h, empty cam dirs
    """
    root = Path(cfg["storage"]["root"])
    st = cfg["storage"]
    now = time.time()
    removed: list[dict] = []
    freed = 0

    def zap(p: Path, reason: str, cam_id: str = "") -> None:
        nonlocal freed
        try:
            sz = p.stat().st_size
        except OSError:
            return
        # the duration sidecar is worthless without its segment
        sidecar = dur_cache_path(p)
        if not dry_run and sidecar:
            try:
                sidecar.unlink()
            except OSError:
                pass
        if not dry_run:
            try:
                p.unlink()
            except OSError:
                return
        freed += sz
        removed.append({"path": p.name, "cam_id": cam_id, "bytes": sz, "reason": reason})

    # 1 + 3 + 4 -> one chronological pass over segments
    seg_age_cut = now - float(st.get("retention_days", 7)) * 86400
    segs: list[tuple[float, Path, str]] = []
    for cam in cfg["cameras"]:
        for ts, p in _segment_entries(cam_dir(root, cam["id"])):
            segs.append((ts, p, cam["id"]))
    segs.sort(key=lambda t: t[0])

    for ts, p, cid in segs:
        if ts < seg_age_cut:
            zap(p, "age", cid)

    def usage_over() -> bool:
        try:
            du = shutil.disk_usage(str(root if root.exists() else root.parent))
        except OSError:
            return False
        if du.used / du.total * 100 > float(st.get("max_usage_percent", 85)):
            return True
        return du.free < float(st.get("keep_free_gb", 5)) * 1024 ** 3

    if st.get("max_usage_percent", 85) < 100 or st.get("keep_free_gb", 0) > 0:
        idx = 0
        while usage_over() and idx < len(segs):
            _, p, cid = segs[idx]
            idx += 1
            if p.exists():
                zap(p, "space", cid)

    # 2 clips by age
    clip_age_cut = now - float(st.get("clips_retention_days", 14)) * 86400
    for m in list_clips(root):
        if m.get("start", now) < clip_age_cut:
            f = Path(root) / m["file"]
            if f.is_file():
                zap(f, "clip-age", m.get("cam_id", ""))
            if not dry_run:
                (clips_dir(root) / f"{m['id']}.json").unlink(missing_ok=True)

    # 5 orphans
    if st.get("sweep_orphans", True):
        for j in clips_dir(root).glob("*.json"):
            try:
                m = json.loads(j.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                if not dry_run:
                    j.unlink(missing_ok=True)
                continue
            if not (Path(root) / m.get("file", "\0")).is_file():
                if not dry_run:
                    j.unlink(missing_ok=True)
                removed.append({"path": j.name, "cam_id": "", "bytes": 0, "reason": "orphan-meta"})
        tmp = Path(root) / TMP_DIR
        if tmp.is_dir():
            for f in tmp.iterdir():
                try:
                    if f.is_file() and now - f.stat().st_mtime > 3600:
                        zap(f, "tmp-stale")
                except OSError:
                    pass
        for cam in cfg["cameras"]:
            d = cam_dir(root, cam["id"])
            try:
                if d.is_dir() and not any(d.iterdir()):
                    if not dry_run:
                        d.rmdir()
            except OSError:
                pass

    return {"dry_run": dry_run, "freed": freed, "removed": len(removed),
            "items": removed[:200], "checked_at": now}
