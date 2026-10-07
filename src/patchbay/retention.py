"""Tiered snapshot retention (#65): which timestamped snapshots to keep.

Pure and filesystem-free: the spec parser and the keeper selection take
strings and datetimes, so the local directory, the delivery directory, and
the /snapshots page all run the same rule and tests need no files.

A snapshot survives if any tier claims it. Periodic tiers keep the EARLIEST
snapshot of each week, month, or year: that keeper is known the day it is
taken, and because pruning never removes a keeper inside its window, the
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

# display order on /snapshots, shortest-lived first
TIERS = ("daily", "weekly", "monthly", "yearly", "first")

_TERM = re.compile(r"(\d+)([dwmy]?)")
_UNIT = {"": "daily", "d": "daily", "w": "weekly", "m": "monthly", "y": "yearly"}


@dataclass(frozen=True)
class KeepSpec:
    """A parsed PATCHBAY_SNAPSHOT_KEEP. Per tier: None = not in the spec
    (keeps nothing), 0 = unlimited, n = the newest n snapshots (daily) or
    the n most recent calendar periods (weekly/monthly/yearly)."""
    daily: int | None = None
    weekly: int | None = None
    monthly: int | None = None
    yearly: int | None = None
    first: bool = False

    def __str__(self) -> str:
        terms = [f"{self.daily}"] if self.daily is not None else []
        terms += [f"{n}{u}" for n, u in ((self.weekly, "w"), (self.monthly, "m"),
                                          (self.yearly, "y")) if n is not None]
        return ",".join(terms + (["first"] if self.first else []))

    def describe(self) -> str:
        """The spec as a sentence fragment for /snapshots and /ops."""
        if self.daily == 0:
            return "every timestamped snapshot"
        parts = [f"the newest {self.daily}" if (self.daily or 1) > 1 else "the newest"]
        for n, period in ((self.weekly, "week"), (self.monthly, "month"),
                          (self.yearly, "year")):
            if n is not None:
                parts.append(f"the first of every {period}" if n == 0 else
                             f"the first of each of the last {n} {period}s"
                             if n > 1 else f"the first of this {period}")
        if self.first:
            parts.append("the first ever")
        return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]


def parse_keep_spec(raw: str) -> KeepSpec:
    """Parse a tier spec such as '30,12m,3y,first'. Raises ValueError on any
    term it can't read: the caller must then prune nothing, because a typo
    in a retention setting must never cost history."""
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
    alone. The newest snapshot is always kept, so a spec without a daily
    term never deletes the file just written."""
    ordered = sorted(set(stamps))
    if not ordered:
        return {}
    claims: dict[datetime, list[str]] = {}

    def claim(ts: datetime, tier: str) -> None:
        claims.setdefault(ts, []).append(tier)

    daily = 1 if spec.daily is None else spec.daily
    for ts in ordered if daily == 0 else ordered[-daily:]:
        claim(ts, "daily")

    newest = ordered[-1]
    periods = (
        # (tier, window, period key, distance in periods from the newest)
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
