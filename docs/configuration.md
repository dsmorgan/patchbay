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

### Snapshots

| Variable | Default | Meaning |
|---|---|---|
| `PATCHBAY_SNAPSHOT_DIR` | `snapshots/` beside the DB | Where `patchbay snapshot` and the /ops button write the self-contained HTML files (timestamped + a stable `patchbay-latest.html`) |
| `PATCHBAY_SNAPSHOT_KEEP` | `30` | Timestamped snapshots to retain (`0` = keep everything) |
| `PATCHBAY_SNAPSHOT_AT` | — | `HH:MM` local time to write one snapshot a day (the poller does it). Unset = on-demand only |
| `PATCHBAY_SNAPSHOT_DELIVER_DIR` | — | Second destination each finished snapshot is copied to (a mounted off-site share). Kept separate from the local directory so a delivery failure never costs you the snapshot; copies land under a temporary name and are renamed, so a sync client never picks up a half-written file |

A snapshot is one fully self-contained HTML file — interactive topology map,
every device and port, links, VLANs, subnets, endpoints, gateways, 24h
traffic graphs for linked ports, and the latest device configs with secrets
redacted. It needs no network to open. Point your off-host sync at the
snapshot directory; `patchbay-latest.html` is the stable name to serve or
ship. Configs are scrubbed, but the file still describes a real network —
treat it as sensitive.

## Data sources

Each collector activates when its variables are set and is skipped otherwise.

| Source | Variables |
|---|---|
| LibreNMS | `LIBRENMS_URL`, `LIBRENMS_TOKEN` |
| Oxidized | `OXIDIZED_URL` (its REST API, such as `http://host:8888`) |
| phpIPAM | `IPAM_URL`, `IPAM_APP_ID`, `IPAM_TOKEN` |
| UniFi Network app | `UNIFI_URL`, `UNIFI_USER`, `UNIFI_PASS` |
| OPNsense | `OPNSENSE_HOST`, `OPNSENSE_API_KEY`, `OPNSENSE_API_SECRET` — see [OPNsense privileges](#opnsense-api-user-privileges) |
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

The interfaces overview endpoint (`interfaces/overview/export`) does not map
to a named privilege in current OPNsense releases; if patchbay logs a 403 for
it, add **Interfaces: Assign network ports** as a fallback — it is the
broadest read-only interfaces privilege available.

`OPNSENSE_HOST` accepts a bare hostname (`opnsense-rtr1.bub.lan`, defaults to
HTTPS) or a full URL with scheme (`http://opnsense-rtr1.bub.lan`) for
installations that do not terminate TLS on the management interface.

## Operator declarations

Facts no protocol can discover. All optional; all use the pattern
`device:interface` for port references. Malformed entries are skipped.

| Variable | Format | Meaning |
|---|---|---|
| `PATCHBAY_ALIASES` | `alias=canonical,…` | Identity aliases — map a chassis serial or an FQDN some source uses onto the canonical device name |
| `PATCHBAY_UNMANAGED` | `dev:iface,…` | Ports the operator knows feed an unmanaged switch, shown even when too few MACs are live to infer one |
| `PATCHBAY_LINKS` | `dev:iface=dev:iface,…` | Declared cabling. The far side may be a bare name (`sw:e1/1/24=basement-tv`) when its port is unknowable. Removing an entry removes the link — the env is the source of truth, not a one-way import |
| `PATCHBAY_WAN_NAME` | `name,…` | Provider names, one cloud node each (default `internet`) |
| `PATCHBAY_WAN_PORT` | `dev:iface,…` | Where each provider physically lands. With none declared the cloud hangs off the firewall |
| `PATCHBAY_RELATED` | `component=owner,…` | Out-of-band component ties (a BMC/CIMC/iDRAC and the server it manages) |
| `PATCHBAY_VLAN_FILTER` | `dev:iface=1+24+73,…` | Trunks with a restricted VLAN list (defaults assume trunks carry every VLAN — allowed-lists aren't readable via SNMP) |
| `PATCHBAY_CAPACITY` | `dev:iface=3G,…` | Real service capacity below the port speed; load math divides by it and the map shows both: "10G (3G)". `G`/`M` suffixes |
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
