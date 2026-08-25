# Brief: overview-exceptions  (2026-08-25, tier: sonnet)  — branch `feat/overview-exceptions`

**Goal (one sentence):** The Overview leads with an *exceptions* strip (or "all clear"), folds each
hypervisor's guests into its card, and drops the Links table, per ADR-0001 Decision 3.

**Why / where it fits:** The landing page mixed "is anything wrong?" with "what do I have?" and
answered neither first. At 7am the first question wins.

## Setup (worktree — mandatory)
```bash
cd D:/git/patchbay
git worktree add .worktrees/overview-exceptions -b feat/overview-exceptions ui/pages-and-map
cd D:/git/patchbay/.worktrees/overview-exceptions
PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q      # green at the base
```
Commit only on this branch. Do not push. Operating instructions: `.claude/agents/implementer.md`.

## Context — read these first (paths, not pastes)
- `CLAUDE.md`; `docs/adr/0001-page-shell-and-map-modes.md` **Decision 3 in full** and Decision 6
  (the `href` convention for links into the map: defaults omitted).
- `src/patchbay/web.py`: `dashboard()` (~line 152–232), `drift()` (~line 890–970), `_age()`,
  `build_topology_graph()` — only its inner `edge_speed()` (~line 400–420), `TOPO_ROLES`.
- `src/patchbay/templates/dashboard.html` (you own it), `drift.html` (read-only: the heading voice and
  the stat-tile pattern), `_ui.html` (`ricon`), `_style.html` (read-only: `.statgrid`, `.stat`, `.card`,
  `.dot`).
- `tests/test_web.py`: how tests `seed(...)` a DB, `test_empty_sections_are_hidden`,
  `test_configured_but_empty_points_at_ops`, and the drift tests (what conflicts look like in the seed).

## Contracts
Exactly ADR-0001 Decision 3:

```python
STALE_MIN = 15                                   # web.py module level, beside _age()
def speed_tier(bps: int | None) -> str           # "" | "slow" (<=100M) | "vslow" (<=10M); edge_speed() calls it
def drift_report(conn, settings) -> dict         # extracted from drift(); drift() renders it unchanged
def _exceptions(conn, settings) -> tuple[list[dict], list[str]]   # (exceptions, checked)
# exception item shape, severities, and the four rules (device-down, slow-link, drift, stale-source)
# are in the ADR — including: unknown/NULL status is never an exception; guests are not listed here;
# a link with no known speed is not slow; drift only when have_ipam; stale = any _age() > STALE_MIN.
```
`hrefs`: device-down item → `/device/<name>`; slow-link item → `/topology?focus=<a_device>` (label
`a_device a_iface ↔ b_device b_iface`, detail the speed); drift → `/drift`; stale-source → `/ops`.

`dashboard()` context: **add** `exceptions`, `checked`, `vms_by_host`, `orphan_vms`; **remove** `links`
and the flat `vms` list. Page order: exceptions strip → stat grid + gateways → Fabric → Access points →
Hypervisors (guests folded in) → orphan guests (only when any). Delete the Links table.

Template: the strip is one `<section class="exceptions">` — one card per exception (severity as a
left border colour via `--crit`/`--warn`/`--muted`, the title, then its items as links with muted
detail), or one all-clear card reading `All clear — ` + `", ".join(checked)`. When `checked` is empty
(nothing could be checked, e.g. a fresh DB) render nothing. CSS in this template's `<style>`.
Hypervisor cards: the existing "N/M VMs running" line becomes a `<summary>` of a `<details>` holding
today's VM table minus the Host column. Section headings in the P1 voice: `Fabric — switches and
firewalls`, `Access points`, `Hypervisors and their guests`, `Guests without a host on this page`.
The onboarding box stays exactly as it is (tests assert its text).

## Scope
**May edit:** `src/patchbay/templates/dashboard.html`; `src/patchbay/web.py` **only** in `dashboard()`,
`drift()` (the extraction), `edge_speed()` (the two-line `speed_tier` call), and new module-level
`STALE_MIN`/`speed_tier`/`drift_report`/`_exceptions` placed directly above `dashboard()`;
`tests/test_web.py` (append under a `# --- overview-exceptions ---` banner; you may also *edit*
`test_empty_sections_are_hidden` so it no longer expects a Links heading to exist at all).
**Read-only:** everything else. Other briefs own `ops()`, `configs()`/`_ox_*`, `device()`, and
`_topomap.html` in parallel — do not touch those regions.

## Acceptance (named, runnable)
- [ ] `PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q` green, including new
      `test_overview_shows_exceptions_when_something_is_wrong` (seed, then mark a device down and
      shrink a link's port speed to 10M: two exception cards with the right hrefs),
      `test_overview_says_all_clear_when_nothing_is`, `test_overview_folds_vms_into_hypervisors`
      (a `<details>` per hypervisor; VM names inside; no `<th>Host</th>` on the page),
      `test_overview_drops_the_links_table`, and `test_drift_*` unchanged and green after the extraction.
- [ ] `D:/git/patchbay/.venv/Scripts/python.exe scripts/screenshots.py --out artifacts/shots/overview-exceptions --pages /,/drift`
      → 4/4 ok. **Read** `home-1400.png` and `home-600.png`: the strip is first (the demo network has
      two IPAM conflicts, so expect a drift card — say what it shows), the stat grid follows, VMs are
      inside their hypervisor cards, no Links table, nothing clipped at 600. `drift-1400.png` unchanged
      in content.
- [ ] Two-repo rule: nothing site-specific in the diff.

## Non-goals
No map changes; no changes to `/drift`'s template; no new stat tiles; no alerting.

## When to ask for help (escalation — one rung up only)
Escalate to the **architect (Opus)** when any of: two failed attempts at the same sub-goal; the ADR
and the code disagree in a way that changes the design; a needed change outside your scope; a test
that can't go green without weakening it; toolchain failure after one retry. §3 message format.

## Done definition
Commit on `feat/overview-exceptions` with the gate tails in the commit message. §9 report:
`STATUS`, `Changed`, `Verified`, `Evidence`, `Open issues`, `Escalations`, `Follow-ups` (include the
`base.html` line for the PM: replace the hardcoded `15` with the `STALE_MIN` global).
