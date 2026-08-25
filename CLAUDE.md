# CLAUDE.md — patchbay

Working notes for AI-assisted development. Read `docs/architecture.md` (spec + roadmap)
and `docs/pluggability.md` (component model) first.

## The two-repo split (absolute rule)

This repo is public and site-agnostic. Everything describing the real network —
hostnames, IPs, credentials, configs, DB files, fixtures — lives in the private
site repo at `../patchbay-site` (never committed here). `.env.example` documents
every knob; the real `.env` is in patchbay-site. Before committing, check nothing
site-specific leaked into code, tests, or docs.

## Dev workflow

- Editable install in `.venv`; use `uv pip install -e '.[web]' --python .venv/bin/python`
  (uv, not pip — the venv may not have pip bootstrapped).
- Run the live instance:
  `PATCHBAY_ENV=../patchbay-site/.env PATCHBAY_DB=../patchbay-site/data/patchbay.db .venv/bin/patchbay web --host 127.0.0.1 --port 8080`
- Templates hot-reload; **Python changes need a server restart**. dotenv only fills
  *missing* env vars, so changed `.env` values also need a process cycle.
- A launchd job (`com.patchbay.poll`, 300 s) can poll on the dev Mac; log at
  `/tmp/patchbay-poll.log` (wiped on macOS reboot). It is currently **unloaded**
  — the container deployment owns polling. Reload with
  `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.patchbay.poll.plist`,
  and unload with `launchctl bootout gui/$UID/com.patchbay.poll`.
- Quick verification without the server: FastAPI `TestClient` against the real site DB.
- `pytest` (install `.[dev]`): 98 tests, no network, hermetic env via the
  `clean_env` fixture. Every normalizer bug family has a regression test —
  add one when fixing anything there.
- Commits go straight to `main` (homelab repo, no PR flow). Small, single-topic commits.

## Architecture invariants

- **Collectors are plugins** (`src/patchbay/collectors/`, registered via `register()`;
  third-party ones via the `patchbay.collectors` entry-point group —
  docs/collectors.md is the contract). They write raw observations; they never
  resolve conflicts — that's `normalize.py`'s job. `collect()` runs in a
  caller-managed transaction: raise on failure, never `conn.commit()`.
- **Evidence ages out**: non-declared links and inferred switches expire after
  `EVIDENCE_TTL` (2h) without a refresh; live evidence is re-upserted every
  poll/normalize. Declared rows live and die by the .env/DB declaration only.
