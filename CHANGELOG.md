# Changelog

This project follows [semantic versioning](https://semver.org/). Before 1.0,
the shared model and the `PATCHBAY_*` declaration syntax may change between
minor versions; the collector contract in
[docs/collectors.md](docs/collectors.md) is the interface most likely to stay
put.

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

[0.1.0]: https://github.com/dsmorgan/patchbay/releases/tag/v0.1.0
