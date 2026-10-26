# patchbay — architecture

*Spec v1.2 · 2026-10-22 · read-only first, break-glass always*

This is the public, site-agnostic version of the architecture spec. Deployment-specific
details (hostnames, addresses, credentials) live in the private site-config repo.
For the component breakdown and how other products plug in (different switch vendors,
hypervisors, IPAMs), see [pluggability.md](pluggability.md).

## Intent

One place to see a whole small network — every switch, port, AP, firewall interface, and
hypervisor uplink — visually, with drill-down to detail (port status, load, PoE, VLANs),
and a periodic self-contained HTML snapshot stored off-network so it's usable precisely
when the network is broken.

**Approach:** hybrid. Proven open-source tools do the commodity work — **LibreNMS** for
SNMP polling and metrics; **Oxidized** for versioned config backup. A thin
custom app (**FastAPI + HTMX + SQLite**) talks to those plus the native APIs that are far
richer than SNMP — UniFi, OPNsense, vSphere, phpIPAM — and owns the unified visualization,
drill-down, and snapshot generation.

**Non-goals (for now):** guest/workload monitoring, log aggregation, NetFlow. Config push
is deferred to the final phase and starts with small, guarded actions.

## System shape

- **Collectors:** LibreNMS (+MariaDB) and Oxidized containers; native-API pollers run
  inside the patchbay app itself.
- **Core:** normalizer producing a shared model — devices, interfaces, links, endpoints,
  subnets, VLANs — in SQLite. Collection is a plugin interface; each source maps into the
  shared model independently.
