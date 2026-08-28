# ADR-0002 — The routed view: the logical network

*Status: draft (2026-08-28) · Issue: [#17](https://github.com/dsmorgan/patchbay/issues/17)*

## Context

The topology map answers "what is cabled to what." Nothing answers "what
network can reach what": which subnets exist, who routes between them, where
the default goes, and which hosts stand in more than one network. That is
the question an operator asks during segmentation work, firewall changes,
and "why can the IoT VLAN see the NAS" incidents — and it is the view
vSphere/ESXi gives per-host that patchbay should give for the whole site.

Everything needed is already in the model, collected per poll:

| fact | source |
|---|---|
| networks: CIDR, VLAN id, name | `subnets` (+ `vlans`) |
| who routes a network | a `firewall`/`router` device with an interface IP inside it |
| real next-hops, incl. cross-router | `routes` (OPNsense/pfSense route tables; BSD `B`/`R` flags = discard) |
| where the default goes | `routes` destination `0.0.0.0/0` / `::/0`, and `gateways` (WAN health) |
| a host's legs | every interface IP; a guest's via `vnic_vlans` + its addresses |
| how alive a network is | `endpoints` with an IP inside the CIDR |

The hard problem named in #17 is **multi-homed hosts**: a NAS with legs in
two VLANs, a hypervisor with vmk interfaces in management and storage. Drawn
naively (a host node inside each network) the same box appears twice and the
lie is structural; drawn as one node with edges into several networks, the
map has to make an attachment *visually distinct* from routing, or every
multi-homed NAS reads as a router.

## Decision 1 — its own page, not a topology mode

`/routed` is a page beside `/topology` (rail: Network, under Topology), not
a fifth `view=` mode. ADR-0001's mode contract is that the graph JSON is
identical in every mode and only the painting changes; the routed view
breaks that on purpose — its nodes are *networks and routing devices*, not
every physical box, and its edges are adjacencies, not cables. Forcing it
into `_topomap.html` would mean two graph schemas in one file and a mode
toggle that swaps both data and drawing — a second implementation wearing
the first one's clothes.

What it does share:

- **The URL-state pattern** (ADR-0001 Decision 1): state in the query
  string, only non-defaults serialized, sanitized rewrite on load, unknown
  params preserved.
- **The visual language**: same role icons and colors, same band treatment,
  same load/health dot vocabulary — it must read as the same product.
- **The pin store**: `positions`, with names namespaced `routed:<node>` so
  arranging one map never disturbs the other.
- **The server-side builder pattern**: `build_routed_graph(conn, settings)`
  beside `build_topology_graph()`, so the snapshot can embed the routed view
  later without a reimplementation. v1 ships live-only; wiring it into the
  snapshot is a follow-up once the view has settled (the physical map
  remains the break-glass essential).

## Decision 2 — the graph: networks and routers, with attachments demoted

Node kinds:

- **network** — one pill per subnet: name, CIDR, VLAN id, live-endpoint
  count. IPv4 and IPv6 subnets sharing a VLAN id fold into one pill (they
  are one broadcast domain; the pill lists both CIDRs).
- **router** — devices with role `firewall`/`router` that have an interface
  IP in ≥1 known subnet, plus any device appearing in `routes.device` (a
  future L3 switch qualifies by evidence, not by role).
- **cloud** — the internet, one per WAN provider (reusing the physical
  map's cloud treatment and `gateways` health).
- **host** *(toggle, default off — see below)* — a non-routing device with
  interface IPs in **two or more** networks: hypervisor vmk legs, a
  multi-homed NAS. Single-homed hosts are never nodes; they are the
  endpoint count on their network's pill. This is the multi-homed answer:
  a device appears **once**, with one attachment edge per leg, exactly as
  the physical map draws one box with several cables.

Edge kinds, visually distinct by construction:

- **routes** (solid, router-colored): router ↔ network, from an interface
  IP inside the CIDR. Labeled with the router's address in that network
  (`.1`), which is usually the network's gateway.
- **next-hop** (solid, directional): router → router, from a `routes` row
  whose gateway lands in another router's interface — the cross-router
  path a two-firewall site has and single-box sites simply won't show.
- **default** (dashed, to the cloud): from `0.0.0.0/0` / `::/0` routes and
  the `gateways` table; carries gateway health color.
- **attached** (thin, muted, dotted): host → network. Deliberately the
  quietest stroke on the map — an attachment is presence, not reachability.
  Labeled by interface (`vmk0`, `eth1`).

A network no router claims and no route reaches renders with an
**isolated** badge — the state the delegated-but-blackholed IPv6 /62s
already exhibit on real data, on the map instead of only in a table.
Discard routes (`B`/`R` flags) are excluded from edges, same as /vlans.

Ranks (soft bands, `layout=free` rules — full XY pins, forceY nudge):
Internet · Routing · Networks · Multi-homed hosts.

## Decision 3 — state table

| param | values | default | meaning |
|---|---|---|---|
| `proto` | `4` \| `6` \| `all` | `4` | which address family's edges paint |
| `hosts` | `1` \| `0` | `0` | show multi-homed host nodes |
| `focus` | a node name | unset | highlight + center, as on /topology |

Small on purpose: modes can come later if the page earns them; every param
follows ADR-0001's serialization rules. The `?state=all`-style aggregate
filters (#26) don't apply — there is no "down" network to hide.

## Decision 4 — what v1 does *not* do

- No firewall-rule awareness: an edge means "routes", not "permits".
  Rule-aware reachability is a different feature with different data
  (config parsing) and belongs to its own issue if ever.
- No per-edge traffic: the physical map owns load; duplicating it here
  would claim precision the routing layer doesn't have.
- No snapshot embedding yet (Decision 1).
- No NAT modeling: the default edge says "leaves here", nothing more.

## Consequences

- A second graph include (`_routedmap.html`, ~a third the size of
  `_topomap.html`) and one builder in `web.py`. The physical map is
  untouched.
- The demo network gains a multi-homed host (a NAS with legs in
  `management` and `servers`) so the view's hardest case is visible on
  the public demo and in tests, not only on real sites.
- `/vlans` keeps its table; the routed view is its picture. Deep links go
  both ways (a network pill links to `/vlans#v<vid>`, the vlans row gains
  a "show on routed map" link), same pattern the physical map set.
