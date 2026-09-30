"""NanoVMS - config load/save.

Single JSON file (config.json) beside the app root. Missing keys are filled
from DEFAULTS so upgrades never break an existing config.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
TEMPLATE_PATH = ROOT / "config.example.json"

_lock = threading.RLock()

DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 1900,
    },
    "storage": {
        "root": (ROOT / "recordings").as_posix(),
        "segment_minutes": 5,
        "retention_days": 7,
        "max_usage_percent": 85,
        "keep_free_gb": 5,
        "clips_retention_days": 14,
        "sweep_orphans": True,
    },
    "ffmpeg": {
        "path": "",              # "" -> autodetect on PATH
        "rtsp_transport": "tcp",
        "rw_timeout_ms": 15_000_000,
        "analyze_duration": 2,
        "probe_size": 2_000_000,
        "stall_timeout_sec": 30,
        "restart_backoff_max": 60,
        "restart_backoff_min": 2,
    },
    "live": {
        "max_concurrent": 2,
        "idle_timeout_sec": 45,
        "fps": 8,
        "jpeg_quality": 7,
        "max_width": 1280,
        # seconds between forced keyframes in the live stream, and fragment
        # length in ms. Both exist for the same reason: a fragment that does not
        # start on an IDR cannot be decoded, and Chrome then shows a black frame
        # with audio instead of an error. Keep gop_sec < frag_ms/1000.
        "gop_sec": 2,
        "frag_ms": 1000,
    },
    "cameras": [],
}

CAMERA_DEFAULTS: dict[str, Any] = {
    "id": "",
    "name": "",
    "url": "",
    "enabled": True,
    "record": True,
    "audio": False,
    "transport": "",          # "" -> inherit ffmpeg.rtsp_transport
    "loop": False,            # true only for file:// / test sources
    "live_passthrough": True, # true = copy video for live view (no transcode)
    "live_rebase_ts": True,  # rebase camera wallclock PTS to 0 for the browser
    # Which video path the live view takes. "auto" lets NanoVMS decide from the
    # probed codec, which is right for almost every camera; the explicit values
    # are the escape hatches a user needs when a tile is black or frozen and
    # hand-editing this file is not an option.
    "live_mode": "auto",     # auto | copy | x264 | mjpeg
    "segment_minutes": 0,     # 0 -> inherit storage.segment_minutes
    "encode": False,          # true = re-encode on record (rawvideo/MJPEG sources)
    "encode_fps": 10,
    "notes": "",
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


LIVE_MODES = ("auto", "copy", "x264", "mjpeg")


def normalize_camera(cam: dict, index: int = 0) -> dict:
    out = _deep_merge(CAMERA_DEFAULTS, cam or {})
    out["id"] = str(out.get("id") or "").strip() or f"cam{index + 1}"
    out["name"] = str(out.get("name") or "").strip() or out["id"]
    out["url"] = str(out.get("url") or "").strip()
    # An unrecognised live_mode must never reach the ffmpeg builder: it would
    # silently fall through to the transcode branch and look like "auto" worked
    # while the user believed they had picked something specific.
    mode = str(out.get("live_mode") or "auto").strip().lower()
    out["live_mode"] = mode if mode in LIVE_MODES else "auto"
    return out


def normalize(cfg: dict) -> dict:
    cfg = _deep_merge(DEFAULTS, cfg or {})
    cams = cfg.get("cameras") or []
    cfg["cameras"] = [normalize_camera(c, i) for i, c in enumerate(cams)]
    # de-duplicate ids
    seen: set[str] = set()
    for i, c in enumerate(cfg["cameras"]):
        base = c["id"]
        while c["id"] in seen:
            c["id"] = f"{base}-{i}"
        seen.add(c["id"])
    cfg["storage"]["root"] = str(Path(cfg["storage"]["root"]).expanduser())
    return cfg


def load(path: Path | str | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    with _lock:
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise SystemExit(f"config unreadable ({p}): {e}")
        else:
            raw = {}
        return normalize(raw)


def save(cfg: dict, path: Path | str | None = None) -> None:
    p = Path(path) if path else CONFIG_PATH
    with _lock:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)


def storage_root(cfg: dict) -> Path:
    return Path(cfg["storage"]["root"])


def config_dir() -> Path:
    return ROOT
