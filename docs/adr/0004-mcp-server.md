# ADR-0004 — Read-only MCP server

*Status: proposed (2026-11-26) · Issue: [#155](https://github.com/dsmorgan/patchbay/issues/155)*

## Context

An AI assistant can troubleshoot the network if it can ask patchbay what
it knows, through the Model Context Protocol (MCP). MCP revision 2026-07-28
defines two transports: stdio, where the client launches the server, and
Streamable HTTP.

patchbay runs as two containers, web UI and poller, sharing a SQLite
database. The UI is LAN-only, gated by `PATCHBAY_AUTH` session cookies and
the `PATCHBAY_ALLOWED_HOSTS` allowlist. Most pages build their data in
framework-free functions such as `attention_items` and `drift_report`; the
device, alerts, and config pages build theirs inside the route handler.
patchbay stays read-only until 1.0 (#24).

## Decision 1 — stdio first, through `patchbay mcp`

`patchbay mcp` serves MCP over stdio. The client runs it inside the web
container with `docker compose exec -T patchbay patchbay mcp`, prefixed with
`ssh nas.example.net` from a laptop. Nothing new listens or runs between
sessions, and the process shares the container's network, so it reaches
oxidized-web.

Streamable HTTP at `/mcp` on the existing app comes second, off by default:
for clients that can't launch a subprocess, and hosts where only root can
use Docker. Both transports serve one tool module.

## Decision 2 — authentication follows the transport

The spec says stdio servers take credentials from the environment and skip
MCP authorization. Whoever can `docker compose exec` can already read the
database, so SSH and Docker access are the gate.

Cookie sessions don't fit an MCP client, so `/mcp` answers only when
`PATCHBAY_MCP_TOKEN` is set. It bypasses the cookie gate, checks that bearer
token through the SDK's `TokenVerifier`, and keeps the Host allowlist. A
static token skips the spec's OAuth 2.1 flow; most clients accept a custom
`Authorization` header. OAuth through the configured OIDC provider waits
until homelab identity providers support what MCP requires: resource
indicators, and client ID metadata documents or dynamic registration.

## Decision 3 — nine tools over the page builders

Each tool returns structured data, typed by its return annotation, and
calls the function its page uses, so answer and page can't drift apart.
Inline handlers get a builder extracted into a framework-free module first,
and `build_topology_graph` returns a dict before the page serializes it.

| tool | answers | page |
|---|---|---|
| `find(query)` | Where a MAC, IP, hostname, or alias is: device, port or SSID, IPAM record, and whether each map draws it | none |
| `device(name)` | Ports, VLANs, endpoints per port, links, tunnels, guests | `/device` |
| `links(slow_only)` | Topology edges with speed, evidence, and 24-hour peak | `/topology` |
| `alerts(history)` | Open alerts with severity, first seen, held or silenced; optionally events | `/alerts` |
| `drift()` | Undocumented, unseen, and conflicting addresses | `/drift` |
| `routed()` | Networks, their routers and hosts, default route, tunnels | `/routed` |
| `freshness()` | Last poll and each source's age | top bar |
| `config_changes(device, since)` | Which configs changed, when, by how many lines | `/configs` |
| `config_diff(device, revision)` | One change as a scrubbed diff | `/configs` |

`find` is the only new query; a UI search box can share it later.

## Decision 4 — what leaves the host

Treat every result as leaving the host, as a snapshot does: the assistant's
provider sees it. Tools build results from named fields, never whole rows,
so a new column never leaks by accident. No tool returns the session secret
or redaction key, a transport URL, `raw_payloads`, a setting's value, or the
password hash.

Config content appears only as diffs, never whole files. Every diff passes
through `scrub_config`, the snapshot's stricter scrubber, including Oxidized
text, which the UI shows unredacted.

## Decision 5 — read-only by construction

The server opens the database `mode=ro`, as `/healthz` does, and never runs
`db.init` or `stamp_first_seen`. Every tool carries `readOnlyHint`. Until
1.0, no tool changes settings, silences, or devices; adding one takes a new
ADR.

## Decision 6 — the dependency

Use the official SDK, `mcp` 2.3.0 (October 2026), pinned `>=2.3,<3` in a
new `mcp` extra that the default image installs (open question 3). It
reuses FastAPI's Starlette, Pydantic, and AnyIO, and adds 16 packages to
today's 21, including compiled `cryptography` and `rpds-py` and a second
HTTP client, `httpx2`. Version 2 (July 2026) renamed `FastMCP` to
`MCPServer` and broke 1.x code, so the upper bound makes the next major
version deliberate.

## Alternatives considered

- **HTTP only.** Reaches any LAN client, but opens a network surface and
  needs tokens before the first tool works.
- **A JSON API behind an OpenAPI-to-MCP bridge.** Two surfaces, and
  generated tool descriptions explain less.
- **A generic SQLite MCP server.** No redaction, sees `app_state` secrets,
  and loses the rules that give tables meaning, such as IPAM being identity,
  never liveness.

## Consequences

- Extracting the device, alerts, and config builders refactors the UI with
  no visible change; page tests guard it.
- Names come from the network, so a hostile DHCP hostname can carry text
  aimed at the assistant. Read-only tools limit what it can make patchbay
  do, not what the assistant does with other tools.
- The image grows, and Dependabot tracks the new packages through `uv.lock`.

## Open questions

1. Are config diffs on by default, or opt-in through an environment
   variable?
2. Does HTTP ship with stdio, or wait for a client that needs it?
3. Does the MCP extra ship in the default image or a separate tag?
4. Is a static bearer token acceptable, or does HTTP wait for OAuth?

## Prototype plan

1. Add the `mcp` extra and `patchbay mcp` with `freshness`, `find`, and
   `alerts`, against the `patchbay demo` database.
2. Extract the device builder; add `device`, `links`, `drift`, and `routed`.
3. Add the config tools, with a test that seeds a secret and checks that no
   tool returns it.
4. Connect a client through `docker compose exec`, answer the example
   questions from #155, and post the transcripts there.
