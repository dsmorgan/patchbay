# Writing a collector

A collector maps one data source into patchbay's shared model. Everything a
collector needs to know is in this file; nothing in core has to change to add
one.

## The contract

```python
class Collector(Protocol):
    name: str                                              # unique slug
    def configured(self, settings) -> bool: ...            # env vars present?
    def collect(self, settings, conn) -> str: ...          # one poll cycle
```

- `configured` decides activation. A deployment that doesn't set your
  variables never runs your code — absence must be silent, not an error.
- `collect` runs inside a transaction the caller manages: it is committed
  after your return and **rolled back if you raise**. Raise freely on API
  failure — a half-finished collection must never land. Never call
  `conn.commit()` yourself.
- Return a one-line human summary (`"5 devices, 209 ports"`); it shows on
  `/ops` and in poll logs.
- Timeouts on every network call. A hung collector stops the whole poll.

## Four rules that keep the model honest

1. **Own your rows.** Write with `source=<your name>`, and never touch another
   collector's rows.
2. **Prune what your source stops reporting**, but never on an empty or failed
   response. An upsert-only store rots: no row lies, rows just never leave.
   Copy the pattern from the vsphere collector's VM retirement — prune only
   after a successful, non-empty listing, scoped to your source. Link evidence
   also ages out on its own (a 2-hour TTL in the normalizer), but same-poll
   pruning is more precise.
3. **Retract what a better source supersedes.** Rule 2 covers your source
   changing its mind. This rule covers a row that was right when you wrote it
   and became wrong once another collector arrived — for example, generic
   hypervisor labels for a firewall VM's NICs, which that firewall's own
   collector later names properly. Operators add integrations over time, so
   write the retraction when you write the row, not after someone reports two
   of everything. [CONTRIBUTING.md](../CONTRIBUTING.md) describes how to test
   for this.
4. **Never resolve conflicts.** If your source names a device differently from
   another source, write your name anyway. The normalizer canonicalizes names,
   merges duplicates by source priority, and supersedes weaker link evidence.
   A collector that tries to be clever about identity fights it.

## Writing into the model

Use the `db` helpers — they upsert by natural key so repeated polls converge:

| Helper | Key | Notes |
|---|---|---|
| `db.upsert_device(conn, name=…, source=…, **fields)` | name | returns device id; `None` fields mean "no opinion" and never overwrite |
| `db.upsert_interface(conn, device_id=…, name=…, **fields)` | (device, name) | speeds in bps; use `""` (not `None`) to clear a text field |
| `db.upsert_link(conn, a_device=…, a_interface=…, b_device=…, b_interface=…, source=…)` | whole row | stored direction-normalized; `"?"` = unknown port |
| `db.upsert_endpoint(conn, mac=…, source=…, **fields)` | mac | MAC lowercased |
| `db.upsert_subnet(conn, cidr=…, source=…, **fields)` | cidr | |
| `db.save_raw(conn, source=…, endpoint=…, payload=…)` | — | raw API payload for debugging; auto-expires after 7 days |

Link `source` values carry meaning in the UI (color = who reported, dash =
stated vs inferred) and in supersede logic (`lldp` > `vsphere-hint` >
`fdb-uplink`; `declared` is operator truth). A new discovery protocol should
reuse an existing tier or discuss a new one.

## Configuration

Core settings arrive as the `settings` dataclass. A third-party collector's
own knobs are plain environment variables — read them via `os.environ` in
`configured()`/`collect()` and document them in your package's README. Keep
the naming convention: `<SOURCE>_URL`, `<SOURCE>_TOKEN`, ….

## Packaging (out-of-tree)

```toml
# pyproject.toml of patchbay-proxmox
[project.entry-points."patchbay.collectors"]
proxmox = "patchbay_proxmox:collector"       # instance or zero-arg factory
```

`pip install patchbay-proxmox` next to patchbay and it is discovered at
startup. A plugin that fails to import is reported and skipped — it can't
take the poll down.

In-tree collectors live in `src/patchbay/collectors/` and self-register with
`register()` at import.

## Vendor quirks go in registries, not conditionals

- 32-bit wrapped ifSpeeds: add the tier to `WRAPPED_SPEED`
  (`collectors/librenms.py`).
- OS-to-role mapping: `OS_ROLE` (`collectors/librenms.py`).
- Config-parsing dialects for VLAN membership: add a parser to `PARSERS`
  (`collectors/oxidized.py`), keyed by the Oxidized model name. A parser takes
  config text and returns `(vlan_names, port_vlan_rows, port_roles)`. Parse
  defensively: a parser that returns nothing keeps the last good data instead
  of wiping it.

## Testing

Capture a **sanitized** API payload (fake hostnames, RFC 5737/3849 addresses,
scrubbed serials — never real site data; see the repo rule in README) as a
JSON fixture, then drive `collect()` against an in-memory SQLite DB with the
HTTP layer stubbed. Assert on the resulting model rows, not on internals.
`tests/` has one of these per built-in collector to copy from.
