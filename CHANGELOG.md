# Changelog

This project follows [semantic versioning](https://semver.org/). Before 1.0,
the shared model and the `PATCHBAY_*` declaration syntax may change between
minor versions; the collector contract in
[docs/collectors.md](docs/collectors.md) is the interface most likely to stay
put.

## [0.6.1] — 2026-08-27

### Fixed

- **pfSense live status actually joins.** pfrest v2's status endpoint names
  the physical interface `hwif`, not `if`, so 0.6.0's collector never
  matched live status — oper state, speed, and MAC silently stayed empty.
  Found and fixed by [@slmingol](https://github.com/slmingol) running it on
  real hardware ([#15](https://github.com/dsmorgan/patchbay/pull/15)); the
  fixture test now uses the real `hwif` shape so the join is locked in.
  The same PR fills the overview card's management address from the
  configured host on both firewall collectors (a polled IP from LibreNMS
  still wins the merge) and propagates IPv6 addresses on pfSense
  interfaces.

## [0.6.0] — 2026-08-27

### Added

- **pfSense collector.** Interfaces (config merged with live status: MAC,
  oper state, speed parsed from the media string, DHCP/PPPoE addresses
  resolved), gateway health (VPN gateways filtered out), VLAN
  sub-interfaces onto the 802.1Q column, and DHCP static mappings as
  endpoints. Needs the [pfrest](https://github.com/pfrest/pfsense-restapi)
  REST API package; one key in `PFSENSE_API_KEY`, sent as `x-api-key`.
  From [@slmingol](https://github.com/slmingol)
  ([#9](https://github.com/dsmorgan/patchbay/pull/9)).

### Changed

- **The Overview's attention strip is now one quiet list below the counts.**
  Running 0.5.0 on a real network showed the pre-categorized cards crying
  wolf: the device-down count duplicated the cards right below it (which
  are the device-state UI, and stay so), a legitimately-100M link was
  flagged forever, and IPAM drift demanded top billing it didn't deserve.
  Now the page opens with what the network *is*; attention items follow as
  one flat ordered list, each line linking to the page that owns the
  answer, and `PATCHBAY_EXPECT` declares a port or device expected so its
  items stay silent. The honest all-clear line stays — it can only claim
  the checks that actually ran.
- **The map arranges freely by default.** The tier bands didn't survive
  contact with a real network: a pseudo-physical map wants the operator's
  arrangement to win. `layout=free` (the new default) restores the soft
  rank force and full two-axis pinning; `layout=tiers` keeps the banded
  layout as an opt-in, from the URL or the "tier lanes" toolbar checkbox.
  Pins saved before 0.5.0 load exactly again in free mode.

### Fixed

- **Config version views no longer double-space.** oxidized-web's
  list-shaped response carries each line's own newline, and joining with
  another one doubled every blank line; CRLF bodies doubled the same way.
- **Interface MACs read `28:80:88:73:42:54`, not `288088734256`.**
  LibreNMS emits `ifPhysAddress` as bare hex and it was stored verbatim
  since 0.1.0 — visible once the endpoints-per-port view put both formats
  side by side. Formatted at ingestion; existing rows self-heal on the
  next poll.

## [0.5.0] — 2026-08-27

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
- `scripts/screenshots.py` seeds the demo network, serves it, and photographs
  every page so a UI change is looked at rather than inferred; ADR-0001 under
  `docs/adr/` records this milestone's decisions.

Everything above is [@Moonchopper](https://github.com/Moonchopper)
([#4](https://github.com/dsmorgan/patchbay/pull/4)).

### Fixed

- **Pages refresh when a poll lands, not on a blind timer.** The old
  60-second meta refresh reloaded into identical data whether or not the
  tab was visible, and the pages that opted out to protect on-screen state
  (topology, ops, configs) never refreshed at all — freezing their own
  "last polled" header. One shared script now asks `/api/freshness` while
  the tab is visible, reloads only when a poll actually finished, catches
  a returning tab up immediately, and defers while the page is busy: an
  open graph or endpoint row, action output on screen, a config diff, a
  map the user has touched, or a focused form control. The header age
  ticks client-side between reloads, so it stays honest everywhere.
- **Port-graph series are named in readable HTML.** rrdtool draws its
  legend as tiny vector paths, illegible after scaling — on the errors
  graph, which color is "Discards In" was a guess, and `58.18m` reads as
  mega until you squint (it's milli: fractions of a packet per second).
  Each port graph now carries a caption naming every series with the exact
  colors the image uses — verified against live LibreNMS output — and
  spelling out the unit suffixes.
- **`OPNSENSE_HOST` accepts a scheme.** A full URL (`http://fw1.example.net`)
  reaches an OPNsense that doesn't terminate TLS on its management
  interface; a bare hostname still defaults to HTTPS. Previously the
  collector hardcoded `https://` and a plain-HTTP firewall surfaced as a
  misleading connect timeout. From [@slmingol](https://github.com/slmingol)
  ([#8](https://github.com/dsmorgan/patchbay/pull/8)), along with the
  OPNsense API-privilege table in
  [docs/configuration.md](docs/configuration.md) and a host-networking
  alternative in the example compose file.

## [0.4.0] — 2026-08-26

### Added

- **A from-nothing deployment path.** `docker-compose.stack.yml` runs the
  whole stack — MariaDB, Redis, LibreNMS and its dispatcher, Oxidized, and
  patchbay — and [docs/deployment.md](docs/deployment.md) walks the wiring:
  Oxidized's two config files, LibreNMS setup and the API token, and the
  container-name URLs (with the Connection-refused trap explained). The
  existing quick start remains the path for networks that already run the
  data layer.

### Fixed

- **Images are now multi-arch (amd64 + arm64).** On an Apple Silicon Mac or
  a Raspberry Pi, the quick start previously failed outright with "no
  matching manifest for linux/arm64" — found by running the deployment
  guide verbatim on an arm64 host.
- **First poll no longer races the web app on a fresh database.** `compose
  up` starts both containers at once and both ran the same schema
  migrations; real filesystems serialize that on SQLite's lock, but Docker
  Desktop's shared mounts could surface it as a spurious "disk I/O error"
  on the very first poll. The poller now waits a beat and retries once.

### Changed

- **A tagged release identifies itself by its tag alone.** The header build
  stamp on a release image reads `0.3.1`, not `0.3.1+<sha>` — the tag
  already names one exact commit, so the sha added nothing. Untagged builds
  keep the full stamp (`0.3.1+abc1234`), and a checkout with uncommitted
  changes appends `-dirty`, so the only builds carrying extra marks are the
  ones where the version alone is ambiguous.

## [0.3.0] — 2026-08-25

The first release with outside contributions: the nav rail and typeface are
from [@Moonchopper](https://github.com/Moonchopper)
([#1](https://github.com/dsmorgan/patchbay/pull/1)), the container pipeline
from [@slmingol](https://github.com/slmingol)
([#2](https://github.com/dsmorgan/patchbay/pull/2)).

### Added

- **Pre-built images on GHCR.** Every push to `main` and every `v*` tag
  builds and publishes `ghcr.io/dsmorgan/patchbay`; releases carry
  `latest`, `X.Y.Z`, `X.Y`, and short-SHA tags, and the SHA is stamped
  into the UI header as the build. The example compose file now defaults
  to the pre-built image (`build:` kept as a commented alternative), so
  the quick start needs no clone — fetch two files and `docker compose up`.
  The example maps the UI to host port 8013.

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

[0.6.1]: https://github.com/dsmorgan/patchbay/releases/tag/v0.6.1
[0.6.0]: https://github.com/dsmorgan/patchbay/releases/tag/v0.6.0
[0.5.0]: https://github.com/dsmorgan/patchbay/releases/tag/v0.5.0
[0.4.0]: https://github.com/dsmorgan/patchbay/releases/tag/v0.4.0
[0.3.0]: https://github.com/dsmorgan/patchbay/releases/tag/v0.3.0
[0.2.0]: https://github.com/dsmorgan/patchbay/releases/tag/v0.2.0
[0.1.1]: https://github.com/dsmorgan/patchbay/releases/tag/v0.1.1
[0.1.0]: https://github.com/dsmorgan/patchbay/releases/tag/v0.1.0