- **Identity vs liveness**: names, MACs, and addresses are stable facts worth
  recording even from a stale/disconnected source (vCenter caches a
  notResponding host's config); speed and oper-status are liveness and must be
  omitted rather than written stale — omitted means "no opinion" in
  `upsert_interface`, so the last good value stands.
- **No cable ends on a kernel port**: `port_kind` returns `kernel` for vmk*/
  vswif*/VM adapters. Their MACs identify *which host* a switch port leads to
  (that's how a hypervisor uplink is found when CDP/LLDP is silent), but the
  physical end is the pnic, so such link ends are demoted to `?`. When several
  interfaces share a MAC, the physical one wins.
- **Auth is a gate, not an identity system** (`auth.py`): HMAC-signed cookie,
  secret persisted in `app_state`; middleware also rejects cross-origin POSTs
  in every mode. Declarations are editable on /ops with env-wins-else-DB
  precedence (`DECLARATION_VARS` in config.py) — the UI never writes the env file.
- **Each collector owns its rows** and must both prune what its source stops
  reporting (see vsphere VM retirement) *and* retract what a better source
  supersedes — a row that was right when written goes wrong the moment another
  collector is configured (vsphere's `Network adapter N` vs the firewall's
  `vmx0`). Upsert-only stores rot: nothing lies, things just never leave. The
  add-an-integration-later path is the only way to catch this; CONTRIBUTING.md
  has the method.
- **Merge rules in `normalize._merge_group`** encode "fresh beats stale, real beats
  placeholder": junk values (`generic`, `amd64`, …) count as absent; versioned OS
  beats a bare fingerprint; status comes from the row with the freshest `last_seen`;
  the interface fold list must name every column (forgetting one silently drops data —
  this bit us with `ip6`).
- **Some ports lie by design.** A mirror/SPAN destination (`port_roles.role =
  'monitor-dst'`, parsed from the running-config) transmits copies of other
  ports' traffic: its MAC table describes the rest of the fabric, so it's
  excluded from FDB inference or it grows a phantom switch. Nothing can
  discover its far end — a declaration is the only possible answer, and the
  map labels the port so that reads as intended rather than missing.
- **A guest can't see its own VLAN tag**: the vSwitch adds and strips it, so a
  firewall VM reports no 802.1Q. `vnic_vlans` (hypervisor port group VLAN, keyed
  by NIC MAC) plus `normalize._apply_vnic_vlans` resolves that onto real
  interfaces; MAC is the join between vCenter's "Network adapter 1" and the
  firewall's `vmx1`. Config-parsed membership always wins; this fills silence.
- **Link evidence model**: `source` on a link records *who reported it* — `lldp` >
  `vsphere-hint` > `fdb-uplink` supersede per port (one cable per port); `declared`
  comes from `PATCHBAY_LINKS` and is pruned when undeclared. The topology legend
  maps color = reporter, dash = stated vs inferred; the load view strips both.
  A flood guard drops discovery neighbors on ports that already have a discovery
  link to a managed switch (M4300 ISDP floods received CDP).
- **Graph proxy** (`/graph` in `web.py`): LibreNMS API token stays server-side;
  SVGs are recolored in transit (`GRAPH_RECOLOR`) because LibreNMS ignores theme
  params for series/grid; health graphs are gated on the sensor endpoints since
  LibreNMS happily renders empty axes.

- **Snapshots** (`snapshot.py`): reuse the live UI's `build_topology_graph()`
  and `fetch_graph_image()` — never reimplement a view for the snapshot, or
  the two drift. The map lives in `templates/_topomap.html`, shared by both;
  set `snapshot = true` before including it. `font_url` is the one style knob
  the snapshot overrides (the bundled typeface as a data URI, like d3).
- **Navigation is data**: `NAV` and `NAV_ICONS` in `web.py`; the rail in
  `base.html` renders from them. Groups are named for the question a page
  answers (Network: what's out there and how it's wired; Records: what the
  documentation says and whether the network agrees), never for the mechanism
  — a page that fits neither is the case for a third heading, not a longer
  list. Ops and sign-out are utilities and live in the rail's foot. The lit
  entry comes from the path prefix, so drill-downs keep their section; a page
  that sets `bare = true` (login) gets no rail and no shell grid.

## Gotchas

- 32-bit ifSpeed wrap: 10G shows up as 1410065408 (or 1410000000) — `WRAPPED_SPEED`
  in the librenms collector unwraps known tiers.
- M4300 config parser: bare `switchport mode trunk` means *all* existing VLANs tagged,
  native 1.
- LLDP port-ids that are MAC strings are normalized to `?` at ingestion, or they
  block link fusion.
- ESXi standard vSwitches speak CDP only (LLDP needs a dvSwitch); CDP advertisements
  source from the vmnic's own MAC.
- On this dev Mac, piped `grep` has been unreliable on unicode content — verify text
  transformations with python, not grep.
- **Inline SVG needs `/>` intact.** The HTML parser only self-closes a tag in
  foreign content when the slash and bracket are adjacent, so the whitespace-eating
  `.../\n  ><g ...` style leaves the element *open* and nests the rest of the
  drawing inside it, where it renders nothing. Cost an hour on the header mark;
  the page still returns 200, so only a screenshot catches it.
- Verify UI changes by screenshotting the running server, not by reading markup:
  `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless
  --disable-gpu --force-device-scale-factor=2 --screenshot=x.png
  --window-size=1200,800 http://127.0.0.1:8080/<page>`; a 600px-wide window
  exercises the collapsed rail.
- Python changes break a running dev server's *templates* before its code:
  templates hot-reload and immediately reference whatever new global `web.py`
  registers, so the old process 500s on every page until restarted.
