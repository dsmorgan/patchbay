# ADR-0003 — Alerting: rules, transports, silences, canaries

*Status: accepted (2026-10-07) · Issue: [#22](https://github.com/dsmorgan/patchbay/issues/22) · Folds in: #49 (expected tunnel missing) · Work split: see the checklist on #22*

*Each decision lists the options considered; the marked recommendation is
the decision.*

## Context

patchbay already knows when something is wrong. The Overview's attention
list (#13, #28) computes a flat list of items worth a look, each with a
stable key, a category, a severity, and a poll-recorded first-seen time.
The engine lives in `attention.py`, imports no web framework, and runs on
the poll path so that "when did patchbay notice" is a fact about polling,
not about who was looking. Phase 6 adds the half that is missing: nobody
is told. An unplugged AP sits on the Overview until someone opens it.

The roadmap scopes phase 6 as alert rules plus a transport, a snapshot on
critical alerts, and port-counter canaries. Issue #49 folded in one more
rule: a configured VPN tunnel that stops running vanishes from the maps by
design, so its absence has to be declared and alerted rather than drawn.

What exists, and what this ADR builds on:

| piece | state today |
|---|---|
| rule engine | `attention_items()`: slow links, IPAM drift, stale sources; device state deliberately absent because the Overview's cards are the device-state UI |
| item identity | `key` per item, first-seen stamped on the poll path, forgotten when the item clears |
| suppression | `PATCHBAY_EXPECT`: a port or a device declared expected silences every item naming it; a declaration, so editable from /ops with env winning over the DB |
| poll cycle | a fresh process per cycle; collectors, normalize, first-seen bookkeeping, then the daily snapshot after the transaction commits |
| snapshots | `write_snapshot()` with local retention (`PATCHBAY_SNAPSHOT_KEEP`) and atomic off-host delivery |
| counters | `rate_history` holds in/out bits per second per port per poll, pruned at 7 days; no error or discard counters are collected |
| state the rules can read | devices (status, last_seen, role), interfaces (oper status), links (source), gateways (status, loss, delay), tunnels (status, last_handshake), config_revisions |

Two constraints shape every decision below. First, patchbay is read-only
until phase 7 and stays site-agnostic: nothing in the repo knows a real
site. Second, LibreNMS already does threshold alerting well; patchbay's
rules cover what a single-source NMS cannot see.

## Decision 1 — one engine, two surfaces

The alert engine grows out of `attention.py`, not beside it. The attention
list and the notification channel evaluate the same rules; they differ in
delivery and in which categories they show.

**Options considered**

- **A. Extend `attention_items()` in place.** Every rule returns items the
  same way; the engine adds state (raised, cleared) and dispatch on top.
  Device state joins the item list with a category the Overview hides.
- **B. A separate alert module with its own rule set.** Cleaner start, but
  two engines drift: a condition the Overview shows but alerting ignores,
  or the reverse, and `PATCHBAY_EXPECT` has to be applied twice.

**Recommendation: A.** `attention.py` keeps its contract (key, category,
severity, text, href, first_seen) and gains categories. A new
`alerting.py` owns the state machine and dispatch, calls
`attention_items()`, and runs on the poll path where `record_first_seen`
runs today. Surfaces filter by category: the Overview keeps hiding device
state, the alert channel sends it.

**Lifecycle.** An alert is stateful. Each poll, the engine diffs the
current item set against the `alerts` table:

| transition | condition | effect |
|---|---|---|
| raise | key present now, absent before | row inserted as `pending`; notified once it has held for the rule's `for` count of polls (default 1, so the AP case notifies within one cycle) |
| active | key held for `for` polls | notify on raise; re-notify every `remind` interval (default: never) |
| clear | key absent now, present before | notify the clear, row marked cleared, kept for history |
| event | a rule that fires once (a config changed) | notify and clear in the same cycle |

Dispatch happens after the poll transaction commits, the way the daily
snapshot already does, so an unreachable webhook never holds the database.
A failed delivery is retried next cycle; three consecutive failures raise a
`source`-category item about the transport itself.

**Inhibition.** When a device is down, every link-down item at its ports is
suppressed, so an unplugged switch reports once, not once per cable. This
is a fixed rule, not configuration.

## Decision 2 — configuration lives in the UI, backed by the database

Rules, transports, silences, and their parameters are rows in the database,
edited on the alerts page. No alerting setting is env-only.

**Options considered**

- **A. Declaration strings**, the `PATCHBAY_*` pattern: editable from /ops
  with env winning. Works for one-line facts, not for a rule with four
  parameters and a route, and the env-wins rule makes every field
  read-only the moment a site sets it.
- **B. Database rows edited in the UI, no env involvement.** The alerts
  page is the authority. The database already holds positions and
  declarations, so this is not a new kind of state.
- **C. B, plus optional env seeding** of transports for headless or
  infrastructure-as-code deployments, applied only when the table is empty.

**Recommendation: B for v1.** Seeding (C) can be added later without
changing the model; it is listed under "not in v1" so it is a conscious
omission.

**Tables**

| table | holds |
|---|---|
| `alert_rules` | one row per built-in rule: enabled, severity override, parameters as JSON, route |
| `alert_transports` | name, kind (webhook preset), URL, extra fields as JSON, enabled, last result |
| `alert_silences` | scope (key pattern, device, device:port, or category), until (null = permanent), reason, created_by, created_at |
| `alerts` | current and cleared alerts: key, rule, severity, state, raised_at, cleared_at, last_notified_at, text, href |
| `alert_events` | history: raised, notified, cleared, silenced, delivery failed; pruned by age and count |

**Secrets.** A webhook URL is a credential. It is stored in the database
(the site's private file), shown masked on the alerts page after it is
saved, never logged, never written to a snapshot: the snapshot writer skips
every `alert_*` table and the scrubber treats the `alerts` text as
leakable like everything else. This is the first secret the database
holds, which is why it is called out.

**Who.** Silences record `created_by`: the OIDC identity when auth is
OIDC, otherwise "operator". Auth stays a gate, not an identity system;
this records what the gate knows and nothing more.

## Decision 3 — transports: one primitive, several presets

The transport primitive is an HTTP webhook. A preset shapes the request for
a known receiver. A site configures as many transports as it wants and
routes each rule, or each severity, to one.

**Options considered**

| option | shape | notes |
|---|---|---|
| **Uptime Kuma push monitor** | `GET {url}?status=up\|down&msg=…` | Stateful: Kuma marks the monitor down while patchbay says down, and fans out through Kuma's own notification providers. One Kuma monitor per route. The reference deployment already uses push monitors as dead-man switches. |
| **Generic webhook** | `POST` JSON with the full alert object | For anything else: n8n, Home Assistant, a script. |
| **ntfy** | `POST` body text, `Title`/`Priority`/`Tags` headers | Phone push without a chat platform. |
| **Discord webhook** | `POST` JSON `{"content": …}` | A generic webhook with one fixed key. |
| **Email (SMTP)** | not a webhook | Needs credentials, TLS, and a retry story. |

**Recommendation.** v1 ships two presets: `kuma` and `generic`. Chat
platforms, ntfy, and Discord are each a template away and follow when
someone asks. Email is out of v1; a site that wants email points Kuma at
it.

**Why Kuma first.** It is the one preset that turns patchbay's stateful
alerts into a stateful monitor. A route to a Kuma transport pushes `down`
while any active alert on that route exceeds the route's minimum severity,
and `up` once the last one clears, every poll, so the monitor also acts as
a dead-man switch for the poller. The generic preset is event-style: one
request on raise, one on clear.

**Routing.** A route is a transport plus a minimum severity. A rule has a
default route; a site can override per rule. The default route for a fresh
install is "the first transport, severity warn and above", so configuring
one transport is enough to start.

**Message content.** Every message carries the severity, the rule, the
item's text, how long it has been firing, and a link back to the page that
owns the answer (the item's `href` on the configured public URL). A
`test` button on each transport sends a sample and shows the response.

## Decision 4 — rules: a built-in catalog with parameters, no rule language

**Options considered**

- **A. Fixed rules**, each with enable, severity, and route. Nothing to
  write, nothing to get wrong, nothing to express that the catalog lacks.
- **B. A rule language**: thresholds over any column, user-defined. This
  is LibreNMS alerting again, and a second place to maintain it.
- **C. A catalog where each rule exposes a few parameters** (a stale
  window, a multiplier, a role list). A rule is still code; a site tunes
  it.

**Recommendation: C.** The catalog is the contract. A new rule is a pull
request with tests, the way a new collector is.

**v1 catalog**

| rule | category | default severity | fires when | parameters |
|---|---|---|---|---|
| device down | `device` | crit | a device in a watched role reports a down state, or goes stale | roles (default: switch, ap, firewall, router, hypervisor); `for` (default 1 poll) |
| link down | `link` | warn | a port that carries a stated link (lldp, unifi, declared) is oper down and its device is up | `for` (default 1) |
| gateway degraded | `gateway` | crit when down, warn on loss | `gateways.status` is not up, or loss exceeds a percentage | loss threshold (default 5 %) |
| expected tunnel missing | `tunnel` | warn | a tunnel declared expected is absent or not up | the expected list lives on the alerts page as declarations (device, type, name), never keys |
| stale source | `source` | warn | exists today | minutes (default `STALE_MIN`) |
| slow link | `link` | warn | exists today | none; silence per port |
| IPAM drift | `ipam` | warn | exists today | none |
| config changed | `config` | info, event | a new `config_revisions` row landed | none |
| port canary | `port` | warn | Decision 5 | floor, multiplier, `for` |
| snapshot failed | `source` | warn, event | the daily or alert-triggered snapshot failed or was not delivered | none |

Every rule ships enabled with its default route except `config changed`
and `slow link`, which ship enabled for the attention list and routed
nowhere, so they inform without paging.

**Rules considered and left out of v1**: new device appeared (useful for
security, noisy on a lab that spins VMs); VLAN drift between sources (the
drift page shows it; alerting it needs the canonical-source rule the page
does not yet have); interface error rate thresholds in absolute terms
(LibreNMS does this).

## Decision 5 — port-counter canaries

The roadmap asks for a port whose error or discard rate jumps orders of
magnitude above its own baseline, because an egress-discard flood on one
trunk is how VLAN flooding announces itself.

**Collection.** LibreNMS exposes per-port rates already computed between
its own polls: `ifInErrors_rate`, `ifOutErrors_rate`, `ifInDiscards_rate`,
`ifOutDiscards_rate`. The collector adds them to the ports query and the
sample lands in `rate_history` as four new columns beside the bit rates,
so canaries ride the existing 7-day retention and index. UniFi reports
absolute counters per port, which needs a delta against the previous poll;
that is a follow-up, and the rule works with whatever ports have samples.

*Corrected in #64:* only the error rates are on LibreNMS's `ports` table.
The discard rates live in `ports_statistics` (split out in 2018), which the
ports listing cannot select: it validates `columns` against `ports` and
refuses the whole request on an unknown one. The only route that returns
them is per port (`/ports/{id}?with=statistics`). The collector therefore
fills the two error columns; the discard columns exist and stay NULL until
a follow-up collects them.

**Baseline options**

| option | how | why not, or why |
|---|---|---|
| **A. Absolute threshold** | fire above N discards per second | What LibreNMS does. Wrong for the ask: a trunk that always discards 20/s is fine, a port that went from 0 to 20/s is the story. |
| **B. The port's own percentile** | baseline = p95 of the port's samples over the last 7 days, excluding the last hour; fire when the current rate is at least `multiplier` × baseline and at least `floor` | Matches "its own baseline". Cheap: compute only for ports above the floor. |
| **C. Decade jump** | compare log10(rate + 1) now with the baseline's log10; fire on ≥ 2 decades | Same as B with the multiplier fixed at 100; the log framing is clearer to explain but no more useful to tune. |
| **D. z-score or EWMA** | statistical deviation from a running mean | Breaks on series that are zero almost always and bursty otherwise, which is exactly what error counters look like. |

**Recommendation: B**, with the roadmap's "orders of magnitude" as the
default multiplier (100) and a floor so a port going from 0 to 2 discards
per second never fires. Defaults, all editable on the rule:

| parameter | default | reason |
|---|---|---|
| floor | 50 packets per second | below this nobody notices and the baseline math is noise |
| multiplier | 100 | two orders of magnitude; a trunk's normal chatter stays quiet |
| `for` | 2 polls | 10 minutes at the default cadence: a single bad sample is not a flood |
| clear below | 10 × baseline | hysteresis, so a flood that oscillates does not raise and clear every poll |
| warm-up | 24 hours of samples | a port with no history is judged against the floor only |

The item key is `port:canary:{device}:{iface}:{counter}`; the text names
the counter, the current rate, and the baseline, so the reader knows why
it fired without opening a graph. The href is the port's page, where the
linked LibreNMS graph shows the shape.

## Decision 6 — silences, and what becomes of `PATCHBAY_EXPECT`

Today one declaration silences both the attention list and, once it
exists, the alert channel, so that the two surfaces never disagree about
what is fine. Issue #22 kept that property. This ADR keeps the property
and changes the mechanism: silences become first-class rows with scope,
expiry, and a reason, and `PATCHBAY_EXPECT` becomes one source of them.

**Options considered**

- **A. Extend `PATCHBAY_EXPECT`** with more syntax (categories, expiry).
  A comma-separated string is the wrong shape for anything with a date or
  a reason, and env-wins makes it read-only on a site that set it once.
- **B. A silence table edited on the alerts page**, with `PATCHBAY_EXPECT`
  entries read as permanent silences for compatibility, shown in the same
  list as read-only env-sourced rows.
- **C. B, and drop `PATCHBAY_EXPECT`** after a deprecation release.

**Recommendation: B now, C later.** A silence has: a scope (`device`,
`device:port`, a category, or an exact item key), `until` (null for
permanent; a snooze picks a duration), a reason, and who set it. Every
surface applies silences the same way: a silenced item is still computed
and stored, marked `silenced`, hidden from the attention list, and never
dispatched. Silenced items show on the alerts page under their own tab, so
a silence is never invisible. When a silence expires, the next poll
re-evaluates and the item raises as new if it still holds.

A `silence` button on every active alert creates one in place, defaulting
to 24 hours. That is the interaction most sites will use most.

## Decision 7 — snapshot on critical

**Options considered**

- **A. Every new critical alert takes a snapshot.** Simple. Without a
  limit, a flapping device fills the disk and the off-host share with
  copies of the same picture.
- **B. A, with a cooldown**: at most one alert-triggered snapshot per
  configured window, default 1 hour, whatever raised it.
- **C. Only criticals on core devices.** Needs a notion of "core" the
  model lacks; the topology's tiers are a layout hint, not a declaration.

**Recommendation: B.** Alert snapshots are named
`patchbay-YYYYMMDD-HHMMSS-alert.html`, kept under their own count
(default 10) so they never evict nightlies, and delivered off-host through
the same atomic path. The cooldown and the keep count are parameters on
the alerts page. The snapshot runs after the poll commits, like the daily
one, and its failure is the `snapshot failed` event.

## Decision 8 — the alerts page

The `/alerts` route exists and shows the full attention list. It grows
tabs: **Active**, **Silenced**, **History**, **Rules**, **Transports**,
**Silences**. The Overview's attention block is unchanged. The summary
strip on `/alerts` gains the count of active alerts by severity and the
delivery state of each transport (last sent, last error). Everything on
the page is a plain form, the way /ops declarations are; no client-side
framework arrives with this phase.

## Decision 9 — what v1 does *not* do

- No env seeding of transports or rules (Decision 2, option C).
- No email transport; Kuma or a generic webhook reaches email.
- No user-defined rules or expressions.
- No per-user notification preferences; routes are per site.
- No UniFi error counters; LibreNMS-polled ports only, until the delta
  follow-up.
- No acknowledgement state distinct from silence. "I know" is a 24-hour
  silence with a reason.
- No alerting on the snapshot's own content.

## Consequences

- `attention.py` stays the single source of what is wrong; `alerting.py`
  is the single source of who hears about it. A new rule is one function
  and one catalog row, with tests, and shows on the Overview and in the
  channel at once.
- The database gains five tables and, for the first time, a secret. The
  snapshot writer and the scrubber must be taught about them before the
  first transport is saved, not after.
- `rate_history` grows four columns. Sample volume is unchanged; row width
  grows by four small numbers.
- The poll cycle gains HTTP calls after commit. A slow webhook delays the
  cycle's exit, not its data. The collector timeout pattern applies.
- `PATCHBAY_EXPECT` becomes legacy input to the silence list. Its help
  text on /ops points at the alerts page.
- Alert history needs retention from day one: `alert_events` is pruned at
  90 days and capped by count in the normalize housekeeping pass, beside
  `raw_payloads` and `rate_history`.
- The done-when from the roadmap holds: an unplugged AP is a `device down`
  item with `for` = 1, notified the cycle it is noticed, with a snapshot
  if it is critical; a discarding port surfaces as a canary with its
  baseline in the text.

## Open questions, settled during implementation

Each is answered in the issue that implements the decision it belongs to.

1. Kuma semantics: one monitor per route (recommended) or one per rule?
   Transports issue.
2. The `device down` role list: is `hypervisor` in by default, given a
   lab host that is powered off on purpose is common? A silence covers it,
   but the first week of a fresh install will page on it. Rules issue.
   *Settled in #60: yes.* A host powered off on purpose is a silence; a
   host that died unannounced takes its guests with it.
3. Canary floor and multiplier: the defaults above are reasoned, not
   measured. A week of samples from the reference site, read before the
   rule ships, settles them. Canaries issue.
   *Deferred in #64:* the counters are not collected before #64, so no
   week of samples could exist when it shipped. The ADR defaults ship as
   provisional; tune them on the Rules tab once a week has accrued.
4. Should `config changed` route to a notifying transport by default? It
   is the one informational event most people want to see, and the one
   most likely to train them to ignore the channel. Transports issue.

## Amendment (2026-10-07): transports, issue #61

**Open questions 1 and 4, settled by the owner on #61.**

- Kuma semantics: one Kuma push monitor per route. A Kuma transport is one
  monitor; a site that wants two routes adds two transports.
- `config changed` routes nowhere by default. It stays on the attention
  list and in History without paging anyone.

**Deviations in the implementation.**

- *Link base.* Decision 3 assumed a configured public URL for absolute
  links, and none exists. The link base is an `app_state` value edited on
  the Transports tab. When it is empty, messages carry paths only.
- *Kuma skips events.* A one-shot `event` notification cannot hold a
  monitor down, so the `kuma` preset does not carry it. Route event rules
  to a `generic` transport to deliver them.
- *No summary strip.* Decision 8's per-transport delivery state in the
  /alerts summary strip is not built. The Transports tab shows each
  transport's last sent time and last error instead.

## Amendment (2026-10-07): snapshot on critical, issue #66

**Deviations and details in the implementation.**

- *Trigger.* A crit `raise` or an `escalate` to crit counts only when
  dispatch hands it to the transports. A crit routed `none` (or silenced)
  takes no snapshot.
- *Cooldown clock.* The window starts at the attempt, not the success, so a
  snapshot that keeps failing is one `snapshot failed` event per window
  rather than one per poll.
- *Settings.* The cooldown and keep count are on the Rules tab, stored in
  `app_state`. `/snapshots` names each file's causing alerts from a sidecar
  record in `app_state` (key, rule, and the alert's text; nothing else).
- *Latest.* An alert snapshot also refreshes `patchbay-latest.html`: it is
  the newest picture of the network.
