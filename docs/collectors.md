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
- **Fetch first, then write.** The transaction takes SQLite's write lock at
  your first write and holds it until the caller commits, so make every
  network call before your first write and keep the write phase free of
  I/O. Reads (`SELECT`) don't take the lock and may come at any point.
  While a collector holds the lock, every save in the web UI waits on it
  (polls never overlap: a lease in the database keeps them apart). A
  collector that interleaves requests and writes still works, but it locks
  the UI out for the length of its collection. The built-in collectors each
  fetch into memory, then write.
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
| `db.record_rates(conn, device, iface, in_bps=…, out_bps=…, in_errors=…, …)` | appends one sample | a port's rates for the load view and the port canaries; see [Reporting port rates](#reporting-port-rates) |

Link `source` values carry meaning in the UI (color = who reported, dash =
stated vs inferred) and in supersede logic (`lldp` > `unifi` >
`vsphere-hint` > `fdb-uplink`; `declared` is operator truth). Supersede
matches on port; `unifi` rows are also matched to `lldp` rows by device
pair, because a controller reports the operator's port label where SNMP
reports the ifName. A new discovery protocol should reuse an existing
tier or discuss a new one.

The `fdb` table carries a `source` column: each collector that writes MAC-table
rows owns them and refreshes with `DELETE FROM fdb WHERE source = '<name>'`
followed by inserts — never an unscoped delete, and never when the fetch that
feeds them failed or came back empty. It also carries `vlan`, the VLAN the MAC
was learned in, and that is part of the key: a MAC on a trunk is one row per
VLAN it talks in. Write the VLAN number when your platform reports it (the
routed view places trunked hosts by it) and leave the default `0` when it
doesn't — the view then falls back to the port's access VLAN.

## Reporting port rates

A collector that can read traffic or error rates feeds two features by
writing them to `rate_history`: the topology and routed views' load mode, and
the port canaries, which flag a port whose error rate jumps far above its own
past. Both are optional. A collector that never writes a rate loses neither
its devices nor its links.

```python
db.record_rates(conn, device, iface,
                in_bps=…, out_bps=…,
                in_errors=…, out_errors=…,
                in_discards=…, out_discards=…)   # every figure optional
```

Call it once per port per poll, inside `collect()`'s transaction like any
other write. It returns `False` and writes nothing when every figure is `None`.

- **Units are per second.** `in_bps` and `out_bps` are bits per second, as
  integers: multiply a bytes-per-second figure by 8. The error and discard
  figures are events per second, as floats. `in` is traffic received on the
  port and `out` is traffic sent from it. Counters are not rates: if your
  source reports a running total, divide the difference between two polls by
  the seconds between them, and report nothing for the first poll, for a
  negative difference (a wrap or a restart), and for a gap of several missed
  polls. The UniFi collector's `_error_rates` does this.
- **Omit what you don't know, and everything when the port isn't live.** Pass
  `None` for a figure your source does not report. A source that caches the
  last reading of a powered-off device or a down port must pass `None` for all
  six: a stale rate recorded as current shows a dead link as busy, and a
  repeated error reading counts as a fresh sample. This is the same rule as
  speed and oper-status in [`upsert_interface`](#writing-into-the-model):
  rates are liveness. Zero is a measurement, not an omission.
- **Name things as you do everywhere else.** `device` and `iface` are the names
  you pass to `upsert_device` and `upsert_interface`. The views and the rules
  join on them, and the normalizer renames a device in `rate_history` when it
  canonicalizes the name.
- **Also set the interface's current rate.** `record_rates` is the history. The
  load view reads a port's current utilization from the `in_bps` and `out_bps`
  fields on `upsert_interface` and its 24-hour peak from `rate_history`. Both
  divide by the port's `speed_bps`, or by the capacity an operator declares in
  `PATCHBAY_CAPACITY`, so a port with no speed shows no load.
- **Samples age out after 7 days.** The normalizer prunes `rate_history` on
  every poll, so you never delete from it.

### How the canaries read the samples

The `port-canary` rule judges one counter at a time (`in_errors`,
`out_errors`, `in_discards`, `out_discards`) on each port whose newest sample
is under 15 minutes old. A port with no sample that recent says nothing about
now.

- **Baseline:** the 95th percentile of that counter over the past 7 days,
  leaving out the last hour so a flood never becomes its own baseline. Only
  samples where you reported the counter count, so omitting a counter you
  can't read costs nothing, and writing `0.0` for one you can't read teaches
  the baseline that the port is perfectly quiet.
- **Raise:** the rate is at least `multiplier` times the baseline (default
  100) and at least `floor` per second (default 50). A port with less than
  `warmup_h` hours of samples (default 24) is judged against the floor alone.
- **Hold and clear:** the alert holds until the rate falls below `clear` times
  the baseline (default 10).
- **Two looks:** the alert becomes active after two samples with different
  timestamps, so one bad sample is quiet. A poll that records no new sample
  is not a second look. If your source refreshes its figures less often than
  patchbay polls, record a sample only when the figures are new: the same
  reading stored twice counts as two looks.

The built-in collectors report error rates only: LibreNMS from its ports
listing, UniFi from counter differences. Nobody collects discards yet
([#86](https://github.com/dsmorgan/patchbay/issues/86)), but the rule judges
them as soon as a collector records them.

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
`tests/` has one of these per built-in collector to copy from. To check
fetch-then-write, have the stub assert `not conn.in_transaction` on every
request, as `test_collectors_fetch_before_they_write` does.
