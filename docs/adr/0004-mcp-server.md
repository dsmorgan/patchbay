# ADR-0004 — Read-only MCP server

*Status: accepted (2026-11-26) · Issue: [#155](https://github.com/dsmorgan/patchbay/issues/155)*

## Context

An AI assistant can troubleshoot the network if it can ask patchbay what
it knows, through the Model Context Protocol (MCP). MCP revision 2026-07-28
defines two transports: stdio, where the client launches the server, and
Streamable HTTP.

patchbay runs as web UI and poller containers sharing a SQLite database.
The UI is LAN-only, gated by session cookies and the
`PATCHBAY_ALLOWED_HOSTS` allowlist. Most pages build their data in
framework-free functions; the device, alerts, and config pages build theirs
inside the route handler. patchbay stays read-only until 1.0 (#24).

The primary user is an assistant on a laptop that reaches the web UI over
the LAN but can't SSH to the container host.

## Decision 1 — HTTP first, at `/mcp`

Streamable HTTP, mounted at `/mcp` on the existing FastAPI app, is the
primary transport: the assistant reaches it the way the browser does.

`patchbay mcp` serves the same tools over stdio, for a client on the host
or a local demo database. stdio needs no gate: whoever can launch it can
already read the database.

## Decision 2 — a static bearer token

Cookie sessions don't fit an MCP client, so `/mcp` has its own gate:

- Unless `PATCHBAY_MCP_TOKEN` is set, `/mcp` answers `404`.
- patchbay's gate checks the `Authorization: Bearer` header in constant
  time, as `check_password` does, before the SDK sees the request. A
  missing or wrong token gets `401`.
- The Host allowlist still applies, and the SDK's DNS-rebinding check gets
  the same host list.
- Nothing logs the token.

patchbay doesn't require TLS for `/mcp`: on a LAN-only install the token
crosses the same network as the UI's session cookie, and TLS belongs to
the reverse proxy when there is one.

A static token skips the spec's OAuth 2.1 flow; most clients accept a custom
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

`find` is the only new query.

## Decision 4 — what leaves the host

Every result leaves the host, as a snapshot does. Tools build results from
named fields, never whole rows, so a new column never leaks. No tool
returns the session secret or redaction key, a transport URL,
`raw_payloads`, a setting's value, or a token or password hash.

Config content is opt-in. `config_changes` returns metadata only.
`config_diff` is registered only with `PATCHBAY_MCP_CONFIG_DIFFS=on`, so the
assistant never plans around a tool it can't call; `config_changes` says
when diffs are off and names the variable. Diffs never carry whole files and
pass through `scrub_config`, the snapshot's stricter scrubber, Oxidized text
included.

## Decision 5 — read-only by construction

Tools open the database `mode=ro`, as `/healthz` does, and never run
`db.init` or `stamp_first_seen`. Every tool carries `readOnlyHint`. Until
1.0, no tool changes settings, silences, or devices; adding one takes a new
ADR.

## Decision 6 — the dependency

Use the official SDK, `mcp` 2.3.0 (October 2026), pinned `>=2.3,<3`. The
default image installs it, and the endpoint stays off until the token is
set. It adds 16 packages to today's 21, including compiled `cryptography` and
`rpds-py` and a second HTTP client, `httpx2`. Version 2 (July 2026) broke
1.x code, so the upper bound makes the next major version deliberate.

## Alternatives considered

- **stdio first, through `docker compose exec` over SSH.** No new network
  surface, but the assistant can't SSH to the host, and Docker on a typical
  NAS needs root.
- **OAuth now.** Needs identity-provider features homelabs rarely have.
- **A JSON API behind an OpenAPI-to-MCP bridge.** Two surfaces, and
  generated tool descriptions explain less.
- **A generic SQLite MCP server.** No redaction, sees `app_state` secrets,
  and loses the rules that give tables meaning, such as IPAM being identity,
  never liveness.

## Consequences

- The web app gains a surface outside the cookie gate, with its own tests.
- Extracting the device, alerts, and config builders refactors the UI with
  no visible change; page tests guard it.
- A hostile DHCP hostname can carry text aimed at the assistant. Read-only
  tools limit what it can make patchbay do, not what the assistant does
  elsewhere.
- The image grows, and Dependabot tracks the new packages through `uv.lock`.

## Open questions

None.

## Prototype plan

1. Mount `/mcp` with the token gate and `freshness`, `find`, and `alerts`,
   against the `patchbay demo` database. Test that `/mcp` answers `404`
   without `PATCHBAY_MCP_TOKEN` and `401` with a wrong token.
2. Document a client config: the URL `http://patchbay.example.net:8080/mcp` and
   the header `Authorization: Bearer <token>`.
3. Extract the device builder; add `device`, `links`, `drift`, and `routed`.
4. Add the config tools, with tests that `config_diff` is absent unless
   enabled and that no tool returns a seeded secret.
5. Answer the example questions from #155 and post the transcripts there.
