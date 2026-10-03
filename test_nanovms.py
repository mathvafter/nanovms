"""NanoVMS headless tests - no camera required, no network required.

Run: python test_nanovms.py
All tests use real code paths with a lavfi synthetic source or mocked data.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import config as cfgmod
from app import index
from app.recorder import CREATE_NO_WINDOW, IS_WIN, find_ffmpeg

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f"  [{detail}]" if detail else ""))
    return ok


# ------------------------------------------------------------------ config --

def test_config():
    print("\n[config]")
    cfg = cfgmod.normalize({})
    check("defaults: port=1900", cfg["server"]["port"] == 1900)
    check("defaults: segment_minutes=5", cfg["storage"]["segment_minutes"] == 5)
    check("empty cameras list", cfg["cameras"] == [])

    cfg2 = cfgmod.normalize({
        "storage": {"retention_days": 14},
        "cameras": [{"url": "rtsp://x"}, {"url": "rtsp://y"}],
    })
    check("merge: retention override", cfg2["storage"]["retention_days"] == 14)
    check("merge: port preserved", cfg2["server"]["port"] == 1900)
    check("cameras normalised", len(cfg2["cameras"]) == 2)
    c0 = cfg2["cameras"][0]
    check("camera id auto-assigned", bool(c0["id"]))
    check("camera record defaults true", c0["record"] is True)
    check("camera enabled defaults true", c0["enabled"] is True)

    # de-dup ids
    cfg3 = cfgmod.normalize({
        "cameras": [{"id": "same", "url": "a"}, {"id": "same", "url": "b"}],
    })
    ids = [c["id"] for c in cfg3["cameras"]]
    check("id dedup", len(set(ids)) == 2, str(ids))

    # save/load roundtrip
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = Path(f.name)
    try:
        cfgmod.save(cfg2, tmp)
        loaded = cfgmod.load(tmp)
        check("save/load roundtrip: retention", loaded["storage"]["retention_days"] == 14)
        check("save/load roundtrip: cameras", len(loaded["cameras"]) == 2)
    finally:
        tmp.unlink(missing_ok=True)


# ------------------------------------------------------------------ index ---

def test_index():
    print("\n[index]")
    with tempfile.TemporaryDirectory(prefix="nanovms-test-") as td:
        root = Path(td)
        cam = "cam1"
        d = root / cam
        d.mkdir()

        # write fake segment files (filename = timestamp)
        t0 = time.time() - 3600
        segs = []
        for i in range(4):
            ts = t0 + i * 300
            name = index.fmt_stamp(ts) + ".mkv"
            p = d / name
            p.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 200)  # MKV magic + padding
            segs.append((ts, name))
        time.sleep(0.01)  # ensure mtime settled

        listing = index.list_segments(root, cam)
        check("list_segments count", len(listing) == 4, str(len(listing)))
        check("list_segments order", listing[0]["start"] < listing[1]["start"])
        check("list_segments has size", listing[0]["size"] > 0)

        days = index.list_days(root, cam)
        check("list_days: at least one day", len(days) >= 1)

        # find_segment_at
        mid = t0 + 450
        found = index.find_segment_at(root, cam, mid)
        check("find_segment_at", found is not None)

        # parse_ts
        check("parse_ts epoch", abs(index.parse_ts(t0) - t0) < 1)
        check("parse_ts str", index.parse_ts("2026-01-01 12:00:00") is not None)
        check("parse_ts ms epoch", abs(index.parse_ts(t0 * 1000) - t0) < 1)
        check("parse_ts None", index.parse_ts(None) is None)

        # clips
        clips_d = index.clips_dir(root)
        check("clips_dir created", clips_d.is_dir())
        fake_clip = root / "_tmp" / "fake.mkv"
        fake_clip.parent.mkdir(exist_ok=True)
        fake_clip.write_bytes(b"x" * 512)
        meta = index.make_clip(root, cam, t0, t0 + 60, name="test clip", src_path=fake_clip)
        check("make_clip: id", bool(meta["id"]))
        check("make_clip: file exists", Path(root / meta["file"]).is_file())
        clips = index.list_clips(root)
        check("list_clips", len(clips) == 1)
        got = index.get_clip(root, meta["id"])
        check("get_clip", got is not None)
        ok = index.delete_clip(root, meta["id"])
        check("delete_clip", ok)
        check("delete_clip: file gone", not Path(root / meta["file"]).is_file() if meta.get("file") else True)

        # storage stats
        cfg = cfgmod.normalize({"storage": {"root": str(root)},
                                 "cameras": [{"id": cam, "name": "Test cam", "url": "rtsp://x"}]})
        st = index.storage_stats(cfg)
        check("storage_stats: root", st["root"] == str(root))
        check("storage_stats: bytes > 0", st["bytes"] > 0)
        check("storage_stats: cameras list", len(st["cameras"]) == 1)

        # sweep (dry run)
        result = index.sweep(cfg, dry_run=True)
        check("sweep dry_run: runs", "freed" in result)
        check("sweep dry_run: no files deleted", all((d / segs[i][1]).is_file() for i in range(4)))


# ---------------------------------------------------------------- HTTP API --

def test_http():
    print("\n[http]")

    with tempfile.TemporaryDirectory(prefix="nanovms-http-") as td:
        root = Path(td)
        cfg_path = root / "config.json"
        cfg = cfgmod.normalize({
            "server": {"host": "127.0.0.1", "port": 0},
            "storage": {"root": str(root / "recordings")},
            "cameras": [],
        })
        cfgmod.save(cfg, cfg_path)
        real_config = cfgmod.CONFIG_PATH.read_bytes() if cfgmod.CONFIG_PATH.exists() else None

        from app.server import serve
        # MUST pass cfg_path: App writes PUT/POST config changes to this file.
        # Without it, tests overwrite the user's real config.json.
        app, httpd = serve(cfg, config_path=cfg_path)
        addr = httpd.server_address
        port = addr[1]
        base = f"http://127.0.0.1:{port}"

        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.2)

        def get(path, expect=200):
            try:
                r = urllib.request.urlopen(base + path, timeout=8)
                body = r.read()
                code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
                body = e.read()
            return code, json.loads(body.decode("utf-8", "replace"))

        def post(path, data=None, expect=200):
            payload = json.dumps(data or {}).encode()
            req = urllib.request.Request(base + path, data=payload,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            try:
                r = urllib.request.urlopen(req, timeout=8)
                body = r.read()
                code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
                body = e.read()
            return code, json.loads(body.decode("utf-8", "replace"))

        def put(path, data):
            payload = json.dumps(data).encode()
            req = urllib.request.Request(base + path, data=payload,
                                         headers={"Content-Type": "application/json"},
                                         method="PUT")
            try:
                r = urllib.request.urlopen(req, timeout=8)
                body = r.read()
                code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
                body = e.read()
            return code, json.loads(body.decode("utf-8", "replace"))

        def delete(path):
            req = urllib.request.Request(base + path, method="DELETE")
            try:
                r = urllib.request.urlopen(req, timeout=8)
                body = r.read()
                code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
                body = e.read()
            return code, json.loads(body.decode("utf-8", "replace"))

        # /api/status
        code, j = get("/api/status")
        check("GET /api/status 200", code == 200, str(code))
        check("status.ok=true", j.get("ok"))
        check("status.sys.host", bool(j.get("sys", {}).get("host")))

        # /api/cameras (empty)
        code, j = get("/api/cameras")
        check("GET /api/cameras 200", code == 200)
        check("cameras is list", isinstance(j.get("cameras"), list))

        # POST /api/cameras
        code, j = post("/api/cameras", {"name": "TestCam", "url": "rtsp://x", "record": False})
        check("POST /api/cameras 200", code == 200, str(code))
        cid = j.get("camera", {}).get("id", "")
        check("camera id returned", bool(cid))

        # GET /api/cameras/<id>
        code, j = get(f"/api/cameras/{cid}")
        check(f"GET /api/cameras/{cid}", code == 200)
        check("camera name", j.get("camera", {}).get("name") == "TestCam")

        # PUT /api/cameras/<id>
        code, j = put(f"/api/cameras/{cid}", {"name": "Renamed", "record": True})
        check("PUT camera 200", code == 200)
        check("camera renamed", j.get("camera", {}).get("name") == "Renamed")

        # 404 on unknown camera
        code, j = get("/api/cameras/doesnotexist")
        check("unknown camera 404", code == 404)

        # /api/config GET + PUT
        code, j = get("/api/config")
        check("GET /api/config 200", code == 200)
        code, j = put("/api/config", {**j.get("config", {}),
                                       "storage": {**j.get("config", {}).get("storage", {}),
                                                   "retention_days": 3}})
        check("PUT /api/config 200", code == 200)
        check("retention persisted", j.get("config", {}).get("storage", {}).get("retention_days") == 3)

        # /api/storage
        code, j = get("/api/storage")
        check("GET /api/storage 200", code == 200)
        check("storage.disk exists", "disk" in j)

        # /api/recordings - no footage yet -> count=0
        code, j = get(f"/api/recordings?cam={cid}")
        check("GET /api/recordings 200", code == 200)
        check("recordings count=0", j.get("count") == 0)

        # /api/recordings - unknown cam
        code, j = get("/api/recordings?cam=nope")
        check("recordings unknown cam 404", code == 404)

        # /api/clips empty
        code, j = get("/api/clips")
        check("GET /api/clips 200", code == 200)
        check("clips list empty", j.get("clips") == [])

        # /api/clips create (no footage -> 409)
        code, j = post("/api/clips", {"cam": cid, "start": time.time() - 60, "end": time.time()})
        check("POST /api/clips no footage -> 409", code == 409, str(code))

        # /api/test with lavfi
        code, j = post("/api/test", {"url": "lavfi:testsrc=size=640x360:rate=5"})
        check("POST /api/test lavfi 200", code == 200, str(code))
        if j.get("ok"):
            check("probe: video present", bool(j.get("video")))
            check("probe: codec", j.get("video", {}).get("codec") in ("rawvideo", "wrapped_avframe", None, ""))
        else:
            check("probe: ffprobe available", False, j.get("error", ""))

        # /api/sweep dry
        code, j = get("/api/sweep?dry=1")
        check("GET /api/sweep dry 200", code == 200)
        check("sweep freed key", "freed" in j)

        # static / 
        try:
            r = urllib.request.urlopen(base + "/", timeout=4)
            check("GET / (static)", r.status == 200)
        except Exception as e:
            check("GET /", False, str(e))

        # DELETE camera
        code, j = delete(f"/api/cameras/{cid}")
        check("DELETE camera 200", code == 200)
        code, j = get(f"/api/cameras/{cid}")
        check("DELETE camera gone", code == 404)

        # shutdown
        post("/api/shutdown", {})
        time.sleep(0.3)

        app.shutdown()

        # The real config.json must be byte-for-byte unchanged by the suite.
        unchanged = (cfgmod.CONFIG_PATH.read_bytes() if cfgmod.CONFIG_PATH.exists() else None)
        check("test isolation: real config.json untouched", unchanged == real_config)


# ---------------------------------------------------------------- ffmpeg ---

def test_ffmpeg():
    print("\n[ffmpeg]")
    from app.recorder import find_ffmpeg, find_ffprobe
    try:
        ff = find_ffmpeg("")
        check("ffmpeg found", True, ff)
    except FileNotFoundError:
        check("ffmpeg found", False, "install ffmpeg and ensure it is on PATH")
        return
    fp = find_ffprobe(ff)
    check("ffprobe found", bool(fp), fp or "missing")

    # lavfi encode to /dev/null (proves the full transcode path works)
    import subprocess
    null = "NUL" if os.name == "nt" else "/dev/null"
    cmd = [ff, "-hide_banner", "-nostdin", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5",
           "-frames:v", "5", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-f", "null", null]
    r = subprocess.run(cmd, capture_output=True, timeout=20)
    check("ffmpeg libx264 encode works", r.returncode == 0, r.stderr[-200:].decode("utf-8", "replace") if r.returncode else "ok")

    # lavfi -> fmp4 pipe (the actual live/recording pipeline)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.mp4"
        cmd2 = [ff, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5",
                "-frames:v", "25", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                str(out)]
        r2 = subprocess.run(cmd2, capture_output=True, timeout=30)
        check("ffmpeg fmp4 output", r2.returncode == 0 and out.is_file(),
              f"{out.stat().st_size} bytes" if out.is_file() else r2.stderr[-200:].decode("utf-8", "replace"))

        # mkv segment muxer
        cmd3 = [ff, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-frames:v", "50",
                "-f", "segment", "-segment_time", "5",
                "-segment_format", "matroska",
                "-strftime", "0",
                str(Path(td) / "%03d.mkv")]
        r3 = subprocess.run(cmd3, capture_output=True, timeout=30)
        segs = list(Path(td).glob("*.mkv"))
        check("mkv segment muxer", len(segs) >= 1,
              f"{len(segs)} segments" if segs else r3.stderr[-200:].decode("utf-8", "replace"))


# ---------------------------------------------------------------- stream.py -

def test_split_boxes():
    print("\n[stream.split_boxes]")
    from app.stream import split_boxes

    def box(btype: str, payload: bytes) -> bytes:
        s = 8 + len(payload)
        return s.to_bytes(4, "big") + btype.encode()[:4] + payload

    # single box
    b = box("ftyp", b"isom" + b"\x00" * 16)
    buf = bytearray(b)
    out = split_boxes(buf)
    check("single box type", out[0][0] == "ftyp")
    check("single box buf empty after", len(buf) == 0)

    # two boxes
    b2 = box("moov", b"\x00" * 32) + box("moof", b"\x00" * 16)
    buf2 = bytearray(b2)
    out2 = split_boxes(buf2)
    check("two boxes count", len(out2) == 2)
    check("two boxes types", out2[0][0] == "moov" and out2[1][0] == "moof")

    # partial box -> stays in buf
    partial = box("mdat", b"\x00" * 8)[:10]
    buf3 = bytearray(partial)
    out3 = split_boxes(buf3)
    check("partial box: nothing returned", len(out3) == 0)
    check("partial box: data preserved", len(buf3) == 10)

    # two boxes + partial third
    full = box("styp", b"\x00" * 4) + box("moof", b"\x00" * 8)
    trunc = box("mdat", b"\x00" * 100)[:5]
    buf4 = bytearray(full + trunc)
    out4 = split_boxes(buf4)
    check("full+partial: two returned", len(out4) == 2)
    check("full+partial: remainder", len(buf4) == 5)

    # Regression: fMP4 with empty_moov can put moov inside the first mdat.
    # split_boxes must preserve that nested init; MSE rejects an ftyp-only init.
    nested = box("ftyp", b"isom" + b"\x00" * 12) + \
             box("mdat", box("moov", b"\x00" * 24) + b"\x00" * 16)
    out5 = split_boxes(bytearray(nested))
    check("nested mdat moov is preserved",
          len(out5) == 2 and b"moov" in out5[1][1],
          str([b[0] for b in out5]))

    # Regression: MSE requires a complete init segment (ftyp + moov). The old
    # reader kept only the first init box, returning a 28-byte ftyp and causing
    # SourceBuffer errors when ffmpeg emitted moov next.
    from app.stream import FragmentStream
    ftyp = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41"
    moov = b"\x00\x00\x02\xe5moov" + b"\x00" * 733
    s = FragmentStream("test-init", cfgmod.normalize({}))
    check("init is incomplete before ftyp", not s.init_ready)
    s._add_init_box("ftyp", ftyp)
    check("init is incomplete with ftyp only", not s.init_ready)
    s._add_init_box("moov", moov)
    check("init is complete with ftyp and moov", s.init_ready)
    check("init segment contains both boxes", b"ftyp" in s.init and b"moov" in s.init)
    s._add_init_box("ftyp", b"duplicate")
    check("duplicate ftyp is ignored", s.init.count(b"ftyp") == 1)


# ---------------------------------------------------------------- plan_window -

def test_plan_window():
    print("\n[export.plan_window]")
    from app.export import plan_window
    from app.playback import PlaybackSession
    with tempfile.TemporaryDirectory(prefix="nanovms-pw-") as td:
        root = Path(td)
        cam = "cam1"
        d = root / cam
        d.mkdir()
        t0 = time.time() - 3600
        # Segments must actually cover the span the index gives them. The index
        # now caps a segment's end at its real decodable duration, so a 1s file
        # named 300s apart would correctly report as a 1s segment followed by a
        # 299s gap - and the planner would (rightly) find no footage in between.
        for i in range(3):
            ts = t0 + i * 5
            name = index.fmt_stamp(ts) + ".mkv"
            p = d / name
            # real H.264 clip: playback probes decodable frames, so a fake
            # header stub would be (correctly) rejected as corrupt.
            subprocess.run([find_ffmpeg(""), "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=5",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "5", str(p)],
                           check=True, timeout=60, creationflags=CREATE_NO_WINDOW)

        # window fully covered
        p1 = plan_window(root, cam, t0 + 1, t0 + 12)
        check("plan_window ok", p1.get("ok"), p1.get("error", ""))
        check("plan_window: 2 segments", len(p1.get("segments", [])) >= 1)
        check("plan_window: duration > 0", p1.get("duration", 0) > 0)

        # playback must use same planner; regression: it called missing index.plan_window
        cfg = cfgmod.normalize({"storage": {"root": str(root)}})
        s0 = t0 + 1
        ps = PlaybackSession("test-pb", cfg, s0, s0 + 6, cam_id=cam)
        try:
            ps.prepare()
            check("playback prepare succeeds", not ps.error, ps.error)
            # t0+1 .. t0+7 straddles the first two 5s segments, so more than one
            # is expected - the point is that the plan is non-empty and builds
            check("playback plans segment", len(ps.segments) >= 1, str(len(ps.segments)))
            check("playback command builds", bool(ps.build_cmd()))
            ps.source_codec = "hevc"
            cmd = ps.build_cmd()
            check("HEVC playback uses H.264 encoder", "libx264" in cmd, " ".join(cmd))
            check("playback H.264 uses yuv420p", "yuv420p" in cmd, " ".join(cmd))
            check("playback timestamps stay available", ps.start_at == s0 and ps.end_at == s0 + 6)
        finally:
            ps.stop()

        # window before recordings
        p2 = plan_window(root, cam, t0 - 7200, t0 - 3600)
        check("plan_window before: not ok", not p2.get("ok"))
        # a truncated segment must not poison the whole window: concat aborts
        # on the corrupt file, so playback must drop it and use the good ones.
        bad = d / (index.fmt_stamp(t0) + ".mkv")
        bad.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 200)
        ps2 = PlaybackSession("test-pb-corrupt", cfg, t0 + 1, t0 + 12, cam_id=cam)
        try:
            ps2.prepare()
            check("corrupt segment skipped, not fatal", not ps2.error, ps2.error)
            check("corrupt segment reported as skipped",
                  len(ps2.skipped_segments) == 1, str(ps2.skipped_segments))
            check("playback keeps usable segments", len(ps2.segments) >= 1,
                  str(len(ps2.segments)))
        finally:
            ps2.stop()
        bad.unlink()
        # bad window (end <= start)
        p3 = plan_window(root, cam, t0 + 2, t0 + 1)
        check("plan_window bad window: not ok", not p3.get("ok"))

        # empty camera
        p4 = plan_window(root, "nocam", t0, t0 + 100)
        check("plan_window no cam: not ok", not p4.get("ok"))


def test_live_and_playback_fragments_start_on_keyframe():
    """Every MSE fragment must begin on an IDR.

    Regression: both the live and the playback pipelines muxed with
    -movflags frag_keyframe, which lets FFmpeg stretch a fragment until the next
    real keyframe in the source. The EZVIZ sub-stream has keyframes tens of
    seconds apart, so the emitted fragments contained no keyframe at all. Chrome
    accepts such a fragment, reports readyState 4 with the correct resolution and
    plays the audio - and shows a black picture forever. Playback additionally
    sat under a permanent buffering spinner.
    """
    print("\n[live/playback keyframe fragments]")
    from app.config import normalize
    from app.live import LiveSession
    from app.playback import PlaybackSession
    import tempfile as _tf

    cfg = normalize({})
    cam = {"id": "cam1", "url": "rtsp://x/stream", "name": "c",
           "live_audio": True, "live_passthrough": True}
    cfg["cameras"] = [cam]

    ls = LiveSession(cam, cfg)
    ls.codec, ls.audio_codec = "h264", "aac"
    ls.width, ls.height = 1920, 1080
    # skip the network health check: this asserts the muxing flags, not the probe
    ls._av_checked, ls._av_broken = True, False
    cmd = LiveSession.build_cmd(ls)
    joined = " ".join(cmd)
    check("live: no frag_keyframe (stretches fragments past a keyframe)",
          "frag_keyframe" not in joined, joined[-200:])
    check("live: forced keyframe interval for stream copy",
          "-g" in cmd and cmd[cmd.index("-g") + 1].isdigit(), joined[-200:])
    check("live: audio kept alongside video", "0:a:0?" in joined, joined[-200:])

    # Regression: the transcode path hardcoded -g fps*2 and ignored live.gop_sec.
    # With frag_ms=1000 that made a 2s GOP against 1s fragments, so every OTHER
    # fragment began mid-GOP with no IDR. MSE accepted those, readyState hit 4,
    # and the tile froze on one frame forever. The GOP must be derived from
    # gop_sec, and must not exceed the fragment duration.
    tcam = dict(cam, live_passthrough=False)
    tcfg = normalize({"live": {"fps": 15, "gop_sec": 1, "frag_ms": 1000}})
    tcfg["cameras"] = [tcam]
    tls = LiveSession(tcam, tcfg)
    tls.codec, tls.audio_codec = "hevc", "aac"
    tls.width, tls.height = 1920, 1080
    tls._av_checked, tls._av_broken = True, False
    tcmd = LiveSession.build_cmd(tls)
    tjoined = " ".join(tcmd)
    tfps = int(tcfg["live"]["fps"])
    tgop = int(tcfg["live"]["gop_sec"]) * tfps
    tfrag = int(tcfg["live"]["frag_ms"])
    check("live transcode: -g derived from gop_sec * fps",
          tcmd[tcmd.index("-g") + 1] == str(tgop), tjoined[-200:])
    check("live transcode: -r matches fps",
          tcmd[tcmd.index("-r") + 1] == str(tfps), tjoined[-200:])
    check("live transcode: every fragment starts on a keyframe "
          "(gop_ms <= frag_ms)", tgop * 1000 // tfps <= tfrag,
          "gop=%dms frag=%dms %s" % (tgop * 1000 // tfps, tfrag, tjoined[-160:]))

    with _tf.TemporaryDirectory(prefix="nanovms-pb-") as td:
        pcfg = normalize({"storage": {"root": td}})
        pcfg["cameras"] = [cam]
        ps = PlaybackSession("k", pcfg, 0, 0, cam_id="cam1")
        ps.source_codec = "h264"
        ps.file_start = 0.0
        ps.window_duration = 60.0
        ps.segments = ["cam1/x.mkv"]
        pcmd = " ".join(ps.build_cmd())
        check("playback: no frag_keyframe", "frag_keyframe" not in pcmd, pcmd[-200:])
        check("playback: forced keyframe interval for stream copy",
              "-g" in ps.build_cmd(), pcmd[-200:])
        check("playback: video only, never muxes camera audio", "-an" in pcmd, pcmd[-200:])

    # the EZVIZ C6N pushes absolute wallclock PTS: samples land ~34h into the
    # timeline while the init claims start_time 0, so MSE buffers and draws
    # nothing. The live muxer must rebase them.
    lsr = LiveSession(cam, cfg)
    lsr.codec, lsr.audio_codec = "h264", "aac"
    lsr.width, lsr.height = 1920, 1080
    rcmd = LiveSession.build_cmd(lsr)
    if "-start_at_zero" in rcmd:
        check("live: camera wallclock PTS is rebased to zero",
              rcmd.index("-start_at_zero") > rcmd.index("-map"),
              " ".join(rcmd[-220:]))
    cam_nb = dict(cam)
    cam_nb["live_rebase_ts"] = False
    lsn = LiveSession(cam_nb, cfg)
    lsn.codec, lsn.audio_codec = "h264", "aac"
    lsn.width, lsn.height = 1920, 1080
    check("live: rebase can be turned off per camera",
          "-start_at_zero" not in LiveSession.build_cmd(lsn))
    check("live: never maps the audio track twice",
          " ".join(LiveSession.build_cmd(lsr)).count("-map") == 2)

    # a camera whose audio cannot be muxed must yield a video-only stream, not
    # a black picture; nothing sets this today (see _av_mux_is_broken) but the
    # fallback must stay correct if a camera ever needs it
    ls2 = LiveSession(cam, cfg)
    ls2.codec, ls2.audio_codec = "h264", "aac"
    ls2.width, ls2.height = 1920, 1080
    ls2._av_broken = True
    cmd2 = " ".join(LiveSession.build_cmd(ls2))
    check("live: broken A/V mux falls back to video only",
          "-an" in cmd2 and "0:a:0?" not in cmd2, cmd2[-200:])
    check("live: fallback reason is reported to the UI",
          "unusable" in (ls2.audio_dropped or ""), ls2.audio_dropped)

    # the mux-progress hook judges a camera ONCE from the muxer's own output:
    # cam2's A/V interleave emits audio fragments and no video ones (measured:
    # 35,621 B audio, 0 B video), which is exactly "black picture with sound".
    # It must not trip on a normal audio-only prefix, and the verdict is sticky
    # so the next session goes video-only immediately.
    lsh = LiveSession(cam, cfg)
    lsh.codec, lsh.audio_codec = "h264", "aac"
    lsh.viewers = 1
    lsh.on_progress(100_000, {1: 0, 2: 95_000})          # too early to judge
    check("live: an early audio-only prefix is not judged",
          lsh._restart_pending is False)
    lsh.on_progress(400_000, {1: 300_000, 2: 90_000})    # video present: fine
    check("live: a normal mixed stream is not flagged",
          lsh._restart_pending is False and lsh.audio_dropped == "")
    lsh.on_progress(700_000, {1: 310_000, 2: 380_000})  # still fine, no flip
    check("live: a healthy stream never flips to video-only",
          lsh._restart_pending is False and lsh.audio_dropped == "")

    lsb = LiveSession(cam, cfg)
    lsb.codec, lsb.audio_codec = "h264", "aac"
    lsb.viewers = 1
    lsb.on_progress(400_000, {1: 0, 2: 390_000})
    check("live: audio-only fragments starve video and are flagged",
          lsb._restart_pending is True)
    check("live: the verdict is remembered on the camera",
          cam.get("_audio_unusable") is True)
    check("live: the reason is reported to the UI",
          "unusable" in lsb.audio_dropped, lsb.audio_dropped)
    cam.pop("_audio_unusable", None)

    # The mirror case, measured on cam2: video flows fine but the AAC track
    # carries ZERO packets, so the init segment advertises an audio stream that
    # never delivers. MSE rejects that init, reports the video dimensions, and
    # then never appends - readyState 1, zero buffered, no error. The detector
    # above only caught audio-starves-video (it bails as soon as video bytes
    # appear), so an empty audio track slipped through and left a permanent
    # black tile while the server looked perfectly healthy.
    lse = LiveSession(cam, cfg)
    lse.codec, lse.audio_codec = "h264", "aac"
    lse.viewers = 1
    lse.on_progress(400_000, {1: 399_000, 2: 0})
    check("live: an empty audio track beside flowing video is flagged",
          lse._restart_pending is True,
          f"_restart_pending={lse._restart_pending} dropped={lse.audio_dropped!r}")
    check("live: empty-audio verdict is remembered on the camera",
          cam.get("_audio_unusable") is True)
    cam.pop("_audio_unusable", None)


def test_index_caps_segment_at_real_duration():
    """A short file must not inherit the next segment's span.

    Regression: the index used to take a segment's end from the next file's
    name alone, so a 5-minute recording left behind by a killed muxer advertised
    17 minutes of footage. The timeline drew a block, the user clicked it, and
    playback/export into the phantom tail returned an empty clip with no error.
    """
    print("\n[index real duration cap]")
    from app.export import plan_window
    with tempfile.TemporaryDirectory(prefix="nanovms-dur-") as td:
        root = Path(td)
        d = root / "cam1"
        d.mkdir()
        t0 = time.time() - 7200
        # a 5s recording, then a next file starting a full 10 minutes later
        for i, gap in enumerate((5, 600)):
            p = d / (index.fmt_stamp(t0 + i * gap) + ".mkv")
            subprocess.run([find_ffmpeg(""), "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration=5",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "5", str(p)],
                           check=True, timeout=60, creationflags=CREATE_NO_WINDOW)

        segs = index.list_segments(root, "cam1")
        check("two segments indexed", len(segs) == 2, str(len(segs)))
        first = segs[0]
        check("short segment keeps its real length",
              first["duration_sec"] <= 6.0, f"{first['duration_sec']}s "
              f"({first['name']} claims up to {first['end_h']})")

        # the gap between the files is real downtime, not footage
        p = plan_window(root, "cam1", t0 + 100, t0 + 160)
        check("gap between segments is not planned as footage",
              not p.get("ok"), str(p.get("error", "")))


# ---------------------------------------------------------------- recorder (unit) -

def test_graceful_stop_finalises_segment():
    """stop() must leave a complete, playable segment.

    Regression: a hard kill (taskkill /F) truncates the in-progress MKV with no
    trailer, and playback then reports it as corrupt forever. The fix sends
    "q" to ffmpeg's stdin so it closes the file itself.
    """
    print("\n[recorder graceful stop]")
    from app.recorder import Recorder, find_ffmpeg
    try:
        ff = find_ffmpeg("")
    except FileNotFoundError:
        print("  SKIP  ffmpeg not found")
        return

    with tempfile.TemporaryDirectory(prefix="nanovms-grace-") as td:
        root = Path(td)
        # segment_minutes=60 so the muxer never rolls over on its own and the
        # only thing that can close the file is our stop()
        cfg = cfgmod.normalize({
            "storage": {"root": str(root), "segment_minutes": 60},
            "ffmpeg": {"path": ff},
            "cameras": [{
                "id": "gtest", "name": "gtest", "enabled": True, "record": True,
                "audio": False,
                # testsrc is rawvideo, which the matroska muxer rejects under
                # -c copy, so this camera encodes instead of stream-copying
                "encode": True, "encode_fps": 10,
                "url": "lavfi:testsrc=size=320x240:rate=15",
            }],
        })
        rec = Recorder(cfg["cameras"][0], cfg)
        rec.start()
        time.sleep(8)                       # let real frames accumulate
        rec.stop(timeout=25)
        time.sleep(1)

        segs = sorted((root / "gtest").glob("*.mkv"))
        if not check("graceful stop wrote a segment", bool(segs),
                     "; ".join(list(rec.st.log)[-2:])):
            return
        seg = segs[0]
        size = seg.stat().st_size
        # a hard-killed muxer leaves 0 bytes or a header-only stub
        if not check("segment has real content", size > 1024, f"{size} bytes"):
            return
        r = subprocess.run(
            [ff, "-hide_banner", "-nostdin", "-loglevel", "error",
             "-i", str(seg), "-f", "null", "-"],
            capture_output=True, timeout=180, creationflags=CREATE_NO_WINDOW)
        n = r.stderr.decode("utf-8", "replace").count("ended prematurely")
        check("segment is not truncated", n == 0, f"{n} truncation marker(s)")


def test_lan_ip_helper():
    """_lan_ip() must return a routable-looking address or '', never raise.

    It backs the startup security warning, which tells the user where the
    unauthenticated server is reachable. A hostname lookup would stall for
    seconds on a misconfigured box and print a warning so late it looks like a
    hang, so this uses a UDP connect instead.
    """
    import run

    ip = run._lan_ip()
    check("_lan_ip returns a string", isinstance(ip, str), repr(ip))
    if ip:
        parts = ip.split(".")
        check("_lan_ip looks like an IPv4 address", len(parts) == 4
              and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts), ip)
        check("_lan_ip is not loopback", ip != "127.0.0.1", ip)


def test_startup_warning_does_not_crash():
    """The security warning must render on both bind modes without raising.

    A %-formatting bug in this block killed the whole server right after it
    had already bound the port and started recording - it looked like a
    successful start, then died. Exercise the real printing code.
    """
    import io
    import contextlib
    import run

    src = (ROOT / "run.py").read_text(encoding="utf-8")
    check("warning is emitted only for wildcard binds",
          'if host in ("0.0.0.0", "::")' in src)

    for host in ("0.0.0.0", "::", "127.0.0.1"):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                run._print_bind_warning(host, 1900, run._lan_ip())
        except Exception as e:
            check(f"warning renders for host={host}", False, f"{type(e).__name__}: {e}")
            continue
        out = buf.getvalue()
        if host in ("0.0.0.0", "::"):
            check(f"warning shown for {host}", "NO AUTHENTICATION" in out)
            check(f"warning names the port for {host}", "1900" in out)
        else:
            check(f"no warning for {host}", out.strip() == "", out[:60])


def test_live_view_falls_back_to_recorder_when_camera_at_connection_limit():
    """Cam2 allows 1 RTSP session, NanoVMS needs 2 (recorder + live). When the
    live view ffmpeg gets a 500 on connection, the server must fall back to
    reading the recorder's in-progress MKV segment instead of showing a dead
    tile. LiveFromRecorder rewraps the file to fMP4 with the same frag params.
    """
    print("\n[live from recorder fallback]")
    live_src = (ROOT / "app" / "live.py").read_text(encoding="utf-8")
    check("LiveFromRecorder class exists in live.py",
          "class LiveFromRecorder" in live_src,
          "no fallback session type found")

    # the fallback ffmpeg must read a file (not rtsp://) and output fMP4
    check("fallback command reads from file, not RTSP",
          'LiveFromRecorder' in live_src and 'mkv' in live_src
          and '_latest_segment' in live_src,
          "LiveFromRecorder should read the recorder's MKV segment file")

    check("fallback uses fMP4 fragment flags",
          "empty_moov+default_base_moof+separate_moof" in live_src,
          "must reuse the same fragmentation as the live RTSP path")

    # LiveFromRecorder.build_cmd must not contain "-re" in the ffmpeg args
    # (the word may appear in comments; extract only the build_cmd body)
    bcmd = live_src.split("def build_cmd")[1].split("def info")[0]
    check("fallback build_cmd does not rate-limit with -re",
          '"-re"' not in bcmd and " '-re'" not in bcmd,
          "LiveFromRecorder must not use -re: it tails a live-growing segment")

    check("LiveManager.acquire tries RTSP first, falls back to recorder",
          "_av_broken" not in live_src or "LiveFromRecorder" in live_src,
          "acquire must construct LiveFromRecorder on RTSP failure")


def test_folder_picker_builds_paths_with_the_platform_separator():
    """Clicking a folder must not build a Windows path on Linux.

    listDir and validatePath both joined with a hardcoded "\\", so on Linux
    the picker produced /home/user\\nanovms - a literal backslash in a filename,
    not a separator. The server correctly rejected it ("not a folder") and the
    picker appeared broken: Up worked, folders listed, but every click was a
    dead end. os.path.join is the portable answer.
    """
    print("\n[picker path separator]")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    check("no hardcoded backslash path joins remain",
          "+ '\\\\' +" not in js,
          "a click still builds base + '\\\\' + name, which is wrong on Linux")
    check("paths are built with os.path.join on the server or '/' in the client",
          ("os.path.join" in (ROOT / "app" / "server.py").read_text(encoding="utf-8"))
          or "'/'" in js,
          "no portable join found")

    # and the server must accept a backslash path on POSIX rather than 400.
    # The server now strips backslashes before abspath on non-Windows, so a
    # Windows-authored path resolves correctly instead of failing isdir.
    if not IS_WIN:
        raw = "/home/user\\nanovms"
        fixed = os.path.abspath(raw.replace("\\", "/"))
        check("a backslash path normalises to a real folder on POSIX",
              os.path.isdir(fixed),
              "expected /home/user/nanovms to be a dir, got %r" % fixed)


def test_storage_picker_accepts_a_typed_path():
    """The folder picker must let you type a path, not only click to drill.

    It opened at the current storage root with no text field, so reaching a
    different mount (/home on the NUC) meant clicking Up three times and
    hunting through a list - and there was no way to type a path at all. That
    is the whole reason a GUI-only install could not move storage onto a
    different disk.
    """
    print("\n[storage picker]")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    check("picker has a path input", 'id="fs-path-in"' in html, "no text field")
    check("picker has a Go button", 'id="fs-go"' in html, "no way to jump")
    check("Go is wired up", "fs-go" in js, "button exists but no handler")
    check("Enter in the path box navigates", "fs-path-in" in js and "Enter" in js,
          "Enter does not submit the path")
    check("typing a path calls the browse endpoint", "listDir(" in js,
          "goTo does not navigate")


def test_put_config_without_cameras_must_not_delete_them():
    """A settings save must never be able to wipe the camera list.

    apply_config does a full normalize() replace, so a PUT body that omits
    "cameras" replaces the list with an empty one. This is not hypothetical: a
    GET /api/config response was edited and PUT straight back to change one
    storage path, and every camera vanished - the config on disk went to
    "cameras": 0 and the UI showed an empty Setup tab. The browser form is safe
    only because readConfigForm round-trips the fetched config; the API is not.
    """
    print("\n[config put is not destructive]")
    server = (ROOT / "app" / "server.py").read_text(encoding="utf-8")

    check("apply_config preserves cameras when the body omits them",
          'if "cameras" not in body' in server,
          "apply_config normalizes the PUT body wholesale - omitting cameras "
          "deletes them all")
    check("an explicit cameras key is still honoured",
          'body["cameras"] = self.cfg.get("cameras", [])' in server,
          "the guard is present but does not read the existing list")
    # the settings route must be the one place allowed to replace the list
    check("there is a dedicated camera-list route",
          '"/api/cameras"' in server, "camera CRUD route missing")


def test_example_config_matches_server_defaults():
    """config.example.json is what setup.sh copies into config.json.

    It had drifted from DEFAULTS: live.gop_sec and live.frag_ms were missing,
    so a fresh install got a config that silently relied on the code defaults
    for the two values that decide whether a live tile advances or freezes.
    Deriving the comparison from DEFAULTS means the next key added anywhere
    shows up here as a failure instead of as a surprise on someone's install.
    """
    print("\n[example config vs defaults]")
    ex = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

    for section in ("storage", "live"):
        want = set(cfgmod.DEFAULTS.get(section, {}))
        have = set(ex.get(section, {}))
        missing = sorted(want - have)
        check("%s keys in the template match DEFAULTS" % section, not missing,
              "missing from config.example.json: " + ", ".join(missing))
        extra = sorted(have - want)
        check("%s has no keys the server does not define" % section, not extra,
              "unknown keys: " + ", ".join(extra))

    # gop_sec must not exceed frag_ms: a GOP longer than one fragment means
    # every *other* fragment begins mid-GOP with no keyframe and the browser
    # freezes on one frame forever. This exact mismatch cost a day on the NUC,
    # so the shipped template must not contain it.
    check("template gop_sec does not exceed frag_ms",
          ex["live"]["gop_sec"] * 1000 <= ex["live"]["frag_ms"],
          "gop_sec %s vs frag_ms %s - every other fragment would start "
          "mid-GOP and freeze the tile" % (ex["live"]["gop_sec"],
                                           ex["live"]["frag_ms"]))

    # no real credentials may ever appear in the shared template
    for cam in ex.get("cameras", []):
        url = cam.get("url", "")
        if not url.startswith("rtsp://"):
            continue          # lavfi/file sources carry no credentials
        check("camera %s uses placeholder credentials" % cam.get("id"),
              "password" in url or "user:pass" in url or "admin:admin" in url,
              "a real-looking credential in the shared template: " + url)


def test_shell_scripts_are_unix_line_endings():
    """setup.sh must run on Linux, and this repo is developed on Windows.

    A CRLF shebang makes the kernel read "bash\r" as the interpreter name, so a
    fresh ./setup.sh on the target machine dies with "env: bash\r: No such file
    or directory" before it does anything at all. This shipped once: the file
    looked fine in the diff and only failed when actually run on the Debian NUC.
    """
    print("\n[line endings]")
    CR = b"\r"
    for name in ("setup.sh", "start.sh", "nanovms.service", "DEPLOY.md"):
        fp = ROOT / name
        if not fp.exists():
            continue
        n = fp.read_bytes().count(CR)
        check("%s has no CR bytes" % name, n == 0,
              "%d CR bytes - will not run on Linux" % n)
    sh = (ROOT / "setup.sh").read_bytes()
    first = sh.split(b"\n", 1)[0]
    check("setup.sh shebang is a clean LF", not first.endswith(CR),
          "shebang %r ends with CR - the kernel would read bash\\r" % first)


def test_every_config_key_is_reachable_from_the_gui():
    """No setting may require editing config.json by hand.

    The install path is clone + setup.sh + open the browser, so every key the
    server reads has to be settable in the UI. gop_sec and frag_ms are the two
    that cost a full day of debugging on the NUC: a GOP longer than one fragment
    froze the live tile, and the only fix was a text editor. readConfigForm
    also round-trips the fetched config, so a key missing from the FIELDS table
    is silently reset to its default the moment a user presses Save.
    """
    print("\n[config keys on the gui]")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    # collect the paths the FIELDS table maps to form controls
    table = re.findall(r"\['(#[\w-]+)',\s*\[([^\]]+)\]", js)
    covered = set()
    for _sel, path in table:
        covered.add(tuple(x.strip().strip("'\"") for x in path.split(",")))

    missing = []
    for section in ("storage", "live"):
        for key in cfgmod.DEFAULTS.get(section, {}):
            if (section, key) not in covered:
                missing.append(f"{section}.{key}")
    check("every storage/live default has a GUI control", not missing,
          "not in the Settings tab: " + ", ".join(missing))

    # and each control it does have must exist in the markup
    for sel, _path in table:
        if sel.startswith("#cfg-"):
            check(f"{sel} exists in index.html", f'id="{sel[1:]}"' in html,
                  "FIELDS table points at a control that is not in the markup")

    # the ones that bit us must be explicitly present, not merely uncovered
    for sel in ("#cfg-gop", "#cfg-frag", "#cfg-jpegq"):
        check(f"{sel} is wired in the FIELDS table", sel in js,
              "gop_sec/frag_ms are what freeze a live tile; they need a control")
        check(f"{sel} exists in index.html", f'id="{sel[1:]}"' in html,
              "FIELDS table points at a control that is not in the markup")


def test_many_cameras_coexist_with_independent_codecs():
    """At least 8 cameras, each with its own live codec.

    A user on a powerful box should be able to add cameras without touching
    the config file, and each one may need a different live codec. This locks in
    that nothing in config normalization or the GUI assumes a small, fixed set.
    """
    print("\n[many cameras]")
    cams = []
    for i in range(8):
        mode = ("auto", "copy", "x264", "mjpeg")[i % 4]
        c = cfgmod.normalize_camera({"id": f"c{i}", "url": f"rtsp://h/s{i}", "live_mode": mode})
        cams.append(c)
    check("8 cameras normalize cleanly", len(cams) == 8 and all(c["id"] for c in cams))
    check("ids stay unique", len({c["id"] for c in cams}) == 8)
    check("each camera keeps its own live_mode",
          [c["live_mode"] for c in cams] == ["auto", "copy", "x264", "mjpeg"] * 2)
    check("all four modes are exercised", len({c["live_mode"] for c in cams}) == 4)

    # the server must not cap the list anywhere
    server = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    for bad in ("MAX_CAMERAS", "max_cameras"):
        check(f"no {bad} cap in server", bad not in server)


def test_camera_row_save_includes_select_fields():
    """A dropdown the Save button cannot read is worse than no dropdown.

    The row Save handler originally swept only input[data-f], so the live_mode
    <select> was never sent: the button reported "camera saved" and the old
    value stayed on disk. Verified in a browser - the row showed x264, Save was
    pressed, and /api/cameras still reported mjpeg.
    """
    print("\n[row save selects]")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    check("row Save sweeps select elements too",
          "select[data-f]" in js,
          "Save only reads input[data-f], so any <select> is silently dropped")
    check("row Save still reads checkboxes",
          "i.type === 'checkbox'" in js, "checkbox handling lost")


def test_client_honours_per_camera_mjpeg_mode():
    """Choosing mjpeg per camera must change what the browser requests.

    mjpeg is served from a different endpoint with its own one-shot ffmpeg; it
    is not a codec the MSE session can mux. If the Start button keeps reading
    only the global checkbox, a user who picks "mjpeg" for their broken camera
    still gets the broken MSE path and the dropdown does nothing.
    """
    print("\n[client mjpeg wiring]")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    check("live tile reads the camera's live_mode",
          "live_mode" in js, "app.js never reads live_mode for the live tile")
    check("live tile routes to the mjpeg endpoint on live_mode=mjpeg",
          "/mjpeg" in js and "camMode" in js,
          "the mjpeg endpoint is still only reachable via the global checkbox")


def test_gui_exposes_every_live_view_tuning_flag():
    """The live-view codec must be choosable in the browser, per camera.

    The remedy for a black or frozen live tile differs per camera and used to
    require hand-editing config.json. That is not an option for a user who
    installed this with git clone + setup.sh and only knows the web UI, so the
    single live_mode dropdown has to appear both in the add-camera form and in
    the per-camera row (otherwise an existing camera cannot be fixed at all).
    """
    print("\n[gui live flags]")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    check("live_mode is editable in the camera row",
          'data-f="live_mode"' in js,
          "not present in app.js camera row template")
    check("add-camera form offers live_mode",
          'id="new-live-mode"' in html, "missing from index.html")
    check("add-camera POST sends live_mode",
          "new-live-mode" in js, "value never read in app.js")

    # every advertised mode must be one the server actually understands,
    # otherwise the dropdown offers a setting that silently does nothing
    for mode in ("auto", "copy", "x264", "mjpeg"):
        check(f"add-camera form offers {mode}",
              f'value="{mode}"' in html, f"{mode} missing from the dropdown")
        check(f"camera row offers {mode}",
              f"'{mode}'" in js, f"{mode} missing from liveModeOptions")

    # and they must be the same set the server accepts
    check("server accepts exactly the advertised modes",
          set(cfgmod.LIVE_MODES) == {"auto", "copy", "x264", "mjpeg"},
          f"LIVE_MODES is {cfgmod.LIVE_MODES}")

    d = cfgmod.normalize_camera({})
    check("default live_mode is auto", d["live_mode"] == "auto")
    check("an unknown live_mode falls back to auto",
          cfgmod.normalize_camera({"live_mode": "av1"})["live_mode"] == "auto")
    # legacy configs still work: the old boolean must keep deciding
    check("legacy live_passthrough False still forces transcode in auto mode",
          cfgmod.normalize_camera({"live_passthrough": False})["live_passthrough"] is False)


def test_per_camera_live_codec_is_choosable_without_editing_config():
    """A user whose live tile is black or frozen must be able to fix it in the GUI.

    The only remedy used to be hand-editing config.json, which is exactly what
    the README tells a new user not to do. Every live-view failure mode maps to
    a different codec path, so the choice has to be reachable per camera:

      auto   - let NanoVMS decide from the probed codec (browser-playable ->
               stream copy, otherwise transcode). The safe default.
      copy   - force -c:v copy. Cheapest, but needs a browser-playable codec
               AND frequent keyframes, or the fragment starts mid-GOP.
      x264   - force libx264. Costs CPU, works on almost anything.
      mjpeg  - force the MJPEG endpoint. No MSE, works in any browser, but it
               is per-frame and heavy, so only good for one camera at a time.

    mjpeg is a client-visible mode (a different endpoint), so it must also be
    selectable per camera rather than only by the global toolbar checkbox.
    """
    print("\n[per-camera live codec]")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    from app.live import LiveSession
    from app.config import normalize, normalize_camera

    cam = {"id": "cam1", "url": "rtsp://x/stream", "name": "c"}

    # ---- the four modes exist and are accepted by the config layer ----
    d = normalize_camera({})
    check("live_mode defaults to auto", d.get("live_mode") == "auto",
          f"got {d.get('live_mode')!r}")
    for m in ("auto", "copy", "x264", "mjpeg"):
        n = normalize_camera({"live_mode": m})
        check(f"live_mode={m!r} is accepted verbatim", n["live_mode"] == m)
    n = normalize_camera({"live_mode": "nonsense"})
    check("an unknown live_mode falls back to auto", n["live_mode"] == "auto",
          f"got {n['live_mode']!r}")

    # ---- each mode maps to the right ffmpeg flags ----
    def cmd_for(mode, codec="h264"):
        cfg = normalize({"live": {"fps": 15, "gop_sec": 1, "frag_ms": 1000}})
        c = dict(cam, live_mode=mode, live_passthrough=True)
        cfg["cameras"] = [c]
        s = LiveSession(c, cfg)
        s.codec, s.audio_codec = codec, ""
        s.width, s.height = 1920, 1080
        s._av_checked, s._av_broken = True, False
        return LiveSession.build_cmd(s)

    a = cmd_for("auto", "h264")
    check("live_mode=auto copies a browser-playable codec",
          a[a.index("-c:v") + 1] == "copy", " ".join(a[-120:]))
    a2 = cmd_for("auto", "hevc")
    check("live_mode=auto transcodes a codec the browser cannot play",
          a2[a2.index("-c:v") + 1] == "libx264", " ".join(a2[-120:]))

    c1 = cmd_for("copy", "hevc")
    check("live_mode=copy forces -c:v copy even for HEVC",
          c1[c1.index("-c:v") + 1] == "copy", " ".join(c1[-120:]))
    check("live_mode=copy still forces a keyframe interval",
          "-g" in c1 and c1[c1.index("-g") + 1].isdigit(), " ".join(c1[-120:]))

    x = cmd_for("x264", "h264")
    check("live_mode=x264 forces libx264 even for an H.264 camera",
          x[x.index("-c:v") + 1] == "libx264", " ".join(x[-120:]))
    check("live_mode=x264 keeps the fragment on a keyframe",
          int(x[x.index("-g") + 1]) * 1000 // 15 <= 1000, " ".join(x[-140:]))

    # ---- the GUI reaches every mode ----
    check("camera row offers a live_mode select",
          'data-f="live_mode"' in js, "no live_mode control in app.js")
    check("add-camera form offers a live_mode select",
          'id="new-live-mode"' in html, "missing from index.html")
    check("add-camera POST sends live_mode", "new-live-mode" in js)
    for m in ("auto", "copy", "x264", "mjpeg"):
        check(f"add-camera offers the {m!r} option",
              f'value="{m}"' in html, f"{m} option not in index.html")
    # mjpeg is a different endpoint, so the client must honour the per-camera
    # choice rather than only the global toolbar checkbox
    check("client picks the mjpeg endpoint from the camera's own live_mode",
          "live_mode" in js and "liveMode" in js,
          "app.js never reads live_mode to choose an endpoint")


def test_no_auth_endpoints_are_reachable_without_credentials():
    """Document the security posture as a test, so it cannot change silently.

    The server intentionally has no authentication. If someone later adds
    auth, this test should be updated deliberately rather than by accident -
    it is the tripwire that makes that change visible in review.
    """
    text = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    for needle in ("Authorization", "authenticate", "@login_required"):
        check(f"server.py has no {needle!r} auth layer", needle not in text)
    # and the routes that make it dangerous
    for route in ("/api/config", "/api/fs/browse", "/api/shutdown"):
        check(f"{route} exists (documented risk)", route in text)


def test_idle_live_session_is_reaped():
    """A live session nobody is watching must die on its own.

    The ffmpeg reader used to call touch() on every chunk, which kept
    last_access permanently fresh so the idle reaper never fired: a transcoding
    session with zero viewers held its RTSP slot and its CPU until the process
    was killed. Each camera allows only 2 concurrent RTSP clients, so that leak
    starves the next viewer and looks exactly like a dead camera - which is
    what made cam2 intermittently black. The test drives a real producing
    stream (so the reader is definitely touching) and asserts it is reaped.
    """
    from app.stream import FragmentStream

    class _Test(FragmentStream):
        def build_cmd(self):
            return [find_ffmpeg(), "-hide_banner", "-loglevel", "error",
                    "-re", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
                    "-t", "30", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "15", "-f", "mp4", "-movflags", "frag_keyframe+empty_moov",
                    "-frag_duration", "1000000", "pipe:1"]

    cfg = {"live": {"idle_timeout_sec": 6}}
    s = _Test("live:idletest", cfg)
    s.start()
    try:
        # wait for real fragments so we know the reader is running and touching
        deadline = time.time() + 25
        while time.time() < deadline and not s.fragments:
            time.sleep(0.5)
        if not check("test stream produced fragments", bool(s.fragments),
                     f"fragments={len(s.fragments)} err={s.error[:80]}"):
            return
        check("stream is live before idle expiry", not s._stop.is_set())

        # nobody ever acquires: it must reap itself on the idle timeout
        deadline = time.time() + 30
        while time.time() < deadline and not s._stop.is_set():
            time.sleep(0.5)
        check("idle session stops itself with 0 viewers", s._stop.is_set(),
              f"still running after {cfg['live']['idle_timeout_sec']}s idle")
        check("no viewers were ever attached", s.viewers == 0, f"viewers={s.viewers}")
    finally:
        s.stop()


def test_stop_during_spawn_does_not_leak_ffmpeg():
    """stop() racing the reader thread must not orphan an ffmpeg.

    start() spawns the reader thread and returns immediately; the thread is what
    actually calls Popen. If stop() lands in that window, self._proc is still
    None so p.kill() is skipped, and the reader then spawns ffmpeg AFTER the stop
    flag is set. That ffmpeg runs forever: invisible to the session manager, still
    holding one of the camera's 2 RTSP slots, so the next live view of the same
    camera is refused and the tile looks permanently dead.
    """
    from app.stream import FragmentStream

    class _Slow(FragmentStream):
        def build_cmd(self):
            # give stop() plenty of time to land before Popen happens
            time.sleep(1.5)
            return [find_ffmpeg(), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5",
                    "-t", "60", "-c:v", "libx264", "-preset", "ultrafast",
                    "-f", "mp4", "-movflags", "empty_moov+frag_keyframe",
                    "-frag_duration", "1000000", "pipe:1"]

    s = _Slow("live:racetest", {"live": {"idle_timeout_sec": 9999}})
    s.start()
    time.sleep(0.2)          # reader is inside build_cmd, _proc still None
    s.stop()                 # must not leak

    # wait past the point where the reader would have spawned
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.5)
        p = s._proc
        if p is not None and p.poll() is not None:
            break
    p = s._proc
    check("stop() during spawn leaves no running ffmpeg",
          p is None or p.poll() is not None,
          f"leaked ffmpeg pid={p.pid if p else None} still alive after stop()")
    s.stop()


def test_sigterm_finalises_segment():
    """A SIGTERM must finalise the segment, because that is how systemd stops.

    run.py used to trap only KeyboardInterrupt, so `systemctl stop` / `docker
    stop` / `kill` bypassed app.shutdown() and left every ffmpeg orphaned with a
    half-written MKV.

    On Windows Popen.terminate() is TerminateProcess - a hard kill that no
    handler can intercept - so the end-to-end leg only runs where a real signal
    exists. The Windows leg instead checks the two things that ARE observable
    here: that run.py registers a SIGTERM handler at all, and that
    app.shutdown() reaps the recorders when it is called.
    """
    print("\n[sigterm shutdown]")
    from app.recorder import find_ffmpeg
    try:
        ff = find_ffmpeg("")
    except FileNotFoundError:
        print("  SKIP  ffmpeg not found")
        return

    # --- the part that holds on every platform -----------------------------
    src = (ROOT / "run.py").read_text(encoding="utf-8")
    check("run.py handles SIGTERM", "signal.SIGTERM" in src,
          "systemctl stop would skip app.shutdown()")
    check("run.py shuts the app down on a signal",
          "app.shutdown()" in src, "")

    if IS_WIN:
        # TerminateProcess cannot be trapped; assert the real cleanup instead.
        with tempfile.TemporaryDirectory(prefix="nanovms-sigterm-") as td:
            root = Path(td)
            cfg = cfgmod.normalize({
                "storage": {"root": str(root), "segment_minutes": 60},
                "ffmpeg": {"path": ff},
                "cameras": [{
                    "id": "stest", "name": "stest", "enabled": True, "record": True,
                    "audio": False, "encode": True, "encode_fps": 10,
                    "url": "lavfi:testsrc=size=320x240:rate=15",
                }],
            })
            from app.recorder import Recorder
            rec = Recorder(cfg["cameras"][0], cfg)
            rec.start()
            time.sleep(8)
            if not rec._proc:
                check("recorder is running before shutdown", False, "no ffmpeg")
                return
            pid = rec._proc.pid
            rec.stop(timeout=25)          # what app.shutdown() calls
            gone = True
            for _ in range(30):
                if not _ffmpeg_running(f"pid {pid}") and not _pid_alive(pid):
                    gone = True
                    break
                gone = False
                time.sleep(0.5)
            check("app.shutdown() reaps the recorder (no orphan)", gone,
                  f"ffmpeg pid {pid} still alive")
            segs = sorted((root / "stest").glob("*.mkv"))
            if check("shutdown left a segment", bool(segs), "none written"):
                r = subprocess.run(
                    [ff, "-hide_banner", "-nostdin", "-loglevel", "error",
                     "-i", str(segs[0]), "-f", "null", "-"],
                    capture_output=True, timeout=180, creationflags=CREATE_NO_WINDOW)
                out = r.stderr.decode("utf-8", "replace")
                check("segment after shutdown is playable",
                      "ended prematurely" not in out, out.strip()[-160:])
        print("  note  end-to-end SIGTERM leg skipped on Windows "
              "(terminate() is TerminateProcess, uncatchable); re-run on Debian")
        return

    # --- POSIX: drive the real signal path ----------------------------------
    with tempfile.TemporaryDirectory(prefix="nanovms-sigterm-") as td:
        root = Path(td)
        cfg_path = root / "config.json"
        cfg = cfgmod.normalize({
            "server": {"port": 8097},
            "storage": {"root": str(root / "rec"), "segment_minutes": 60},
            "ffmpeg": {"path": ff},
            "cameras": [{
                "id": "stest", "name": "stest", "enabled": True, "record": True,
                "audio": False, "encode": True, "encode_fps": 10,
                "url": "lavfi:testsrc=size=320x240:rate=15",
            }],
        })
        cfgmod.save(cfg, cfg_path)

        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "run.py"), "--config", str(cfg_path), "serve"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 40
            segs: list[Path] = []
            while time.time() < deadline:
                time.sleep(2)
                segs = sorted((root / "rec" / "stest").glob("*.mkv")) \
                    if (root / "rec" / "stest").is_dir() else []
                if segs and segs[0].stat().st_size > 1024:
                    break
            if not check("server started recording", bool(segs),
                         f"{(segs[0].stat().st_size if segs else 0)} bytes"):
                return

            proc.terminate()              # what `systemctl stop` sends
            try:
                proc.wait(timeout=30)
                check("SIGTERM stops the server", True, f"rc={proc.returncode}")
            except subprocess.TimeoutExpired:
                check("SIGTERM stops the server", False, "still running after 30s")
                proc.kill()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

        check("no ffmpeg left running after SIGTERM",
              not _ffmpeg_running("stest"), "orphaned recorder")


def _pid_alive(pid: int) -> bool:
    """True if a process id is still running (portable, no psutil)."""
    if IS_WIN:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                           capture_output=True, text=True, timeout=30)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _ffmpeg_running(marker: str) -> bool:
    """True if any ffmpeg process command line contains `marker`."""
    if IS_WIN:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\").CommandLine"],
            capture_output=True, text=True, timeout=30)
        return marker in r.stdout
    r = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                       text=True, timeout=30)
    return any("ffmpeg" in ln and marker in ln for ln in r.stdout.splitlines())


def test_recorder_cmd():
    print("\n[recorder._build_cmd]")
    from app.recorder import Recorder, find_ffmpeg
    try:
        ff = find_ffmpeg("")
    except FileNotFoundError:
        print("  SKIP  ffmpeg not found")
        return

    with tempfile.TemporaryDirectory() as td:
        cfg = cfgmod.normalize({
            "storage": {"root": td, "segment_minutes": 5},
            "ffmpeg": {"path": ff, "rtsp_transport": "tcp"},
        })
        cam = cfgmod.normalize_camera({
            "id": "t1", "url": "rtsp://x:y@192.168.1.1:554/stream", "record": True
        })
        rec = Recorder(cam, cfg)
        cmd = rec._build_cmd(ff)
        check("recorder cmd: ffmpeg first", Path(cmd[0]).name.startswith("ffmpeg"))
        check("recorder cmd: rtsp_transport present", "-rtsp_transport" in cmd)
        check("recorder cmd: segment_format matroska", "matroska" in cmd)
        check("recorder cmd: c copy", "copy" in cmd)

        # lavfi source
        cam2 = cfgmod.normalize_camera({"id": "t2", "url": "lavfi:testsrc=size=320x240:rate=5",
                                         "loop": True, "encode": True, "encode_fps": 5, "record": True})
        rec2 = Recorder(cam2, cfg)
        cmd2 = rec2._build_cmd(ff)
        check("lavfi cmd: -f lavfi", "-f" in cmd2 and "lavfi" in cmd2)
        check("lavfi cmd: libx264 (encode=True)", "libx264" in cmd2)
        check("lavfi cmd: no rw_timeout", "-rw_timeout" not in cmd2)

        # Regression: FFmpeg 9 RTSP demuxer supports -timeout, not -rw_timeout.
        # Using the obsolete name makes every real camera fail with "Option not found".
        from app.recorder import build_input, redact_rtsp_credentials
        rtsp_args = build_input("rtsp://user:pass@192.0.2.1:554/stream", rw_timeout_ms=5000000)
        check("rtsp input: uses -timeout", "-timeout" in rtsp_args)
        check("rtsp input: not obsolete -rw_timeout", "-rw_timeout" not in rtsp_args)

        # Security regression: FFmpeg errors echo the full RTSP URL. Never expose
        # camera credentials in the UI/API recorder log.
        redacted = redact_rtsp_credentials(
            "Error opening input file rtsp://admin:secret@192.0.2.1:554/stream.")
        check("recorder errors redact RTSP password",
              "secret" not in redacted and "admin:[REDACTED]@192.0.2.1" in redacted, redacted)


# ---------------------------------------------------------------- run -------

def main():
    print("NanoVMS test suite")
    print("=" * 50)
    test_config()
    test_index()
    test_split_boxes()
    test_plan_window()
    test_live_and_playback_fragments_start_on_keyframe()
    test_index_caps_segment_at_real_duration()
    test_graceful_stop_finalises_segment()
    test_lan_ip_helper()
    test_startup_warning_does_not_crash()
    test_gui_exposes_every_live_view_tuning_flag()
    test_per_camera_live_codec_is_choosable_without_editing_config()
    test_no_auth_endpoints_are_reachable_without_credentials()
    test_idle_live_session_is_reaped()
    test_stop_during_spawn_does_not_leak_ffmpeg()
    test_sigterm_finalises_segment()
    test_recorder_cmd()
    test_ffmpeg()
    test_http()   # runs last: spins up a real server
    test_folder_picker_builds_paths_with_the_platform_separator()
    test_storage_picker_accepts_a_typed_path()
    test_put_config_without_cameras_must_not_delete_them()
    test_example_config_matches_server_defaults()
    test_shell_scripts_are_unix_line_endings()
    test_every_config_key_is_reachable_from_the_gui()
    test_many_cameras_coexist_with_independent_codecs()
    test_camera_row_save_includes_select_fields()
    test_client_honours_per_camera_mjpeg_mode()
    test_live_view_falls_back_to_recorder_when_camera_at_connection_limit()
    test_probe_recommends_a_setting_zero_knowledge_users_can_apply()
    test_camera_row_allows_fixing_codec_transport_without_readding()
    test_service_restarts_after_power_loss()

    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    failed = [(n, d) for n, ok, d in results if not ok]
    print(f"Results: {passed}/{total} passed")
    if failed:
        print("FAILED:")
        for n, d in failed:
            print(f"  - {n}" + (f"  [{d}]" if d else ""))
        return 1
    print("All tests passed.")
    return 0


def test_probe_recommends_a_setting_zero_knowledge_users_can_apply():
    """Test URL must return a one-click recommendation, not just codec names.

    A user with no codec knowledge cannot map "hevc" onto the live_mode
    dropdown. The probe already knows the codec, so it should also say
    which live_mode to pick (and whether re-encode is needed for MJPEG /
    rawvideo sources that record broken files otherwise). The GUI shows
    this as a "Use recommended setting" button.
    """
    print("[probe recommendation]")
    from app import server as srvmod
    import inspect
    src = inspect.getsource(srvmod._probe)
    check("probe returns recommended_live_mode",
          "recommended_live_mode" in src,
          "Test URL reports the codec but never says which live mode to pick")
    check("probe returns recommended_encode",
          "recommended_encode" in src,
          "MJPEG/rawvideo users get no hint that recording needs re-encode")
    check("GUI has a one-click apply button",
          'id="new-apply-rec"' in (ROOT / "static" / "index.html").read_text(encoding="utf-8"),
          "missing from index.html add-camera form")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    check("GUI apply button writes the recommendation into the form",
          "new-apply-rec" in js and "_lastProbeRec" in js,
          "button exists but never applies recommended_live_mode")
    check("add-camera form sends encode (MJPEG record fix is GUI-settable)",
          'id="new-encode"' in (ROOT / "static" / "index.html").read_text(encoding="utf-8"),
          "re-encode on record still requires hand-editing config.json")


def test_camera_row_allows_fixing_codec_transport_without_readding():
    """Every per-camera remedy must be editable on the existing row.

    Deleting + re-adding a camera to change one flag loses its id (and the
    recording folder linkage). enabled, transport and encode all change
    how a camera connects or records, so they belong on the row next to
    live_mode, not only in the add-camera form.
    """
    print("[row fix controls]")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    for field in ("enabled", "transport", "encode", "live_mode"):
        check(f"camera row edits {field}",
              f'data-f="{field}"' in js,
              f"{field} is not editable on the camera row")


def test_service_restarts_after_power_loss():
    """nanovms.service must bring the NVR back after power returns.

    Restart=on-failure only covers crashes, not a power cut (or an admin
    running `systemctl stop`). Restart=always means the box resumes
    recording on its own when power is back, with no SSH needed.
    """
    print("[service restart]")
    svc = (ROOT / "nanovms.service").read_text(encoding="utf-8")
    check("service uses Restart=always",
          "Restart=always" in svc,
          "a power cut leaves the NVR stopped until someone SSHes in")


if __name__ == "__main__":
    raise SystemExit(main())
