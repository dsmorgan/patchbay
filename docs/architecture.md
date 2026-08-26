# patchbay — architecture

*Spec v1.1 · 2026-08-15 · read-only first, break-glass always*

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
SNMP polling, metrics, and alerting; **Oxidized** for versioned config backup. A thin
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
- **Outputs:** LAN-only browser UI; self-contained HTML snapshots shipped to storage that
  is off the monitored failure domain; alerting via LibreNMS transports.

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
3. **Logical L3/VLAN overlay** — highlight a VLAN/subnet across the fabric; includes an
   IPAM drift report (live ARP/leases vs. IPAM records).
4. **Config & change history** — Oxidized git log rendered as one cross-device timeline
   with in-app diffs, plus snapshot-to-snapshot topology diffs.

## Snapshots

- One fully self-contained HTML file: inline CSS/JS/SVG, zero external requests.
  Contains topology, all device/port detail, IPAM tables, leases, ARP, latest configs.
- Nightly schedule + on-demand + (later) automatically on critical alert.
- Written to cloud-synced storage on a separate device; optional S3 copy with lifecycle
  rules. Retention: 30 dailies, 12 monthlies, all alert-triggered.
- **Redaction:** configs are scrubbed before embedding (secret stripping + a scrub pass
  for SNMP communities, hashes, PSKs). A snapshot ends up on cloud storage; treat it as
  leakable.

## Security

- Read-only everywhere until the operations phase: SNMPv3 auth+priv RO users, read-only
  API roles/tokens per source.
- UI is LAN-only. Secrets in env files on the host, never in the repo or snapshots.
- Write actions (final phase) get a separate credential set, per-action confirmation,
  and an audit log.
- Legacy-crypto devices, such as FastIron 08.0.30 with its DH-group1-SHA1-only SSH, are handled
  with client-side legacy KEX options scoped to those hosts only — never by loosening
  defaults globally — and management access is restricted at the network level.

## Phased roadmap

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
| 5.5 | firewall config history: Oxidized's opnsense model over a forced-command SSH key, revision-block noise stripped; the config.xml carries live private keys, so the repo never leaves the backup host and snapshots keep excluding configs | a firewall rule change shows up in /configs like any switch change |
| 6 | alerting rules + transport; snapshot-on-critical-alert; port-counter canaries — flag any port whose error or discard rate jumps orders of magnitude above its own baseline (an egress-discard flood on one trunk is how VLAN flooding announces itself, and today it's only found by reading per-port graphs) | unplugged AP → notification within one polling cycle + automatic snapshot; a port suddenly discarding thousands of packets/sec surfaces without anyone opening its graph |
| 7 | guarded write ops: port enable/disable, PoE cycle, AP restart/locate; audit log | a hung PoE device can be power-cycled from its port page, action audit-logged |
