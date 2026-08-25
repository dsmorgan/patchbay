# Brief: configs-timeline  (2026-08-25, tier: sonnet)  — branch `feat/configs-timeline`

**Goal (one sentence):** `/configs` opens with a cross-device "What changed" timeline — the latest
versions across every Oxidized node, newest first, each row linking to its diff — above today's
node table, degrading to a reduced page whenever Oxidized is absent or partly broken, per ADR-0001
Decision 5.

**Why / where it fits:** `docs/architecture.md`'s fourth view is "one cross-device timeline with
in-app diffs". Today's page is per device. With the timeline, Records reads cleanly: Drift is the
record vs reality *now*; Configs is the record over *time*.

## Setup (worktree — mandatory)
```bash
cd D:/git/patchbay
git worktree add .worktrees/configs-timeline -b feat/configs-timeline ui/pages-and-map
cd D:/git/patchbay/.worktrees/configs-timeline
PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q      # green at the base
```
Commit only on this branch. Do not push. Operating instructions: `.claude/agents/implementer.md`.

## Context — read these first (paths, not pastes)
- `CLAUDE.md`; `docs/adr/0001-page-shell-and-map-modes.md` **Decision 5 in full**.
- `src/patchbay/web.py`: `configs()`, `config_node()`, `_ox_client()`, `_ox_nodes()`,
  `_ox_version_text()` (~line 1420–1520). Note how `config_node` calls `/node/version.json` and reads
  `date`/`time`, `message`/`subject`, `author`, `oid`.
- `src/patchbay/templates/configs.html` (you own it), `confignode.html` (read-only: the versions
  table columns and the diff link shape).
- `tests/test_web.py`: `test_configs_page_degrades_without_oxidized`, and how other tests use
  `monkeypatch`/`clean_env`. `httpx.MockTransport` is the test double (no new dependency).

## Contracts
Exactly ADR-0001 Decision 5:
```python
OX_TIMELINE_LIMIT, OX_TIMELINE_PER_NODE, OX_TIMELINE_BUDGET = 50, 20, 8.0
def _ox_ts(date: str | None) -> float | None      # "%Y-%m-%d %H:%M:%S %z", then ISO 8601; None if unparseable
def _ox_timeline(client, nodes) -> tuple[list[dict], list[str]]   # (entries, problem node names)
# entry keys: node, oid, prev, date, ts, message, author, href="/configs/<node>?v=<oid>&prev=<prev>"
# (href without &prev when prev is None). Newest first by (ts or 0) desc; unparseable dates last.
# Stop when OX_TIMELINE_BUDGET seconds elapse and set truncated=True.
```
`configs()` context adds `entries`, `problems`, `truncated`; keeps `nodes`, `error`, `oxidized_url`.
Degradation: `oxidized_url` unset → `error="unconfigured"` (today's prose, unchanged); unreachable →
`error=<str>` (today's line); one node's `version.json` failing → that node in `problems`, the page
still renders, footnote `Could not read history for: <names>` in muted text.
Page: sections `What changed` (table: when / device / message / diff; `truncated` adds a muted line
`showing the first N — more history is on each device`) then `Devices and their last backup`
(today's table, renamed heading). Empty timeline with reachable Oxidized: `No versions recorded yet.`

## Scope
**May edit:** `src/patchbay/templates/configs.html`; `src/patchbay/web.py` **only** `configs()` and the
new `_ox_ts`/`_ox_timeline`/constants placed directly beside `_ox_nodes`; `tests/test_web.py`
(append under a `# --- configs-timeline ---` banner).
**Read-only:** everything else. Other briefs own `dashboard()`/`drift()`, `ops()` + snapshot routes,
`device()`, `_topomap.html` in parallel — do not touch those. `confignode.html` is not yours.

## Acceptance (named, runnable)
- [ ] `PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q` green, including new
      `test_configs_timeline_lists_newest_first` (MockTransport serving two nodes with interleaved
      dates; assert row order and hrefs), `test_configs_timeline_survives_one_bad_node` (one node's
      `version.json` → 500; the other's rows render; the footnote names the bad node),
      `test_configs_timeline_unreachable_is_a_state_not_a_crash` (transport raises `httpx.ConnectError`;
      page 200 with the unreachable line), and the existing `test_configs_page_degrades_without_oxidized`
      unchanged.
- [ ] `D:/git/patchbay/.venv/Scripts/python.exe scripts/screenshots.py --out artifacts/shots/configs-timeline --pages /configs`
      → 2/2 ok (the demo has no Oxidized: the unconfigured prose, header intact). **Read** the PNG. For
      the configured look, render the page once through `TestClient` with the MockTransport into an
      HTML file and screenshot it from `file://` with Chrome (`"/c/Program Files/Google/Chrome/Application/chrome.exe" --headless=new --screenshot=… file:///…`);
      read that PNG too and say what the timeline looks like.
- [ ] Two-repo rule: node names in tests are demo-style (`core1`, `edge1`); no real hostnames.

## Non-goals
No changes to `config_node()` or `confignode.html`; no caching; no snapshot-to-snapshot diffs.

## When to ask for help (escalation — one rung up only)
Escalate to the **architect (Opus)** when any of: two failed attempts at the same sub-goal; the ADR
and the code disagree in a way that changes the design; a needed change outside your scope; a test
that can't go green without weakening it; toolchain failure after one retry. §3 message format.

## Done definition
Commit on `feat/configs-timeline` with the gate tails in the commit message. §9 report: `STATUS`,
`Changed`, `Verified`, `Evidence`, `Open issues`, `Escalations`, `Follow-ups`.
