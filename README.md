<img src="docs/mark.svg" alt="" width="120">

# patchbay

One console for a small network or homelab: physical topology, live health, an
IPAM overlay, config history, patch panels, and self-contained HTML snapshots
you can open with the network down.

For what's in the current release, see the [changelog](CHANGELOG.md). For
what's planned, see the [roadmap](docs/architecture.md#phased-roadmap).

## Quick start

Run the web UI and a background poller from the pre-built image — no clone needed:

```sh
curl -O https://raw.githubusercontent.com/dsmorgan/patchbay/main/docker-compose.example.yml
curl -O https://raw.githubusercontent.com/dsmorgan/patchbay/main/.env.example
mkdir -p data && cp .env.example data/.env    # fill in the tools you already run
docker compose -f docker-compose.example.yml up -d
```

The UI is on http://localhost:8484. `patchbay.db` is created in `./data` on first run.
To build from source instead, comment the `image:` lines in the compose file and uncomment
the `build:` blocks, then run with a checkout present.

Or run from a checkout, which gives you the CLI and an editable install:

```sh
uv venv && uv pip install -e '.[web]'
cp .env.example .env                          # fill in the tools you already run
patchbay poll                                 # run every configured collector
patchbay show devices|links|subnets|vlans|endpoints
patchbay web                                  # dashboard on :8080
```

Same code either way — the difference is what runs it. The container starts
two services, the web UI and a poller that polls every five minutes and
survives a reboot. A checkout runs only what you launch: `patchbay web`
serves, and `patchbay poll` collects once, so **nothing repolls until you
schedule it** with cron, systemd, or launchd. `PATCHBAY_SNAPSHOT_AT` is
checked during a poll, so the nightly snapshot follows whatever schedules the
polling; without one you still get snapshots on demand, from the CLI or the
ops page. Use a checkout to evaluate patchbay or write a collector, and the
container for anything you intend to keep running.

### Try it without a network

`patchbay demo` writes a fictional network — two switches, a firewall,
hypervisors and guests, APs, an inferred unmanaged switch, drift findings, a
day of traffic — into `demo.db`. Nothing in it is real and no credentials are
needed:

```sh
patchbay demo                                 # writes ./demo.db
PATCHBAY_DB=demo.db patchbay web              # dashboard on :8080
PATCHBAY_DB=demo.db patchbay snapshot         # one self-contained HTML file
```

