"""NanoVMS CLI.

    python run.py                     start server (config.json)
    python run.py --port 9000
    python run.py test <url>          probe a camera URL without saving it
    python run.py add <name> <url>    add a camera to config.json
    python run.py sweep [--dry]       run retention/free-space cleanup
    python run.py stats               storage + camera summary
    python run.py check               environment/ffmpeg check
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config as cfgmod          # noqa: E402
from app import index                     # noqa: E402


def _lan_ip() -> str:
    """Best-effort LAN address, so the startup warning is actionable.

    Avoids a DNS lookup of the hostname, which can block for seconds on a
    misconfigured Windows box; connects a UDP socket (no packets are sent)
    and reads back the address the OS would route through.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.2)
        s.connect(("192.0.2.1", 9))     # TEST-NET-1: unroutable, never leaves
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def _print_bind_warning(host: str, port: int, lan: str = "") -> None:
    """Print the no-authentication notice, but only on a wildcard bind.

    Split out of cmd_serve so it can be exercised directly: an inlined block
    of %-formatting once carried a stray tuple that killed the server *after*
    it had bound the port and started recording, which looks like a clean
    start right up until the process disappears.
    """
    if host not in ("0.0.0.0", "::"):
        return
    # The server has no authentication, so on a multi-device or untrusted
    # network this is a real exposure, not a theoretical one: /api/config
    # returns camera RTSP urls (with passwords), /api/fs/browse walks the
    # filesystem, and /api/shutdown stops the server. Binding every interface
    # stays the default on purpose - viewing an NVR from a phone is the point -
    # but the user has to be told, once, at startup.
    lan = lan or _lan_ip()
    print()
    print("  " + "!" * 66)
    print("  !  NO AUTHENTICATION. Anyone who can reach this port can read")
    print("  !  your camera passwords, browse your files and stop the server.")
    if lan:
        print("  !  Reachable at: http://%s:%d" % (lan, port))
    else:
        print("  !  Bound to all network interfaces.")
    print("  !  Fine on a private home LAN. Use a VPN, an SSH tunnel")
    print("  !  (ssh -L %d:127.0.0.1:%d you@host), or set server.host to"
          % (port, port))
    print("  !  127.0.0.1 in config.json on any shared/public network.")
    print("  " + "!" * 66)
    print()


