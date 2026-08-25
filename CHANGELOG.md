# Changelog

This project follows [semantic versioning](https://semver.org/). Before 1.0,
the shared model and the `PATCHBAY_*` declaration syntax may change between
minor versions; the collector contract in
[docs/collectors.md](docs/collectors.md) is the interface most likely to stay
put.

## [Unreleased]

### Added

- **Every page opens with what it answers.** A page header inside the content
  column carries the title and a one-line purpose — the same sentence the
  rail shows on hover, from `NAV` — with the page's controls at the right
  and a breadcrumb on drill-downs. Section headings say what the section
  answers ("Cabled to", "Guests on this host") rather than naming a noun.
- **The Overview leads with exceptions.** Devices not up, links below their
  speed tier, IPAM conflicts, and stale sources come first — or one
  "All clear" line naming what was checked. Guests fold into their
  hypervisor's card; the Links table is gone (the map and each device page
  answer it).
- **The map's state lives in the URL, and `view` is a mode.** Wiring (the new
  default: grey cables, dashed where inferred) · Load · VLAN · Evidence (the
  old reporter colours), plus `vlan=`, `focus=`, `load=now|peak` and the
  visibility toggles — only non-defaults serialised, so a view is a link.
  The map draws its four tiers as labelled bands (Internet / Edge / Fabric /
  Access & compute), owns each node's Y so nodes slide along their lane,
  fits its content to the frame on load and after every change, and centres
  a focused node. Pins are X-only now; previously pinned nodes jump into
  their lane once. All of it works in the offline snapshot.
- **Snapshots page** under Records: what is kept (newest first, per-file
  download), where it is delivered, the schedule, and "snapshot now". The
  `/ops` snapshot buttons are gone; `/ops` is reordered collect-now →
  declarations → what patchbay is running on.
- **Configs opens with "What changed"** — the latest versions across every
  Oxidized node, newest first, each row linking to its diff — above the
  per-device table. Degrades to a reduced page when Oxidized is absent or
  one node's history cannot be read.
- **Deep links.** A VLAN row opens the map in VLAN mode with that VLAN lit; a
  device page has "show on map"; patch-panel rows are anchorable
  (`/patchpanel#p3`). The device page's ports table carries an Endpoints
  column that expands to the MACs learned on that port.
- Process: `docs/process/multi-agent-playbook.md`, agent definitions under
  `.claude/agents/`, briefs and a manual test guide under `docs/process/`,
  ADR-0001 under `docs/adr/`, and `scripts/screenshots.py` — a harness that
  seeds the demo network, serves it, and photographs every page so a UI
  change is looked at rather than inferred.

### Changed

