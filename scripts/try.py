#!/usr/bin/env python3
"""Run any branch or worktree of patchbay in its own container, on its own
port, with demo data — and open it.

    ./scripts/try.py                     # pick from worktrees, local and remote branches
    ./scripts/try.py ui/nav-rail         # a branch, remote branch, tag, or sha
    ./scripts/try.py .worktrees/process  # a worktree: what's on disk, uncommitted edits included
    ./scripts/try.py --list              # what's running, and where
    ./scripts/try.py --stop ui/nav-rail  # or --stop-all
    ./scripts/try.py main --data ../patchbay-site/data   # your own .env + db, not demo data

A branch builds from its commit (`git archive` piped into `docker build`, so
nothing is checked out and a remote branch needs no worktree); a directory
builds what's on disk. Each ref becomes image patchbay:<slug> and container
patchbay-try-<slug> on a port derived from the name, so several branches sit
side by side for comparison. Instances stay up until you stop them; Ctrl-C
here only stops one when you pass --follow.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo import (LABEL, Fail, build_dir, docker, ensure_docker, follow, git,  # noqa: E402
                  git_stamp, launch, open_browser, pick_port, remove_container,
                  say, wait_ready)

PREFIX = "patchbay-try-"
PORT_BASE, PORT_SPAN = 8100, 400     # a ref's port is hashed into this range


@dataclass
class Cand:
    kind: str                 # running | worktree | local | remote
    ref: str                  # what to build (a git ref) or, for a dir, its branch
    sha: str
    path: Path | None = None  # set for worktrees: build the directory, not the ref
    note: str = ""
    url: str = ""             # set for running instances
    src: str = ""             # running instances built from a directory: which
    cname: str = ""           # running instances: the container's actual name
    author: str = ""          # who wrote the tip commit, the name as git records it

    @property
    def slug(self) -> str:
        return slug(self.ref)

    @property
    def name(self) -> str:
        return self.cname or PREFIX + self.slug


def slug(ref: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", ref.lower()).strip("-.")[:60] or "x"


def ref_port(s: str) -> int:
    return PORT_BASE + zlib.crc32(s.encode()) % PORT_SPAN


# --- what there is to try ---------------------------------------------------

def repo_root() -> Path:
    root = git(Path(__file__).resolve().parent, "rev-parse", "--show-toplevel")
    if not root:
        raise Fail("not inside a git checkout")
    return Path(root)


def fetch(root: Path) -> None:
    say("fetch", "remotes...")
    r = subprocess.run(["git", "-C", str(root), "fetch", "--all", "--prune", "--quiet"],
                       text=True, capture_output=True, timeout=60,
                       env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode:
        say("warn", f"fetch failed, listing what's already here: {r.stderr.strip()[:200]}")


def running() -> list[Cand]:
    r = docker("ps", "--filter", f"label={LABEL}=try", "--format",
               '{{.Names}}\t{{.Label "patchbay.ref"}}\t{{.Label "patchbay.src"}}\t'
               '{{.Label "patchbay.build"}}\t{{.Label "patchbay.url"}}\t{{.Status}}',
               check=False)
    out = []
    for line in (r.stdout or "").splitlines():
        cname, ref, src, build, url, status = line.split("\t")
        out.append(Cand("running", ref, build, url=url, note=status.lower(),
                        src=src, cname=cname))
    return out


def worktrees(root: Path) -> list[Cand]:
    out, cur = [], {}
    for line in git(root, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur:
                out.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v
    cands = []
    for w in out:
        path = Path(w["worktree"])
        branch = w.get("branch", "").replace("refs/heads/", "") or path.name
        # plain `status --porcelain`, untracked files included, because an
        # untracked module under src/ is in the build - and it's the rule the
        # UI's own -dirty stamp uses
        dirty = bool(git(path, "status", "--porcelain"))
        cands.append(Cand("worktree", branch, w["HEAD"][:7], path=path,
                          note="uncommitted changes" if dirty else "",
                          author=git(path, "log", "-1", "--format=%an")))
    return cands


def branches(root: Path) -> tuple[list[Cand], list[Cand]]:
    # the full refname, not :short - that abbreviates refs/remotes/fork/HEAD
    # to a bare "fork", which is a remote, not a branch
    fmt = ("%(refname)\t%(objectname:short)\t%(authorname)\t"
           "%(committerdate:relative)\t%(upstream:short)\t%(subject)")
    local, remote = [], []
    for kind, area in (("local", "refs/heads/"), ("remote", "refs/remotes/")):
        for line in git(root, "for-each-ref", f"--format={fmt}", "--sort=-committerdate",
                        area).splitlines():
            ref, sha, author, when, upstream, subject = line.split("\t", 5)
            ref = ref[len(area):]
            if ref.endswith("/HEAD"):
                continue
            note = f"{when} | {subject[:50]}" + (f"  ^{upstream}" if upstream else "")
            (local if kind == "local" else remote).append(
                Cand(kind, ref, sha, note=note, author=author))
    # a remote branch identical to a local one adds nothing to the menu -
    # origin/main at main's sha is just main; what remains is genuinely remote
    local_shas = {c.sha for c in local}
    remote = [c for c in remote if c.sha not in local_shas]
    return local, remote


def resolve(root: Path, arg: str) -> Cand:
    """An explicit argument: a directory (worktree) or anything git can
    resolve to a commit. Fetches once when a name looks unknown."""
    p = Path(arg)
    if p.is_dir():
        if not (p / "Dockerfile").is_file():
            raise Fail(f"{p} has no Dockerfile - not a patchbay checkout")
        p = p.resolve()
        branch = git(p, "rev-parse", "--abbrev-ref", "HEAD") or p.name
        return Cand("worktree", branch if branch != "HEAD" else p.name,
                    git(p, "rev-parse", "--short", "HEAD"), path=p)
    for attempt in (0, 1):
        sha = git(root, "rev-parse", "--verify", "--quiet", "--short", f"{arg}^{{commit}}")
        if sha:
            kind = "remote" if arg.split("/")[0] in git(root, "remote").split() else "local"
            return Cand(kind, arg, sha)
        if attempt == 0:
            fetch(root)
    raise Fail(f"nothing named {arg!r}: not a directory, branch, tag, or commit")


# --- the menu -----------------------------------------------------------------

def menu(root: Path) -> Cand:
    groups = [("Running now - pick one to open it", running()),
              ("Worktrees - builds the directory, uncommitted changes included", worktrees(root))]
    local, remote = branches(root)
    groups += [("Local branches - builds the commit", local),
               ("Remote branches - builds the commit", remote)]
    items: list[Cand] = []
    width = max((len(c.ref) for _, cs in groups for c in cs), default=10)
    awidth = max((len(c.author) for _, cs in groups for c in cs), default=0)
    print()
    for title, cands in groups:
        if not cands:
            continue
        print(f"  {title}")
        for c in cands:
            items.append(c)
            detail = c.url if c.kind == "running" else str(c.path) if c.kind == "worktree" else ""
            note = "  ".join(x for x in (detail, c.sha if c.kind != "running" else "",
                                         c.note, c.src) if x)
            print(f"{len(items):4}  {c.ref.ljust(width)}  {c.author.ljust(awidth)}  {note}")
        print()
    if not items:
        raise Fail("nothing to try - no branches?")
    while True:
        try:
            ans = input("number, or part of a name (q quits): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(0)
        if ans.lower() in ("q", "quit", ""):
            raise SystemExit(0)
        if ans.isdigit() and 1 <= int(ans) <= len(items):
            return items[int(ans) - 1]
        hits = [c for c in items if c.kind != "running" and ans.lower() in c.ref.lower()]
        exact = [c for c in hits if c.ref.lower() == ans.lower()]
        if exact or len(hits) == 1:
            return (exact or hits)[0]
        print(f"  {'ambiguous' if hits else 'no match'}: "
              + (", ".join(c.ref for c in hits[:6]) or "try a number"))


# --- build & run ----------------------------------------------------------------

def build(root: Path, c: Cand) -> tuple[str, str]:
    """Build c into an image; returns (tag, build stamp)."""
    tag = f"patchbay:{c.slug}"
    if c.path:
        stamp = git_stamp(c.path)
        build_dir(c.path, tag, stamp)
        return tag, stamp
    say("build", f"{tag} from {c.ref} @ {c.sha} (git archive - committed state only)")
    archive = subprocess.Popen(["git", "-C", str(root), "archive", "--format=tar", c.ref],
                               stdout=subprocess.PIPE)
    r = subprocess.run(["docker", "build", "-t", tag, "--build-arg", f"GIT_SHA={c.sha}", "-"],
                       stdin=archive.stdout)
    archive.stdout.close()
    if archive.wait() or r.returncode:
        raise Fail(f"build of {c.ref} failed")
    return tag, c.sha


def run(root: Path, c: Cand, args: argparse.Namespace) -> None:
    tag, stamp = build(root, c)
    remove_container(c.name)      # before choosing a port, so a rebuild keeps its port
    port = pick_port(args.port or ref_port(c.slug), fixed=bool(args.port))
    data = Path(args.data) if args.data else None
    if data and not data.is_dir():
        raise Fail(f"--data {data} is not a directory")
    what = f"{c.path} (working tree)" if c.path else f"{c.ref} @ {c.sha}"
    say("run", f"{c.name} <- {what}" + (f", data from {data}" if data else ", demo data"))
    url = launch(tag, c.name, port, args.bind,
                 {LABEL: "try", "patchbay.ref": c.ref, "patchbay.build": stamp,
                  "patchbay.src": str(c.path or "")},
                 data_dir=data)
    say("wait", url)
    wait_ready(url, c.name)
    say("ok", f"{c.ref} is up at {url}  (build {stamp})")
    if not args.no_open:
        open_browser(url)
    if args.follow:
        follow(c.name)
    else:
        say("info", f"logs: docker logs -f {c.name}   stop: {sys.argv[0]} --stop {c.ref}")


def stop(ref: str | None, everything: bool) -> None:
    live = running()
    if everything:
        names = [c.name for c in live]
    else:
        # a directory stops the instance built from it, a branch the one built
        # from that branch; failing both, the name run() would have formed
        want = str(Path(ref).resolve()) if Path(ref).is_dir() else ref
        names = [c.name for c in live if want in (c.ref, c.src)] or [PREFIX + slug(ref)]
    if not names:
        say("stop", "nothing running")
    for n in names:
        say("stop", n if remove_container(n) else f"{n} wasn't running")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="try.py", description="run a branch or worktree of patchbay in a container")
    ap.add_argument("ref", nargs="?", help="branch, remote branch, tag, sha, or a checkout "
                    "directory; omit for a menu")
    ap.add_argument("--data", metavar="DIR", help="mount DIR as /data (your .env + patchbay.db) "
                    "instead of seeding demo data - note a branch may migrate that database "
                    "forward, so point this at a copy")
    ap.add_argument("--port", type=int, help="host port (default: one derived from the ref)")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="interface to publish on (default 127.0.0.1)")
    ap.add_argument("-f", "--follow", action="store_true",
                    help="follow the logs; Ctrl-C then stops and removes the instance")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    ap.add_argument("--no-fetch", action="store_true", help="don't fetch remotes before the menu")
    ap.add_argument("--list", action="store_true", help="list running instances")
    ap.add_argument("--stop", metavar="REF", help="stop and remove the instance for REF")
    ap.add_argument("--stop-all", action="store_true", help="stop and remove every instance")
    args = ap.parse_args(argv)

    ensure_docker()
    if args.stop or args.stop_all:
        stop(args.stop, args.stop_all)
        return 0
    if args.list:
        for c in running():
            print("  ".join(x for x in (f"  {c.ref.ljust(36)}", c.url, c.sha, c.note, c.src) if x))
        return 0
    root = repo_root()
    if args.ref:
        c = resolve(root, args.ref)
    else:
        if not args.no_fetch:
            fetch(root)
        c = menu(root)
        if c.kind == "running":
            open_browser(c.url)
            return 0
    run(root, c, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
