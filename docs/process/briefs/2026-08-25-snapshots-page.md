# Brief: snapshots-page  (2026-08-25, tier: sonnet)  — branch `feat/snapshots-page`

**Goal (one sentence):** A `/snapshots` page under Records lists kept snapshots with per-file
downloads and a "snapshot now" action, and `/ops` loses its snapshot buttons and is reordered,
per ADR-0001 Decision 4.

**Why / where it fits:** The break-glass snapshot is a headline feature whose whole UI was two
buttons on Ops. A record of the network belongs under Records; Ops shrinks to feeding and
configuring patchbay.

## Setup (worktree — mandatory)
```bash
cd D:/git/patchbay
git worktree add .worktrees/snapshots-page -b feat/snapshots-page ui/pages-and-map
cd D:/git/patchbay/.worktrees/snapshots-page
PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q      # green at the base
```
Commit only on this branch. Do not push. Operating instructions: `.claude/agents/implementer.md`.

## Context — read these first (paths, not pastes)
- `CLAUDE.md`; `docs/adr/0001-page-shell-and-map-modes.md` **Decision 4 in full**.
- `src/patchbay/web.py`: `NAV` (~line 113 — the `/snapshots` entry already exists), `ops()`,
  `ops_snapshot` (`POST /ops/snapshot`), `ops_snapshot_latest` (`GET /ops/snapshot/latest`)
  (~line 1030–1120). `src/patchbay/snapshot.py` `write_snapshot()` (filenames, `patchbay-latest.html`,
  retention). `src/patchbay/config.py` (~line 174–184, 376–384: `snapshot_dir`, `snapshot_keep`,
  `snapshot_deliver_dir`, `snapshot_at`). `src/patchbay/cli.py` `cmd_demo` (`demo.MARKER`, how the
  demo-seeded state is detected).
- `src/patchbay/templates/ops.html` (you own it), `configs.html` (read-only: the unconfigured/empty
  prose pattern), `drift.html` (heading voice).
- `tests/test_web.py`: `PAGES`, `test_ops_snapshot_download_404s_before_first_snapshot` and the other
  ops/snapshot tests; `tests/test_snapshot.py` (how a snapshot dir is set up in tests).

## Contracts
Exactly ADR-0001 Decision 4:
- `GET /snapshots` → `templates/snapshots.html` (new). Context: `snapshots` (newest first;
  `name`, `when` (from the filename, no timezone claimed), `ts`, `size_mb`, `href`), `latest`
  (`exists`, `href` = `/snapshots/patchbay-latest.html`), `settings_view` (`dir`, `deliver_dir`, `at`,
  `keep`), `is_demo` (`db.get_state(conn, demo.MARKER) == "1"`), plus `ages` like every page.
- `GET /snapshots/{name}` → the file as `attachment`, only when
  `re.fullmatch(r"patchbay-(?:\d{8}-\d{6}|latest)\.html", name)` **and**
  `(dir / name).resolve().parent == Path(dir).resolve()`; otherwise 404. Place both routes directly
  after `ops_snapshot_latest`. `GET /ops/snapshot/latest` stays.
- "Snapshot now" reuses `POST /ops/snapshot`. Move the act-button `<style>`, the `<pre id="out">` and
  the button-click `<script>` from `ops.html` into `templates/_actions.html` (new), included by both
  pages after their buttons; the `.dclsave` script stays in `ops.html`.
- Page: purpose from NAV. Sections: a row of actions (`snapshot now`, and `download the latest`
  when it exists) → `Kept here` table (when / file / size / download) or the empty state
  (`No snapshot yet — take one, or set PATCHBAY_SNAPSHOT_AT for one a day`) → `Where they go`
  (dir, deliver dir or the muted caution from the ADR, schedule, keep). When `is_demo`, one line:
  `This model is the demo network, so these snapshots are safe to share.`
- `/ops` after: `Collect now` → `Declarations — what only you can tell patchbay` (with the
  `conflicts` and `warnings` panels moved into this section, above the fields) → `What patchbay is
  running on` (the effective-configuration table, last). Remove the `snapshot now` button, the
  `download the latest snapshot` link, and `has_snapshot` from the `/ops` context. Nothing else on
  Ops changes.
- `tests/test_web.py::PAGES` gains `"/snapshots"`.

## Scope
**May edit:** `src/patchbay/templates/snapshots.html` (new), `templates/_actions.html` (new),
`templates/ops.html`; `src/patchbay/web.py` **only** `ops()` and the two new routes beside
`ops_snapshot_latest`; `tests/test_web.py` (`PAGES` line, and append under a
`# --- snapshots-page ---` banner).
**Read-only:** everything else. Other briefs own `dashboard()`/`drift()`, `configs()`/`_ox_*`,
`device()`, `_topomap.html` in parallel — do not touch those. `NAV` and `_style.html` are PM-owned;
page CSS goes in the page's `<style>`.

## Acceptance (named, runnable)
- [ ] `PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q` green, including new
      `test_snapshots_page_lists_newest_first` (write two timestamped files + latest into a tmp
      `PATCHBAY_SNAPSHOT_DIR`; order and hrefs), `test_snapshot_download_serves_only_the_pattern`
      (`/snapshots/patchbay-20250101-000000.html` → 200 attachment; `/snapshots/..%2F..%2Fpatchbay.db`,
      `/snapshots/other.html`, `/snapshots/patchbay-latest.html` when absent → 404),
      `test_snapshots_page_empty_state`, `test_ops_no_longer_offers_snapshot_buttons`; and
      `test_ops_snapshot_download_404s_before_first_snapshot` still green.
- [ ] `D:/git/patchbay/.venv/Scripts/python.exe scripts/screenshots.py --out artifacts/shots/snapshots-page --pages /snapshots,/ops`
      → 4/4 ok. **Read** `snapshots-1400.png` (empty state, the demo line, the "where they go" section
      with the caution) and `ops-1400.png` (three sections in the new order, no snapshot buttons, the
      poll buttons still there). Then click-test the action once: run the server
      (`PATCHBAY_ENV=/nonexistent/.env PATCHBAY_DB=<a demo db> PYTHONPATH=src python -m uvicorn patchbay.web:app --port 8095`),
      `curl -X POST http://127.0.0.1:8095/ops/snapshot -H "origin: http://127.0.0.1:8095"`, then
      `GET /snapshots` lists it and `GET /snapshots/patchbay-latest.html` downloads it. Paste the tails.
- [ ] Two-repo rule: nothing site-specific in the diff.

## Non-goals
No changes to `snapshot.py`; no retention UI; no delivery changes; no per-file demo detection.

## When to ask for help (escalation — one rung up only)
Escalate to the **architect (Opus)** when any of: two failed attempts at the same sub-goal; the ADR
and the code disagree in a way that changes the design; a needed change outside your scope; a test
that can't go green without weakening it; toolchain failure after one retry. §3 message format.

## Done definition
Commit on `feat/snapshots-page` with the gate tails in the commit message. §9 report: `STATUS`,
`Changed`, `Verified`, `Evidence`, `Open issues`, `Escalations`, `Follow-ups` (lines for `README.md` /
`docs/configuration.md` if the page deserves a mention — PM applies).
