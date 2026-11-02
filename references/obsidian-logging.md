# Obsidian logging — templates

All session notes go to `<vault>/Sessions/`. Route A writes a single note; Route B gets a session folder. Filenames are fixed so wikilinks always resolve.

Rules for every note:
- YAML frontmatter as in the templates below; tags always include `duel-vault` and `duel-vault/<phase>`
- Wikilinks (`[[...]]`) to sibling notes, never relative markdown links
- `> [!decision]` callout for every decision; `> [!warning]` for deviations, escalations to the user, failures; `> [!escalation]` whenever work moves up a model tier (`references/model-tiers.md`)
- Written **during** the phase, not reconstructed afterwards
- Language: the vault's operating language (from CLAUDE.md)
- Respect any extra frontmatter fields the vault's CLAUDE.md mandates for all notes (e.g. `status`, `rank`) — the vault's rules stack on top of these templates

Every rule above that a machine can check is checked by `scripts/vault-lint.py` (`references/vault-lint.md`). Run it on the notes you just wrote rather than re-reading them: filenames below are exactly what it expects, and it is the closing gate on both routes.

## Route A — single session log

`<vault>/Sessions/<YYYY-MM-DD> <session-slug>.md`

```markdown
---
project: <vault-name>
session: <session-slug>
route: A
date: <YYYY-MM-DD>
skills_used: [<project/global skills invoked>]
executor: <sonnet | inline>   # who executed: the executor-tier model, or inline
notebooklm: true          # only when the NotebookLM lane is open; omit otherwise
tags: [duel-vault, duel-vault/session]
---

# Sessione — <title>

**Task:** one-line summary · **Route:** A (esecuzione diretta) · **Esito:** ✅/⚠️/❌

## Cosa è stato fatto
(brief narrative + list of files created/modified, as wikilinks where they are notes)

## Decisioni
> [!decision] ...
> [!escalation] ...   ← only if the executor's work had to be taken over inline

## Attività
(paste `session-log.py render --format block --heading Attività` here at closing)

## Chiusura
(end-of-session duties performed per CLAUDE.md: indices, Home.md, commit, ...)

## Token
Claude main: ~<n> est. · Claude delegated (`<executor>`): <n> measured | unavailable · no Codex on this route
```

Route A has no session folder, so when the NotebookLM lane is open the provenance goes
inline: add a `## NotebookLM` section and paste the fragment from
`nblm-import.py manifest --format block` into it (see below).

## Route B — session folder

```
<vault>/Sessions/<session-slug>/
├── 00 - <session-slug> MOC.md
├── 01-requirements.md
├── 02-plan.md
├── 02a-consensus-rounds/plan-round-N-{claude,codex}.md
├── 03-build-log.md
├── 04-review-cycle-N.md
├── 05-final-report.md
├── 06-sources.md            ← only when the NotebookLM lane is open
└── 07-activity-log.md       ← generated from the journal at closing (`session-log.py render`)
```

### 00 — MOC (index)

```markdown
---
project: <vault-name>
session: <session-slug>
type: duel-vault-moc
date: <YYYY-MM-DD>
model: <codex-plan-model>
model_build: <codex-build-model>
status: in-progress | paused | complete | escalated
notebooklm: true          # only when the NotebookLM lane is open; omit otherwise
tags: [duel-vault, duel-vault/moc]
---

# Duel Vault — <Session title>

**Progetto:** <vault-name> · **Goal:** one-line summary
**Codex:** critica `<model>` · build `<model_build>` · **Started:** <date> · **Status:** <status>

## Phases
- [[01-requirements]] — ✅/🔄/⬜
- [[02-plan]] — ✅/🔄/⬜ (consensus in N rounds)
- [[03-build-log]] — ✅/🔄/⬜
- [[04-review-cycle-1]] — ✅/🔄/⬜
- [[05-final-report]] — ✅/🔄/⬜
- [[06-sources]] — ✅/🔄/⬜ (only with `notebooklm: true`)
- [[07-activity-log]] — ✅/⬜ (full journal: activities, decisions, changes, tokens)

## Key decisions
> [!decision] (appended as they happen, with link to the note where the detail lives)
```