- **Outputs:**
  - A LAN-only browser UI.
  - Self-contained HTML snapshots, daily, on demand, and (opt-in) on a critical alert,
    copied to storage that is off the monitored failure domain ([Snapshots](#snapshots)).
  - Alerts from patchbay's own rules ([ADR-0003](adr/0003-alerting.md)). The poller
    evaluates them each cycle and sends them through transports: an Uptime Kuma push
    monitor or a generic JSON webhook. Each rule can route to a different transport or to
    none, a silence mutes what is known and fine, and the `/alerts` page shows the
    active list, the history (kept 90 days, at most 10,000 events), the rules, the
    declared tunnels, the transports, and the silences.
  - `/healthz`: unauthenticated, `200 ok` while the last poll finished within three poll
    intervals and `503 stale` after. A stopped poller raises no alerts, so this is how a
    monitor catches it. The response carries nothing else.
  - `patchbay backup PATH`: a consistent copy of the database, which holds state that
    polling cannot regenerate (see [Database backup](#database-backup)).

**Deployment principle:** the stack must not run on the infrastructure it monitors.
Reference deployment is containers on a NAS that sits outside the hypervisor failure
domain, with snapshots written to a *different*, cloud-synced device — the tool's own
host must never be the snapshot store. Containers get explicit CPU/memory limits when
the host has other duties, such as serving datastores.

## Topology inference (the keystone)

No single source sees the whole network; correlation across four does:

1. **LLDP** (via LibreNMS) — switch↔switch, switch↔AP, switch↔hypervisor links.
2. **FDB/MAC tables** — places every endpoint MAC on a physical port. A port with many
   learned MACs and no LLDP neighbor ⇒ an inferred unmanaged switch.
3. **Hypervisor network hints** — ESXi's CDP/LLDP hints tie each physical NIC to a switch
   port, extending the map through vSwitches to VMs.
4. **IPAM + DHCP leases** — turns MACs into names and subnets.

Two ports resist all of it, and patchbay names both rather than guessing:

- A **mirror (SPAN) destination** transmits copies of other ports' traffic. It doesn't
  forward, learn, or advertise, so its MAC table describes the rest of the fabric and
  not itself. patchbay parses these out of the running-config into `port_roles`,
  excludes them from MAC-table inference — otherwise the mirrored MACs hang a phantom
  switch off the probe port — and marks them on the map. Only a declaration can say
  what a mirror port is cabled to.

### Host placement on the routed view

The routed view answers "what is on which network" from the same evidence, ranked by
how much each source can actually know:

- **A device's own interfaces** place it: an interface address is a leg on that
  address's network; a firewall's addresses make it *route* those networks. A guest
  VM's NIC seen by ARP with an address, or its port group VLAN, places it too —
  routers only ever from their own config.
- **ARP and the controller** see hosts that aren't devices: a hostname with
  addresses on two networks is one dual-homed host (fused by canonical short name;
  randomized privacy MACs never fuse, four iPads are not one host), a wireless
  client counts inside its AP.
- **The switch MAC table** sees hosts ARP can't — an isolated VLAN has no router to
  ask. A MAC counts on the VLAN the switch learned it in (LibreNMS reports that per
  entry; platforms that don't fall back to the port's access VLAN, and trunks stay
  ambiguous there). A MAC that resolves to no hostname or address anywhere is a bond
  member or kernel port of something already drawn: counted, never listed.
- **IPAM is identity, never liveness**: it lends names, and adds legs on networks
  nothing can observe for a host some observer already saw — but never draws a host
  by itself, and a documented gateway address is the router, not a host.
- **Powered-off VMs and down devices don't count**, however many legs they have on
  paper; the virtualization box remembers them in its tooltip.
- A **guest interface's VLAN** is invisible to the guest: the vSwitch adds and strips
  the tag, so a firewall VM sees untagged frames and reports no 802.1Q. The hypervisor
  knows, and the NIC's MAC joins the two views — vCenter's "Network adapter 1" and the
  firewall's `vmx1` are one NIC. Port group VLANs land in `vnic_vlans` and the
  normalizer resolves them onto real interfaces. A switch's own config always wins;
  this only fills silence.

## The four views

1. **Physical topology** — interactive server-rendered SVG; click through link → port →
   device, with per-port status, VLANs, PoE, traffic sparkline, learned MACs.
2. **Live health dashboard** — summary-first: up/down, uplink utilization, PoE budgets,
   AP client counts, WAN state, host/VM placement. State encoded as color + shape.
3. **Logical L3 / VLAN** — the routed view draws networks as lanes with who routes
   each, the hosts standing in several, one wireless box, one virtualization box, and
   where the default route and VPN tunnels leave; the VLAN overlay highlights a
   VLAN/subnet across the fabric, with an IPAM drift report (live ARP/leases vs. IPAM
   records).
4. **Config & change history** — one cross-device timeline with in-app diffs, from two
   sources: the Oxidized git log for the devices Oxidized backs up, and patchbay's own
   revisions of an OPNsense firewall's `config.xml` (secret-bearing elements replaced by
   keyed stamps, the newest `PATCHBAY_CONFIG_KEEP` kept per device, 50 by default). A stored change also raises
   the `config-changed` alert, which informs and routes nowhere unless you route it.
   Snapshot-to-snapshot topology diffs are not planned; see [Snapshots](#snapshots).

## Snapshots

- One fully self-contained HTML file: inline CSS, JS, SVG, and typeface, zero external
  requests. It holds the topology map and the routed view, every device with its ports,
  links, VLANs, subnets, endpoints (from ARP, leases, the MAC table, and IPAM), gateways,
  tunnels, 24-hour traffic graphs for linked ports, and the latest stored config of each
  device, including an OPNsense firewall's.
- Three triggers:
  - Daily at `PATCHBAY_SNAPSHOT_AT`, run by the poller.
  - On demand: `patchbay snapshot`, or the button on `/ops`.
  - With `PATCHBAY_ALERT_SNAPSHOT` on (it is off by default), after the poll that raises a critical alert or
    escalates one to critical. The file is `patchbay-YYYYMMDD-HHMMSS-alert.html`, at most
    one per `PATCHBAY_ALERT_SNAPSHOT_COOLDOWN` (60 minutes by default). A silenced alert
    never takes one. `/snapshots` lists each alert snapshot with the alerts that took it.
- Written to `PATCHBAY_SNAPSHOT_DIR`, then copied to `PATCHBAY_SNAPSHOT_DELIVER_DIR`:
  a path the host can reach on a different device, such as a mounted share that a cloud
  sync client watches. The copy lands under a temporary name and is renamed, so a sync
  client never reads a partial file. A failed delivery never costs you the local file and
  raises the `snapshot-failed` alert.
- Tiered retention, `PATCHBAY_SNAPSHOT_KEEP`, default `30,12m,3y,first`: the first snapshot
  of each of the last 30 days, 12 months, and 3 years, plus the first ever, and always the
  newest. Alert snapshots are counted separately: the newest `PATCHBAY_ALERT_SNAPSHOT_KEEP`
  (10 by default) in each directory. The tiers and the alert count never touch each
  other's files. Pruning covers the local and the delivery directory. A spec that fails
  to parse prunes nothing. See [configuration.md](configuration.md#snapshot-retention).
- **Redaction:** configs are scrubbed before embedding (secret stripping + a scrub pass
  for SNMP communities, hashes, PSKs); the firewall's revisions were already reduced to
  keyed stamps when stored and are scrubbed again on the way in. Alert transport URLs
  never enter a snapshot. A snapshot ends up on cloud storage; treat it as leakable.
- **Not planned:** an S3 client and snapshot-to-snapshot topology diffs. A site-side tool
  can copy the delivery directory to a bucket today, for example `rclone sync`.

### Database backup

`patchbay.db` holds what polling cannot rebuild: webhook URLs, silences, alert rules and
history, declared tunnels, the firewall's config history, and the per-install key behind
its redaction stamps. The snapshot job does not copy it.

`patchbay backup PATH` writes a consistent copy with SQLite's backup API, which is safe
while the poller runs. The file is mode 600 and contains secrets. The command refuses to
write inside `PATCHBAY_SNAPSHOT_DELIVER_DIR`, because that directory is meant for a sync
client. [deployment.md](deployment.md#back-up-and-restore) covers scheduling and restore.

## Security

- Read-only everywhere until the operations phase: SNMPv3 auth+priv RO users, read-only
  API roles/tokens per source.
- UI is LAN-only. Secrets in env files on the host, never in the repo or snapshots.
  Webhook URLs live in the database, masked in the UI and kept out of snapshots and logs.
- Write actions (final phase) get a separate credential set, per-action confirmation,
  and an audit log.
- Legacy-crypto devices, such as FastIron 08.0.30 with its DH-group1-SHA1-only SSH, are handled
  with client-side legacy KEX options scoped to those hosts only — never by loosening
  defaults globally — and management access is restricted at the network level.

## Phased roadmap

Planning and tracking of future work now happens in
[GitHub issues](https://github.com/dsmorgan/patchbay/issues); this table
records the phases and their status. The open phases are tracked as
[#25](https://github.com/dsmorgan/patchbay/issues/25) (5 remainder),
[#22](https://github.com/dsmorgan/patchbay/issues/22) (6), and
[#24](https://github.com/dsmorgan/patchbay/issues/24) (7).

| Phase | Scope | Done when |
|---|---|---|
| 0 ✅ | SNMPv3 + LLDP enabled everywhere; RO credentials for all APIs; DNS names | every device answers `snmpwalk` and every API answers a smoke test from the host |
| 1 ✅ | LibreNMS + Oxidized up; all devices onboarded; configs versioning to git | all devices green, port graphs populating, one clean commit per device |
| 2 ✅ | patchbay skeleton: pollers, normalized model, health dashboard + inventory pages | one page lists every device live; any port drills down |
| 3 ✅ | physical topology map with unmanaged-switch inference | rendered map matches physical reality |
| 4 ✅ | VLAN/subnet overlay, IPAM drift report, config-diff timeline | clicking a VLAN shows its full path; "what changed since *date*" answers in one view |
| 4.5 ✅ | LibreNMS graph proxy: patchbay fetches rendered port graphs via the LibreNMS API (token stays server-side) and embeds them in device pages — LibreNMS itself stays a setup-only tool | a port's 24h traffic graph is visible without ever logging into LibreNMS |
| 4.6 ✅ | dedicated bug-hunt passes: adversarial review of the normalizer, collectors, and UI state, separate from feature work; repeated at least once more before any public release | every finding from the first pass is fixed or explicitly dispositioned |
| 4.7 ✅ | NAS deployment: patchbay container joins the compose stack beside LibreNMS/Oxidized; DB + env migrate off the dev machine; the dev-machine poller is retired | patchbay serves from the NAS through a reboot, no laptop involved |
| 4.75 ✅ | TLS (optional): `PATCHBAY_TLS` = off / direct (cert+key file paths, renewed cert picked up without manual restart) / documented reverse-proxy termination; reference cert automation = Certwarden pull-script pattern, but any file-dropping ACME client works | the UI is reachable over HTTPS with an auto-renewing cert — or plain HTTP only by explicit choice |
| 4.8 ✅ | authentication (optional): `PATCHBAY_AUTH` = none / single shared password / OAuth2-OIDC against a generic provider config (client id/secret, endpoints, claim paths — the glidepath pattern, minus roles and the user table); session cookie + enforce-all middleware | unauthenticated requests see only the login page; `none` mode behaves exactly as today |
| 4.85 ✅ | configuration audit + visibility: every knob documented with its default; /ops shows the effective config (secrets redacted) with each value's source; then DB-backed editing for operator declarations with env-wins-over-DB precedence | a new operator can read and understand the whole running config without a shell |
| 4.9 ✅ | contributor edges: collector discovery via Python entry points (third-party collectors install as packages, zero core edits), vendor quirks isolated behind registries, a collector authoring guide + CONTRIBUTING.md, sanitized-fixture pattern for tests | an out-of-tree collector for new gear works without touching this repo |
| 4.95 ✅ | test suite, fresh install, graceful degradation: pytest with per-collector sanitized fixtures, page smoke tests, a clean-machine install following the README verbatim, and a degradation matrix — any integration absent means a reduced page, never an error. Each degraded install is then *upgraded* by adding the missing source back, and must converge on what a clean install produces | quickstart works from scratch; removing any single collector leaves every page rendering; adding one back reaches the same model as a clean install |
| 5 🔶 | snapshot generator ✅ (`patchbay snapshot` + /ops trigger: self-contained HTML with interactive map, device/port detail, endpoints, redacted configs, linked-port graphs as data URIs, local retention), daily schedule ✅ (`PATCHBAY_SNAPSHOT_AT`, run by the poller) and off-host delivery ✅ (`PATCHBAY_SNAPSHOT_DELIVER_DIR`, atomic rename so a sync client never reads a partial file); REMAINING: wire the reference deployment's share and cloud sync | tool host powered off → still troubleshoot from last night's snapshot |
| 5.5 ✅ | firewall config history: the opnsense collector pulls config.xml over the API (backup page privilege on the existing key), redacts secret-bearing elements to keyed content stamps (#93), strips revision-block noise, and keeps revisions in `config_revisions`; /configs merges them with the Oxidized timeline. Snapshots embed the latest revision, scrubbed a second time on the way in. Oxidized-over-SSH remains a documented fallback | a firewall rule change shows up in /configs like any switch change |
| 6 ✅ | verified live on a reference site (unplugged AP: critical notification raised and cleared, alert snapshot written). Design: [ADR-0003](adr/0003-alerting.md); alerting rules + transport; snapshot-on-critical-alert; port-counter canaries — flag any port whose error or discard rate jumps orders of magnitude above its own baseline (an egress-discard flood on one trunk is how VLAN flooding announces itself, and today it's only found by reading per-port graphs). Shipped with error rates only: no source collects discards yet, so the discard half is tracked in [#83](https://github.com/dsmorgan/patchbay/issues/83) and [#86](https://github.com/dsmorgan/patchbay/issues/86) | unplugged AP → notification within one polling cycle + automatic snapshot; a port whose error rate jumps orders of magnitude surfaces without anyone opening its graph (discards: #83, #86) |
| 7 | guarded write ops: port enable/disable, PoE cycle, AP restart/locate; audit log | a hung PoE device can be power-cycled from its port page, action audit-logged |
