# patchbay — configuration reference

All configuration is environment variables, typically loaded from a site `.env`
file named by `PATCHBAY_ENV` (default `./.env`). Real values never live in this
repo. Every variable is optional unless its section says otherwise; a feature
whose variables are unset stays off, and the UI degrades gracefully.

Parsing is forgiving by design: a malformed entry in any list-valued variable
is skipped, not fatal — but skipped entries are reported (`patchbay poll`
prints them; `/ops` shows them), because a silently dropped declaration would
*remove* the fact it declared on the next poll.

## Config in the UI: env wins, DB fills silence

The `/ops` page shows the **effective configuration** — what patchbay parsed,
secrets redacted — and lets you **edit operator declarations** (the
`PATCHBAY_*` declaration variables below) in the same syntax as the env file.
The sync model is single-ownership per key, never bidirectional:

- The UI never writes the env file. Edits are stored in the database.
- Per variable, an env-file value wins and renders read-only in the UI.
- Bootstrap, credentials, TLS, and auth are env-file-only, forever.
- The `/ops` export block renders DB-stored declarations as ready-to-paste
  `.env` lines, so a value can be promoted back to file ownership anytime.

UI edits take effect on the next poll (declarations act during normalize).

## Core

| Variable | Default | Meaning |
|---|---|---|
| `PATCHBAY_ENV` | `./.env` | Path to the env file to load (set in the process environment, not in the file itself) |
| `PATCHBAY_DB` | `patchbay.db` | SQLite database path |
| `PATCHBAY_TLS_VERIFY` | `1` | Verification for *outbound* API calls: `1`, `0`, or a CA bundle path |
| `PATCHBAY_BUILD` | — | Build identity shown in the page header, so "is this the version I think it is?" has an answer. The Dockerfile sets it from the `GIT_SHA` build argument; set it yourself only if you package patchbay some other way |

## Serving the UI

### TLS

| Variable | Default | Meaning |
|---|---|---|
| `PATCHBAY_TLS` | `off` | `off` = plain HTTP (fine on a trusted LAN or behind a TLS-terminating reverse proxy); `direct` = patchbay serves HTTPS itself |
| `PATCHBAY_TLS_CERT` | — | PEM certificate chain path (required for `direct`) |
| `PATCHBAY_TLS_KEY` | — | PEM private key path (required for `direct`) |

