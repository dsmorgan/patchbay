"""Tiered snapshot retention (#65): which timestamped snapshots to keep.

Pure and filesystem-free: the spec parser and the keeper selection take
strings and datetimes, so the local directory, the delivery directory, and
the /snapshots page all run the same rule and tests need no files.

A snapshot survives if any tier claims it. Every periodic tier keeps the
EARLIEST snapshot of each day, week, month, or year: that keeper is known
the day it is taken, extra on-demand snapshots never evict it, and because pruning never removes a keeper inside its window, the
earliest file still present in a period is always its true first snapshot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

DEFAULT_SPEC = "30,12m,3y,first"

# Only plain timestamped snapshots take part. patchbay-latest.html and
# alert-triggered copies (patchbay-YYYYMMDD-HHMMSS-alert.html, #66) keep
# their own rules, so a pattern that admitted them would let the tiers
# delete files they don't govern.
SNAPSHOT_NAME = re.compile(r"patchbay-(\d{8}-\d{6})\.html")
# the alert-triggered copies (#66): a plain newest-n count of their own
ALERT_SNAPSHOT_NAME = re.compile(r"patchbay-(\d{8}-\d{6})-alert\.html")

# display order on /snapshots, shortest-lived first. "newest" is the file
# most recently written, kept whatever the spec; "all" is a bare 0.
TIERS = ("newest", "all", "daily", "weekly", "monthly", "yearly", "first")

_TERM = re.compile(r"(\d+)([dwmy]?)")
_UNIT = {"": "daily", "d": "daily", "w": "weekly", "m": "monthly", "y": "yearly"}


@dataclass(frozen=True)
class KeepSpec:
    """A parsed PATCHBAY_SNAPSHOT_KEEP. Per tier: None = not in the spec
    (keeps nothing), 0 = unlimited, n = the first snapshot of each of the n
    most recent calendar days, weeks, months, or years. `everything` is a
    bare 0, the pre-tier "keep every file", which no tier spells."""
    daily: int | None = None
    weekly: int | None = None
    monthly: int | None = None
    yearly: int | None = None
    first: bool = False
    everything: bool = False

    def __str__(self) -> str:
        if self.everything:
            return "0"
        terms = [f"{self.daily}"] if self.daily is not None else []
        terms += [f"{n}{u}" for n, u in ((self.weekly, "w"), (self.monthly, "m"),
                                          (self.yearly, "y")) if n is not None]
        return ",".join(terms + (["first"] if self.first else []))

    def describe(self) -> str:
        """The spec as a sentence fragment for /snapshots and /ops."""
        if self.everything:
            return "every timestamped snapshot"
        parts = ["the newest"]
        for n, period in ((self.daily, "day"), (self.weekly, "week"),
                          (self.monthly, "month"), (self.yearly, "year")):
            if n is not None:
                parts.append(f"the first of every {period}" if n == 0 else
                             f"the first of each of the last {n} {period}s"
                             if n > 1 else f"the first of the latest {period}")
        if self.first:
            parts.append("the first ever")
        return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]


def parse_keep_spec(raw: str) -> KeepSpec:
    """Parse a tier spec such as '30,12m,3y,first'. Raises ValueError on any
    term it can't read: the caller must then prune nothing, because a typo
    in a retention setting must never cost history."""
    if raw.strip() == "0":
        # 0 meant "keep everything" before tiers existed; as 0d it would
        # now thin same-day extras, deleting files the old setting kept
        return KeepSpec(everything=True)
    tiers: dict[str, int | bool] = {}
    for term in raw.split(","):
        term = term.strip().lower()
        if term == "first":
            name, value = "first", True
        else:
            m = _TERM.fullmatch(term)
            if not m:
                raise ValueError(f"unreadable term {term!r}" if term
                                 else "empty term")
            name, value = _UNIT[m.group(2)], int(m.group(1))
        if name in tiers:
            # '30,10' could mean either; refusing beats guessing on a
            # setting that deletes files
            raise ValueError(f"{name} tier given twice")
        tiers[name] = value
    return KeepSpec(**tiers)  # type: ignore[arg-type]


def select_keepers(stamps: Iterable[datetime],
                   spec: KeepSpec) -> dict[datetime, tuple[str, ...]]:
    """Map each kept timestamp to the tiers that claim it; a timestamp
    missing from the result may be pruned. Windows count back from the
    newest snapshot, not the clock, so the answer depends on the file list
    alone. Empty periods count toward a window. The newest snapshot is
    always kept, so a later same-day snapshot, or one under a spec with no
    daily term, survives until the next one is written."""
    ordered = sorted(set(stamps))
    if not ordered:
        return {}
    claims: dict[datetime, list[str]] = {}

    def claim(ts: datetime, tier: str) -> None:
        claims.setdefault(ts, []).append(tier)

    newest = ordered[-1]
    if spec.everything:
        return {ts: ("all",) for ts in ordered}
    periods = (
        # (tier, window, period key, distance in periods from the newest)
        ("daily", spec.daily,
         lambda t: t.toordinal(),
         lambda a, b: a - b),
        ("weekly", spec.weekly,
         lambda t: t.toordinal() - t.weekday(),   # the Monday of its week
         lambda a, b: (a - b) // 7),
        ("monthly", spec.monthly,
         lambda t: t.year * 12 + t.month - 1,
         lambda a, b: a - b),
        ("yearly", spec.yearly,
         lambda t: t.year,
         lambda a, b: a - b),
    )
    for tier, window, key, distance in periods:
        if window is None:
            continue
        top = key(newest)
        seen: set[int] = set()
        for ts in ordered:   # ascending, so the first hit is the earliest
            k = key(ts)
            if k in seen:
                continue
            seen.add(k)
            if window == 0 or distance(top, k) < window:
                claim(ts, tier)

    if spec.first:
        claim(ordered[0], "first")
    if newest not in claims:
        claim(newest, "newest")
    return {ts: tuple(t for t in TIERS if t in tiers)
            for ts, tiers in claims.items()}


def stamp_of(name: str) -> datetime | None:
    """The timestamp a snapshot file name carries, or None for any name the
    tiers don't govern (latest, alert copies, strays, impossible dates)."""
    m = SNAPSHOT_NAME.fullmatch(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def classify(names: Iterable[str],
             spec: KeepSpec) -> tuple[dict[str, tuple[str, ...]], list[str]]:
    """Split snapshot file names into (kept name -> tiers, prunable names).
    Names the tiers don't govern appear in neither."""
    stamped = {n: ts for n in names if (ts := stamp_of(n)) is not None}
    keep = select_keepers(stamped.values(), spec)
    kept = {n: keep[ts] for n, ts in stamped.items() if ts in keep}
    prune = sorted(n for n, ts in stamped.items() if ts not in keep)
    return kept, prune


def alert_stamp_of(name: str) -> datetime | None:
    """The timestamp an alert snapshot's file name carries, or None for any
    other name, tiered snapshots included."""
    m = ALERT_SNAPSHOT_NAME.fullmatch(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def alert_prunable(names: Iterable[str], keep: int) -> list[str]:
    """The alert snapshots beyond the newest `keep` (0 = unlimited). Names
    that are not alert snapshots are never returned, so this count cannot
    touch a file the tiers govern, as classify() cannot touch an alert one."""
    if keep <= 0:
        return []
    stamped = sorted(((ts, n) for n in names if (ts := alert_stamp_of(n)) is not None),
                     reverse=True)
    return sorted(n for _, n in stamped[keep:])
