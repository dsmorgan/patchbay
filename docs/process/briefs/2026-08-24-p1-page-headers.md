# Brief: p1-page-headers  (2026-08-24, tier: sonnet)  — branch `feat/p1-page-headers`

**Goal (one sentence):** Every shell page uses the page-header slot seeded in `base.html` — title and
purpose from `NAV`, drill-downs overriding `page_title` and setting `breadcrumb`, page controls in
`controls` — drops its old top-bar `crumb` block and its now-duplicate top `h2`, and its section
headings say what the section answers rather than naming a noun.

**Why / where it fits:** The rail says where you are; the page header says what the page answers, in the
same words, with the page's controls in one predictable place. This is the base every later brief in the
milestone (Overview exceptions, map modes, Snapshots page, Configs timeline) builds on.

## Setup (worktree — mandatory)
```bash
cd D:/git/patchbay
git worktree add .worktrees/p1-page-headers -b feat/p1-page-headers ui/pages-and-map
cd D:/git/patchbay/.worktrees/p1-page-headers
PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q      # 98 passed at the base
```
Commit only on this branch. Do not push. Operating instructions: `.claude/agents/implementer.md`.

## Context — read these first (paths, not pastes)
- `CLAUDE.md` (two-repo rule, invariants, gotchas — especially "inline SVG needs `/>` intact")
- `src/patchbay/templates/base.html` lines 72–95: the slot. Blocks: `breadcrumb`, `page_title`
  (defaults to the NAV entry's name), `purpose` (defaults to the NAV description), `controls`.
- `src/patchbay/templates/_style.html`: `.page`, `.page .purpose`, `.page .controls`, `.page .breadcrumb`
  (read-only — per-page CSS goes in the page's own `<style>`; `ops.html` shows the pattern).
- `src/patchbay/templates/topology.html`: the one page already converted (`controls` holds "reset layout").
- `src/patchbay/templates/drift.html`: the heading voice to copy ("Conflicts — same IP, different story").
- `src/patchbay/web.py` `NAV` (~line 113) for the exact title/purpose each page inherits.
- `tests/test_web.py`: `PAGES`, `test_all_pages_render_on_empty_db`, `test_empty_sections_are_hidden`.

## Contracts (per page — exact)
| Page | `page_title` | `breadcrumb` | `purpose` | `controls` | Remove |
|---|---|---|---|---|---|
| `vlans.html` | (NAV default) | — | (NAV default) | — | `crumb` block; the `<h2>VLANs</h2>`. Keep the legend paragraph. Rename `Subnets without a VLAN mapping` → `Subnets no VLAN claims`. |
| `drift.html` | (default) | — | (default) | — | `crumb`; `<h2>IPAM vs. observed reality</h2>` (the purpose line says it). Headings already in voice — keep. |
| `patchpanel.html` | (default) | — | (default) | the panel `<select>` when `panels \| length > 1`, plus `<span class="muted">{{ size }} positions</span>` when `size` | `crumb`; the `<h2>` that held the name/select. Add `<h2>Positions — {{ sel }}</h2>` above the table. |
| `configs.html` | (default) | — | (default) | — | `crumb`; the `<h2>Device configuration history (Oxidized)</h2>`. Add `<h2>Nodes and their last backup</h2>` above the table (only in the non-error branch). |
| `confignode.html` | `{{ node }}` | `<a href="/configs">Configs</a> / {{ node }}` inside `<nav class="breadcrumb">` | `configuration versions from Oxidized` | the existing "view running config" link (drop its inline style; `.controls` sizes it) | `crumb`; the `<h2>Versions — {{ node }} …</h2>` becomes `<h2>Versions</h2>`. Keep the Diff/Config headings. |
| `device.html` | `{{ d.name }}` | `<a href="/">Overview</a> / {{ d.name }}` | `{{ d.role or '?' }} · {{ d \| hw or '?' }}` | the show-all / hide-idle-logical link that today sits in a `<p>` under Ports (when `hidden` or `show_all`); move the *link* only — the "hidden (idle logical): …" count text stays under the Ports heading as a muted line | `crumb`. Heading voice: `Ports (24 shown) — click a port for its traffic graph` → `Ports — {{ n }}{% if hidden %} shown{% endif %}; click one for its traffic graph`; `Links` → `Cabled to`; `Guests (n)` → `Guests on this host (n)`; `Endpoints seen here (n)` keep. Keep the identity card and the Utilization toggle exactly as they are (their JS is wired by id). |
| `ops.html` | (default: "Ops") | — | (default) | — | `crumb`. Keep every section and all JS as is. |
| `dashboard.html`, `topology.html`, `login.html`, `snapshot.html`, `_*.html` | **do not touch** | | | | |

Rules: the `{% block crumb %}` lines are deleted from every page above, not emptied. `base.html` and
`_style.html` are PM-owned — do not edit them; the PM removes the top-bar crumb slot at merge. No new
CSS unless a page needs it, and then in that page's `<style>`. Every `/>` in inline SVG stays intact.

## Scope
**May edit:** the seven page templates named above; `tests/test_web.py` (add tests only).
**Read-only:** everything else.

## Acceptance (named, runnable)
- [ ] `PYTHONPATH=src D:/git/patchbay/.venv/Scripts/python.exe -m pytest -q` green, including new
      `tests/test_web.py::test_pages_carry_a_header` — for every path in `PAGES` plus `/device/sw1`
      (after `seed(...)`, see how other tests seed), the body contains `<header class="page">`, the
      expected `<h1>` text (NAV name, or the device name for `/device/sw1`), and **does not** contain
      the old crumb text (`"/ vlans"`, `"/ drift"`, `"/ ops"`, `"/ patch panels"`, `"/ configs"`).
- [ ] `D:/git/patchbay/.venv/Scripts/python.exe scripts/screenshots.py --out artifacts/shots/p1` → 16/16 ok
      (default pages, 1400 and 600). **Read** at least `vlans-1400`, `drift-1400`, `patchpanel-1400`,
      `device_core1-1400`, `configs-1400`, `ops-1400`, `device_core1-600` and say what you see: one header
      per page, no duplicate title beneath it, controls on the right, breadcrumb on the device page,
      nothing clipped at 600.
- [ ] Two-repo rule: the diff contains no addresses outside RFC 5737/3849 and no real hostnames.

## Non-goals
No changes to what the pages compute or list; no Overview restructuring (next brief); no map work;
no new routes. Don't "improve" copy beyond the headings named above.

## When to ask for help (escalation — one rung up only)
Escalate to the **architect (Opus)** when any of: two failed attempts at the same sub-goal; a block or
variable the contract assumes doesn't exist; a test that can't go green without weakening it; toolchain
failure after one retry. Use the message format in `docs/process/multi-agent-playbook.md` §3.

## Done definition
Commit on `feat/p1-page-headers` with the gate tails in the commit message. Report in the §9 format:
`STATUS`, `Changed`, `Verified` (commands + output tails), `Evidence` (PNG paths + one-line readings),
`Open issues`, `Escalations`, `Follow-ups` (including the `base.html` line the PM should remove).
