# Model tiers — who spends which tokens

duel-vault separates **judgement** from **execution**, and the two are not worth the same
price. Judgement — triaging, interviewing, writing and arbitrating the plan, reviewing the
result — is where a strong model earns its cost and where a mistake is expensive.
Execution — following an approved plan, applying a project skill to a brief, running
verification commands, waiting on a download — is work a cheaper model does as well, as
long as it gets a self-contained brief and its output is checked by someone who judges.

The rule: **the main thread keeps judgement; execution goes down a tier; deterministic work
goes to scripts and costs no model tokens at all.**

## The tiers

| Tier | Who | Default | Does |
|------|-----|---------|------|
| **0 — Script** | `scripts/*.py`, shell | — | lint, session history, NotebookLM manifest/import, file-exists checks |
| **1 — Mechanical** | Claude subagent | `haiku` | runs the commands it is given, verbatim, and reports what happened: plan verification checks, NotebookLM wait-and-download. Never edits, never decides |
| **2 — Executor** | Claude subagent | `sonnet` | Route A execution: applies a project skill or a documented workflow to an execution brief |
| **2 — Builder** | Codex `codex_build` | user's choice — a cheaper model | Phase 3 build steps, Phase 4 fix cycles |
| **3 — Critic** | Codex `codex_plan` | user's choice — the strongest model | Phase 2 plan critique; escalation target for the builder |
| **3 — Orchestrator** | Claude main thread | the user's session model | triage, interview, plan authoring, arbitration, review, session logs. Never delegated |

The subagent tiers are set in `~/.claude/duel-vault.config.json` (below) and passed as the
Agent tool's `model` parameter. The main thread's model is the user's Claude Code session —
not the skill's to change.

## When to delegate — and when not to

A subagent starts cold: it pays for its own system prompt and for the brief before doing any
work. Delegation saves tokens only when the work would otherwise pull a lot of content
through the main thread's context, where every later turn carries it again.

- **Delegate** when execution means several tool calls, writing more than a small note,
  reading large inputs, or producing long output (a test suite, a build, a multi-file
  deliverable).
- **Stay inline** when the work is one or two tool calls or a small edit — the cold start
  costs more than the work.
- **Never spawn an agent to run a Tier 0 script.** `vault-lint.py`, `session-history.py`
  and `nblm-import.py` print compact output by design; run them directly.
- **Interactive parts stay inline.** A subagent cannot ask the user anything. If a project
  skill asks questions as it goes, the main thread runs the questions and delegates only
  the non-interactive production that follows.
- **Voice-sensitive or editorial work** (writing in the author's voice, editorial
  judgement) goes to the executor tier at minimum — never to the mechanical tier.

State the choice in the triage line — "Route A — esecuzione delegata a `sonnet`" or "Route
A — inline, due modifiche" — so the user can override it as they can override the route.

## Briefs — the subagent knows only what you send

Like Codex, a subagent has no memory of this conversation. Every brief is self-contained.
Write it to `<vault>/.duel-vault/<slug>/prompts/` when a working dir exists (Route B), so
it stays auditable; on Route A pass it directly.

**Executor brief (Tier 2, Route A):**

```
Task: <one paragraph — what to produce, for whom, where it lands>
Vault: <abs path> · Language: <operating language from CLAUDE.md>
Follow: <abs path to the project skill's SKILL.md> — read it first and follow it
        (or: the workflow CLAUDE.md documents for this, quoted)
Binding conventions: <the CLAUDE.md excerpts that apply: naming, frontmatter, folders>
Known failure modes: <the relevant lines from the session-history block>
Touch only: <files/folders it may create or modify>
Do not: write session logs, update Home.md or indices, commit — the main thread does that
Check before reporting: <the verification the skill or workflow defines, if any>
Report: every file created or modified (abs path); anything you could not do and why;
        every deviation from the skill or the conventions, with its reason.
```

**Verifier brief (Tier 1, Phase 3/4):**

```
Run these commands exactly as written, in order, from <dir>. Do not modify any file,
do not fix anything, do not substitute other commands.

  1. <command>    expect: <expected result, from the plan's verification>
  2. ...

Save the full output of each to <vault>/.duel-vault/<slug>/raw/verify-<id>-<n>.txt.
Report only: a table  # · command · expected · actual (one line) · PASS/FAIL,
then the last 20 lines of output for every FAIL.
```