- **Navigation is a rail, not a row of links.** Pages sit in a left rail
  grouped by the question they answer — *Network* (Overview, Topology,
  VLANs, Patch panels) and *Records* (Drift, Configs) — with Ops and
  sign-out in the rail's foot. The current page is lit by one accent edge,
  and drill-downs keep their section lit (a device page under Overview, a
  node's history under Configs). The rail collapses to icons from a toggle
  at its top or foot; the choice is remembered per browser, applied before
  first paint, and forced on viewports too narrow for labels. The groups
  are data (`NAV` in `web.py`), so a new page is one line. The sign-in
  page has no rail: nothing behind it is reachable yet.
- **Typeface.** The UI is set in IBM Plex Sans (variable weight, bundled
  under the SIL OFL — see NOTICE), replacing the system font, so hierarchy
  can lean on weight rather than brightness, which a dark ground has little
  of to spend. Snapshots inline the font as a data URI and read the same
  offline.
- Smaller borrowings from the same style guide, none of which touch the
  palette: a fainter rule between table cells than under the header row, a
  rim-light on cards in place of a shadow that never showed on a dark
  ground, form controls inheriting the page face, and a visible focus ring
  on everything focusable.

### Fixed

- Snapshots are written as UTF-8 with LF line endings regardless of the
  host's locale. On Windows the `≤` in the topology legend made
  `patchbay snapshot` fail outright with a `charmap` encode error.

## [0.2.0] — 2026-08-23

The repository is public as of this release, with a
[live demo](https://dsmorgan.github.io/patchbay/) on GitHub Pages.

### Added

- **`patchbay demo`** writes a complete fictional network to `demo.db` — no
  credentials, no real site: RFC 5737/3849 addresses, locally administered
  MACs, invented names. The seed writes the raw evidence collectors would
  and runs the real normalizer over it, so inference, endpoint placement,
  declared links, and guest-VLAN resolution are exercised rather than
  staged. Deterministic, so screenshots reproduce. Refuses to overwrite a
  database it didn't create unless you pass `--force`.
- A snapshot generated from a demo-seeded model carries a "safe to share"
  banner instead of the treat-as-sensitive one, making it a publishable
  zero-install demo. One such snapshot ships in the repo and serves as
  [the live demo](https://dsmorgan.github.io/patchbay/demo-snapshot.html).

## [0.1.1] — 2026-08-23

Three fixes, all found by deploying 0.1.0 fresh against a real network and
comparing the result to a database that had been running for weeks. Each one
is invisible to a fresh install and only shows up over time, so none was
reachable from the test suite as it stood.

### Fixed

- **The ops page polled without foreign keys enforced.** `PRAGMA foreign_keys`
  is per-connection, and the web app's connection never set it. `/ops` runs a
  full poll and normalize on that connection, so a device retired there left
  its interface rows behind, while the identical poll from `patchbay poll`
  cleaned up correctly — two code paths with different integrity guarantees.
- **phpIPAM never retired subnets it stopped reporting.** The address book got
  a full refresh each poll but subnets were upsert-only, so a subnet deleted in
  phpIPAM stayed on the VLAN pages and in the drift report indefinitely. Now
  pruned, scoped to phpIPAM's own rows and guarded on a non-empty listing.
  VLANs are deliberately left alone: three collectors write that table and the
  `source` column can't say who owns a row.
- **Retired devices left orphaned interfaces.** Normalize now sweeps interface
  rows whose device is gone, which repairs databases already carrying them
  rather than only preventing new ones.

A database carrying all three now converges on exactly what a fresh install
produces, across every table.

## [0.1.0] — 2026-08-23

First tagged release. Everything below is read-only: patchbay never writes to
a network device.

### Views

- **Physical topology.** An interactive map built from LLDP/CDP neighbors,
  switch MAC tables, hypervisor network hints, and operator declarations. Edge
  color names the source that reported a link, and a dashed edge means
  inferred rather than stated. Unmanaged switches are inferred from ports
  carrying many MACs with no LLDP neighbor. Thickness encodes speed, a load
  view recolors by 24-hour utilization, and `PATCHBAY_CAPACITY` renders a
  service rate below the port speed as "10G (3G)".
- **Health dashboard.** Device state, hardware, management IPs, and VM
  placement, with each headline count explaining what it counted.
- **VLAN overlay and IPAM drift.** Highlight a VLAN across the fabric with its
  trunks, access ports, gateway, and subnets; the drift page reports where
  IPAM records and live ARP or lease data disagree.
- **Config history.** One cross-device timeline of config diffs, read from
  Oxidized over its REST API. Config text is parsed in memory and never
  stored.
- **Patch panels.** Panels declared in `PATCHBAY_PANELS` and populated from
  port descriptions.
- **Ops page.** Effective configuration with secrets redacted and each value's
  source, editable operator declarations, stale-declaration reports, and
  triggers for a poll, a LibreNMS rediscovery, or a snapshot.

### Sources

Collectors for LibreNMS, Oxidized, phpIPAM, UniFi, OPNsense, and vSphere. Each
one activates only when its variables are set, and no source is required:
every page degrades to the evidence that exists. Third-party collectors
install as their own packages through the `patchbay.collectors` entry-point
group, with no core changes.

### Snapshots

`patchbay snapshot` writes one self-contained HTML file — interactive map,
every device and port, links, endpoints, traffic graphs, and configs with
secrets redacted — that opens with no network at all. Runs on demand, on a
daily schedule (`PATCHBAY_SNAPSHOT_AT`), or from the ops page, with an
optional off-host copy (`PATCHBAY_SNAPSHOT_DELIVER_DIR`) written under a
temporary name and renamed, so a sync client never reads a partial file.

### Serving

Optional TLS (`PATCHBAY_TLS=direct` picks up renewed certificates without a
restart) or a reverse proxy in front. Optional authentication as a shared
password or OIDC against any provider. The LibreNMS graph proxy keeps the API
token server-side and recolors graphs in transit.

### Known limitations

- Firewall config history is not implemented; OPNsense contributes live state
  only.
- No alerting. LibreNMS handles it for now.
- Config parsers cover the FastIron and Netgear M4300 dialects. Other vendors
  work for topology, load, and status, but their per-port VLAN membership
  falls back to SNMP and declarations.

[0.2.0]: https://github.com/dsmorgan/patchbay/releases/tag/v0.2.0
[0.1.1]: https://github.com/dsmorgan/patchbay/releases/tag/v0.1.1
[0.1.0]: https://github.com/dsmorgan/patchbay/releases/tag/v0.1.0
