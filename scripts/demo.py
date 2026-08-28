#!/usr/bin/env python3
"""Start the patchbay demo in a container and open it in your browser.

One command, nothing to configure: pulls the published image (or builds this
checkout with --build), seeds the fictional demo network inside the
container, waits for the UI to answer, and opens it. Ctrl-C stops and
removes the container; nothing is written to the host.

    ./scripts/demo.py                 # published image, on http://127.0.0.1:8013
    ./scripts/demo.py --build         # the code in this checkout instead
    ./scripts/demo.py -d              # leave it running; --stop removes it

Needs Docker and Python 3.9+, not a patchbay install.
"""

from __future__ import annotations

import argparse
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

IMAGE = "ghcr.io/dsmorgan/patchbay:latest"
LOCAL_TAG = "patchbay:local"
NAME = "patchbay-demo"
PORT = 8013           # the compose example's port, so the URL is the familiar one

# The container seeds its own data on start: the image ships `patchbay demo`,
# /data is an anonymous volume (the Dockerfile's VOLUME) removed with the
# container, and /data/.env doesn't exist, so no site config can leak in and
# every start is the same fresh fictional network.
DEMO_CMD = ["sh", "-c",
            "patchbay demo --db /data/patchbay.db && "
            "exec patchbay web --host 0.0.0.0 --port 8080"]


class Fail(SystemExit):
    def __init__(self, msg: str):
        super().__init__(f"[fail] {msg}")


def say(tag: str, msg: str) -> None:
    print(f"[{tag}]".ljust(8) + msg, flush=True)


# --- docker ---------------------------------------------------------------

def docker(*args: str, check: bool = True, capture: bool = True,
           **kw) -> subprocess.CompletedProcess:
    cmd = ["docker", *args]
    r = subprocess.run(cmd, text=True, capture_output=capture, **kw)
    if check and r.returncode:
        err = (r.stderr or "").strip() if capture else ""
        raise Fail(f"docker {args[0]} exited {r.returncode}" + (f": {err}" if err else ""))
    return r


def ensure_docker() -> None:
    if not shutil.which("docker"):
        raise Fail("docker not found - install Docker Desktop (or docker-ce) first")
    if docker("info", "--format", "{{.ServerVersion}}", check=False).returncode:
        raise Fail("docker is installed but its daemon isn't answering - "
                   "start Docker Desktop and retry")


def pull(image: str) -> None:
    say("pull", image)
    r = docker("pull", "--quiet", image, check=False)
    if r.returncode:
        # offline is fine if a copy is already here - say so rather than
        # silently demoing something older than the user expects
        if docker("image", "inspect", image, check=False).returncode:
            raise Fail(f"couldn't pull {image}: {(r.stderr or '').strip()}")
        say("warn", "pull failed - using the copy already on this machine")


def container_state(name: str) -> str | None:
    """'running', 'exited', ... or None when no such container."""
    r = docker("inspect", "-f", "{{.State.Status}}", name, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def remove_container(name: str) -> bool:
    """Stop and remove, volumes included. True if there was one."""
    if container_state(name) is None:
        return False
    docker("rm", "-f", "-v", name)
    return True


def container_port(name: str) -> int | None:
    """Host port `name` publishes for the UI, or None."""
    r = docker("port", name, "8080", check=False)
    m = re.search(r":(\d+)", (r.stdout or "").strip())
    return int(m.group(1)) if m else None


def published_ports() -> set[int]:
    """Host ports docker already publishes, whatever the container. A bind
    probe alone misses these on Linux hosts running without docker-proxy,
    where iptables owns the port and nothing is listening on it."""
    r = docker("ps", "--format", "{{.Ports}}", check=False)
    return {int(m) for m in re.findall(r":(\d+)->", r.stdout or "")}


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred: int, fixed: bool = False) -> int:
    """`preferred`, or the next free port above it - unless it was asked for
    explicitly, in which case busy is an error, not a silent move."""
    taken = published_ports()
    port = preferred
    while port in taken or not port_free(port):
        if fixed:
            raise Fail(f"port {port} is in use")
        port += 1
        if port > preferred + 200:
            raise Fail(f"no free port in {preferred}-{port}")
    return port


def launch(image: str, name: str, port: int, bind: str = "127.0.0.1") -> str:
    """Run the demo detached as `name` and return the URL to reach it."""
    remove_container(name)
    url = f"http://127.0.0.1:{port}/"
    docker("run", "-d", "--name", name, "-p", f"{bind}:{port}:8080",
           image, *DEMO_CMD)
    return url


