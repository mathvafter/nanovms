"""NanoVMS - clip export.

Default path is stream copy: segments are already H.264/MKV, so cutting a range
out of them costs ~0 CPU (fast on a Celeron). A `precise=True` export re-encodes
to frame accuracy - slower, use for evidence-grade exports.

Two-stage cut:
  segments overlapping [start, end] -> concat list -> seek inside the concat
  -> stream copy the window. Segment boundaries are keyframes, so copy-mode
  clips always begin on a keyframe (never a torn frame).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import index
from .recorder import CREATE_NO_WINDOW, find_ffmpeg


def _concat_list(seg_paths: list[Path], workdir: Path) -> Path:
    """Write an ffmpeg concat demuxer list. Paths are escaped per ffmpeg rules."""
    f = workdir / "concat.txt"
    lines = []
    for p in seg_paths:
        s = p.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{s}'")
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def plan_window(root: Path, cam_id: str, start: float, end: float,
                cfg: dict | None = None) -> dict:
    """Which segments cover a time window, and where the cut points fall."""
    segs = index.list_segments(root, cam_id, cfg=cfg)
    if not segs:
        return {"ok": False, "error": "no recordings for this camera", "segments": []}
    if end <= start:
        return {"ok": False, "error": "end must be after start", "segments": []}

    picked = [s for s in segs if s["end"] > start and s["start"] < end]
    if not picked:
        lo, hi = segs[0]["start_h"], segs[-1]["end_h"]
        return {"ok": False, "segments": [],
                "error": f"no footage in that window (available {lo} .. {hi})"}

    base = picked[0]["start"]
    # seek offset inside the concatenated stream
    offset = max(0.0, start - base)
    duration = end - start
    total = sum(s["duration_sec"] for s in picked)
    # clamp: never ask for more than exists
    if offset >= total:
        return {"ok": False, "segments": [], "error": "window is past the end of footage"}
    duration = min(duration, total - offset)
    return {
        "ok": True,
        "segments": picked,
        "base": base,
        "offset": offset,
        "duration": duration,
        "clamped": duration < (end - start) - 0.01,
    }


def render_clip(cfg: dict, cam_id: str, start: float, end: float, out: Path,
                precise: bool = False, audio: bool = False,
                timeout: float = 600.0) -> dict:
    """Cut [start, end] for a camera into `out`. Returns a result dict."""
    root = Path(cfg["storage"]["root"])
    plan = plan_window(root, cam_id, start, end, cfg=cfg)
    if not plan["ok"]:
        return plan

    ffmpeg = find_ffmpeg(cfg["ffmpeg"].get("path", ""))
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="nanovms-cut-") as td:
        work = Path(td)
        if len(plan["segments"]) == 1:
            src_args = ["-i", str(root / plan["segments"][0]["path"])]
        else:
            lst = _concat_list([root / s["path"] for s in plan["segments"]], work)
            src_args = ["-f", "concat", "-safe", "0", "-i", str(lst)]

        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
        cmd += src_args
        cmd += ["-ss", f"{plan['offset']:.3f}", "-t", f"{plan['duration']:.3f}"]

        if precise:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        else:
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        if audio:
            cmd += ["-map", "0:v:0", "-map", "0:a:0?",
                    "-c:a", "aac" if precise else "copy"]
        else:
            cmd += ["-an"]

        cmd += [str(out)]

        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"export timed out after {timeout:.0f}s"}
        except OSError as e:
            return {"ok": False, "error": f"ffmpeg failed to start: {e}"}

        if r.returncode != 0 or not out.is_file() or out.stat().st_size < 1024:
            err = r.stderr.decode("utf-8", "replace").strip().splitlines()
            return {"ok": False, "error": (err[-1] if err else "ffmpeg produced no output"),
                    "cmd": " ".join(cmd)}

        return {
            "ok": True,
            "path": str(out),
            "size": out.stat().st_size,
            "duration": plan["duration"],
            "segments": len(plan["segments"]),
            "precise": precise,
            "elapsed_sec": round(time.time() - t0, 2),
            "clamped": plan["clamped"],
        }


def export_to_clip(cfg: dict, cam_id: str, start: float, end: float,
                   name: str = "", precise: bool = False,
                   audio: bool = False) -> dict:
    """Render into the clips area and register metadata. Returns clip meta."""
    root = Path(cfg["storage"]["root"])
    target = index.clip_target(root)
    res = render_clip(cfg, cam_id, start, end, target, precise=precise, audio=audio)
    if not res.get("ok"):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return res
    meta = index.make_clip(root, cam_id, start, end, name=name, src_path=target)
    return {"ok": True, "clip": meta, "elapsed_sec": res.get("elapsed_sec")}


def download_segment(cfg: dict, rel_path: str) -> Path | None:
    """Resolve a segment path from the index and confirm it is inside storage.root."""
    root = Path(cfg["storage"]["root"]).resolve()
    p = (root / rel_path).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None                       # traversal attempt
    if p.is_file() and p.suffix.lower() in (".mkv", ".mp4", ".jpg", ".ts"):
        return p
    return None