def cmd_serve(args) -> int:
    from app.server import serve
    cfg = cfgmod.load(args.config)
    if args.port:
        cfg["server"]["port"] = args.port
    if args.host:
        cfg["server"]["host"] = args.host
    if not Path(cfg["storage"]["root"]).exists():
        Path(cfg["storage"]["root"]).mkdir(parents=True, exist_ok=True)

    app, httpd = serve(cfg, config_path=cfgmod.CONFIG_PATH if args.config is None else args.config)
    host, port = httpd.server_address[0], httpd.server_address[1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    # recorder threads spawn ffmpeg asynchronously, so a zero here right after
    # serve() is just startup ordering - not a failure. Give them a moment.
    deadline = time.time() + 3.0
    n_rec = app.recorders.recording_count()
    while time.time() < deadline and n_rec < len(cfg["cameras"]):
        time.sleep(0.1)
        n_rec = app.recorders.recording_count()
    print(f"NanoVMS listening on http://{shown}:{port}  "
          f"({n_rec}/{len(cfg['cameras'])} cameras recording)")
    _print_bind_warning(host, port)
    if args.open_browser:
        import webbrowser
        threading.Thread(target=lambda: (time.sleep(0.6),
                                         webbrowser.open(f"http://{shown}:{port}")),
                         daemon=True).start()
    # systemd stops services with SIGTERM, not SIGINT: without this handler a
    # `systemctl stop` / `docker stop` / `kill` would bypass app.shutdown(),
    # orphan every ffmpeg recorder and truncate the in-progress segment. Only
    # KeyboardInterrupt (Ctrl-C) and SIGTERM are trapped; SIGKILL cannot be
    # caught, so the service file must use TimeoutStopSec with ExecStop.
    stopping = threading.Event()

    def _on_signal(signum, _frame):
        print(f"\nstopping (signal {signum})...", flush=True)
        stopping.set()
        # ask serve_forever to return; doing it from a handler thread that then
        # joins itself would deadlock
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            # not on the main thread, or the platform lacks it: harmless
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        app.shutdown()
        httpd.server_close()
    return 0


def cmd_test(args) -> int:
    from app.server import _probe
    cfg = cfgmod.load(args.config)
    res = _probe(args.url, cfg, transport=args.transport, timeout=args.timeout)
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def cmd_add(args) -> int:
    cfg = cfgmod.load(args.config)
    cam = cfgmod.normalize_camera({
        "name": args.name,
        "url": args.url,
        "record": not args.no_record,
        "audio": args.audio,
        "id": args.id or "",
    }, len(cfg["cameras"]))
    if any(c["id"] == cam["id"] for c in cfg["cameras"]):
        i = 2
        while any(c["id"] == cam["id"] for c in cfg["cameras"]):
            cam["id"] = f"{cam['id'].rstrip('-0123456789') or 'cam'}-{i}"
            i += 1
    cfg["cameras"].append(cam)
    cfgmod.save(cfg)
    print(f"added {cam['id']} ({cam['name']}) record={cam['record']}")
    print(f"config: {cfgmod.CONFIG_PATH}")
    return 0


def cmd_sweep(args) -> int:
    cfg = cfgmod.load(args.config)
    res = index.sweep(cfg, dry_run=args.dry)
    print(json.dumps({k: v for k, v in res.items() if k != "items"}, indent=2))
    if args.verbose:
        for it in res["items"]:
            print(f"  {it['reason']:<12} {it['cam_id']:<10} {it['bytes']:>12,}  {it['path']}")
    return 0


def cmd_stats(args) -> int:
    cfg = cfgmod.load(args.config)
    st = index.storage_stats(cfg)
    d = st["disk"]
    print(f"storage root : {st['root']}")
    print(f"total used   : {st['bytes'] / 1024**2:,.1f} MB")
    print(f"disk         : {d['used'] / 1024**3:.1f}/{d['total'] / 1024**3:.1f} GB "
          f"({d['percent']}%) free {d['free'] / 1024**3:.1f} GB")
    print(f"clips        : {st['clips']['count']} ({st['clips']['bytes'] / 1024**2:.1f} MB)")
    print()
    print(f"{'camera':<12} {'segments':>9} {'days':>5} {'size MB':>10}")
    for c in st["cameras"]:
        print(f"{c['cam_id']:<12} {c['segments']:>9} {c['days']:>5} {c['bytes'] / 1024**2:>10.1f}")
    return 0


def cmd_check(args) -> int:
    ok = True
    print(f"python       : {sys.version.split()[0]}")
    print(f"config       : {cfgmod.CONFIG_PATH} "
          f"({'exists' if cfgmod.CONFIG_PATH.exists() else 'will be created'})")
    try:
        from app.recorder import find_ffmpeg, find_ffprobe
        f = find_ffmpeg(args.ffmpeg or "")
        print(f"ffmpeg       : {f}")
        print(f"ffprobe      : {find_ffprobe(f) or 'MISSING'}")
    except FileNotFoundError as e:
        ok = False
        print(f"ffmpeg       : NOT FOUND ({e})")
    cfg = cfgmod.load(args.config)
    root = Path(cfg["storage"]["root"])
    try:
        root.mkdir(parents=True, exist_ok=True)
        t = root / ".write-test"
        t.write_text("x")
        t.unlink()
        print(f"storage      : {root} (writable)")
    except OSError as e:
        ok = False
        print(f"storage      : {root} NOT WRITABLE ({e})")
    print(f"cameras      : {len(cfg['cameras'])}")
    for c in cfg["cameras"]:
        state = "on" if c.get("record") else "off"
        print(f"  - {c['id']:<12} {c['name'][:28]:<28} rec={state} {c['url'][:52]}")
    print(f"server       : will bind {cfg['server']['host']}:{cfg['server']['port']}")
    print()
    print("READY" if ok else "NOT READY - fix the items above")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="run.py", description="NanoVMS - lightweight NVR")
    p.add_argument("--config", default=None, help="path to config.json")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="start the server (default)")
    s.add_argument("--port", type=int)
    s.add_argument("--host")
    s.add_argument("--open", dest="open_browser", action="store_true",
                   help="open the UI in a browser")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("test", help="probe a camera URL")
    s.add_argument("url")
    s.add_argument("--transport", default="tcp", choices=["tcp", "udp", "udp_multicast", "http"])
    s.add_argument("--timeout", type=float, default=12)
    s.set_defaults(func=cmd_test)

    s = sub.add_parser("add", help="add a camera")
    s.add_argument("name")
    s.add_argument("url")
    s.add_argument("--id")
    s.add_argument("--no-record", action="store_true")
    s.add_argument("--audio", action="store_true")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("sweep", help="run retention cleanup")
    s.add_argument("--dry", action="store_true", help="report only, delete nothing")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_sweep)

    s = sub.add_parser("stats", help="storage summary")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("check", help="environment check")
    s.add_argument("--ffmpeg", default="")
    s.set_defaults(func=cmd_check)

    args = p.parse_args()
    if not getattr(args, "func", None):
        args = p.parse_args(["serve"])
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
