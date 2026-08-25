# Brief: map-tiers-fit  (2026-08-25, tier: sonnet)  — branch `feat/map-tiers-fit`

**Goal (one sentence):** The map draws its four tiers as labelled swimlanes, fixes every node's Y to
its tier (nodes drag along the lane; pins become X-only), fits its content to the frame on load and
after every change, and centres a `focus`ed node, per ADR-0001 Decision 2.

**Why / where it fits:** The rank bands exist but are invisible, the force layout "breathes" in two
dimensions, and the demo already clips a node off the frame at 1400px. Drawn tiers make the map read
top-down as a diagram; one free dimension makes it stable; fit makes the first impression whole.

## Setup (worktree — mandatory)
```bash
cd D:/git/patchbay
git worktree add .worktrees/map-tiers-fit -b feat/map-tiers-fit ui/pages-and-map   # base includes map-url-modes
cd D:/git/patchbay/.worktrees/map-tiers-fit
PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q      # green at the base
```
Commit only on this branch. Do not push. Operating instructions: `.claude/agents/implementer.md`.

## Context — read these first (paths, not pastes)
- `CLAUDE.md`; `docs/adr/0001-page-shell-and-map-modes.md` **Decision 2 in full** (band table, `fy`
  rule, X-only pins, layer order, the `fit`/`focusNode` sketch, when fit runs) and the Decision 1 state
  model the previous brief implemented (`state`, `set`, `render`, `focus`).
- `src/patchbay/templates/_topomap.html` — all of it, as merged from `map-url-modes`. You own this file.
  Note `rankY`, `assignSlots`, the `forceY`/`forceX` forces, the drag handlers, `savePos`, `applyFilters`,
  the `focused` halo class, `SNAPSHOT`.
- `src/patchbay/web.py` `RANK`/`TOPO_ROLES` (read-only), `build_topology_graph()` node `rank`/`px`/`py`
  (read-only), the `/api/positions` routes (read-only — unchanged schema).
- `tests/test_web.py` (append only), `tests/test_snapshot.py` (must stay green).

## Contracts
Exactly ADR-0001 Decision 2:
1. **Bands** — four, contiguous fractions of the layout height: Internet 0–0.16, Edge 0.16–0.38,
   Fabric 0.38–0.66, Access & compute 0.66–1.00, keyed by rank −1/0/1/2. Drawn in a `gBands` layer
   first inside the zoom group: a faint rect per band (`var(--card)` at low opacity alternating, or
   a 1px `--grid` rule between bands — pick the quieter of the two after looking at it), the name at
   the left inside edge in the legend's muted micro style, and the ADR's tooltip text as `<title>`.
   A band renders only when a rank has ≥1 visible node; its width spans the visible nodes' X extent
   plus 400px each side, recomputed at each settle.
2. **Y owned by the tier** — every node gets `fy = bandCenter(rank)` always; delete `forceY`; drag sets
   `d.fx` only; `savePos` keeps POSTing `{name, x, y}` with the node's band Y; `py` is ignored on load
   (`px` still honoured). `rankY`'s role is replaced by `bandCenter()`, which derives from the current
   `#topo` height.
3. **Layers** — `gBands`, then `gContent` holding links, labels, nodes (in today's order). `fit()`
   measures `gContent`'s bbox, never `g`.
4. **Fit** — `zoom.scaleExtent([0.2, 3])`; `fit(animate)` per the ADR sketch (pad 28, clamp to the
   extent, centre); runs on `sim.on("end")`, at the end of `applyFilters()`, and from a debounced
   (150 ms) `ResizeObserver` on `#topo` that also recomputes band centres and every `fy`. A **`fit`**
   button in the toolbar (last `.grp`, next to nothing else) calls `fit(true)`; it exists in the snapshot
   too. `reset layout` stays in `topology.html`'s controls (read-only, untouched).
5. **Focus** — `focusNode(name)`: after the first settle, centre that node at scale `max(k, 0.9)` with a
   400 ms transition, keep the `focused` halo. Runs whenever `state.focus` is set at render; clearing
   the chip calls `fit(true)`.
6. **Snapshot** — everything above works with `SNAPSHOT` (no persistence; fit and focus still run).

## Scope
**May edit:** `src/patchbay/templates/_topomap.html`; `tests/test_web.py` (append under a
`# --- map-tiers-fit ---` banner).
**Read-only:** everything else.

## Acceptance (named, runnable)
- [ ] `PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q` green, including new
      `tests/test_web.py::test_topology_page_renders_tier_bands` (the four band names appear in the
      markup/JS the page ships, and the `fit` button is present) and `tests/test_snapshot.py` green.
- [ ] `D:/git/patchbay/.venv/Scripts/python.exe scripts/screenshots.py --out artifacts/shots/map-tiers-fit --widths 1400,600 --snapshot --pages "/topology,/topology?focus=core1,/topology?view=load&hosts=1"`
      → all ok. **Read every PNG**: four labelled bands; every node inside its band; the whole graph
      inside the frame at both widths (the internet cloud is no longer clipped at the top); `focus=core1`
      centred and haloed; `hosts=1` re-fits with the extra nodes; the snapshot paints bands too.
- [ ] Manual (state in the report that you did it in a real browser, or that you could not): drag a
      node — it moves in X only; reload — X kept, Y in-lane; `reset layout` still clears pins.
- [ ] Two-repo rule: nothing site-specific in the diff.

## Non-goals
No change to `/api/positions` or the `positions` schema; no migration of saved Y; no new modes; no
minimap; no route changes.

## When to ask for help (escalation — one rung up only)
Escalate to the **architect (Opus)** when any of: two failed attempts at the same sub-goal; the ADR
and the merged `map-url-modes` code disagree in a way that changes the design; a needed change outside
your scope; a test that can't go green without weakening it; toolchain failure after one retry.
§3 message format from `docs/process/multi-agent-playbook.md`.

## Done definition
Commit on `feat/map-tiers-fit` with the gate tails in the commit message. §9 report: `STATUS`,
`Changed`, `Verified`, `Evidence` (PNG paths + one-line readings), `Open issues`, `Escalations`,
`Follow-ups`.