### 01 — Requirements

```markdown
---
project: <vault-name>
session: <session-slug>
phase: interview
date: <YYYY-MM-DD>
approved_by_user: true
tokens_codex: 0
tokens_claude_est: <n>
tags: [duel-vault, duel-vault/interview]
---

# Requirements — <Session title>

## Goal
## Non-goals
## Requirements
### R1 — <title>
<description>
**Acceptance:** <verifiable criterion>
### R2 — ...
## Constraints
(include the binding vault conventions from CLAUDE.md that apply to this task)
## Accepted assumptions
## Interview notes
(condensed Q&A; note which answers came from CLAUDE.md/Home.md instead of the user)
## Token usage
Codex: 0 (no invocations in this phase) · Claude: ~<n> est.
```

### 02 — Plan

```markdown
---
project: <vault-name>
session: <session-slug>
phase: planning
date: <YYYY-MM-DD>
consensus_rounds: <N>
plan_version: <M>
approved_by_user: true
tokens_codex: <n>
tokens_claude_est: <n>
tags: [duel-vault, duel-vault/plan]
---

# Plan — <Session title>

Consensus reached in <N> rounds ([[02a-consensus-rounds/plan-round-1-claude|round files]]).

## Steps
### Step 1 — <title>  `[R1, R3]`
**Deliverable:** ...
**Verification:** ...
### Step 2 — ...

## Requirements coverage
| Req | Steps |
|-----|-------|
| R1  | 1, 4  |

## Project skills used by the plan
| Skill | Step(s) | Why |
|-------|---------|-----|

## Contested points resolved during consensus
> [!decision] <point> — resolved as <X> because <reason> (round <N>)

## Token usage
| Round | Codex in | Codex out | Codex total |
|-------|----------|-----------|-------------|
| 1     | n        | n         | n           |
| **Phase total** | | | **n** |

Claude: ~<n> est.
```

### 03 — Build log

Append one entry per Codex invocation, live:

```markdown
---
project: <vault-name>
session: <session-slug>
phase: build
date: <YYYY-MM-DD>
tokens_codex: <n>
tokens_claude_est: <n>
tokens_claude_delegated: <n>   # only when a subagent ran in this phase (verifier, NotebookLM wait)
tags: [duel-vault, duel-vault/build]
---

# Build log — <Session title>

## Invocation 1 — Step(s) 1–2 · <timestamp>
**Model:** `<codex-build-model>` · effort <e> · **Prompt:** <one-line summary> · **Duration:** <s> · **Exit:** ok/retry/fail
**Files touched:** `a.py`, `b.md` (verified ✅)
**Verification:** inline ✅ | verifier `<haiku>` — n/n PASS (raw: `raw/verify-1-*.txt`)
**Codex report:** <distilled final message>
**Tokens:** in n · out n · total n (measured)
> [!warning] Deviation: <what/why/accepted-or-corrected>   ← only if any
> [!escalation] Step <k>: `<build-model>` → `<plan-model>` — <why>   ← only if the step moved up a tier

(at end of phase, append:)
## Token usage
Codex phase total: n (build `<model>`: n · escalated `<model>`: n) · Claude main: ~<n> est. · Claude delegated: n measured
```

### 04 — Review cycle

```markdown
---
project: <vault-name>
session: <session-slug>
phase: review
cycle: <N>
date: <YYYY-MM-DD>
tokens_codex: <n>
tokens_claude_est: <n>
tokens_claude_delegated: <n>   # only when the verifier ran in this cycle
tags: [duel-vault, duel-vault/review]
---

# Review — cycle <N>

## Findings
### F1 · BLOCKER · <title>  `[R2]`
**Where:** `file:line` · **What:** ... · **Expected:** ...
### F2 · MAJOR · ...
### F3 · MINOR · ...
(vault-convention violations — invalid frontmatter, broken wikilinks, wrong language — are findings too)

## Verdict
BLOCKERs: n · MAJORs: n · MINORs: n → **loop back to build** / **clean**

## Fix-brief sent to Codex
(verbatim, if loop-back happened)

## Token usage
Codex (fix invocations this cycle): n measured · Claude main (review work): ~<n> est. · Claude delegated (verifier): n measured
```

