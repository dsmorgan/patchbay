# Changelog

This project follows [semantic versioning](https://semver.org/). Before 1.0,
the shared model and the `PATCHBAY_*` declaration syntax may change between
minor versions; the collector contract in
[docs/collectors.md](docs/collectors.md) is the interface most likely to stay
put.

## [Unreleased]

### Fixed
- A switch-to-switch cable that both LibreNMS (LLDP) and the UniFi
  controller report drew twice when the controller stored the operator's
  port label where SNMP reported the ifName. The normalizer now also
  matches unifi links to lldp links by device pair, keeping genuine
  parallel cables (#56, Sam). The pair pass runs after the weaker-source
  passes, so a retired unifi row still claims its port against MAC-table
  inference and ghost switches.

## [0.17.0] — 2026-09-24

### Added

- **Topology: undo and group moves** (#55, from Sam / @slmingol). Ctrl+Z
  or Cmd+Z steps back up to ten node moves. Shift+drag on the background
  draws a lasso; dragging any selected node moves the group together,
  spacing kept, and one undo restores the whole group. Escape or a
  shift-click on the background clears the selection. Landed with two
  fixes: undo redraws the node even after the simulation has cooled, and
  it restores the node's saved state instead of turning a settled node's
  hold into a pin.

## [0.16.0] — 2026-09-24

### Added

- **Load view: UniFi switch ports carry throughput** (#53, from Sam /
  @slmingol). The controller reports a rolling byte rate per switch port,
  the same figure its own UI shows, and the collector now stores it as the
  port's in/out rate and as a `rate_history` sample. Switch-to-switch and
  switch-to-AP legs on UniFi-managed switches color in the load view and
  get a 24-hour peak, where before only SNMP-polled ports did. An AP's
  uplink carries the same figures and is stored too. A down device keeps
  its last good reading, as it does for port status and speed.

- **Load view: the busier end of a cable wins.** Where both ends of a
  link report a rate, the edge used to show whichever end sorted first by
  name. The two ends read the same wire through different windows, a
  UniFi rolling rate against a five-minute SNMP average, so the edge now
  shows the higher reading, for the current figure and the 24-hour peak
  separately. A burst one poller caught no longer hides behind the
  other's average.

### Changed

- **Deployment guide: verify snapshot delivery before you need it** (#25).
  A new step walks through mounting the off-host share into both patchbay
  services, triggering one snapshot, checking the share from another
  machine for the renamed copy and no leftover `.part` file, waiting for
  the sync, and opening the off-host copy with the stack down.

### Fixed

- **Rate samples age out whatever the source.** The seven-day
  `rate_history` prune lived in the LibreNMS collector, so a site whose
  rates came from another source would have kept every sample forever.
  The normalizer's housekeeping pass prunes now, every cycle, beside the
  raw-payload expiry.

- **The test suite passes under bare `pytest`** (#54, from Sam / @slmingol).
  Two test modules import helpers from a sibling through the `tests`
  package, which resolves only with the repository root on the path.
  `python -m pytest` puts the working directory there and plain `pytest`
  does not, so the same tree passed on one machine and failed eight tests
  on another. The pytest config adds the root now.

## [0.15.0] — 2026-09-23

### Changed

- **Agent instructions live in `AGENTS.md`**, the convention shared across
  AI coding tools, instead of `CLAUDE.md`. `CLAUDE.md` and
  `CLAUDE.local.md` are gitignored for personal notes (Sam's suggestion
  in #51). Claude Code 2.1.277 or later reads `AGENTS.md` natively.

### Fixed

- **Topology: a filter toggle no longer re-flows the map** (#52, from
  Sam / @slmingol). Every toggle restarts the simulation, and on a map
  without saved positions that moved every node the operator hadn't
  dragged — by a hundred pixels or more for `core only`. Now the first
  settle is the arrangement: the nodes that took part in it are held where
  they landed, and a node a toggle reveals later flows into the gaps
  between them. Drag still pins and saves, shift-click frees, reload
  re-settles. A map whose nodes all carry saved positions is unchanged.

- **pfSense: a 404 note says which kind of absence it is** (#51, from
  Sam / @slmingol). The OpenVPN and IPsec status endpoints also answer 404
  when that feature is not configured on the firewall, pfrest installed
  and current, and the old note sent operators after the package. Those
  two calls are optional now and their note names the feature; a core
  endpoint's 404 still means pfrest is missing or too old, and says so.

## [0.14.0] — 2026-09-17

### Added

- **Routed view: every router draws** (#50, ADR-0002 Decision 4). The
  routing tier holds them all: routers whose lanes don't overlap share a
  column, overlapping ones — an HA pair, a core router behind the edge
  firewall — take successive columns toward the internet, and whoever
  holds a default route stands last beside its cloud (a multi-egress site
  gets a cloud per default route). Router boxes are open frames now, so a
  lane bound for a further router visibly passes the nearer one; a lane
  lists every router that claims it (all gateways in the tooltip); a
  tunnel leaves the router that terminates it. A plain router wears the
  router glyph and color, a firewall the firewall's, as on the Overview.
  Scenario tests cover the HA pair, the inner router, and two egress
  routers — no live site yet exercises them, so they are the contract.

- **Routed view: Load mode** (#50, ADR-0002 Decision 3). The router's
  per-network legs are the only edges on this view with counters — its
  VLAN interfaces, when the firewall is a polled device — so Load
  heat-tints a segment where each lane meets the router, the number
  beside it, and the default route's drop from the WAN interface, on the
  topology's palette with the same `now` / `24h peak` select
  (`load=peak`). Busier direction over capacity; a declared service
  capacity beats the port speed. Lanes and attachments go quiet; grey
  means no measurement. The demo seeds firewall counters so the public
  snapshot shows it.

- **Routed view: Protocol mode** (#50, ADR-0002 Decision 3). The third
  radio paints address families: IPv4 lanes teal, IPv6 lanes amber, a
  dual-stack lane teal with an amber dash riding it, and every attachment
  dot — a host's leg, the router's gateway — colored by the families it
  actually holds there. The select narrows to one family and dims
  whatever lacks it (`proto=4` / `proto=6`; the default is both). A leg
  now lists every address a device holds on a network, and a rail knows
  its IPv4 and IPv6 gateways separately (both in the tooltip).

- **Routed view in the snapshot** (#50). The break-glass file now carries
  the L3 picture under the topology map, from the same builder and
  template as /routed — never a second implementation. Its state lives in
  memory there (the URL belongs to the topology map on the same page),
  the `vertical rails` chip redraws in place instead of reloading, and
  double-click jumps to the device's section or the VLAN's row.

- **Topology: derived zones** (#47). A translucent hull now sits behind
  each hypervisor and the guests that are nodes on the map (a virtualized
  firewall, a router VM), and a fainter one around the whole cluster when
  the hypervisors share a vSphere — named after the vSphere server, like
  the routed view's box. Zones are computed from the parent relationship
  patchbay already holds, never drawn by hand; a grouping force pulls
  members together, the VM-on-host edge hides inside a drawn zone, and
  zones draw under everything and take no clicks. In the tiers layout a
  zone keeps only the members in its hypervisor's band, so a hull never
  wraps the fabric between an Edge-band guest and its host. The `zones`
  chip (`zones=0`) turns them off. The demo's hypervisors gained specs
  so the public snapshot shows the #44 line.

- **Topology: node detail panel** (#45). Clicking a node no longer
  leaves the map: a panel floats over the map's right edge with the
  node's role, status and how long ago it was seen, address, hardware,
  OS, specs, host, VLANs, and every link with the port at each end, the
  speed, utilization, and who reported it. A peer's name in the list
  selects that node; Escape or × closes; the selection rides the URL as
  `sel=`. The device page is the secondary action — double-click, or
  ⌘/Ctrl-click for a new tab — the routed view's contract. The panel
  reads the graph JSON, so the snapshot has it too (its link jumps to
  the device's section). Read-only by design: edits belong on /ops.

- **Topology: node card refresh** (#44). Every card node now shares one
  anatomy: a rounded-square icon chip anchors the left edge, the status
  LED pins the top-right corner, and the name and subtitle read from a
  fixed inset — so a mixed row of switches, hypervisors, APs, and hosts
  scans as one kind of thing. Hypervisors gain a third line of hardware
  mini-specs (`24c · 192 GB`), recorded by the vSphere collector from
  vCenter's hardware summary into two new `devices` columns (`cpus`,
  `mem_bytes`, migrated on start) and printed on the device page and
  Overview card too; the line only appears when the data does. Role color
  stays the stroke and the glyph, status stays the LED — no glow channel.

### Fixed

- **The routed view remembers your view options.** A bare `/routed` — the
  rail's link — restores the last-used mode, family and load choice,
  visibility toggles, and axis from the browser's storage by rewriting the
  URL on arrival, the way `/topology` has since #16. The two maps now
  follow one rule: an explicit URL always wins outright, `focus` is never
  remembered, and the snapshot is exempt.

- **Device merge dropped the new specs columns.** A hypervisor that
  LibreNMS also polls folds its vSphere row into the SNMP-owned primary,
  and the merge's identity field list must name every column or the
  specs vanish on every poll (the way `ip6` once did). Named, with a
  regression test.

- **Topology: speed and VLAN chips on edges** (#43). The bare mid-edge
  speed text is now a pill on the edge, and under it a second pill names
  the link's VLANs when there are three or fewer — an access link says
  `VLAN 20`, a trunk carrying twelve stays quiet (the tooltip lists
  them). Chips carry facts, not provenance: card fill and a line border,
  so edge color stays the reporter's alone; the speed text still warns
  amber at ≤100M and red at ≤10M, and the load view appends the
  utilization. The `edge chips` chip (`chips=0`) hides them.

- **Topology: port names on hover** (#48). Edges no longer print their
  interface names permanently — on a dense map neighboring labels
  overlapped into noise. Hovering an edge (its hit area is now a wide
  invisible twin of the line, which also carries the tooltip) reveals the
  names at both ends; hovering a device reveals the names on every one of
  its links — the "what is plugged into this switch" question. Revealed
  names paint above every node. The `port names` chip (`ports=1`) shows
  them all, for the old always-on picture.

- **Device totals in the nav rail** (#46). Every page's rail carries how
  many devices patchbay knows and how they split: up, down, and stale — a
  device no source has reported for two hours counts as stale whatever
  its last status said, the same window that ages out inferred links.
  Inferred unmanaged switches are a guess, not a device, and don't count.
  Collapsed, the numbers stack under their dots; the block links to the
  Overview. The snapshot header carries the same totals, frozen at
  generation beside the data ages.

## [0.13.0] — 2026-09-08

### Added

- **Routed view: networks-as-lanes layout** (default; the `vertical rails`
  chip turns the same drawing on its side). VLANs are horizontal lanes
  reading edge → internet, left to right: lane labels with subnets in a
  left gutter, loose single-homed chips at the edge, one logical
  **wireless** container holding every AP's clients (per-AP attribution in
  tooltips), hypervisor slabs spanning their lanes with tenants inside,
  the router spanning everything it routes, and the internet cloud plus
  tunnels at the far right. Height is fixed by network count, width grows
  with devices — landscape-native, made to read at a distance.
- **Routed view: one virtualization box.** All hypervisors fold into a
  single box — the wireless container's sibling — named after the vSphere
  server when the guest-aware collector owns a hypervisor row. The routed
  view is logical, so which physical host a VM runs on becomes tooltip
  detail, and a router that runs as a guest draws inside the box.
- **Routed view: one renderer, two axes.** The vertical-rails view is the
  lanes drawing turned upright — same containers, boxes, chips, and
  rules, edge at the bottom and the internet at the top — instead of the
  earlier tiered layout. Lanes sit tighter, multi-homed
  hosts share a column when their spans don't overlap, and VPN tunnels
  sit beside the internet cloud so the transport leaves the router
  straight from its edge. The viewBox hugs the drawing, so **fit** means
  the whole map.
- **Routed view: click focuses, a page is the secondary action.** A plain
  click on a network, host, box, or router now focuses it in place —
  highlight what's attached, dim the rest, `focus=` in the URL — and a
  second click or a click on the background clears it. Double-click opens
  the thing's page; ⌘/Ctrl-click or middle-click opens it in a new tab.
  Navigating away on a plain click felt like falling through the map.
- **Routed view: the shell's last-polled indicator** and auto-refresh, which
  every other page already had.
- **Routed view: Evidence mode**, and with it the segmented view control
  from ADR-0002 Decision 3. Each lane takes the color of its strongest
  reporter — a firewall interface (it routes it), a switch carrying the
  VLAN, the controller, a hypervisor port group, IPAM alone (dashed:
  documented, nothing carries it), a route learned through a tunnel —
  the tag carries a badge per reporter, and everything attached goes
  quiet. `view=evidence` in the URL. Load, Protocol, multi-router sites,
  and snapshot embedding are tracked in #50; ADR-0002 carries an
  amendment recording the lanes-era decisions. The "still settling"
  banner is gone.

### Fixed

- **Powered-off VMs and down devices no longer count on the routed view.**
  A VM that is off still has legs on paper (its port group VLAN, a
  documented address, a cached guest IP) and was counted in the VM chips;
  now only active devices count, unknown status still counts, and the
  virtualization box lists the sleeping guests in its tooltip. The
  builder gained scenario tests for typical sites — ARP-only flat
  networks, IPv6-only sightings, ARP plus learned-VLAN fusion without
  IPAM, mixed-case MACs, addresses outside every network, wireless
  clients across APs, an empty database, and a router with no addresses.
- **Merging a re-duplicated device no longer drops its addresses.** A
  device LibreNMS re-creates under its FQDN every poll merged into the
  fresher row, and colliding port rows on the older duplicate were
  discarded wholesale — so the firewall's interface addresses (which only
  the firewall collector writes) vanished whenever that collector was
  skipped or failed, and with them the routed view's gateway exclusion,
  which let dnsmasq's "gateway" ARP rows draw as a phantom host spanning
  every network. Identity facts (ip, ip6, mac, description, ifindex) now
  fill the primary's gaps whatever their age; liveness (status, speed,
  rates) still follows the fresher row. Separately, an ARP or IPAM row
  carrying a MAC some device's interface owns is that device, never a
  host, whatever name the address wears.

### Changed

- **IPAM is identity, never liveness.** phpIPAM no longer writes endpoint
  rows (it re-stamped `last_seen` every poll, so documented-but-gone
  hosts never aged out and ghost hosts haunted the routed view); it now
  only lends hostnames to endpoints real observers saw, and legacy
  doc-rows are retired on the next poll. On the routed view, IPAM
  addresses matching an observed host add "ipam" legs — including
  networks nothing can observe, like an isolated storage VLAN — but IPAM
  alone never draws a host. UniFi clears stale AP attribution for
  clients gone from the controller's station list, so ex-wireless hosts
  stop counting as clients. A tunnel route whose destination contains
  local networks (WireGuard allowed-ips for the home supernet) is the
  tunnel's source side: named on hover, never drawn as a network.
- **Switch MAC tables discover hosts, but a bare MAC is not a host.** A
  MAC learned on a pure access port counts as a sighting on that port's
  VLAN (trunks and mirror destinations excluded), named by its hostname
  or, failing that, its address from any endpoint or IPAM row — a
  WAN-side neighbor with an address outside every documented subnet
  shows as that address. A MAC with neither is usually a bond member or
  kernel port of a host already drawn, so it is counted in the lane's
  tooltip and never listed as a host.
- **The switch MAC table keeps the VLAN a MAC was learned in.** LibreNMS
  reports it per entry (platforms that don't leave it 0), and `fdb` rows
  are now keyed by it, so a trunked host — a storage box with a VLAN
  interface per network on one 10G port — is placed on every network it
  talks in, from real switch evidence, no alias needed. Legs name the
  address that belongs on *that* network, and a second address on the
  same network (bond plus trunk sub-interface on mgmt) rides the leg in
  the tooltip. Databases from before the change migrate in place.
- **Guests are placed by what ARP saw their NICs do.** A VM's collector
  reports NIC MACs but no guest addresses, and an untagged port group
  never reaches the VLAN tags, so a two-NIC guest drew as single-homed.
  A device's NIC seen by ARP (or documented in IPAM) with an address is
  now a leg on that address's network; `mgmt_ip` is the last resort.
  Routers still claim networks from their own interface config only.
- **phpIPAM lends names by exact address first, MAC second.** One NIC can
  carry several documented addresses; matching by MAC alone handed the
  wrong row's name to whichever came first. Names an earlier MAC-only
  lend got wrong are corrected on the next poll.

## [0.12.0] — 2026-09-03

### Added

- **Firewall VPN tunnels are first-class, type-labeled objects**
  ([#42](https://github.com/dsmorgan/patchbay/issues/42)). WireGuard,
  OpenVPN, and IPsec tunnels from OPNsense (peer/session/SA status
  endpoints) and pfSense (OpenVPN/IPsec status; the WireGuard VPN gateway,
  previously dropped, becomes tunnel health) land in a new `tunnels`
  table — prune-per-type with empty-response guards, and no key material
  stored, not even public keys. On the topology they draw as dashed
  purple egress nodes hung off their firewall, labeled with type and
  peer; on the routed view they sit beside the internet cloud, and a
  subnet reachable *through* a tunnel rails off the tunnel node — drawn
  even when nothing local claims it, because reachability through the
  tunnel is its participation. WireGuard liveness derives from handshake
  age (up / idle / down); tunnel *interfaces* stay out of the port model,
  exactly as before. The demo network gains a site-b WireGuard peer so
  both maps show the feature out of the box. The OPNsense API user needs
  the VPN page privileges — optional, everything else degrades cleanly
  without them. The terminating firewall's device page lists its tunnels
  in their own section, separate from the port table.

- **Routed view redesign**: rails spread to a computed gap (wider for
  more networks, capped for few); hypervisors and APs draw as spanning
  boxes in tiers of their own, with guest VMs grouped inside their
  hypervisor and wireless clients inside their AP — every host counts in
  exactly one place. Dual-homed hosts known only from ARP now fuse by
  canonical hostname into real host boxes (rail gateway addresses are
  excluded, so dnsmasq's per-VLAN "gateway" rows can't invent a phantom
  host). Boxes carry the topology's role icons and colors, and hovering
  a network now dims isolated (gray) rails too.

### Fixed

- OPNsense 403 poll notes now name the exact privilege to grant (and note
  that a Status sub-privilege covers the VPN reads) instead of "the
  matching page privilege".
- Brocade/Ruckus FastIron ports no longer show the long-form port name
  ("GigabitEthernet1/1/10") as their description — that is the vendor
  echoing the ifName when no comment is set, not documentation.
- Snapshot polish: per-source data ages are humanized ("16 h", not
  "967m"), VM cards drop the "· ?" when no hardware string exists, and
  VPN tunnels now appear in the snapshot both on the map and as a table
  under the terminating firewall.
- Hypervisor device pages split vmk* kernel interfaces into their own
  section — they carry the management addresses but no cable ends on
  one, so they no longer pad the physical port list.
- The configs page shows the firewall's management IP on its API-sourced
  row, the patch-panel section header is just the panel's declared name,
  and the topology toolbar controls share one height.

## [0.11.2] — 2026-09-01

### Fixed

- The transient duplicate AP↔switch cable visible right after a poll starts
  is gone ([#38](https://github.com/dsmorgan/patchbay/issues/38)): normalize
  now runs inside the same transaction as each collector, so the web UI
  never reads fresh-but-unnormalized state. A normalize failure rolls back
  to a savepoint and the source's data still lands; per-source atomicity is
  unchanged.
- The liveness and pruning gaps from the post-0.10.0 audit
  ([#41](https://github.com/dsmorgan/patchbay/issues/41)) — the family where
  nothing lies, things just never leave:
  - vSphere no longer writes a VM's cached `powerState` (with a fresh
    timestamp) while the owning host is not responding — a virtualized
    firewall's own status report wins again. Placement still lands.
  - UniFi no longer writes port oper/admin/speed from a down or
    heartbeat-missed device's cached `port_table`; the last good values
    stand, same gate temperature already had.
  - `vnic_vlans` is refreshed by replace: a port group moved to untagged,
    or a deleted VM's MAC, no longer re-emits phantom 802.1Q membership
    forever.
  - Devices removed from LibreNMS or the UniFi controller are retired
    instead of haunting every page with frozen status (each collector's
    own rows only, guarded on a non-empty listing).
  - Endpoint observations (ARP, leases, controller clients, the IPAM
    address book) age out after a week unrefreshed, so /drift stops
    treating months-old rows as live sightings.
  - VLANs that vanish from every switch config and SNMP table get the
    same claim-aware prune phpIPAM's rows got in 0.11.1.
  - The oxidized per-device `port_vlans` rewrite is scoped to its own
    rows, like the `port_roles` delete beside it always was.

## [0.11.1] — 2026-08-31

### Fixed

- phpIPAM prunes VLANs deleted from IPAM: the collector's own rows only,
  guarded on a real listing, and never a VLAN a device still claims —
  that one just loses its IPAM documentation. Previously a deleted VLAN
  sat on /vlans (and everywhere else) forever.

### Changed

- The routed view carries a visible still-settling note with a link to
  the issue tracker.

## [0.11.0] — 2026-08-31

### Added

- **The routed view discovers the internet uplink.** The default route's
  exit interface resolves to its VLAN's rail (untagged membership first,
  else the rail holding the next-hop address): that rail draws green, the
  drop line from the cloud names it ("via VLAN 299"), and hovering either
  lights both. Sites whose WAN lands on a dedicated appliance port keep
  the plain cloud-to-router drawing — no configuration either way. The
  opnsense collector now records default routes (normalized to
  `0.0.0.0/0` / `::/0`); reachability checks skip /0 destinations.
- The break-glass snapshot embeds the latest patchbay-held firewall config
  revision — already redacted at capture, and scrubbed a second time by the
  snapshot's own redactor on the way in.
- `PATCHBAY_SNAPSHOT_AT` is editable on /ops like the other declarations,
  with inline help and an HH:MM parse warning.
- UniFi device temperature ([#40](https://github.com/dsmorgan/patchbay/issues/40)):
  the controller's `general_temperature` (hardware LibreNMS can't see —
  UniFi gear doesn't speak ENTITY-SENSOR MIB) lands as a device fact on
  the detail page. Only where `has_temperature` says the sensor is real,
  and only while the device is up — stale liveness is omitted, not
  written.

- Targeted deletion: /snapshots rows and a patchbay-held device's stored
  config revisions can be deleted from the UI (one revision or all;
  Oxidized history is untouched).

### Security

- `raw_payloads` strips credential-looking fields before storage — the
  LibreNMS device payload carries SNMPv3 secrets, UniFi's carries
  controller keys. Existing rows age out with the 7-day retention.
- Firewall-config redaction matches secret tags as substrings
  (`<rocommunity>`, `<sharedsecret>`, `<varusersfreeradiuspassword>`) and
  redacts OTP seeds; the snapshot scrubber learns otp/totp/seed. Delete
  stored revisions captured before this and let the next poll re-capture.
- The routed view's graph JSON is script-escaped like the topology's — a
  hostile device or VLAN name could break out of the script element.
- pfSense and phpIPAM no longer wipe gateways, port_vlans, or the IPAM
  address book when a poll answers 403 or empty — degraded visibility
  keeps last good data, matching the collector contract.

### Changed

- Routed-view rails must participate: a network appears when a router
  routes it, a drawn host has a leg on it, or single-homed hosts count
  against it. IPAM-only supernets, aggregates, and VLANs no device claims
  stay on /vlans, where documentation-vs-reality is the point.
- /configs shows canonical short device names for Oxidized nodes enrolled
  by FQDN, matching the rest of the UI; URLs keep the full node name.
- Topology toolbar pills share one height, and the selected view mode uses
  the same teal ring as active filter chips.

## [0.10.0] — 2026-08-30

### Added

- **The routed view: the logical network** ([#17](https://github.com/dsmorgan/patchbay/issues/17),
  [ADR-0002](docs/adr/0002-routed-view.md)). `/routed` answers "what can
  reach what": networks as vertical rails (routed teal with an angled fan
  from the router, unrouted grey — the missing fan line *is* the isolated
  badge), VLAN tags linking into /vlans, one dashed "×N hosts" box per
  network for the single-homed crowd, and multi-homed hosts drawn once on
  their home rail (fastest interface, highest-VLAN tiebreak) with rim
  dots where a rail meets the box and thin lines to rails beyond it. Rail
  order is VLAN-number order pulled tighter by a greedy pass that
  shortens attachment lines; `PATCHBAY_ROUTED_ORDER` pins rails when the
  computed order chafes — no dragging, ever. Hover highlights a network's
  world and carries subnets, gateway, and sources; zoom, pan, and fit
  match the topology frame. The demo network gains a three-legged NAS and
  storage-legged hypervisors so the hard cases render out of the box.

- **The firewall joins config history**
  ([#23](https://github.com/dsmorgan/patchbay/issues/23)). Grant the
  OPNsense API key the "Diagnostics: Configuration History" privilege
  (the one covering `api/core/backup/*`) and every poll pulls
  config.xml, redacts secret-bearing elements down to content hashes (a
  rotated key still reads as a change; no secret is ever stored), strips
  the revision-block noise, and keeps a revision only when something real
  changed — with the change's description and author lifted onto the
  timeline. /configs lists the firewall beside the Oxidized nodes with
  the same view and diff pages, and works with or without Oxidized
  configured. Snapshots deliberately embed none of it.

### Changed

- **UniFi devices show product names, not API codes**
  ([#39](https://github.com/dsmorgan/patchbay/pull/39), thanks
  [@slmingol](https://github.com/slmingol)). Known raw model codes
  ("U7PG2") translate to display names ("AC Pro") at ingestion, a
  known-misreported LibreNMS sysDescr is corrected, and stale raw codes
  already stored yield to the translated name in the merge — exact known
  codes only, so an untranslated code stays visible rather than erased.

- **One toggle vocabulary: filter chips**
  ([#36](https://github.com/dsmorgan/patchbay/issues/36)). The topology
  toolbar's display filters (hide offline & unlinked, core only, wired
  hosts, behind unmanaged, tier lanes) are now the same pill chips the
  alerts and aggregate pages use, with a shared documented style —
  focusable, toggling on Enter/Space, state in `aria-pressed`. URL and
  remembered-state behavior are unchanged, and the snapshot's map gets
  the same toolbar. Segmented controls keep the view modes; the header's
  auto-refresh pill stays the action-chip variant.

### Fixed

- **A link reported under an alias no longer expires while alive**
  ([#37](https://github.com/dsmorgan/patchbay/issues/37)). When a rename
  flipped a link out of sorted orientation and it collided with its
  canonical twin, the fresh sighting's timestamp was discarded — the
  surviving row sat frozen until the evidence TTL expired a cable that
  was being reported every poll. The collision now merges timestamps,
  the same rule the per-field rewrite already used.

## [0.9.0] — 2026-08-29

### Added

- **Auto-refresh is a visible switch, not a hidden rule.** The header's
  "last polled" readout gains an **auto-refresh** pill: green-dotted while
  the page will reload on the next poll, hollow when you've parked it —
  per browser, honored on every page. It replaces the topology map's
  first-touch-holds-forever behavior, which could quietly leave a browser
  showing 16-hour-old data after one wheel scroll. Ages now read in human
  units everywhere ("16 h", not "967 min"), in the header, its per-source
  tooltip, and the stale-sources attention item alike.

- **One command to see it running.** `scripts/demo.py` pulls the published
  image, seeds the demo network inside the container, waits for the UI, and
  opens it in your browser; Ctrl-C removes it, and nothing touches the host.
  `--build` does the same for the checkout you're in.

- **UniFi switches join the map**
  ([#32](https://github.com/dsmorgan/patchbay/pull/32), thanks
  [@slmingol](https://github.com/slmingol)). The UniFi collector now
  collects USW switches with their full port tables, writes the
  controller's LLDP-discovered uplinks as links (switch-to-switch and
  AP-to-switch, drawn in the AP color and labeled "controller-reported"
  in the evidence view), and feeds wired clients' learned switch ports
  into the MAC table so they place like any other FDB evidence. The
  `fdb` table gains a `source` column (existing rows migrate to
  `librenms`) so each collector owns and refreshes only its own rows.
  Controller-reported cables slot into the one-cable-per-port order:
  device-level LLDP beats them, and they beat hypervisor hints and
  MAC-table inference.

- **Name your unmanaged switches**
  ([#33](https://github.com/dsmorgan/patchbay/pull/33), thanks
  [@slmingol](https://github.com/slmingol)). `PATCHBAY_UNMANAGED` accepts an
  optional label — `closet-switch=sw1:1/0/8` — and the topology node shows
  that name instead of `unmanaged@sw1:1/0/8`; the legacy form is unchanged.
  Declaring a port that FDB inference already claimed replaces the inferred
  node, and removing or renaming a declaration evicts the old node. A label
  that collides with a real device's name falls back to the auto-generated
  name rather than hijacking the device, and named nodes keep the VLAN
  chips of their feeding port.

- **OPNsense interfaces carry more of the model**
  ([#31](https://github.com/dsmorgan/patchbay/pull/31), thanks
  [@slmingol](https://github.com/slmingol)). The collector now records
  `admin_status` (from `enabled`), link speed (parsed from the statistics
  line rate, so port-load views cover firewall interfaces), and 802.1Q
  membership for VLAN sub-interfaces; tunnel interfaces (`tun`/`ovpn`/
  `gif`/`gre`/`ipsec`/`wg`) are filtered out as virtual endpoints with no
  cable, and rows written before the filter existed are purged.

### Fixed

- **`pip show patchbay` tells the truth.** The package version is now read
  from `patchbay.__init__` at build time; installed metadata had been stuck
  at 0.4.0 since the version moved out of `pyproject.toml`.

- **No more phantom switches on inter-switch uplinks**
  ([#34](https://github.com/dsmorgan/patchbay/issues/34), thanks
  [@slmingol](https://github.com/slmingol)). LLDP links can record a port by
  its ifAlias description while the FDB records it by ifName; the mismatch
  let remote MACs crossing a known uplink grow an inferred unmanaged switch
  that doesn't exist. Normalize now bridges descriptions to ifNames when
  building the linked-port set, and existing phantoms age out on their own.

### Changed

- **A source edit rebuilds the image in seconds.** The Dockerfile installs
  dependencies in their own layer before copying `src/`, so a rebuild after
  a code change no longer re-downloads fastapi, uvicorn, and pyvmomi
  (8 s → 2 s on a fast link; the whole build, on a slow one). The image
  itself is unchanged.

## [0.8.0] — 2026-08-27

### Added

- **Aggregate views: all ports, all AP clients, all guests**
  ([#26](https://github.com/dsmorgan/patchbay/issues/26)). "Show me every
  up port on the fabric" no longer means a device-by-device tour: `/ports`
  lists every physical port across switches, routers, and firewalls
  (up by default), `/clients` every wireless client (active within the
  staleness window by default), and `/guests` every VM (running by
  default) — each with a device column linking home, an `?state=all`
  filter addressable in the URL, and an entry link on its Overview
  section heading. Row cells render through shared macros in `_ui.html`,
  the same ones the per-device tables now use, so the two cannot drift;
  port graphs stay on the device page, one click away.

- **Attention gets a real section, and a page of its own**
  ([#28](https://github.com/dsmorgan/patchbay/issues/28)). The Overview's
  attention list is now a distinct bordered section with a heading, a
  severity-colored per-category summary strip, and a ~4-row scroll cap —
  the Overview stays a summary. A new top-level **Alerts** page (in the
  rail under Network) lists everything, filterable by category and
  severity with the filtered state in the URL. Every item now carries a
  stable identity, a category, and a first-seen time recorded at poll
  time — shown as "for 42m" — so the phase-6 alerting engine
  ([#22](https://github.com/dsmorgan/patchbay/issues/22)) can extend these
  rows with history rather than replace them. The check rules moved to
  `attention.py`, importable without the web stack; `PATCHBAY_EXPECT`
  suppression applies throughout.

## [0.7.0] — 2026-08-27

### Added

- **Routers belong to the fabric**
  ([#27](https://github.com/dsmorgan/patchbay/issues/27)). Devices with role
  `router` now appear in the overview's Fabric section (heading updated to
  say so), on the topology map in the Edge tier beside firewalls, in the
  map's core-only filter and legend, and in normalize's network-role and
  trunk-propagation sets. Routers get their own icon (the switch arrows,
  circled) and card color. A routed VM behaves like a virtualized firewall:
  a Fabric card of its own, still folded in under its hypervisor. No
  collector emits the role yet — this makes the UI ready for one that does.

- **The map remembers your view options**
  ([#16](https://github.com/dsmorgan/patchbay/issues/16)). A bare
  `/topology` — the rail's link — restores the last-used mode, load/VLAN
  choice, visibility toggles, and layout from the browser's storage by
  rewriting the URL on arrival, so reload and share keep working. An
  explicit URL always wins outright: a deep link renders its own params
  plus defaults, never a merge with remembered state. `focus` and foreign
  query params are never remembered, and the snapshot keeps its
  hash-state behavior untouched.

- **The rail gets out of the way**
  ([#21](https://github.com/dsmorgan/patchbay/issues/21)). By default the
  nav rail now collapses back to icons after you pick a page — its job
  ended with the navigation. A pin button (shown while expanded) locks it
  open across pages, restoring the old behavior; collapsing by hand also
  unpins. The collapse slides instead of snapping (~180 ms; skipped
  entirely under `prefers-reduced-motion`). The stored preference migrates:
  a previously chosen "expanded" reads as pinned.

- **Actions reflect their own results**
  ([#19](https://github.com/dsmorgan/patchbay/issues/19)). Taking a
  snapshot now puts the new file in the kept list without a reload, and the
  same mechanism covers every action: on completion, the page re-fetches
  itself and swaps its marked live regions in place — /snapshots' latest
  link and kept list; /ops' last-poll report, declaration conflicts, export
  lines, and effective-config table (including after a declaration save).
  Output on screen and half-typed fields are never disturbed.

- **Declarations explain themselves on /ops**
  ([#20](https://github.com/dsmorgan/patchbay/issues/20)). Every declaration
  field carries a "?" toggle with one sentence on what it does, the syntax,
  and a realistic example — sourced from a single `DECLARATION_HELP`
  structure beside `DECLARATION_VARS` in config.py, not hand-duplicated in
  the template. A malformed entry's warning now renders beside its own
  field (opening the help, so syntax and error meet in one place) instead
  of in a page-top box; the box remains only for warnings that name no
  declaration, like an unreadable database.

### Fixed

- **Patch panel size no longer reads as part of the title**
  ([#18](https://github.com/dsmorgan/patchbay/issues/18)). The declared
  "N positions" moved from the header's controls slot — where the
  pages-and-map milestone had left it sitting beside the page title — to
  the muted purpose line, where data belongs.

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
