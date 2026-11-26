"""What the /alerts page lists, built without a web framework.

The page, the Overview, and the MCP `alerts` tool (ADR-0004) all read the
attention list through these functions, so the three can't disagree about
what is open, since when, or what holds it. Nothing here writes: callers
that may migrate the database (`db.init`) do so before calling in.
"""

from __future__ import annotations

import sqlite3
import time

from . import alerting, db, silences
from .attention import EVENT_WINDOW_S, attention_items, stamp_first_seen

# History shows the newest events; the table holds up to
# alerting.EVENT_KEEP_MAX, which is an archive, not a page
ALERT_HISTORY_ROWS = 200


def span(mins: float) -> str:
    """A compact duration for the attention lists: minutes, then hours,
    then days. Each unit rounds before its boundary check, so 59.6 minutes
    reads as 1h, not 60m, and 47.9 hours as 2d, not 48h."""
    m = round(mins)
    if m < 60:
        return f"{m}m"
    h = round(mins / 60)
    if h < 48:
        return f"{h}h"
    return f"{round(mins / 1440)}d"


def since(ts: float | None) -> str:
    """How long an attention item has been firing, from its poll-recorded
    first-seen. None means the poll hasn't recorded it yet — just noticed."""
    if ts is None:
        return "new"
    return span(max(0, (db.now() - ts) / 60))


def event_leaves(it: dict) -> str | None:
    """How long until a one-shot event leaves the Active tab (#154), or
    None for an item that is not an event. An event has no condition to
    end, so it stays EVENT_WINDOW_S after it happened and then drops off.
    The remaining time never reads as 0m while the row is still shown."""
    rule = alerting.RULES.get(it.get("rule"))
    if rule is None or not rule.event or it.get("at") is None:
        return None
    return span(max(1, (it["at"] + EVENT_WINDOW_S - db.now()) / 60))


def mark_held(conn: sqlite3.Connection, items: list[dict]) -> None:
    """Tag each item the alert engine holds while its device is down (#118),
    "held: sw1 is down": the rail badge counts what the engine pages about,
    so the two numbers differ by exactly these, and the tag says why."""
    down = alerting.down_devices(conn, db.now())
    for it in items:
        it["held"] = alerting.held_text(it, down) if down else ""


def alert_lists(conn: sqlite3.Connection, settings
                ) -> tuple[list[dict], list[dict], list[str]]:
    """The Active and Silenced tabs: (active, silenced, checked). Each item
    is an attention item annotated with first-seen, how long it has fired,
    whether the engine holds it, when an event leaves, and the silence
    scopes it offers; a silenced item also says which silence covers it
    and until when. `checked` names the checks that ran."""
    every, checked = attention_items(conn, settings)
    stamp_first_seen(conn, every)
    mark_held(conn, every)
    for it in every:
        it["since"] = since(it["first_seen"])
        it["leaves"] = event_leaves(it)
        it["scopes"] = silences.scopes_for(it)
    active = [it for it in every if not it["silenced"]]
    silenced = [it for it in every if it["silenced"]]
    for it in silenced:
        it["silence_what"] = silences.describe(it["silenced"])
        it["silence_until"] = silences.until_text(it["silenced"])
    return active, silenced, checked


def alert_history(conn: sqlite3.Connection, *, category: str | None = None,
                  severity: str | None = None,
                  limit: int = ALERT_HISTORY_ROWS) -> list[dict]:
    """The History tab: the alert engine's event log, newest first, with
    a local `when` for display."""
    where, args = [], []
    if category:
        where.append("category = ?"); args.append(category)
    if severity:
        where.append("severity = ?"); args.append(severity)
    return [
        {**dict(r), "when": time.strftime("%Y-%m-%d %H:%M",
                                          time.localtime(r["ts"]))}
        for r in conn.execute(
            "SELECT * FROM alert_events"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY ts DESC, id DESC LIMIT ?",
            (*args, limit))]
