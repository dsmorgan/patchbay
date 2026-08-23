# Contributing to patchbay

Contributions are welcome, especially collectors for gear the reference
deployment doesn't have: Cisco or UniFi switches, Proxmox, XCP-ng, NetBox,
MikroTik, Omada, and so on.

## Ground rules

- **No real network data in the repo, ever.** No hostnames, IPs, credentials,
  serials, configs, or captured payloads from a real site. Test fixtures use
  fake names, RFC 5737 (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`)
  and RFC 3849 (`2001:db8::/32`) addresses, and scrubbed serials. This is a
  blocking check on every PR.
- **patchbay aggregates; it does not poll.** If LibreNMS, Oxidized, or a native
  API already does something well, integrate with it instead of reimplementing
  it in core.
- **Read-only** until the operations phase. Nothing merged before then may
  write to a network device.

## Adding support for new gear

This usually means one of three things:

1. **A collector** for a new source. See
   [docs/collectors.md](docs/collectors.md). An out-of-tree collector — your
   own package, published through the entry point — needs no PR at all.
   In-tree contributions need a sanitized fixture test.
2. **A registry entry** for a vendor quirk: a wrapped-speed tier, an
   OS-to-role mapping, an Oxidized config-dialect parser. These are small PRs
   and very welcome.
3. **An IPAM or NMS integration.** Deep links and graph proxying follow the
   patterns in `web.py` (`_ipam_link`, `/graph`). Keep tokens server-side.

## Development

```sh
uv venv && uv pip install -e '.[web,dev]'
pytest                                   # unit + page-smoke tests, no network
patchbay web                             # against your own site .env
```

For background, read [docs/architecture.md](docs/architecture.md) for the spec
and roadmap, [docs/pluggability.md](docs/pluggability.md) for the component
model, and [CLAUDE.md](CLAUDE.md) for the invariants the code relies on — worth
reading even if you're not an AI.

### Testing degradation, and the bug class it catches

Graceful degradation is a promise this project makes, so test it by leaving one
integration out at a time: a fresh data directory, an `.env` with exactly one
source's variables removed, a first poll from zero, and every page fetched.
Nobody runs all six sources on day one, so most real installs look like one of
these.

Then run each scenario the other way: add the missing integration back and poll
again. **The result must match the model a clean install produces, byte for
byte.** That comparison is where the interesting bugs live, because it catches
rows that were correct when written and turned wrong once a better source
appeared. Three such bugs turned up this way, and no normal test run could
reach any of them:

- a firewall VM kept vCenter's generic `Network adapter 1..6` alongside its
  real `vmx0..5`, permanently, once its own collector was configured;
- MACs identified as a known device's NIC stayed duplicated as anonymous
  endpoints;
- MACs learned on a port an operator had declared became endpoints instead of
  belonging to the device at the far end.

All three share one cause: a collector must retract what it supersedes, not
merely stop writing it. Upsert-only stores rot — nothing lies, things just
never leave. When you add a collector, ask what it makes obsolete.

Test a first poll against an empty database on its own, too. Anything ordered
after a step that should precede it looks correct forever afterward, because
every later poll finds the previous poll's output already on disk.

## Style

Match the surrounding code. Comments explain constraints the code can't show,
not what the next line does. Keep commits small and single-topic.