### 05 — Final report

```markdown
---
project: <vault-name>
session: <session-slug>
phase: final
date: <YYYY-MM-DD>
fix_cycles_used: <N>
tokens_codex_total: <n>
tokens_claude_est_total: <n>
tokens_claude_delegated_total: <n>   # omit when no subagent ran
tags: [duel-vault, duel-vault/final]
---

# Final report — <Session title>

## Requirements outcome
| Req | Status | Notes |
|-----|--------|-------|
| R1  | ✅     |       |
| R2  | ⚠️     | <residual limitation> |

## Residual issues (MINOR, accepted)
## Optimization opportunities (not implemented)
## Closing duties performed
(indices/Home.md/memory/commit per CLAUDE.md — each with ✅)

## Session stats
Consensus rounds: N · Build invocations: N · Fix cycles: N · Tier escalations: N
Models: critica `<codex_plan>` · build `<codex_build>` (effort <e>) · verifier `<claude_mechanical>`

## Token consumption
| Phase | Codex (measured) | Claude main (est.) | Claude delegated (measured) | NotebookLM |
|-------|------------------|--------------------|-----------------------------|------------|
| 1 — Interview | 0 | ~n | 0 | n ops |
| 2 — Planning  | n | ~n | 0 | 0 |
| 3 — Build     | n | ~n | n | n ops |
| 4 — Review    | n | ~n | n | n ops |
| **Total**     | **n** | **~n** | **n** | **n ops** |

> [!info] Codex figures come from CLI-reported usage; Claude main figures are character-based estimates (chars/4), not billing data; Claude delegated figures are the usage each subagent reported on completion (`unavailable` when it reported none). NotebookLM exposes no token counts — its column counts operations only.

(drop the NotebookLM column entirely when the lane never opened)

## Trail
[[01-requirements]] → [[02-plan]] → [[03-build-log]] → [[04-review-cycle-1]] → this
```

### 07 — Activity log

Generated, never hand-written: `scripts/session-log.py render --vault "$V" --session "$S"`
turns the session's journal (`.duel-vault/<slug>/journal.jsonl`) into a note with
frontmatter `phase: activity`, `events: <n>`, tags `[duel-vault, duel-vault/activity]`, and
sections Timeline, Decisions, Changes, Tokens. Regenerate it whenever you want the vault to
show the full record; always at closing. See `references/session-journal.md`.

Set the MOC's `status: paused` while the session waits on a resource
(`references/session-recovery.md`), back to `in-progress` on resume.

### 06 — Sources (NotebookLM lane only)

Written by `scripts/nblm-import.py manifest`, not by hand — it reads the live notebook so
the corpus is recorded as it actually is. Refresh it whenever sources are added.

```markdown
---
project: <vault-name>
session: <session-slug>
phase: sources
date: <YYYY-MM-DD>
notebook_id: <notebook-uuid>
sources: <n>
artifacts: <n>
tags: [duel-vault, duel-vault/notebooklm]
---

# NotebookLM — <Session title>

> [!info] Notebook `<notebook-uuid>` · <n> sources · <n> artifacts · captured <YYYY-MM-DD>

## Sources
| # | Title | Type | Status | Origin |
|---|-------|------|--------|--------|

## Artifacts
| # | Type | Title | Status | Artifact ID |
|---|------|-------|--------|-------------|

## Prompts
(every generation prompt, verbatim — artifacts are not reproducible, the inputs are all
that can be audited)

## Trail
[[00 - <session-slug> MOC]]
```

A MOC with `notebooklm: true` and no `06-sources.md` is a lint error: the lane ran and its
provenance was lost.

### Deliverable notes

Notes produced *from* NotebookLM artifacts are deliverables, not session logs: they live
where the vault's `CLAUDE.md` says content lives, follow the vault's own frontmatter
conventions, and are written by `nblm-import.py import` (which stacks the vault's mandated
fields on top of its own). They carry `source: notebooklm`, the `notebook_id`, a provenance
callout and a wikilink back to the session, so a note found six months later can be traced
to the corpus that produced it.
