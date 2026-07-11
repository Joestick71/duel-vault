# Consensus protocol — Phase 2 bidirectional channel

The channel is file-based, turn-alternating, and machine-parseable. Claude always moves first.

## Message format

Every message (both authors) is a markdown file starting with this header block:

```
---
protocol: duet-consensus/v1
round: <N>
author: claude | codex
verdict: PROPOSE | REVISE | APPROVE
plan_version: <M>
---
```

Followed by sections:

- `## Plan` — the full current plan (always the complete plan, never a diff; both sides must be able to read one file and know the whole state)
- `## Changes from previous version` — bullet list, or "none"
- `## Critiques` — numbered objections to the counterpart's last version (empty if APPROVE)
- `## Rebuttals` — responses to the counterpart's critiques: for each, `ACCEPTED` (with the change made) or `REJECTED` (with the reason)

## Verdict semantics

- `PROPOSE` — first message of the session only (Claude, round 1)
- `REVISE` — "I changed the plan and/or I have critiques; not done"
- `APPROVE` — "I accept plan_version M exactly as written, no changes"

**Consensus** = both authors have emitted `APPROVE` for the same `plan_version`. In practice: if Codex replies APPROVE to Claude's version M, and Claude has no further changes, Claude writes its own APPROVE message for version M and the phase ends.

## Turn mechanics

1. Round N, Claude: write `plan-round-N-claude.md`, send its full content to Codex inside a prompt that also contains: `01-requirements.md`, the protocol rules (copy the Message format and Verdict semantics sections verbatim into the prompt), and the instruction to respond *only* in protocol format.
2. Round N, Codex: save the reply as `plan-round-N-codex.md`. Parse the header. If the header is malformed, re-prompt once with a format correction; if still malformed, treat the content as critiques with verdict REVISE and note the protocol violation in the log.
3. Claude integrates: every Codex critique gets an explicit ACCEPTED or REJECTED rebuttal in the next message. No critique may be silently dropped.
4. Increment `plan_version` only when the plan text actually changes.

## Round cap and escalation

Hard cap: **5 rounds** (from `session.json`). If round 5 ends without consensus:

1. Write `plan-disagreement.md`: for each unresolved point — Claude's position, Codex's position, concrete impact of each choice, and Claude's recommendation.
2. Present it to the user as tie-breaker. The user's ruling is final; apply it, mark the plan `plan_version: final (user-arbitrated)`, and proceed to Checkpoint 2.

## What makes a good plan (both sides optimize for this)

- Every step maps to at least one requirement `R#`; every requirement is covered by at least one step
- Every step has a deliverable and a verification method ("run X, expect Y")
- Steps are ordered by dependency; independent steps are marked parallelizable
- Risky/uncertain steps are front-loaded so failures happen early
- No step is so large that a Codex invocation can't complete it in one pass

## Anti-patterns to police

- **Rubber-stamping:** an APPROVE in round 1–2 with zero critiques is suspicious. Claude should explicitly ask Codex for at least one round of substantive critique ("identify the weakest step and the biggest unstated risk") before accepting an early APPROVE.
- **Oscillation:** if the same point flips twice (A→B→A), freeze it, mark it contentious, and resolve it via requirements — or park it for user escalation rather than burning rounds.
- **Scope creep:** any step not traceable to a requirement gets cut or sent back to the user as a proposed requirement change.
