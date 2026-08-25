# Brief: map-url-modes  (2026-08-25, tier: sonnet)  — branch `feat/map-url-modes`

**Goal (one sentence):** The topology map's whole state lives in the URL and `view` is a mode
(Wiring · Load · VLAN · Evidence) picked from a segmented control, per ADR-0001 Decision 1 — same
route, same graph JSON, same snapshot template, every existing capability kept.

**Why / where it fits:** The code already thinks in modes (the load checkbox swaps the legend
wholesale); the UI presents orthogonal checkboxes and a nine-style legend. A mode in the URL makes
a view linkable from every other page (brief `deep-links-and-device-endpoints` builds those links)
and survives the reload on purpose rather than by Firefox's accident.

## Setup (worktree — mandatory)
```bash
cd D:/git/patchbay
git worktree add .worktrees/map-url-modes -b feat/map-url-modes ui/pages-and-map
cd D:/git/patchbay/.worktrees/map-url-modes
PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q      # green at the base
```
Commit only on this branch. Do not push. Operating instructions: `.claude/agents/implementer.md`.

## Context — read these first (paths, not pastes)
- `CLAUDE.md`; `docs/adr/0001-page-shell-and-map-modes.md` **Decision 1 in full** (the state table,
  the JS state model, the paint-per-mode table, the legend `data-modes` assignments) and the
  "Consequences" section. Decision 2 (tiers, fit, `focus` centering) is the *next* brief — not yours.
- `src/patchbay/templates/_topomap.html` — all of it. You own this file. Note `SNAPSHOT`,
  `applyLoadView`, `applyVlan`, `syncEdgeOpacity`, `applyFilters`, the legend markup, the toolbar.
- `src/patchbay/templates/topology.html` (read-only: the `controls` block and the disabled refresh),
  `templates/snapshot.html` (read-only: how the map is included with `snapshot = true`).
- `src/patchbay/web.py` `build_topology_graph()` (read-only) for edge fields: `cls`, `util`, `putil`,
  `speed`, `vlans`, and node `vlans`, `status`, `role`.
- `tests/test_web.py` (append only) and `tests/test_snapshot.py` (must stay green: it asserts
  `const SNAPSHOT = true` and that d3 is inlined).

## Contracts
Everything in ADR-0001 Decision 1 is the contract. Specifics the implementation must hit:

1. **State** — `DEFAULTS`, `readState()` (URL `search`, then `hash` when it contains `=`; sanitize:
   unknown `view` → `wiring`, `vlan` not in `graph.vlans` → unset, booleans from `1`/`0`), `writeState()`
   (only non-defaults, in table order, unknown params preserved verbatim, `history.replaceState`;
   **no-op when `SNAPSHOT`**), `set(patch)`, `render()` in the ADR's order. A `popstate` listener
   re-reads and re-renders. `applyControls()` writes the DOM from `state` at init — delete the old
   "run once at init" calls and their Firefox comment.
2. **Toolbar** — keep every existing element id (`hideoff`, `coreonly`, `hosts`, `unmhosts`, `loadmode`,
   `vlansel`, `legend`, `leg-heat`, `leg-peak`, `leg-vlan`, `topo`). Add, first in the toolbar, a
   segmented control: `<span class="grp modes" role="radiogroup" aria-label="View">` holding four
   `<input type="radio" name="view" id="view-<mode>" value="<mode>">` + `<label for=…>` pairs, labels
   **Wiring · Load · VLAN · Evidence** in that order. The `load` group (`now` / `24h peak` select) renders
   only in `view=load`; the `vlan` group only in `view=vlan` (toggle a `hidden` attribute on the group
   spans). The four visibility checkboxes stay visible in every mode. A `<span class="chips" id="chips">`
   shows a chip for any state that is set but whose control is hidden — `vlan 20 ×`, `focus core1 ×`,
   `peak ×` — and the × clears that param via `set({...})`. CSS for the segmented control and chips
   goes in this template's `<style>` (palette tokens only: `--selected` for the lit segment, `--muted`
   text, `--ink` when lit; `input:focus-visible + label` gets the accent outline).