The verifier reports; the main thread judges. A PASS is evidence, not a verdict — the plan's
verification may itself be too weak, and that is a review finding.

**Check what comes back.** Verify the files the subagent claims exist, read the deliverable
against the task and the conventions, open the raw output when a verifier's table looks
too clean. Delegated work gets the same scrutiny as Codex work: trust, but verify. Then record it: the subagent
does not write the journal — log the files it reports with `session-log.py change
--actor subagent:<model>` and its usage with `session-log.py invocation --resource
claude_delegated` (`session-journal.md`).

## Escalation — cheap first, but not stubbornly

Infrastructure errors (auth, rate limit, timeout) are not quality signals: retry once on the
same tier, as `SKILL.md` says. **Quality failures** escalate one tier:

| Tier | Escalate when | To |
|------|---------------|----|
| Builder (`codex_build`) | a step's verification still fails after one corrective re-prompt, or a BLOCKER/MAJOR survives a fix cycle | `codex_plan`, for that step or finding only |
| Executor (`claude_executor`) | its output fails the main thread's check after one corrective re-brief | main thread, inline |
| Mechanical (`claude_mechanical`) | its report is malformed, incomplete, or contradicts the raw output files | main thread, inline |

Escalation is per step, never for the rest of the session: the next step starts on the cheap
tier again. Record every escalation with an `> [!escalation]` callout in the note where it
happened (build log, review cycle, Route A log), naming the step, the tier it left, the tier
it went to, and why:

```markdown
> [!escalation] Step 4: `<build model>` → `<plan model>` — test di integrazione ancora rosso
> dopo il re-prompt correttivo (stesso import circolare)
```

`session-history.py` extracts these callouts across sessions. If the priors show the same
kind of step escalating in this vault again and again, say so at triage and propose starting
that kind of step on the higher tier — a recommendation the user accepts or rejects, never a
silent change.

## Configuration

In `~/.claude/duel-vault.config.json`:

```json
"models": {
  "claude_mechanical": "haiku",
  "claude_executor": "sonnet",
  "codex_plan": null,
  "codex_build": null,
  "codex_build_effort": null
}
```

- `claude_mechanical`, `claude_executor` — values for the Agent tool's `model` parameter
  (`haiku`, `sonnet`, `opus`). A missing key falls back to the default above.
- `codex_plan`, `codex_build` — Codex model names. There is **no built-in default**: which
  models exist depends on the user's subscription. Ask on the first Route B session —
  recommend the strongest model for `codex_plan` and a cheaper, faster one for
  `codex_build` — save the answers, and on later sessions confirm them in one line rather
  than asking from scratch. Never pick silently. Using the same model for both is allowed;
  it just forgoes the saving.
- `codex_build_effort` — reasoning effort for build and fix invocations, passed as
  `-c model_reasoning_effort=<value>` once the preflight confirms the flag
  (`codex-cli.md`). `null` passes nothing and keeps the model's default. Recommend
  `medium`; `low` suits plans whose steps are mechanical. Escalated invocations never pass
  it — an escalation exists to spend more, not less.

Snapshot the resolved `models` block into `session.json` at Route B setup, so a resumed
session uses the same tiers it started with.

## Accounting

Each tier is accounted where it is measured, never blended into another:

- **Codex** — measured per invocation as before. The build log records which model ran each
  invocation, so the critic/builder split is recoverable.
- **Claude delegated** — when a subagent's completion reports its token usage, record that
  figure under `tokens_claude_delegated` (measured), per phase. When it reports none, record
  `"unavailable"`: the main thread cannot see the subagent's traffic, so an estimate would be
  invented.
- **Claude main** — `tokens_claude_est`, estimated as before. The briefs the main thread
  writes and the reports it reads count here.

All three are recorded through `scripts/session-log.py` (`invocation` for Codex and
subagents, `tokens add --resource claude_main` for the estimate), which also checks them
against the session's budgets — `references/session-recovery.md`.
