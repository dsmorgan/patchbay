# ADR-0001 — Page shell, map modes, and the pages that answer a question

*Status: accepted (2026-08-25) · Milestone: `ui/pages-and-map` · Architect: Opus, for PM fan-out*

## Context

The nav rail (`NAV`/`NAV_ICONS` in `web.py`, rendered by `base.html`) and the page header slot
(`breadcrumb` / `page_title` / `purpose` / `controls`) already shipped. What is left is the work the
rail implies: pages that answer their heading's question, and a map whose *state is addressable*.

Three constraints shape every decision below.

1. **The snapshot reuses the live views** (`snapshot.py` → `build_topology_graph()`,
   `templates/_topomap.html` with `snapshot = true`). No map feature may need a server, and
   `history.replaceState` throws on `file://` in Chrome (opaque origin).
2. **`web.py` is one file** and briefs run in parallel worktrees. Ownership is by *function region*.
3. **Two-repo rule**: examples here use the public demo model (`core1`, `edge1`, `fw1`, `hyp1`).

## Decision 1 (A) — the map's state lives in the URL, and `view` is a mode

`/topology` and the snapshot render the same `_topomap.html`. The route is **unchanged**: the graph
JSON is identical in every mode; the client paints. State table (the source of truth):

| param | values | default | scope |
|---|---|---|---|
| `view` | `wiring` \| `load` \| `vlan` \| `evidence` | `wiring` | how edges are painted |
| `load` | `now` \| `peak` | `now` | which utilization number; preserved across modes |
| `vlan` | a vid present in `graph.vlans` | unset | dims what doesn't carry it — **in every mode** |
| `focus` | a node name | unset | highlights one node (see Decision 2 for centering) |
| `hideoff` | `1` \| `0` | `1` | hide offline & unlinked |
| `coreonly` | `1` \| `0` | `0` | core only |
| `hosts` | `1` \| `0` | `0` | wired hosts |
| `unmhosts` | `1` \| `0` | `0` | behind unmanaged |

- **Only non-defaults are serialized**, in table order, so the plain map is a bare `/topology`.
- Invalid values fall back to the default and the URL is rewritten to the sanitized form on load.
- **Unknown params are preserved verbatim** (a link never decays when passed through the map).
- `history.replaceState` on every change — reload and share work; Back leaves the page rather than
  unwinding toggle flips. A `popstate` listener re-reads and re-renders anyway.
- **Snapshot**: `readState()` reads `location.search`, then `location.hash` if it contains `=`
  (`file:///…/patchbay-latest.html?view=load` works; so does `#view=load`). Writes are suppressed
  when `SNAPSHOT` — the hash belongs to the `#dev-<name>` anchors, and a snapshot's path is not a
  shareable handle. One `if (SNAPSHOT)`, no second implementation.

**Controls.** The four visibility checkboxes stay checkboxes in every mode — they answer *what is on
the map*, which is orthogonal to *how it is painted*. The mode picker is a radio group styled as a
segmented control (`<input type="radio" name="view">` + `<label>`; keyboard-navigable for free).
`now|peak` renders only in `view=load`; the vlan `<select>` renders only in `view=vlan`. State that
is set but whose control is hidden shows as a **chip** in the toolbar (`vlan 20 ×`, `focus core1 ×`)
so it is never invisible; the × clears that param.

**JS state model** (replaces reading `document.getElementById(...).checked` everywhere):

```js
const DEFAULTS = {view:"wiring", load:"now", vlan:null, focus:null,
                  hideoff:true, coreonly:false, hosts:false, unmhosts:false};
const state = readState();            // URL -> object (sanitized)
function set(patch){ Object.assign(state, patch); writeState(); render(); }
function render(){ applyControls(); applyFilters(); applyPaint(); applyVlan();
                   applyLegend(); fit(); }        // order matters: filters -> fit
```

`applyControls()` writes the DOM from `state` at init, which also kills the Firefox
checkbox-restore hazard the old `applyLoadView()`/`applyVlan()` init calls worked around.

**Paint per mode** (`applyPaint`):

