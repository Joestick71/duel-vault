# Obsidian logging — templates

All session notes go to `<vault>/Sessions/`. Route A writes a single note; Route B gets a session folder. Filenames are fixed so wikilinks always resolve.

Rules for every note:
- YAML frontmatter as in the templates below; tags always include `vault-duet` and `vault-duet/<phase>`
- Wikilinks (`[[...]]`) to sibling notes, never relative markdown links
- `> [!decision]` callout for every decision; `> [!warning]` for deviations, escalations, failures
- Written **during** the phase, not reconstructed afterwards
- Language: the vault's operating language (from CLAUDE.md)
- Respect any extra frontmatter fields the vault's CLAUDE.md mandates for all notes (e.g. `status`, `rank`) — the vault's rules stack on top of these templates

## Route A — single session log

`<vault>/Sessions/<YYYY-MM-DD> <session-slug>.md`

```markdown
---
project: <vault-name>
session: <session-slug>
route: A
date: <YYYY-MM-DD>
skills_used: [<project/global skills invoked>]
tags: [vault-duet, vault-duet/session]
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

## Route B — session folder

```
<vault>/Sessions/<session-slug>/
├── 00 - <session-slug> MOC.md
├── 01-requirements.md
├── 02-plan.md
├── 02a-consensus-rounds/plan-round-N-{claude,codex}.md
├── 03-build-log.md
├── 04-review-cycle-N.md
└── 05-final-report.md
```

### 00 — MOC (index)

```markdown
---
project: <vault-name>
session: <session-slug>
type: vault-duet-moc
date: <YYYY-MM-DD>
model: <codex-model>
status: in-progress | complete | escalated
tags: [vault-duet, vault-duet/moc]
---

# Vault Duet — <Session title>

**Progetto:** <vault-name> · **Goal:** one-line summary
**Codex model:** `<model>` · **Started:** <date> · **Status:** <status>

## Phases
- [[01-requirements]] — ✅/🔄/⬜
- [[02-plan]] — ✅/🔄/⬜ (consensus in N rounds)
- [[03-build-log]] — ✅/🔄/⬜
- [[04-review-cycle-1]] — ✅/🔄/⬜
- [[05-final-report]] — ✅/🔄/⬜

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
tags: [vault-duet, vault-duet/interview]
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
tags: [vault-duet, vault-duet/plan]
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
tags: [vault-duet, vault-duet/build]
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
tags: [vault-duet, vault-duet/review]
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
tags: [vault-duet, vault-duet/final]
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
| Phase | Codex (measured) | Claude (est.) |
|-------|------------------|---------------|
| 1 — Interview | 0 | ~n |
| 2 — Planning  | n | ~n |
| 3 — Build     | n | ~n |
| 4 — Review    | n | ~n |
| **Total**     | **n** | **~n** |

> [!info] Codex figures come from CLI-reported usage; Claude figures are character-based estimates (chars/4), not billing data.
## Trail
[[01-requirements]] → [[02-plan]] → [[03-build-log]] → [[04-review-cycle-1]] → this
```
