"""patchbay CLI: run poll cycles and inspect the normalized model."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time

from . import __version__, db
from .collectors import all_collectors, available
from .config import load_settings


def cmd_poll(args: argparse.Namespace) -> int:
    settings = load_settings()
    collectors = available(settings)
    if args.source:
        missing = set(args.source) - set(collectors)
        if missing:
            unconfigured = set(args.source) & set(all_collectors())
            print(f"not configured or unknown: {', '.join(sorted(missing))} "
                  f"(configured: {', '.join(sorted(collectors)) or 'none'})", file=sys.stderr)
            return 2 if missing - unconfigured else 1
        collectors = {n: c for n, c in collectors.items() if n in args.source}
    if not collectors:
        print("no collectors configured — set credentials in the env file", file=sys.stderr)
        return 1
    rc = 0
    lines: list[str] = []

    def say(line: str, err: bool = False) -> None:
        lines.append(line)
        print(line, file=sys.stderr if err else sys.stdout)

    for w in settings.parse_warnings:
        say(f"[warn] {w}", err=True)
    with db.connect(settings.db_path) as conn:
        # `compose up` starts the web app and this poller together, and on a
        # fresh database both run the same schema migrations at once. Real
        # filesystems serialize that on SQLite's lock; Docker Desktop's
        # shared mounts (macOS/Windows) can surface the collision as a
        # spurious "disk I/O error" instead, so give the peer a moment and
        # try once more before believing it.
        try:
            db.init(conn)
        except sqlite3.OperationalError:
            time.sleep(3)
            db.init(conn)
        for name, collector in sorted(collectors.items()):
            try:
                summary = collector.collect(settings, conn)
                conn.commit()  # each source lands atomically
                say(f"[ok]   {name}: {summary}")
            except Exception as e:  # one failing source must not block the others
                conn.rollback()  # ...and must not commit half a collection
                say(f"[fail] {name}: {e}", err=True)
                rc = 1
        from .normalize import normalize

        try:
            say(f"[ok]   normalize: "
                f"{normalize(conn, settings.aliases, settings.unmanaged, settings.links, settings.vlan_filters, prune_declared=settings.declarations_readable)}")
        except Exception as e:  # a normalize bug must not discard the cycle
            conn.rollback()
            say(f"[fail] normalize: {e}", err=True)
            rc = 1
        db.save_last_poll(conn, lines)
        try:
            from . import snapshot as snap

            due = snap.due_today(conn, settings)
        except ImportError:  # no web extra installed — snapshots unavailable
            due = False
    # the daily snapshot runs after the poll transaction commits: it opens its
    # own connection and would otherwise contend with this one
    if due:
        try:
            path = snap.write_snapshot(settings)
            say(f"[ok]   snapshot: {path}")
        except snap.DeliveryError as e:
            say(f"[warn] snapshot written but delivery failed: {e}", err=True)
        except Exception as e:
            say(f"[fail] snapshot: {e}", err=True)
            rc = 1
        with db.connect(settings.db_path) as conn:
            snap.mark_done(conn)      # once a day even if delivery failed —
            db.save_last_poll(conn, lines)   # retrying every 5 min won't help
    return rc


def cmd_web(args: argparse.Namespace) -> int:
    import uvicorn

    settings = load_settings()
    if settings.auth_mode not in ("none", "password", "oidc"):
        print(f"PATCHBAY_AUTH={settings.auth_mode!r} — expected none, password, or oidc",
              file=sys.stderr)
        return 2
    if settings.auth_mode == "password" and not (settings.password_hash or settings.password_plain):
        print("PATCHBAY_AUTH=password needs PATCHBAY_PASSWORD_HASH "
              "(from `patchbay hash-password`) or PATCHBAY_PASSWORD", file=sys.stderr)
        return 2
    if settings.auth_mode == "oidc":
        missing = [k for k, v in (("PATCHBAY_OIDC_CLIENT_ID", settings.oidc_client_id),
                                  ("PATCHBAY_OIDC_CLIENT_SECRET", settings.oidc_client_secret),
                                  ("PATCHBAY_OIDC_AUTH_URL", settings.oidc_auth_url),
                                  ("PATCHBAY_OIDC_TOKEN_URL", settings.oidc_token_url)) if not v]
        if missing:
            print(f"PATCHBAY_AUTH=oidc needs {', '.join(missing)}", file=sys.stderr)
            return 2

    if settings.tls_mode == "off":
        uvicorn.run("patchbay.web:app", host=args.host, port=args.port)
        return 0
    if settings.tls_mode != "direct":
        print(f"PATCHBAY_TLS={settings.tls_mode!r} — expected off or direct", file=sys.stderr)
        return 2

    import threading
    import time
    from pathlib import Path

    cert, key = settings.tls_cert, settings.tls_key
    for var, p in (("PATCHBAY_TLS_CERT", cert), ("PATCHBAY_TLS_KEY", key)):
        if not p or not Path(p).is_file():
            print(f"PATCHBAY_TLS=direct needs {var} pointing at a readable PEM file",
                  file=sys.stderr)
            return 2

    def mtimes() -> list[int] | None:
        try:
            return [Path(cert).stat().st_mtime_ns, Path(key).stat().st_mtime_ns]
        except OSError:
            return None  # mid-replacement — try again next round

    # serve in a loop: when the cert/key files change on disk (an ACME client
    # renewed them), cycle the listener so the new cert takes effect without
    # anyone remembering to restart the service
    while True:
        server = uvicorn.Server(uvicorn.Config(
            "patchbay.web:app", host=args.host, port=args.port,
            ssl_certfile=cert, ssl_keyfile=key))
        renewed = threading.Event()
        baseline = mtimes()

        def watch():
            while not server.should_exit:
                time.sleep(60)
                current = mtimes()
                if current and current != baseline:
                    renewed.set()
                    server.should_exit = True
                    return

        threading.Thread(target=watch, daemon=True).start()
        server.run()
        if not renewed.is_set():  # real shutdown (signal), not a renewal
            return 0
        print("[tls] certificate changed on disk — restarting listener")


def cmd_snapshot(args: argparse.Namespace) -> int:
    settings = load_settings()
    try:
        from .snapshot import write_snapshot
    except ImportError as e:
        print(f"snapshot needs the web extra (pip install 'patchbay[web]'): {e}",
              file=sys.stderr)
        return 2
    from .snapshot import DeliveryError

    try:
        path = write_snapshot(settings, args.out)
    except DeliveryError as e:
        print(f"snapshot written locally, delivery failed: {e}", file=sys.stderr)
        return 1
    print(f"{path} ({path.stat().st_size / 1e6:.1f} MB)"
          + (f" -> {settings.snapshot_deliver_dir}"
             if settings.snapshot_deliver_dir and not args.out else ""))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    import os

    from . import demo

    path = args.db
    if os.path.exists(path):
        import sqlite3

        try:
            with db.connect(path) as conn:
                is_demo = db.get_state(conn, demo.MARKER) == "1"
        except sqlite3.Error:
            is_demo = False   # not even a patchbay database — definitely guard
        # never silently overwrite a database that a real poll built
        if not (is_demo or args.force):
            print(f"{path} exists and is not a demo database — use --force to "
                  f"overwrite, or --db for another path", file=sys.stderr)
            return 2
        os.remove(path)
    with db.connect(path) as conn:
        summary = demo.seed(conn)
    print(f"demo network written to {path} ({summary})")
    print(f"view it:      PATCHBAY_DB={path} patchbay web")
    print(f"snapshot it:  PATCHBAY_DB={path} patchbay snapshot")
    return 0


def cmd_hash_password(args: argparse.Namespace) -> int:
    import getpass

    from .auth import hash_password

    pw = getpass.getpass("password: ")
    if pw != getpass.getpass("again: "):
        print("mismatch — nothing hashed", file=sys.stderr)
        return 1
    print(f"PATCHBAY_PASSWORD_HASH={hash_password(pw)}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    settings = load_settings()
    queries = {
        "devices": "SELECT name, mgmt_ip, os, status, source FROM devices ORDER BY name",
        "links": "SELECT a_device, a_interface, b_device, b_interface, source FROM links "
                 "ORDER BY a_device, a_interface",
        "subnets": "SELECT cidr, description, vlan, source FROM subnets ORDER BY cidr",
        "vlans": "SELECT vid, name, source FROM vlans ORDER BY vid",
        "endpoints": "SELECT mac, ip, hostname, device, interface FROM endpoints ORDER BY hostname",
    }
    with db.connect(settings.db_path) as conn:
        db.init(conn)
        rows = conn.execute(queries[args.table]).fetchall()
    for row in rows:
        print("  ".join("-" if v is None else str(v) for v in row))
    print(f"({len(rows)} {args.table})", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="patchbay", description="homelab network console")
    parser.add_argument("--version", action="version", version=f"patchbay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_poll = sub.add_parser("poll", help="run collectors")
    p_poll.add_argument("--source", action="append", help="only this collector (repeatable)")
    p_poll.set_defaults(func=cmd_poll)

    p_show = sub.add_parser("show", help="print a table from the model")
    p_show.add_argument("table", choices=["devices", "links", "subnets", "vlans", "endpoints"])
    p_show.set_defaults(func=cmd_show)

    p_web = sub.add_parser("web", help="serve the dashboard")
    p_web.add_argument("--host", default="0.0.0.0")
    p_web.add_argument("--port", type=int, default=8080)
    p_web.set_defaults(func=cmd_web)

    p_snap = sub.add_parser("snapshot",
                            help="write a self-contained break-glass HTML snapshot")
    p_snap.add_argument("--out", help="explicit output path "
                        "(default: timestamped file in PATCHBAY_SNAPSHOT_DIR)")
    p_snap.set_defaults(func=cmd_snapshot)

    p_demo = sub.add_parser("demo", help="write a fictional demo network "
                            "(no credentials or real site needed)")
    p_demo.add_argument("--db", default="demo.db", help="where to write it "
                        "(default ./demo.db)")
    p_demo.add_argument("--force", action="store_true",
                        help="overwrite a non-demo database at --db")
    p_demo.set_defaults(func=cmd_demo)

    p_hash = sub.add_parser("hash-password",
                            help="hash a UI password for PATCHBAY_PASSWORD_HASH")
    p_hash.set_defaults(func=cmd_hash_password)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
