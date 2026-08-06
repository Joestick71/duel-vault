# Obsidian logging — templates

All session notes go to `<vault>/Sessions/`. Route A writes a single note; Route B gets a session folder. Filenames are fixed so wikilinks always resolve.

Rules for every note:
- YAML frontmatter as in the templates below; tags always include `duel-vault` and `duel-vault/<phase>`
- Wikilinks (`[[...]]`) to sibling notes, never relative markdown links
- `> [!decision]` callout for every decision; `> [!warning]` for deviations, escalations, failures
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
notebooklm: true          # only when the NotebookLM lane is open; omit otherwise
tags: [duel-vault, duel-vault/session]
---

# Sessione — <title>

**Task:** one-line summary · **Route:** A (esecuzione diretta) · **Esito:** ✅/⚠️/❌

## Cosa è stato fatto
(brief narrative + list of files created/modified, as wikilinks where they are notes)

## Decisioni
> [!decision] ...

## Chiusura
(end-of-session duties performed per CLAUDE.md: indices, Home.md, commit, ...)

## Token
Claude: ~<n> est. (no Codex on this route)
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
└── 06-sources.md            ← only when the NotebookLM lane is open
```

### 00 — MOC (index)

```markdown
---
project: <vault-name>
session: <session-slug>
type: duel-vault-moc
date: <YYYY-MM-DD>
model: <codex-model>
status: in-progress | complete | escalated
notebooklm: true          # only when the NotebookLM lane is open; omit otherwise
tags: [duel-vault, duel-vault/moc]
---

# Duel Vault — <Session title>

**Progetto:** <vault-name> · **Goal:** one-line summary
**Codex model:** `<model>` · **Started:** <date> · **Status:** <status>

## Phases
- [[01-requirements]] — ✅/🔄/⬜
- [[02-plan]] — ✅/🔄/⬜ (consensus in N rounds)
- [[03-build-log]] — ✅/🔄/⬜
- [[04-review-cycle-1]] — ✅/🔄/⬜
- [[05-final-report]] — ✅/🔄/⬜
- [[06-sources]] — ✅/🔄/⬜ (only with `notebooklm: true`)

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
tags: [duel-vault, duel-vault/build]
---

# Build log — <Session title>

## Invocation 1 — Step(s) 1–2 · <timestamp>
**Prompt:** <one-line summary> · **Duration:** <s> · **Exit:** ok/retry/fail
**Files touched:** `a.py`, `b.md` (verified ✅)
**Codex report:** <distilled final message>
**Tokens:** in n · out n · total n (measured)
> [!warning] Deviation: <what/why/accepted-or-corrected>   ← only if any

(at end of phase, append:)
## Token usage
Codex phase total: n · Claude: ~<n> est.
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
Codex (fix invocations this cycle): n measured · Claude (review work): ~<n> est.
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
Consensus rounds: N · Build invocations: N · Fix cycles: N

## Token consumption
| Phase | Codex (measured) | Claude (est.) | NotebookLM |
|-------|------------------|---------------|------------|
| 1 — Interview | 0 | ~n | n ops |
| 2 — Planning  | n | ~n | 0 |
| 3 — Build     | n | ~n | n ops |
| 4 — Review    | n | ~n | n ops |
| **Total**     | **n** | **~n** | **n ops** |

> [!info] Codex figures come from CLI-reported usage; Claude figures are character-based estimates (chars/4), not billing data. NotebookLM exposes no token counts — its column counts operations only.

(drop the NotebookLM column entirely when the lane never opened)

## Trail
[[01-requirements]] → [[02-plan]] → [[03-build-log]] → [[04-review-cycle-1]] → this
```

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
| # | Type | Status | Artifact ID |
|---|------|--------|-------------|

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