def wait_ready(url: str, name: str, timeout: float = 90) -> None:
    # no proxy: a corporate HTTP_PROXY would otherwise swallow 127.0.0.1
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = container_state(name)
        if state != "running":
            logs = docker("logs", "--tail", "30", name, check=False)
            raise Fail(f"{name} is {state or 'gone'} before it answered:\n"
                       + (logs.stdout + logs.stderr).rstrip())
        try:
            with opener.open(url, timeout=2) as r:
                if r.status < 500:
                    return
        except urllib.error.HTTPError as e:
            if e.code < 500:      # any non-5xx answer means the server is up
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    raise Fail(f"{url} still not answering after {timeout:.0f}s - try: docker logs {name}")


def open_browser(url: str) -> None:
    say("open", url)
    try:
        ok = webbrowser.open(url)
    except Exception:
        ok = False
    if not ok:
        say("warn", "no browser could be opened here - open the URL yourself")


def follow(name: str) -> None:
    """Stream the container's output; Ctrl-C (or the container exiting)
    removes it, so a demo leaves nothing behind."""
    say("logs", "following the container - Ctrl-C stops and removes it")
    try:
        subprocess.run(["docker", "logs", "-f", name])
    except KeyboardInterrupt:
        pass
    finally:
        print()
        say("stop", f"removing {name}")
        remove_container(name)


# --- building from a checkout ---------------------------------------------

def git(path: Path | str, *args: str, timeout: float = 30) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], text=True,
                       capture_output=True, timeout=timeout)
    return r.stdout.strip() if r.returncode == 0 else ""


def git_stamp(path: Path) -> str:
    """The build stamp the UI header shows: short sha, -dirty when the tree
    has uncommitted changes - the same rule web.py applies to a checkout."""
    sha = git(path, "rev-parse", "--short", "HEAD")
    if not sha:
        return "dev"
    return sha + ("-dirty" if git(path, "status", "--porcelain") else "")


def repo_root() -> Path:
    here = globals().get("__file__")   # absent when piped in over stdin
    start = Path(here).resolve().parent if here else Path.cwd()
    root = git(start, "rev-parse", "--show-toplevel")
    if not root or not (Path(root) / "Dockerfile").is_file():
        raise Fail("--build needs a patchbay checkout - run from a clone, "
                   "or drop --build to use the published image")
    return Path(root)


def build_dir(context: Path, tag: str, stamp: str) -> None:
    say("build", f"{tag} from {context} ({stamp})")
    docker("build", "-t", tag, "--build-arg", f"GIT_SHA={stamp}", str(context),
           capture=False)


# --- main -----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="demo.py", description="start the patchbay demo in a container "
        "and open it in your browser")
    ap.add_argument("--build", action="store_true",
                    help="build the image from this checkout instead of pulling the published one")
    ap.add_argument("--image", default=IMAGE, help=f"image to run (default {IMAGE})")
    ap.add_argument("--port", type=int,
                    help=f"host port (default {PORT}, or the next free one above it)")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="interface to publish on; 0.0.0.0 exposes the demo to your LAN "
                         "(default 127.0.0.1)")
    ap.add_argument("-d", "--detach", action="store_true",
                    help="leave the container running in the background")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    ap.add_argument("--stop", action="store_true", help="stop and remove a running demo")
    args = ap.parse_args(argv)

    ensure_docker()
    if args.stop:
        say("stop", NAME if remove_container(NAME) else f"{NAME} wasn't running")
        return 0

    if args.build:
        root = repo_root()
        image = LOCAL_TAG
        build_dir(root, image, git_stamp(root))
    else:
        image = args.image
        pull(image)

    # remove any previous demo before choosing a port, so a rerun keeps its
    # port. When the old demo held the wanted port, hand it straight to
    # docker: Docker Desktop (macOS) keeps a removed container's published
    # port bound for up to ~30s, so a bind probe would report it busy and
    # drift every rerun up one port — but docker rebinds its own
    # just-released port immediately.
    old_port = container_port(NAME)
    remove_container(NAME)
    want = args.port or PORT
    port = want if want == old_port else pick_port(want, fixed=bool(args.port))
    if port != PORT and not args.port:
        say("port", f"{PORT} is busy - using {port}")
    say("run", f"{NAME} from {image}")
    url = launch(image, NAME, port, args.bind)
    if args.bind != "127.0.0.1":
        say("warn", f"published on {args.bind}:{port} - the demo has no login")
    say("wait", f"seeding the demo network and starting the UI at {url}")
    wait_ready(url, NAME)
    say("ok", f"patchbay demo is up at {url}")
    if not args.no_open:
        open_browser(url)
    if args.detach:
        me = sys.argv[0] if sys.argv[0] not in ("-", "") else "demo.py"
        say("info", f"left running in the background - stop it with: {me} --stop")
        return 0
    follow(NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
