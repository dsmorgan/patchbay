#!/usr/bin/env python
"""Screenshot harness: the demo network, served and photographed.

Seeds a fresh demo database (or uses --db), serves it on a spare port with the
checkout's code (PYTHONPATH=src, so no editable install is needed), and
screenshots a list of pages at one or more widths with headless Chrome. The
point is that a UI change gets *looked at* — by a person, or by an agent
reading the PNGs — instead of inferred from markup: an inline SVG that lost its
`/>` still returns 200 and renders nothing.

    python scripts/screenshots.py --out artifacts/shots/my-change
    python scripts/screenshots.py --pages /,topology?view=load --widths 1400   # leading / optional
    python scripts/screenshots.py --db demo.db --snapshot

Exit status is 1 if any page did not return 200 or a screenshot failed, so the
run doubles as a smoke test. Every page × width is listed in <out>/manifest.json
and printed as a `[shots]` line. Chrome comes from PATCHBAY_CHROME or the usual
install locations; the 600px width exercises the collapsed rail.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

DEFAULT_PAGES = ["/", "/topology", "/vlans", "/patchpanel", "/drift", "/configs",
                 "/ops", "/device/core1"]
CHROME_CANDIDATES = {
    "Windows": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
    "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium"],
    "Linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
}


def find_chrome(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("PATCHBAY_CHROME")
    if env:
        return env
    for cand in CHROME_CANDIDATES.get(platform.system(), []):
        if os.path.isabs(cand) and os.path.exists(cand):
            return cand
        found = shutil.which(cand)
        if found:
            return found
    sys.exit("no Chrome/Edge found — set PATCHBAY_CHROME or pass --chrome")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed_demo(path: Path) -> None:
    from patchbay import db, demo

    with db.connect(str(path)) as conn:
        demo.seed(conn)


def status_of(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return 0


def wait_for(url: str, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if status_of(url) == 200:
            return True
        time.sleep(0.25)
    return False


def page_path(raw: str) -> str:
    """A page as a path. Git Bash rewrites a leading-slash argument into a
    Windows path (`/configs` -> `C:/Program Files/Git/configs`) unless
    MSYS_NO_PATHCONV=1, so a drive-letter prefix is stripped back off, and a
    bare `configs` is accepted so nobody has to fight the shell at all."""
    p = raw.strip()
    if re.match(r"^[A-Za-z]:/", p):
        p = p.split("/", 3)[-1]
    return "/" + p.lstrip("/")


def slug(page: str) -> str:
    s = page.strip("/") or "home"
    for ch in "/?&=":
        s = s.replace(ch, "_")
    return s


def shoot(chrome: str, url: str, out: Path, width: int, height: int) -> bool:
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           f"--window-size={width},{height}", f"--screenshot={out}", url]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=90, check=False)
    except subprocess.TimeoutExpired:
        return False
    return out.exists() and out.stat().st_size > 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="artifacts/shots/latest", help="output directory")
    ap.add_argument("--pages", default=",".join(DEFAULT_PAGES),
                    help="comma-separated paths (query strings allowed)")
    ap.add_argument("--widths", default="1400,600", help="comma-separated viewport widths")
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--db", help="serve this database instead of seeding a fresh demo")
    ap.add_argument("--port", type=int, help="fixed port (default: a free one)")
    ap.add_argument("--chrome", help="browser executable")
    ap.add_argument("--snapshot", action="store_true",
                    help="also generate a snapshot and screenshot it from file://")
    args = ap.parse_args()

    chrome = find_chrome(args.chrome)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pages = [page_path(p) for p in args.pages.split(",") if p.strip()]
    widths = [int(w) for w in args.widths.split(",") if w.strip()]

    tmp = Path(tempfile.mkdtemp(prefix="patchbay-shots-"))
    db_path = Path(args.db).resolve() if args.db else tmp / "demo.db"
    if not args.db:
        seed_demo(db_path)
    env = dict(os.environ,
               PATCHBAY_DB=str(db_path),
               PATCHBAY_ENV=str(tmp / "no.env"),   # never the site config
               PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    port = args.port or free_port()
    base = f"http://127.0.0.1:{port}"
    log = open(out / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "patchbay.web:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    manifest: list[dict] = []
    failed = False
    try:
        if not wait_for(base + "/", 30):
            print(f"[shots] server on {base} never answered - see {out / 'server.log'}")
            return 1
        targets = [(p, base + p) for p in pages]
        if args.snapshot:
            os.environ.update(PATCHBAY_DB=env["PATCHBAY_DB"], PATCHBAY_ENV=env["PATCHBAY_ENV"])
            from patchbay.config import load_settings
            from patchbay.snapshot import write_snapshot

            snap = write_snapshot(load_settings(), str(out / "snapshot.html"))
            targets.append(("snapshot", snap.resolve().as_uri()))
        for page, url in targets:
            code = 200 if url.startswith("file:") else status_of(url)
            for w in widths:
                png = out / f"{slug(page)}-{w}.png"
                ok = code == 200 and shoot(chrome, url, png, w, args.height)
                failed |= not ok
                manifest.append({"page": page, "width": w, "status": code,
                                 "file": str(png) if ok else None})
                print(f"[shots] {page} @{w} -> {'ok' if ok else 'FAILED'} ({code}) {png.name}")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if not args.db:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"[shots] {sum(1 for m in manifest if m['file'])}/{len(manifest)} ok -> {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