| mode | stroke | dash |
|---|---|---|
| `evidence` | `null` (CSS class = reporter) | `null` (CSS) |
| `wiring` | cables → `var(--muted)`; **non-cables keep their CSS colour** (`virtualized`, `oob`, `wan`) | inferred cables (`fdbuplink`, `fdbinference`, `fdbhost`) → `"7 3"`; other cables → `"none"`; non-cables → `null` |
| `load` | `heat(util)` (unchanged, incl. `--line` for "no measurement") | `"none"` |
| `vlan` | as `wiring` | as `wiring` |

`vlan` dimming composes with any mode (today's `syncEdgeOpacity` behaviour is kept: the VLAN dim
always wins over the mode's opacity).

**Legend.** Every `.item` carries `data-modes="wiring load vlan evidence"` (a subset). `applyLegend()`
shows an item iff `data-modes` contains `state.view`; `#leg-vlan` additionally requires
`state.vlan != null`, `#leg-peak` requires `state.load === "peak"`. Assignments:

- thickness, ports key, nodes key, hint → all modes.
- **new** "confirmed cable" (solid grey) and "inferred cable" (dashed grey) → `wiring`.
- LLDP/CDP, switch MAC-table, hypervisor-reported, declared, unmanaged feed → `evidence`.
- VM on host, WAN, BMC/out-of-band → `wiring vlan evidence` (structural, still coloured there).
- heat scale → `load`; vlan note → any mode with `vlan` set.

## Decision 2 (B) — drawn tiers, Y owned by the tier, fit-to-view

`RANK` becomes four **labelled swimlanes** drawn behind the nodes. Names and bands (fractions of the
layout height, contiguous, so bands scale with `#topo`'s 72vh):

| rank | name | band | tooltip |
|---|---|---|---|
| −1 | **Internet** | 0.00–0.16 | what the site connects out through |
| 0 | **Edge** | 0.16–0.38 | the firewall/router between inside and out |
| 1 | **Fabric** | 0.38–0.66 | the switches that carry everything else |
| 2 | **Access & compute** | 0.66–1.00 | what plugs in: hypervisors, APs, unmanaged switches, wired hosts |

- Node `fy = bandCenter(rank)` for **every** node, always. `forceY` is deleted; the simulation now
  solves X only (`forceX` slots, `forceCollide`, `forceLink` all keep working — their Y components
  are discarded by the fixed `fy`). Drag sets `d.fx` only: nodes slide along their lane.
- **Pins are X-only.** The `positions` table is unchanged (`y NOT NULL`); the client keeps POSTing
  `{name, x, y}` with the node's current band Y, and **ignores `py` on load**. Already-saved rows keep
  their X (still meaningful — order within a lane); their Y is superseded by the band. Consequence to
  expect on the first load after this ships: pinned nodes jump vertically into their lane. No migration.
- Bands render only for ranks with ≥1 *visible* node; band width is recomputed from the visible
  nodes' X extent (+400 px margin) at each settle, label at the left inside edge.
- **Layer order inside the zoom group `g`**: `gBands`, `gLinks`, `gLabels`, `gNodes`. Fit measures
  `gContent` (links+labels+nodes) — **not** `g` — or the bands' margin inflates the bbox.

```js
const zoom = d3.zoom().scaleExtent([0.2, 3]).on("zoom", e => g.attr("transform", e.transform));
function fit(animate){                        // pad 28, clamp k to scaleExtent
  const b = gContent.node().getBBox(); if (!b.width) return;
  const k = Math.min(3, Math.max(0.2, Math.min((W-56)/b.width, (H-56)/b.height)));
  const t = d3.zoomIdentity.translate((W-k*(2*b.x+b.width))/2, (H-k*(2*b.y+b.height))/2).scale(k);
  (animate ? svg.transition().duration(400) : svg).call(zoom.transform, t);
}
function focusNode(name)   // centre that node at scale max(k, 0.9) + a .focused halo
```

`scaleExtent`'s lower bound drops 0.4 → 0.2 or a large map cannot shrink to fit.

Fit runs: on `sim.on("end")`, after every `applyFilters()` (which restarts the sim), and on a
debounced `ResizeObserver` for `#topo` (which also recomputes band centres and every `fy`). A **`fit`
button lives in the toolbar**, not in `{% block controls %}` — the snapshot has no controls block.
`reset layout` stays where it is (live page only).

## Decision 3 (C) — Overview leads with exceptions

`/` route context changes: **add** `exceptions`, `checked`, `vms_by_host`, `orphan_vms`;
**remove** `links` and the flat `vms` list. Page order: exceptions strip → stat grid + gateways →
Fabric → Access points → Hypervisors (guests folded in). The **Links table is deleted** (the map
answers it, and `/device/<name>` keeps its own).

```python
STALE_MIN = 15          # module-level; same rule the top bar uses

def speed_tier(bps: int | None) -> str      # "" | "slow" (<=100M) | "vslow" (<=10M)
# extracted from build_topology_graph.edge_speed, which now calls it. The ONE shared piece.

def drift_report(conn, settings) -> dict    # extracted from drift(); returns exactly the dict
# /drift renders today ("undocumented","external","unseen","n_dhcp_quiet","conflicts",
# "in_sync","have_ipam"). /drift renders drift_report(...); / uses len(r["conflicts"]) only.

def _exceptions(conn, settings) -> tuple[list[dict], list[str]]
exception = {"kind": "device-down" | "slow-link" | "drift" | "stale-source",
             "severity": "crit" | "warn" | "info",
             "title": "2 devices are not up",     # says what it counted
             "href": "/drift" | None,             # where the whole answer lives
             "items": [{"label": "ap2", "detail": "down · ap", "href": "/device/ap2"}]}
# checked: ["every device up", "no slow links", "IPAM in sync", "every source fresh"] —
# only the checks that could actually run; the all-clear card renders ", ".join(checked).
```

Rules, so the strip never cries wolf:

- **device-down**: `devices.role in TOPO_ROLES` and `status in ('down','notResponding')` → crit;
  `disabled` → info. `unknown`/NULL is *no opinion*, never an exception. Guests (`role='vm'`) are
  reported on their hypervisor's card, not here.
- **slow-link**: for each row in `links`, the better-known end's `speed_bps` through `speed_tier()`;
  `vslow` → crit, `slow` → warn. A link with **no** known speed is not slow (same rule as the map).
  One small SELECT of its own — the map's `speed_of` pass is not refactored for this.
- **drift**: `len(drift_report(...)["conflicts"])`, and only when `have_ipam`.
- **stale-source**: any `_age(conn)` value `> STALE_MIN`, one exception naming the sources.

VMs fold into their hypervisor card as `<details>` (`<summary>` is the existing "N/M VMs running"
line) with today's VM table columns minus *Host*. `vms_by_host: dict[str, list[Row]]` keyed by
`devices.parent`; `orphan_vms` (parent not a hypervisor on this page) keeps its own small section so
nothing disappears.

## Decision 4 (D) — `/snapshots` under Records; `/ops` loses its snapshot buttons

- `GET /snapshots` → `templates/snapshots.html`. Context:
  `snapshots: list[{name, when, ts, size_mb, href}]` (newest first, `when`/`ts` parsed from the
  filename, **no timezone claimed**), `latest: {"exists": bool, "href": "/snapshots/patchbay-latest.html"}`,
  `settings_view: {"dir", "deliver_dir", "at", "keep"}`, `is_demo: bool`.
- `GET /snapshots/{name}` serves the file: `re.fullmatch(r"patchbay-(?:\d{8}-\d{6}|latest)\.html", name)`
  **and** `(dir/name).resolve().parent == Path(dir).resolve()`, else 404. The pattern admits no
  separators, so `%2F`-smuggled traversal cannot match. `Content-Disposition: attachment`.
  `GET /ops/snapshot/latest` **stays** (tested, possibly proxied/bookmarked); only its UI link moves.
- "Snapshot now" reuses `POST /ops/snapshot` unchanged. The act-button CSS+JS moves out of
  `ops.html` into `templates/_actions.html`, included by both pages.
- Per-file "is this a demo snapshot?" is **not** detected — the marker sits ~120 KB into a 4 MB file.
  Instead: when the current model is demo-seeded (`db.get_state(conn,'demo_seed') == '1'`) the page
  says so once. When `deliver_dir` is unset, a muted caution: *this host is the only copy, and a
  snapshot is for when this host is down.*
- **`/ops` after**: `Collect now` (prose, buttons, output, last-poll details) → `Declarations`
  (**with the `conflicts` and `warnings` panels moved into it** — both are about declarations) →
  `Effective configuration` (the read-only table, last). Removed: the `snapshot now` button, the
  `download the latest snapshot` link, and `has_snapshot` from the `/ops` context.

## Decision 5 (E) — `/configs` is a cross-device "what changed"

```python
OX_TIMELINE_LIMIT, OX_TIMELINE_PER_NODE, OX_TIMELINE_BUDGET = 50, 20, 8.0   # entries, per node, seconds
def _ox_ts(date: str) -> float | None        # "%Y-%m-%d %H:%M:%S %z" then ISO; None if unparseable
def _ox_timeline(client, nodes) -> tuple[list[dict], list[str]]   # (entries, problem node names)
entry = {"node": "core1", "oid": "…", "prev": "…"|None, "date": "…", "ts": 1.77e9|None,
         "message": "…", "author": "…", "href": "/configs/core1?v=<oid>&prev=<prev>"}
```

One `GET /node/version.json?node_full=<node>` per node (exactly what `config_node` already calls),
newest first by `(ts or 0)` descending, unparseable dates last. Stop early when `OX_TIMELINE_BUDGET`
elapses (set `truncated`). Page: **"What changed"** table (when / device / message / diff) then
**"Devices and their last backup"** (today's node table). Context adds `entries`, `problems`,
`truncated`.

Degradation (all must render a reduced page, never an error):
`oxidized_url` unset → `error="unconfigured"` (today's prose); unreachable → `error=<str>`;
one node's `version.json` failing → that node in `problems`, rendered as a muted footnote.

Tested with `monkeypatch.setattr(web, "_ox_client", lambda s: httpx.Client(
transport=httpx.MockTransport(handler), base_url="http://ox.invalid"))` — no new dependency.

## Decision 6 (F) — deep links: what is in scope

**In:** `/topology?view=vlan&vlan=<vid>` from each VLANs row · `/topology?focus=<device>` from a
device page ("Show on map") and from Overview exception items · `/patchpanel?panel=<n>#p<i>` —
patch-panel rows get `id="p<i>"` and a `:target` highlight, so the anchor exists.

**Out (follow-ups):** the map's `[n]` tag linking to its panel (the map does not know *which*
declared panel matched — `build_topology_graph` would have to record the panel name); HTML edge
tooltips that can hold links (SVG `<title>` cannot); a device → `/configs/<node>` link (the device
route has no `oxidized_url` in context).

Convention for every page that links to the map: **build the URL from Decision 1's table, defaults
omitted.** A VLAN row links `?view=vlan&vlan=20`, never `?view=vlan&vlan=20&hideoff=1`.

## Decision 7 (G) — the device page folds endpoints into ports

Route context: `endpoints_by_port: dict[str, list[Row]]` keyed by the port name **as it appears in
`ports`** (match exact, then `removeprefix("ethernet")`, server-side so it is testable) and
`endpoints_other: list[Row]` for everything else (AP clients keyed by SSID, MAC-only rows).

The ports table gains an **Endpoints** column showing the count; clicking it toggles a
`<tr class="eprow">` with today's endpoint columns — the same mechanism as the existing `.graphrow`,
not a `<details>` in a narrow cell. The soft-refresh guard must also pause on `.eprow`
(`!document.querySelector(".graphrow, .eprow")`). "Endpoints seen here" survives only as
**"Endpoints not tied to a port (N)"**, rendered when `endpoints_other` is non-empty.

## Consequences

- The map's whole state is client-side: **A, B and F need no route change**, and a shared link
  reproduces exactly what the sender saw.
- Load and VLAN no longer compose *by checkbox* but still compose by URL (`?view=load&vlan=20`);
  the chip keeps that visible. No capability is lost.
- `/` now runs the drift computation on every load (endpoints × IPAM in Python). Acceptable at
  homelab scale; if a large site measures slow, add a counts-only path to `drift_report` — measured,
  not assumed.
- `test_empty_sections_are_hidden` asserts `<h2>Links</h2>`; brief 3 must update it.
- `docs/demo-snapshot.html` and `docs/index.html` are regenerated by the PM after the map briefs land.

## Brief split

Seed first (PM, before fan-out — small edits to PM-owned regions):
**`NAV` gains** `("/snapshots", "Snapshots", "snapshots", "Break-glass copies of the whole picture — what is kept, and where it is delivered")`
under *Records*; **`NAV_ICONS["snapshots"]** = `"M2 4.5h14v3H2z M3.5 7.5h11V15h-11z M7 10.5h4"`;
`tests/test_web.py::PAGES` gains `/snapshots`. Every brief appends its tests at the **end** of
`tests/test_web.py` under a `# --- <slug> ---` banner (the one shared file; conflicts stay mechanical).

| # | slug | tier | scope | depends on | parallel? |
|---|---|---|---|---|---|
| 1 | `map-url-modes` | sonnet | `templates/_topomap.html` | seed | yes |
| 2 | `map-tiers-fit` | sonnet | `templates/_topomap.html` | **brief 1 merged** | no — same file |
| 3 | `overview-exceptions` | sonnet | `templates/dashboard.html`; `web.py`: `dashboard()`, new `_exceptions()`/`STALE_MIN`, `speed_tier()` out of `edge_speed`, `drift_report()` out of `drift()` | seed | yes |
| 4 | `snapshots-page` | sonnet | `templates/snapshots.html` (new), `templates/_actions.html` (new), `templates/ops.html`; `web.py`: `ops()`, new `snapshots()`/`snapshot_file()` beside `ops_snapshot_latest` | seed | yes |
| 5 | `configs-timeline` | sonnet | `templates/configs.html`; `web.py`: `configs()` + `_ox_ts`/`_ox_timeline` beside `_ox_nodes` | seed | yes |
| 6 | `deep-links-and-device-endpoints` | sonnet | `templates/vlans.html`, `templates/device.html`, `templates/patchpanel.html`; `web.py`: `device()` only | briefs 1+3 for the link targets to *work* (the hrefs are correct regardless) | yes |

**web.py adjacency** (all disjoint regions; merge in order 3 → 4 → 5 → 6 and re-run the gate):
3 owns `dashboard`/`drift`/`edge_speed`; 4 owns `ops` + the snapshot routes; 5 owns `configs`/`_ox_*`;
6 owns `device`. Only brief 3 edits `build_topology_graph` (the two-line `speed_tier` extraction).

### Acceptance tests, by brief

1. `test_topology_page_renders_mode_control` (four radios + `data-modes` legend items present);
   screenshots `/topology`, `?view=load&load=peak`, `?view=vlan&vlan=20`, `?view=evidence` —
   **read the PNGs**: mode lit, legend matches the mode, no stale heat swatches in wiring.
2. `test_topology_page_renders_tier_bands` (four band labels in the markup); screenshots at 1400 and
   600 px: bands labelled, whole graph inside the frame, nothing clipped; manual: drag a node (moves
   in X only), reload (X kept, Y in-lane), `reset layout`.
3. `test_overview_shows_exceptions_when_something_is_wrong`,
   `test_overview_says_all_clear_when_nothing_is`, `test_overview_folds_vms_into_hypervisors`,
   `test_overview_drops_the_links_table`; update `test_empty_sections_are_hidden`.
4. `test_snapshots_page_lists_newest_first`, `test_snapshot_download_serves_only_the_pattern`
   (`/snapshots/..%2F..%2Fpatchbay.db`, `/snapshots/other.html` → 404),
   `test_snapshots_page_empty_state`, `test_ops_no_longer_offers_snapshot_buttons`
   (and `test_ops_snapshot_download_404s_before_first_snapshot` still green).
5. `test_configs_timeline_lists_newest_first`, `test_configs_timeline_survives_one_bad_node`,
   `test_configs_timeline_unreachable_is_a_state_not_a_crash`, existing
   `test_configs_page_degrades_without_oxidized` unchanged.
6. `test_device_page_folds_endpoints_into_ports`, `test_vlans_row_links_to_the_map`,
   `test_patchpanel_rows_are_anchored`.

Every brief also runs the whole `pytest -q` suite and
`python scripts/screenshots.py --out artifacts/shots/<slug>` for its pages, and states in its report
what the PNGs show.
