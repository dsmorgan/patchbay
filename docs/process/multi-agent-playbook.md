# Multi-Agent Project Playbook

> **Portable.** This document is written to be copied into future projects. Project-specific commands
> live in `CLAUDE.md`; this file holds the *method*. Owner: Austin. First edition: 2026-08-22
> (Beat Farmer bootstrap). Iterate it at every milestone retro — see [§12](#12-retro-template).

## 0. Why this exists

Big projects run by AI agents fail in predictable ways: the most capable (most expensive) model
writes all the code; agents flail on a stuck problem and burn tokens; nobody verifies claims; decisions
get re-litigated every session; humans discover obvious bugs that a screenshot would have caught. This
playbook is the counter-pattern: **route work to the cheapest model that reliably does it, escalate
one rung when struggling, verify everything with evidence, and write decisions down once.**

## 1. Principles

1. **Right model for the job.** Capability costs tokens. Most work is well-specified; give it to the
   implementer tier. Reserve the heavy tiers for the open-ended, the ambiguous, and the stuck.
2. **Escalate, don't flail.** "Struggling too much" has a definition (§3). When hit, reach *one rung
   up* for input/help. Flailing is the single largest token sink.
3. **Verification over trust.** "Done" means a command ran and its output is in the report. The PM
   re-runs the gate. Screenshots and perf reports are evidence, not decoration.
4. **Contracts before fan-out; seed the pattern first.** Parallel agents need shared interfaces and
   a thin running slice to pattern-match against. Never fan out onto a blank repo.
5. **Self-contained briefs.** An agent should not have to rediscover the project. If the brief needs
   three paragraphs of context, write them — it's cheaper than the agent's exploration.
6. **Humans own taste, product and scope.** Agents own execution and evidence. Foundational choices
   are presented as an *array of options* with a recommendation, never assumed.
7. **Decisions are written once.** ADRs stop re-litigation. If a session starts by re-deriving a
   decision, that's a playbook failure.
8. **Determinism and tooling are features.** Seeds, headless sims, harness modes and budgets are
   built early because they make every later task cheaper.

## 2. Roles & model routing

| Tier | Model (today) | Role | Typical work | Must **not** |
|---|---|---|---|---|
| **Lead / PM** | Fable | Intake with the human, vision, decomposition, contracts, briefs, integration, gates, ADRs, retros, *the conversation* | Write/curate docs, define interfaces, seed thin slices, review, decide escalations | Write bulk feature code; debug in a loop; hold a subagent's work hostage to its own context |
| **Architect / Firefighter** | Opus | Open-ended design, ambiguous specs, sticky bugs, cross-cutting refactors, perf investigations, risky reviews; **answers Sonnet's escalations** | Design sessions, root-cause hunts, integration-heavy modules, contract changes | Routine implementation that Sonnet can do; decide product/taste questions |
| **Implementer** | Sonnet | Most of the code | Well-specified modules, tests, CI, boilerplate, docs-from-code, mechanical refactors, harness scripts | Invent architecture; change contracts; continue past the help trigger |
| **Scout** (optional) | Haiku / `Explore` agent | Read-only search & summarization | "Where is X?", "list the call sites", "summarize this file" | Edit anything |

Routing heuristics the PM applies when writing a brief:
- Spec is crisp, tests are stateable, files are known → **Sonnet**.
- Spec needs judgment, the change spans modules, or Sonnet has already failed twice → **Opus**.
- The question is "what should we build / what does the human want" → **Fable ↔ Austin**.

## 3. Escalation ladder (strict adjacency)

```
 Sonnet ──asks──▶ Opus ──asks──▶ Fable ──asks──▶ Austin
   │                 │               │
   └── never skips ──┘── never skips ┘
```

- **Frame:** an agent *reaches for a higher model's input/help when struggling too much on a task*.
  Escalation is normal and cheap; flailing is abnormal and expensive.
- **One rung at a time.** Sonnet only ever asks Opus. Opus only ever asks Fable. A lower tier is not
  considered able to reliably assess its needs *beyond* the next layer above it. The upper rung may
  resolve, take over, reframe, or escalate further — that judgment is the upper rung's.
- **Fable → Austin** only for taste/product/scope decisions, destructive actions, or "the options
  materially change the work".

**"Struggling too much" — escalate when any of these is true:**
1. Two attempts at the same sub-goal failed (build/test still red, or the approach had to be thrown away).
2. The spec is ambiguous in a way that changes the design (not just a naming choice).
3. The fix needs a change to a shared contract/interface not owned by the brief.
4. A test cannot be made green without weakening it.
5. Toolchain/tool failure persists after one retry.
6. Anything security-sensitive, destructive, or outside the brief's file scope.

**Escalation message (both directions, always):**
```
ESCALATION from <tier> on <brief id>
Context: <one paragraph: goal + where I am>
Tried: 1) … (result/output)  2) … (result/output)
Blocking question: <one precise question>
Options I see: A) … B) … (my lean: …)
Files/lines: <pointers>
```

**Mechanics in Claude Code (this repo's agent files encode these):**
- Sonnet → Opus: preferred — spawn an `architect` agent (Opus) with the escalation message and wait
  for its answer, then continue. Fallback — return early with `STATUS: ESCALATE` + the message; the
  PM routes it to Opus and resumes the Sonnet agent with the answer.
- Opus → Fable: return `STATUS: ESCALATE` + the message (Opus is a subagent of the PM session).
- Fable → Austin: `AskUserQuestion` with options, or a clearly marked question in the turn summary.

## 4. Workflow phases

| # | Phase | Owner | Output |
|---|---|---|---|
| 0 | **Intake** | Fable + Austin | Goals, pillars, constraints, what "done" looks like |
| 1 | **Vision & decisions** | Fable (Opus for open-ended design sessions) | GDD / spec, ADRs, milestone list; foundational options presented to Austin |
| 2 | **Contracts & seed** | Fable or Opus | Interfaces/types, thin *running* vertical slice, verification harness proven end-to-end (toolchain risk retired) |
| 3 | **Fan-out** | Sonnet (parallel, disjoint files) | Modules + tests per brief |
| 4 | **Gates** | Each agent, then Fable re-runs | Build/test/lint/harness output in reports |
| 5 | **Integrate & review** | Fable (Opus for risky diffs) | Merged slice, updated docs |
| 6 | **Human playtest** | Austin | Feedback; manual test guide provided by the PM |
| 7 | **Retro** | Fable | Playbook + CLAUDE.md updates; token/retry/escalation metrics |

Phases 3–5 repeat per milestone. Phase 2 repeats whenever a new subsystem is introduced.

## 5. Brief anatomy

Every delegated task uses [`briefs/TEMPLATE.md`](briefs/TEMPLATE.md). Non-negotiable sections:
**Goal** (one sentence), **Context** (paths to read, *not* pasted contents), **Contracts** (interfaces
the work must satisfy), **Acceptance** (tests/commands that must pass, named), **Scope** (files the
agent may touch; everything else is read-only), **Non-goals**, **When to ask for help** (the §3
triggers, naming the next rung), **Done definition** (commands run + outputs shown; report format §9).

Briefs are saved in `docs/process/briefs/<date>-<slug>.md` so retros can study them.

## 6. Verification gates

Project commands live in `CLAUDE.md`. The *gate order* is universal:
1. **Build / typecheck** — red means stop.
2. **Unit tests** — for the pure core; fast; every behavior change adds one.
3. **Lint / format / analyzers.**
4. **Harness smoke** — run the app in its deterministic harness mode; inspect the screenshot(s)
   and perf report *as an agent* (read the image; read the JSON).
5. **Perf budget** — the harness exits non-zero on violations; budgets live in a checked-in file.
6. **Manual test guide** — the PM writes a procedural guide for Austin before any PR/push. Automated
   gates don't catch feel.

Rule: **show output, don't claim.** A report that says "tests pass" without the test runner's tail is
rejected.

## 7. Token discipline

- Paths, not pastes. Point agents at files/line ranges; let them read what they need.
- Keep `CLAUDE.md` short (< ~150 lines) and link out; it is loaded every session.
- Briefs are self-contained; "look around and figure it out" is banned.
- Use scout agents for searches; the PM's context is the scarcest resource.
- Stable, static agent definitions (`.claude/agents/*.md`) so prompt caches hit.
- Don't re-derive decisions — read the ADR.
- A failing test is the cheapest spec. Prefer writing one over a paragraph of prose.
- Batch independent tool calls; never poll when a notification will arrive.
- **Retry budget = 2, then escalate.**
- Record per brief: model, wall time, retries, escalations, outcome (§12).

## 8. Parallelism & isolation

- Parallel agents get **disjoint file scopes**; the PM owns integration files.
- Multiple human/agent *sessions* on one repo use git worktrees, unique ports and session scratchpads.
- Shared contracts change only via the PM (or Opus with the PM's sign-off), never mid-fan-out.
- One integration owner per milestone; agents never merge each other's work.

## 9. Communication contract (subagent final report)

```
STATUS: DONE | PARTIAL | ESCALATE | BLOCKED
Changed: <files>
Verified: <command> → <last ~10 lines of output>   (one block per gate run)
Evidence: <paths to screenshots / reports, with a one-line reading of each>
Open issues: <known gaps, flaky bits, TODOs left in code (should be none)>
Escalations: <none | the message(s) sent and answers received>
Follow-ups: <suggested next briefs>
```

## 10. Human-in-the-loop

- Foundational decisions → options array + recommendation, wait for the pick.
- Taste/product questions are **batched** into the turn summary or one `AskUserQuestion`.
- Before push/PR: manual test guide (procedural, feature-specific), run command, branch/worktree.
- Never narrow, widen or transform scope silently; say what was left out and why.

## 11. Adoption checklist (new project)

1. Copy this file to `docs/process/multi-agent-playbook.md`; copy `briefs/TEMPLATE.md`.
2. Create `.claude/agents/{implementer,architect,debugger,qa}.md` with model frontmatter
   (sonnet / opus / opus / sonnet) and the escalation clauses.
3. Write `CLAUDE.md`: commands (build/test/lint/harness), conventions, routing pointer, branch rules.
4. Build the verification harness *before* feature work (screenshot + perf report + budgets).
5. Add `.claude/settings.json` permissions for the gate commands so agents aren't blocked.
6. Write ADR-0001 (stack) from the options array Austin picked.
7. Schedule the retro at the first milestone boundary.

## 12. Retro template

```
Milestone: …        Dates: …
Briefs: N (Sonnet a / Opus b / Fable-authored code c)
Retries: … Escalations: Sonnet→Opus x, Opus→Fable y, Fable→Austin z
What the ladder caught: …
What slipped past the gates (found by Austin): …
Token hot-spots (top 3 briefs by spend) and why: …
Playbook changes: …   CLAUDE.md changes: …
```

## 13. Retro log

Copied into patchbay on 2026-08-24 from Beat Farmer's edition, whose own retro log stays
there. patchbay's first milestone under the playbook is `ui/pages-and-map` (ADR-0001); its
retro is due after the owner's playtest of that branch.

## 14. Learnings (cumulative — fold into the sections above when stable)

1. **Seed the pattern, then fan out.** A thin running slice + proven harness before any parallel brief made
   every Sonnet brief land first time. Contracts-only fan-out onto a blank repo would not have.
2. **The harness must not measure itself.** Instrument (`spikes` with frame indices) *before* hypothesizing;
   exclude harness-induced artifacts explicitly and document why in code.
3. **Worktree-per-brief + disjoint file scopes ⇒ conflict-free merges** — except shared docs. **During
   fan-out the PM owns `CLAUDE.md`/`README`; agents put doc deltas in their report** (the only merge
   conflict of M0 was CLAUDE.md touched by two briefs).
4. **"Documented-not-fixed" is a valid, valuable outcome.** Tell implementers explicitly; it prevents
   silent "fixes" of intended behaviour.
5. **Evidence lives in the agent's worktree (gitignored).** The PM copies the artifacts it wants to keep
   (WAVs, PNGs) into the integration worktree or the PR description.
6. **Budgets need a warmup and a CI variant.** JIT/atlas/texture-upload frames and software rendering
   would otherwise fail honest code.
7. **No wall-clock assertions in unit tests.** A "renders 10 s of audio in < 200 ms" test passed locally,
   failed on Ubuntu CI (344 ms), then passed there and failed on Windows CI (2 s) — shared-runner noise
   plus parallel test classes. Unit tests assert *behaviour*; throughput goes in the harness `report.json`
   with its own (dev/CI) budget. Keep at most a generous sanity guard (≥ 5× the expected value).
8. **CI is evidence too.** The first PR's CI runs found (7) and proved the Xvfb + llvmpipe harness path;
   treat the first green CI as part of the milestone's done-definition, not an afterthought.
9. **QA findings become the next brief, not a conversation.** The wave-1 QA's three cosmetic findings and the
   juice brief's pacing finding were batched into one `m1-polish` brief with evidence requirements — cheaper and
   more reliable than round-tripping each to the original implementer.
10. **Taste decisions get an in-engine comparison, not prose.** Four theme candidates rendered by the harness from
    the same seed/frames, published side by side, let the owner pick or blend from evidence. The harness's
    determinism is what makes candidates comparable.
11. **Golden-hash tests protect "no gameplay change" briefs.** The music brief's `Targets_Unchanged_GoldenHash`
    (captured *before* edits) turned a risky generator change into a provable no-op for gameplay.
12. **Sequence briefs that share files; parallelize the rest.** Wave 1 (disjoint) ran in parallel; juice → polish →
    hygiene ran sequentially because they all touch `FieldScene`/`DevPanel`/props. Zero merge conflicts in M1.
13. **Write the evidence into the commit message.** A background agent killed *between* its commits and its §9 report
    loses only the report — the art pass's architect-review notes survived in `646fc46`'s message. Cheap insurance; now
    required by every brief's Done definition ("gate tails in the final commit message").
14. **Background agents die with the session / on user interruption; worktree WIP survives.** Relaunch the same agent type
    with a *continuation* brief ("inherit the WIP, read the diff, build, continue to acceptance") — worked twice in M2
    (shop, art). Prefer foreground when the result is needed next; background when the user may interject.
15. **A rule nobody owns is the bug you ship.** Free-Kick stacking (the bot bought three free Silos) fell between the shop
    brief (costs) and the bot brief (build policy). The PM's integration gate — a *bot-played* phased harness run with
    `report.json.game` read, not just the evidence slice — is what caught it; keep one such cross-brief gate per wave.
16. **The simulator must play the run the player plays.** Balance tuned against a pre-placed six-turret field said "fine";
    the first shop-built bot run died in wave 3. Align the simulator with the real run constructor (`CreateRun` + the
    bot's build policy) *before* tuning, and re-tune once (wave 2b) — then hold.
17. **Feel before balance (Austin, Decision 4).** Sims are for floors ("no hard floor": accuracy 0 + survival-first must
    fully grow within `MaxPrestigesNoGroove`) and for catching regressions, not for final numbers — those wait for the
    owner's feel playtest. Briefs under a balance hold name the *only* numbers they may touch.
18. **Recon in the brief saves the first hour.** The retire brief's § Recon (reference counts, two gotchas: reused
    `ApproachRate*` cost fields, harvest "Combo") let a 34-file deletion land in 23 min with zero escalations. Ten minutes
    of PM grep is the cheapest token the milestone spends.
19. **PM docs in parallel with a running agent is free** when the files are disjoint (different worktrees, docs vs code):
    GDD sync + test guide + briefs were written alongside the art pass and the retirement with zero conflicts. Shared
    docs (`CLAUDE.md`/`README`/milestone tables) stay PM-owned (learning 3).
20. **A bot is not a player, and an offline render is not the live device.** The defense was silent for every human
    from the shop brief to the PR (four briefs, one QA pass, 198 green tests): the player's START WAVE runs *before*
    the session step, the bot's *after*, and only the bot path was ever exercised; the offline WAV proved the renderer,
    not the scene→device wiring. Every harness/QA pass must include (a) one run that drives the **player's** input
    path (`--start-wave-after`) and (b) one **live** run with the device stream recorded (`--audio-tap`) and *read*
    (per-second RMS / low-band), the way PNGs are read. Owner playtests remain the gate that catches what the
    harness cannot see — schedule them earlier (after the first brief that changes the live loop, not after the wave).

## 15. Changelog

- **2026-08-22 v0.1** — First edition from the Beat Farmer bootstrap. Added the strict one-rung
  escalation ladder (Sonnet→Opus→Fable→Austin) and the "struggling too much" triggers at Austin's
  direction; added the options-array rule for foundational decisions.
- **2026-08-22 v0.2** — M0 retro + learnings §13–14 (PM-owned shared docs during fan-out,
  documented-not-fixed, evidence location, budget warmup/CI variants).
- **2026-08-23 v0.3** — M1 retro + learnings 9–12 were added on 2026-08-22 without a changelog line; M2 retro + learnings 13–20 (20: bot ≠ player, offline ≠ live device — the silent-defense bug) (evidence in commit
  messages, continuation briefs, unowned rules, sim plays the real run, feel before balance, recon in the brief, parallel PM docs).
- **2026-08-24 (patchbay)** — copied into this repo unchanged apart from §13, which is reset to
  patchbay's own log; agent definitions and the brief template adapted to a Python/FastAPI
  repo whose gates are `pytest` and a screenshot harness.