3. **Paint** — `applyPaint()` per the ADR table. Cables = every edge class except `virtualized`, `oob`,
   `wan`; inferred cables = `fdbuplink`, `fdbinference`, `fdbhost`. Wiring/VLAN: cables `var(--muted)`,
   inferred dashed `"7 3"`, confirmed `"none"`, non-cables `null`. Load: `heat(util)` unchanged (peak
   from `putil` when `state.load === "peak"`), dash `"none"`. Evidence: `null`/`null` (today's CSS).
   The VLAN dim (`syncEdgeOpacity` behaviour) composes with every mode and always wins.
4. **`focus`** — read/written/chipped, and the named node gets class `focused` (a halo: a second
   `rect` stroke or `filter: drop-shadow` in this template's CSS, accent colour). No centering yet —
   that is Decision 2.
5. **Legend** — every `.item` gets `data-modes`; add the two wiring items ("confirmed cable" solid
   `--muted`, "inferred cable" dashed `--muted`); `applyLegend()` per the ADR (`#leg-vlan` also needs
   `state.vlan`, `#leg-peak` needs `load === "peak"`). Remove the old `lnksrc` show/hide.
6. **Snapshot** — `python scripts/screenshots.py --snapshot --pages /topology` must produce a working
   snapshot: opening `snapshot.html?view=load` or `snapshot.html#view=load` from `file://` paints load;
   nothing writes to `history` under `SNAPSHOT`.

## Scope
**May edit:** `src/patchbay/templates/_topomap.html`; `tests/test_web.py` (append at the end under a
`# --- map-url-modes ---` banner).
**Read-only:** everything else — `topology.html`, `snapshot.html`, `web.py`, `_style.html`, `base.html`.

## Acceptance (named, runnable)
- [ ] `PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q` green, including new
      `tests/test_web.py::test_topology_page_renders_mode_control` (the four radios with those ids and
      values; at least one legend `.item` per mode by `data-modes`; the old `lnksrc` class gone) and
      the existing `tests/test_snapshot.py` unchanged and green.
- [ ] `D:/git/patchbay/.venv/Scripts/python.exe scripts/screenshots.py --out artifacts/shots/map-url-modes --widths 1400 --snapshot --pages "/topology,/topology?view=load&load=peak,/topology?view=vlan&vlan=20,/topology?view=evidence,/topology?view=load&vlan=30,/topology?focus=core1"`
      → all ok. **Read every PNG** and say what you see: the lit segment matches the URL; wiring shows
      grey edges with dashes only on inferred cables and no heat swatches in the legend; load shows heat
      colours and the heat legend, with "pk" labels when peak; vlan dims non-carriers and shows the vlan
      select; evidence shows today's reporter colours; `load&vlan=30` shows a `vlan 30 ×` chip; `focus`
      shows a halo on core1 and a `focus core1 ×` chip; the snapshot PNG paints the map.
- [ ] Two-repo rule: nothing site-specific in the diff.

## Non-goals
No tiers, no fixed Y, no fit-to-view, no `focus` centering (Decision 2, next brief). No route changes.
No changes outside `_topomap.html`. Don't restyle the map beyond the toolbar/chips/halo CSS named above.

## When to ask for help (escalation — one rung up only)
Escalate to the **architect (Opus)** when any of: two failed attempts at the same sub-goal; the ADR
and the code disagree in a way that changes the design; a needed change outside your scope; a test
that can't go green without weakening it; toolchain failure after one retry. Use the §3 message format
from `docs/process/multi-agent-playbook.md`.

## Done definition
Commit on `feat/map-url-modes` with the gate tails in the commit message. Report in the §9 format:
`STATUS`, `Changed`, `Verified`, `Evidence` (PNG paths + one-line readings), `Open issues`,
`Escalations`, `Follow-ups`. No TODOs. No claims without output.