For zero installation at all, **[open the live demo](https://dsmorgan.github.io/patchbay/demo-snapshot.html)** —
it's a break-glass snapshot of the demo network: the interactive topology
map, every device and port, VLANs, and endpoints, in one self-contained HTML
file that works entirely offline. A real deployment's nightly snapshot looks
exactly like this, built from your own network.

Nothing in [.env.example](.env.example) is required. Each collector activates
only when its variables are set, so start with the one or two tools you already
run — LibreNMS alone gets you most of the topology — and add the rest later.
With nothing configured, every page still renders and the dashboard lists what
to set. [docs/configuration.md](docs/configuration.md) documents every variable.

## What it does

A homelab accumulates managed switches from several vendors, a firewall,
wireless APs, a hypervisor, and a few unmanaged switches, and no single tool
shows how they connect. patchbay aggregates what other tools already collect
and renders one navigable picture:

- **Physical topology** — an interactive map of what is plugged into which
  port, built from LLDP/CDP neighbors, switch MAC tables, and hypervisor
  network hints, plus your declarations for what no protocol can see. Every
  edge carries its evidence: color is who reported it (switch, hypervisor,
  operator), and a dashed line means inferred rather than stated. Unmanaged
  switches are inferred from ports with many MACs and no LLDP neighbor, and
  drawn distinctly. Link thickness encodes speed, a load view recolors edges by
  24-hour utilization, and a service-capacity override renders as "10G (3G)".
- **Health dashboard** — device up/down state, hardware, management IPs, and VM
  placement on cards styled to match the topology nodes. Device pages embed
  LibreNMS graphs (traffic and errors per port; CPU, memory, storage, and
  temperature per device; 24-hour to 1-year windows) through a server-side
  proxy, so the API token never reaches the browser. The graphs are recolored
  to the patchbay palette in transit.
- **VLAN overlay** — select a VLAN to highlight it across the fabric: which
  trunks carry it, which access ports sit in it, and its gateway and subnets,
  with deep links into your IPAM. The drift page reports where IPAM records and
  live ARP or lease data disagree.
- **Config history** — one timeline of config diffs across every device, so you
  can answer "what changed since it last worked". Rendered from Oxidized's git
  repository.
- **Patch panels** — panels you declare in `PATCHBAY_PANELS`, mapped from port
  descriptions, so the wall plate and the switch port agree on record.
- **Ops page** — the effective configuration with secrets redacted, editable
  declarations, and buttons to poll a collector or trigger LibreNMS
  rediscovery after you recable something.
- **Break-glass snapshots** — `patchbay snapshot`, or the button on the ops
  page, writes one self-contained HTML file: the interactive map, every device
  and port, links, endpoints, traffic graphs, and redacted configs, all
  openable with no connectivity. Ship the snapshot directory to storage that
  survives the network going down.

## Architecture

patchbay does not reimplement polling. Proven tools do the commodity work, and
patchbay does the aggregation, correlation, and visualization they can't:

```
devices ──► LibreNMS  (SNMP metrics, LLDP links, graphs, alerting)
        ──► Oxidized  (config backup → git)
        ──► native APIs (UniFi, OPNsense, vSphere, phpIPAM — richer than SNMP)
                 │
                 ▼
         patchbay core (FastAPI + Jinja2 + SQLite)
         normalizer → devices / interfaces / links / endpoints / subnets / VLANs
                 │
                 ▼
         topology · health · VLAN overlay · config diffs · graph proxy · snapshots
```

The normalizer holds the correlation logic: it merges devices across sources
(the freshest status wins, a versioned OS beats a bare fingerprint, and
placeholder values never overwrite real data), applies link-evidence precedence
(LLDP, then hypervisor hint, then MAC-table inference, one cable per port),
guards against CDP floods, and removes anything a source stops reporting, so a
deleted VM or an undeclared link leaves the map.

### Modularity

Collection is a plugin interface. Each source — LibreNMS, Oxidized, UniFi,
OPNsense, vSphere, phpIPAM — is a self-contained collector that maps its source
into the shared model. Supporting new gear means writing one collector, not
changing core. The reference deployment uses:

| Component | Live data via | Config backup |
|---|---|---|
| Netgear M4300 series | SNMPv3 + LLDP (LibreNMS) | Oxidized (SSH) |
| Brocade/Ruckus ICX (FastIron) | SNMPv3 + LLDP (LibreNMS) | Oxidized (SSH, legacy KEX) |
| OPNsense | SNMP + REST API | not yet — see the roadmap |
| UniFi APs | self-hosted Network app API | controller autobackup |
| VMware vSphere / ESXi 7 | pyVmomi | n/a |
| Unmanaged switches | inferred from MAC tables | n/a |
| phpIPAM | REST API | n/a |

Third-party collectors install as their own packages through the
`patchbay.collectors` entry-point group, with no core edits. For more detail,
read [docs/architecture.md](docs/architecture.md) for the spec and roadmap,
[docs/pluggability.md](docs/pluggability.md) for the component architecture and
how other vendors' gear plugs in, [docs/collectors.md](docs/collectors.md) for
the collector authoring guide, and [CONTRIBUTING.md](CONTRIBUTING.md) if you
want to add support for your own hardware.

## Operator declarations

Declare facts no protocol can discover in your site `.env`. All are optional:

| Variable | Declares |
|---|---|
| `PATCHBAY_LINKS` | cables to devices that report nothing |
| `PATCHBAY_UNMANAGED` | ports that feed an unmanaged switch |
| `PATCHBAY_ALIASES` | identity merges, so one box stops appearing as two |
| `PATCHBAY_VLAN_FILTER` | trunks that carry a restricted VLAN list |
| `PATCHBAY_PANELS` | patch-panel layout and the port-description pattern |
| `PATCHBAY_CAPACITY` | service rates below the port speed |
| `PATCHBAY_RELATED` | out-of-band ties, such as a BMC and its server |
| `PATCHBAY_WAN_NAME` / `_PORT` | your providers and where each lands |

Both WAN variables take lists, so redundant circuits and a second ISP both fit.
Declared links are pruned when you remove them: the `.env` is the source of
truth, not a one-way import. For the exact syntax of each variable, see
[docs/configuration.md](docs/configuration.md#operator-declarations). You can
also edit any of these on the ops page, where the env file still wins whenever
it sets a value.

patchbay checks your declarations against the network on every poll. When
discovery contradicts one, the observation wins and the ops page reports the
stale declaration. A port carries one cable, so patchbay never draws both.

One port is an exception, because no protocol can describe it: a mirror or SPAN
destination transmits copies of other ports' traffic, so its MAC table
describes the rest of the fabric instead of what it's cabled to. patchbay finds
these in the device config, excludes them from inference, and labels them on
the map. Only a declaration can say where such a port leads.

## Site configuration is never in this repo

All deployment-specific data — hostnames, IPs, credentials, SNMP communities,
device serials, collected configs, snapshots, and fixtures captured from real
networks — lives outside this repository, in env files and a private
site-config repo. The `.gitignore` guards the common paths, but the rule is
absolute: **if it describes a real network, it doesn't get committed here.**

## License

[MIT](LICENSE)
