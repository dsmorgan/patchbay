# Brief: <slug>  (<date>, tier: sonnet | opus)  — branch `feat/<slug>`

**Goal (one sentence):** …

**Why / where it fits:** one or two sentences tying this to the milestone and `docs/architecture.md`.

## Setup (worktree — mandatory when other briefs run in parallel)
```bash
cd D:/git/patchbay
git worktree add .worktrees/<slug> -b feat/<slug> <base-branch>
cd .worktrees/<slug>
```
Run everything with the repo venv: `D:/git/patchbay/.venv/Scripts/python.exe` (Windows) or
`.venv/bin/python` (Mac), with `PYTHONPATH=src` so the checkout, not the installed package, is what runs.
Commit only on this branch. Do not push. Operating instructions: `.claude/agents/implementer.md`.

## Context — read these first (paths, not pastes)
- `CLAUDE.md` (two-repo rule, invariants, gotchas)
- `docs/adr/…` — the decision that constrains this work
- `src/patchbay/…` — the pattern to follow / the contract to satisfy

## Contracts
Routes, template blocks, query parameters, data shapes this work must satisfy or produce
(signatures, not prose). If a contract must change, **stop and escalate** (see "When to ask for help").

## Scope
**May edit:** `src/patchbay/templates/<page>.html`, `src/patchbay/web.py` (only the named route/region), `tests/…`
**Read-only:** everything else. `templates/_style.html`, `templates/base.html`, `web.py`'s `NAV`
block, `CLAUDE.md`, `README.md`, `CHANGELOG.md` are **PM-owned during fan-out** — put the lines you would
add in your report's `Follow-ups`; the PM applies them at merge time. Page-specific CSS goes in a
`<style>` block in the page template (the existing pattern: `ops.html`, `confignode.html`).

## Acceptance (named, runnable)
- [ ] `PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q` green, including new tests: `tests/test_web.py::<name>` …
- [ ] Screenshots: `python scripts/screenshots.py --out artifacts/shots/<slug> --pages …` → every page 200;
      **Read the PNGs** and state what is visible (rail lit correctly, header present, nothing clipped).
- [ ] Two-repo rule: nothing site-specific in the diff (RFC 5737/3849 addresses only).

## Non-goals
What this brief deliberately does **not** do (so the agent doesn't wander).

## When to ask for help (escalation — one rung up only)
Escalate to **<next rung: architect (Opus) | PM (Fable)>** when any of: two failed attempts at the same
sub-goal; spec ambiguity that changes the design; a needed contract change; a test that can't go green
without weakening it; toolchain failure after one retry; anything destructive/out of scope.
Use the escalation message format from `docs/process/multi-agent-playbook.md` §3.

## Done definition
Report in the §9 format: `STATUS`, `Changed`, `Verified` (commands + output tails), `Evidence`
(paths + one-line readings of screenshots), `Open issues`, `Escalations`, `Follow-ups`.
Gate tails go in the final commit message too. No TODOs left in code. No claims without output.
