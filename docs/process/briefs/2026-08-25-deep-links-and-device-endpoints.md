# Brief: deep-links-and-device-endpoints  (2026-08-25, tier: sonnet)  — branch `feat/deep-links-and-device-endpoints`

**Goal (one sentence):** Pages link into the map with its URL state (a VLAN row → the VLAN view; a
device → `focus`), patch-panel rows are anchorable, and the device page folds "Endpoints seen here"
into the ports table as an expandable count column, per ADR-0001 Decisions 6 and 7.

**Why / where it fits:** Purpose clarity comes from the round trip: the map shows *where*, the pages
show *what*. And the FDB says which port a MAC is on — show it there.

## Setup (worktree — mandatory)
```bash
cd D:/git/patchbay
git worktree add .worktrees/deep-links-and-device-endpoints -b feat/deep-links-and-device-endpoints ui/pages-and-map
cd D:/git/patchbay/.worktrees/deep-links-and-device-endpoints
PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q      # green at the base
```
Commit only on this branch. Do not push. Operating instructions: `.claude/agents/implementer.md`.

## Context — read these first (paths, not pastes)
- `CLAUDE.md`; `docs/adr/0001-page-shell-and-map-modes.md` **Decisions 6 and 7 in full**, and the
  state table in Decision 1 (the URL params you link to — build them with defaults omitted).
- `src/patchbay/web.py`: `device()` (~line 1530–1640: how `ports`, `endpoints`, `port_vlans` are built),
  `patchpanel()` (read-only), `vlans()` (read-only).
- `src/patchbay/templates/device.html` (you own it: the ports table, the `.graphrow` toggle script,
  the soft-refresh guard at the bottom, the `controls` block), `vlans.html`, `patchpanel.html`.
- `tests/test_web.py`: the device/vlans/patchpanel tests and how they seed.

## Contracts
- **VLANs**: in each VLAN row, the vlan id cell links to `/topology?view=vlan&vlan=<vid>` (title
  `show on the map`). Rows without a vid (aggregates) are unchanged.
- **Device**: `controls` gains `<a href="/topology?focus=<name urlencoded>">show on map</a>` before the
  existing show-all/hide-idle link. The route adds `endpoints_by_port: dict[str, list[Row]]` keyed by
  the port name *as it appears in `ports`* (exact match first, then `p.name.removeprefix("ethernet")`),
  and `endpoints_other: list[Row]` for the rest (AP clients keyed by SSID, MAC-only rows). Matching
  is server-side. The ports table gains a last column **Endpoints** showing the count as a link
  (`<a href="#" class="ep" data-if="…">3</a>`; `·` when 0) that toggles a `<tr class="eprow">`
  beneath the port with today's endpoint columns (Hostname / IP / MAC / VLAN) — the same mechanism
  as `.graphrow`; opening one closes the other for that port. The soft-refresh guard pauses on
  `.graphrow, .eprow`. "Endpoints seen here" becomes **`Endpoints not tied to a port (N)`**, rendered
  only when `endpoints_other` is non-empty, same columns as today.
- **Patch panels**: each populated row gets `id="p<position>"`; a `:target` rule in the page's
  `<style>` highlights the row (`--selected` background, accent left inset like the rail). Nothing
  else changes; `/patchpanel?panel=<name>#p<i>` now lands on a row.

## Scope
**May edit:** `src/patchbay/templates/vlans.html`, `device.html`, `patchpanel.html`; `src/patchbay/web.py`
**only** `device()`; `tests/test_web.py` (append under a `# --- deep-links-and-device-endpoints ---` banner).
**Read-only:** everything else. Other briefs own `dashboard()`/`drift()`, `ops()` + snapshot routes,
`configs()`/`_ox_*`, `_topomap.html` in parallel — do not touch those. The map's `focus`/`view=vlan`
handling lands with the map brief; your hrefs are correct regardless and the tests assert the strings.

## Acceptance (named, runnable)
- [ ] `PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q` green, including new
      `test_device_page_folds_endpoints_into_ports` (seed; a port with endpoints shows its count and
      an `eprow` with the hostnames; `Endpoints seen here` gone; the not-tied section only when
      applicable), `test_vlans_row_links_to_the_map` (`?view=vlan&vlan=20` present; no `hideoff=` in
      any href), `test_patchpanel_rows_are_anchored` (`id="p1"` present).
- [ ] `D:/git/patchbay/.venv/Scripts/python.exe scripts/screenshots.py --out artifacts/shots/deep-links --pages /device/core1,/vlans,/patchpanel`
      → 6/6 ok. **Read** `device_core1-1400.png` (Endpoints column with counts; "show on map" in the
      header controls), `vlans-1400.png` (vlan ids are links), `patchpanel-1400.png`. Then render
      `/device/core1` once through `TestClient`, save to HTML, open it with Chrome from `file://` with
      `#` and a click? — not possible headless; instead assert the `eprow` toggle in the test and
      **read the JS you changed** for the open/close logic, stating it in the report.
- [ ] Two-repo rule: nothing site-specific in the diff.

## Non-goals
No map changes; no `[n]` → panel link from the map; no edge tooltips; no device → configs link.

## When to ask for help (escalation — one rung up only)
Escalate to the **architect (Opus)** when any of: two failed attempts at the same sub-goal; the ADR
and the code disagree in a way that changes the design; a needed change outside your scope; a test
that can't go green without weakening it; toolchain failure after one retry. §3 message format.

## Done definition
Commit on `feat/deep-links-and-device-endpoints` with the gate tails in the commit message. §9 report:
`STATUS`, `Changed`, `Verified`, `Evidence`, `Open issues`, `Escalations`, `Follow-ups`.