In `direct` mode patchbay watches both files and cycles its listener when they
change, so any ACME automation that drops renewed files — a
[Certwarden](https://www.certwarden.com/) pull script, a certbot deploy hook,
`acme.sh` — works with no patchbay-specific integration and no manual restart.
Reverse-proxy termination (nginx, Caddy, a NAS's built-in proxy) is equally
supported: leave `PATCHBAY_TLS=off` and bind patchbay to localhost or a
container network.

### Authentication

patchbay is read-only, so authentication is a gate, not an identity system:
no user accounts, no roles. What it protects is visibility — configs, drift,
and topology describe your real network — plus the `/ops` action triggers.

| Variable | Default | Meaning |
|---|---|---|
| `PATCHBAY_AUTH` | `none` | `none`, `password`, or `oidc` |
| `PATCHBAY_PASSWORD_HASH` | — | For `password` mode: output of `patchbay hash-password` |
| `PATCHBAY_PASSWORD` | — | For `password` mode: the shared secret in plain text (works, but prefer the hash) |
| `PATCHBAY_SESSION_HOURS` | `12` | Session cookie lifetime |
| `PATCHBAY_SESSION_SECRET` | auto | Cookie-signing secret. Auto-generated once and persisted in the DB when unset; set explicitly only to share sessions across replicas |

`oidc` runs a standard authorization-code flow against any OAuth2/OIDC
provider, described generically — endpoints plus a claim path, no
vendor-specific code:

| Variable | Default | Meaning |
|---|---|---|
| `PATCHBAY_OIDC_CLIENT_ID` | — | Required |
| `PATCHBAY_OIDC_CLIENT_SECRET` | — | Required |
| `PATCHBAY_OIDC_AUTH_URL` | — | Required: the provider's authorization endpoint |
| `PATCHBAY_OIDC_TOKEN_URL` | — | Required: the token endpoint |
| `PATCHBAY_OIDC_USERINFO_URL` | — | Preferred identity source when set; otherwise the id_token's claims are used |
| `PATCHBAY_OIDC_SCOPES` | `openid email profile` | Requested scopes |
| `PATCHBAY_OIDC_IDENTITY_PATH` | `email` | Dot-path to the identity claim, such as `email` or `preferred_username` |
| `PATCHBAY_OIDC_ALLOWED` | any | Comma-separated identities allowed in; unset = any authenticated identity |
| `PATCHBAY_OIDC_REDIRECT_URL` | derived | Exact redirect URL registered at the provider; derived from the request (honoring `X-Forwarded-Proto`) when unset |

The token-endpoint exchange trusts the id_token over TLS (standard for the
direct back-channel) and uses `PATCHBAY_TLS_VERIFY` for that connection —
so **don't set `PATCHBAY_TLS_VERIFY=0` while using OIDC**: a disabled verify
lets a MITM on the token endpoint forge identities. Keep it `1` (or a CA
bundle path) in any OIDC deployment.

### Config history

| Variable | Default | Meaning |
|---|---|---|
| `PATCHBAY_CONFIG_KEEP` | `50` | Firewall config revisions kept per device; older ones are trimmed when a poll stores a new revision (`0` = keep everything). A value that is not a whole number of 0 or more falls back to `50` with a parse warning. Also editable on `/ops` when the env file doesn't set it; `/configs` shows the effective value and its source |

### Snapshots

| Variable | Default | Meaning |
|---|---|---|
| `PATCHBAY_SNAPSHOT_DIR` | `snapshots/` beside the DB | Where `patchbay snapshot` and the `/snapshots` page write the self-contained HTML files (timestamped + a stable `patchbay-latest.html`) |
| `PATCHBAY_SNAPSHOT_KEEP` | `30,12m,3y,first` | Retention tiers for timestamped snapshots; see [Snapshot retention](#snapshot-retention). Also editable on `/ops` when the env file doesn't set it |
| `PATCHBAY_SNAPSHOT_AT` | — | `HH:MM` local time to write one snapshot a day (the poller does it). Unset = on-demand only. Also editable on `/ops` when the env file doesn't set it |
| `PATCHBAY_SNAPSHOT_DELIVER_DIR` | — | Second destination each finished snapshot is copied to (a mounted off-site share). Kept separate from the local directory so a delivery failure never costs you the snapshot; copies land under a temporary name and are renamed, so a sync client never picks up a half-written file |

A snapshot is one fully self-contained HTML file — interactive topology map,
every device and port, links, VLANs, subnets, endpoints, gateways, 24h
traffic graphs for linked ports, and the latest device configs with secrets
redacted. It needs no network to open. Point your off-host sync at the
snapshot directory; `patchbay-latest.html` is the stable name to serve or
ship. Configs are scrubbed, but the file still describes a real network —
treat it as sensitive.

#### Snapshot retention

`PATCHBAY_SNAPSHOT_KEEP` is a comma-separated list of tiers. A timestamped
snapshot survives pruning if any tier claims it:

| Term | Tier | Keeps |
|---|---|---|
| `<n>` or `<n>d` | daily | The earliest snapshot of each of the last `n` calendar days |
| `<n>w` | weekly | The earliest snapshot of each of the last `n` weeks (Monday to Sunday) |
| `<n>m` | monthly | The earliest snapshot of each of the last `n` calendar months |
| `<n>y` | yearly | The earliest snapshot of each of the last `n` calendar years |
| `first` | first | The oldest snapshot ever taken |

- `0` in a tier means unlimited for that tier: `0m` keeps the first of
  every month. A bare `0` keeps every snapshot, as it did before tiers.
- A tier you leave out keeps nothing. The newest snapshot is always kept,
  even when no tier claims it, such as a second snapshot on the same day.
  The next snapshot prunes it.
- A bare integer is a daily tier: with one snapshot a night, `30` keeps the
  same 30 files it did before tiers. Extra on-demand snapshots no longer
  push nightlies out: only the first snapshot of each day counts.
- Periods count back from the newest snapshot, in calendar periods: with
  `12m`, a month with no snapshot is still one of the 12, and with `30`, a
  day with no snapshot is still one of the 30.
- Keepers are the earliest snapshot of each period, so a keeper is known
  the day it is taken. Pruning never removes a keeper inside its window, so
  the earliest file left in a month is that month's true first snapshot.

The default, `30,12m,3y,first`, keeps about 46 files: the first snapshot of
each of the last 30 days (the nightlies), the
first of each of the last 12 months, the first of each of the last 3 years,
and the first ever. At 5 MB a snapshot that is roughly 230 MB.

Pruning runs after each snapshot, on the local directory and the delivery
directory alike, and judges each by its own file names. It governs only
`patchbay-YYYYMMDD-HHMMSS.html` files: `patchbay-latest.html` and
alert-triggered copies (`…-alert.html`) are never pruned by the tiers.

If the spec fails to parse, patchbay prunes nothing until you fix it, and
`/ops` and `/snapshots` show the parse warning. A typo in a retention
setting never deletes history. `/snapshots` lists the tier that keeps each
file; a file that no tier claims (after you tighten the spec) goes with the
next snapshot.

## Data sources

Each collector activates when its variables are set and is skipped otherwise.

| Source | Variables |
|---|---|
| LibreNMS | `LIBRENMS_URL`, `LIBRENMS_TOKEN` |
| Oxidized | `OXIDIZED_URL` (its REST API, such as `http://host:8888`) |
| phpIPAM | `IPAM_URL`, `IPAM_APP_ID`, `IPAM_TOKEN` |
| UniFi Network app | `UNIFI_URL`, `UNIFI_USER`, `UNIFI_PASS` |
| OPNsense | `OPNSENSE_HOST`, `OPNSENSE_API_KEY`, `OPNSENSE_API_SECRET` — see [OPNsense privileges](#opnsense-api-user-privileges) |
| pfSense | `PFSENSE_HOST`, `PFSENSE_API_KEY` — requires the [pfSense REST API package](#pfsense-rest-api-package) |
| vSphere | `VSPHERE_HOST`, `VSPHERE_USER`, `VSPHERE_PASS`, optional `VSPHERE_TLS_VERIFY` (per-source verify override) |

`VSPHERE_TLS_VERIFY` exists because a stock vCenter serves a self-signed VMCA
certificate that nothing trusts. Set it to `0` only until you fix that, and
prefer either of the real fixes: install a certificate from a CA you trust, or
point the variable at the VMCA root bundle
(`https://<vcenter>/certs/download.zip`) so verification passes on the real
certificate. Leaving verification off means anything on the path can
impersonate vCenter.

### OPNsense API user privileges

Create a dedicated read-only user (System → Access → Users → +) with a
scrambled password and an API key. The API key authenticates all collector
calls; the password is never used. Grant the user these privileges — a 403
on any endpoint is logged and skipped rather than failing the whole poll, so
partial grants degrade gracefully:

| Privilege | Endpoint it unlocks |
|---|---|
| Diagnostics: ARP Table | ARP table → endpoint MAC/IP mapping |
| Diagnostics: Routing Tables | Route table → subnet reachability |
| System: Gateways | Gateway health and status |
| DHCP: Leases | DHCPv4 lease table → hostname/IP mapping |
| VPN: Wireguard: Status | WireGuard peers → tunnel objects with handshake-based health |
| Status: OpenVPN | OpenVPN sessions → tunnel objects |
| Status: IPSec | IPsec phase-1 SAs → tunnel objects |

The three VPN privileges are optional: without them (or on releases whose
API lacks the endpoints) tunnels simply stay off the maps — nothing else
degrades. Their names are as verified on OPNsense 26.1 — upstream names
them inconsistently (two under **Status:**, one under **VPN:**) and has
renamed ACL pages across major releases, so if a 403 persists after a
grant, search the privilege list for the page the logged endpoint path
suggests.

The interfaces overview endpoint (`interfaces/overview/export`) does not map
to a named privilege in current OPNsense releases; if patchbay logs a 403 for
it, add **Interfaces: Assign network ports** as a fallback — it is the
broadest read-only interfaces privilege available.

`OPNSENSE_HOST` accepts a bare hostname (`fw1.example.net`, defaults to
HTTPS) or a full URL with scheme (`http://fw1.example.net`) for
installations that do not terminate TLS on the management interface. Over
plain HTTP the API key and secret travel in cleartext on every poll, so
use it only on a management network you trust end to end.

### pfSense REST API package

pfSense does not ship a usable REST API by default. This collector requires
the **pfSense REST API** package from [pfrest](https://github.com/pfrest/pfsense-restapi)
(distinct from the legacy `pfsense-api` package). Install the package build
matching your pfSense version — the
[pfrest install docs](https://pfrest.org/INSTALL_AND_CONFIG/) own that
version matrix — then restart the web GUI.

Navigate to **System > API**, enable the API, and create a key under
**API Keys**. It goes in `PFSENSE_API_KEY`; the collector sends it as the
`x-api-key` request header on every call. (`PFSENSE_API_SECRET` from this
collector's first round is still read as a fallback.)

`PFSENSE_HOST` must include the scheme (`https://firewall.example.internal`).
A bare hostname defaults to HTTPS.

The API user needs read access to interfaces, gateways, and DHCP services —
plus OpenVPN and IPsec status for VPN tunnel objects (WireGuard tunnel
health comes from its VPN gateway, since pfrest has no WireGuard status
endpoint). A 403 on any endpoint is logged and skipped rather than aborting
the poll, so partial privilege grants degrade gracefully.

## Operator declarations

Facts no protocol can discover. All optional; all use the pattern
`device:interface` for port references. Malformed entries are skipped.

| Variable | Format | Meaning |
|---|---|---|
| `PATCHBAY_ALIASES` | `alias=canonical,…` | Identity aliases — map a chassis serial or an FQDN some source uses onto the canonical device name. The routed view fuses hostnames through them too: an IPAM or ARP name like `nas1ten24` aliased to `nas1` becomes another leg of nas1 instead of a host of its own |
| `PATCHBAY_UNMANAGED` | `dev:iface,…` | Ports the operator knows feed an unmanaged switch, shown even when too few MACs are live to infer one. A named form gives the box a label: `k8s-switch=dev:iface` |
| `PATCHBAY_LINKS` | `dev:iface=dev:iface,…` | Declared cabling. The far side may be a bare name (`sw:e1/1/24=basement-tv`) when its port is unknowable. Removing an entry removes the link — the env is the source of truth, not a one-way import |
| `PATCHBAY_WAN_NAME` | `name,…` | Provider names, one cloud node each (default `internet`) |
| `PATCHBAY_WAN_PORT` | `dev:iface,…` | Where each provider physically lands. With none declared the cloud hangs off the firewall |
| `PATCHBAY_RELATED` | `component=owner,…` | Out-of-band component ties (a BMC/CIMC/iDRAC and the server it manages) |
| `PATCHBAY_VLAN_FILTER` | `dev:iface=1+24+73,…` | Trunks with a restricted VLAN list (defaults assume trunks carry every VLAN — allowed-lists aren't readable via SNMP) |
| `PATCHBAY_CAPACITY` | `dev:iface=3G,…` | Real service capacity below the port speed; load math divides by it and the map shows both: "10G (3G)". `G`/`M` suffixes |
| `PATCHBAY_EXPECT` | `dev:iface` or `dev`,… | Conditions declared expected, silenced on the Overview's attention list: a port whose link is legitimately slow (`core1:1/0/16`), or a whole device (`hyp1`) to quiet every item naming it. An alert nobody can silence trains everyone to ignore the list |
| `PATCHBAY_ROUTED_ORDER` | `name-or-vid,…` | Routed-view rail order override (ADR-0002): named networks (VLAN id or name) pin to the left in the declared order; the rest keep the computed order |
| `PATCHBAY_PANELS` | `name:size=regex,…` | Patch panels. The regex's first capture group is the panel position claimed by a port description; distinct prefixes keep panels apart; size `0` = sized by the highest position seen |

### More than one internet connection

`PATCHBAY_WAN_NAME` and `PATCHBAY_WAN_PORT` both take lists, paired by
position:

```sh
# one provider, two circuits: one cloud node with two cables to it
PATCHBAY_WAN_NAME="Example Fiber"
PATCHBAY_WAN_PORT="core1:1/0/16,core1:1/0/17"

# two providers, one port each
PATCHBAY_WAN_NAME="Example Fiber,Example Cable"
PATCHBAY_WAN_PORT="core1:1/0/16,core1:1/0/17"
```

Ports past the last provider named join that provider. Providers past the last
port have nowhere to land, so patchbay drops them and says so on the ops page.
Gateways pair with providers in the order the firewall reports them; a
provider with no matching gateway shows "no gateway reported" rather than
borrowing another one's status.

To describe several providers *and* several circuits each, declare the ports
as ordinary links with `PATCHBAY_LINKS` and name one landing port per provider
here.
