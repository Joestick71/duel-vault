# duel-vault

A Claude Code skill that pairs **Claude** and **Codex CLI** to work on projects defined inside an Obsidian vault. The vault is the source of truth: its `CLAUDE.md` (binding conventions), `Home.md` (current state) and its own project-specific skills drive every decision Claude and Codex make.

## What it does

Point the skill at a named vault project (e.g. `lavora sul progetto <nome>`) and it:

1. **Bootstraps context** — reads the vault's `CLAUDE.md`, `Home.md`, and the frontmatter of its `.claude/skills/*/SKILL.md` files to learn the project's conventions, state, and existing tooling.
2. **Triages the task** into one of two routes:
   - **Route A — direct execution.** Routine, small tasks that match an existing project skill or workflow. Claude writes an execution brief, a cheaper Claude subagent (default `sonnet`) carries it out, Claude checks the result and writes a single compact session log.
   - **Route B — full duet.** Substantial, multi-step deliverables. Runs the complete four-phase workflow below.
3. **Runs the duet** (Route B only):
   - **Phase 1 — Interview.** Claude interviews the user one question at a time until requirements are fully pinned down and confirmed.
   - **Phase 2 — Consensus planning.** Claude drafts a plan, Codex critiques it via `codex exec` on its strongest model in a read-only sandbox, and the two iterate until both approve the same plan version.
   - **Phase 3 — Build.** Codex executes the approved plan step by step on a cheaper build model in a write-enabled sandbox; Claude orchestrates but does not write code, and long verification runs go to a `haiku` subagent.
   - **Phase 4 — Critical review.** Claude reviews the result against every requirement and the vault's own conventions, loops fixes back through Codex when needed, and writes a final report.
4. **Logs everything in Obsidian** — every phase produces frontmatter-tagged notes (with wikilinks, decision/warning callouts) in the vault's `Sessions` folder, written in the vault's own operating language.
5. **Spends tokens by tier** — judgement stays on the main model, execution goes to cheaper ones, deterministic checks go to scripts (see *Model tiers* below).
6. **Tracks tokens** — Codex usage is measured per invocation and per model, subagent usage is taken from what each subagent reports, main-thread Claude usage is estimated, and a running token table is kept throughout the session, checked against optional per-session budgets.
7. **Records everything and survives cut-offs** — a complete journal of activities, decisions, file changes and tokens; a `next_action` that is always current; a clean pause when Codex or Claude runs out of quota and a resume as soon as it is back; and a progress snapshot in the project's own NotebookLM notebook (see *Journal, recovery and the progress notebook* below).

Diagrams of the use cases, the session lifecycle, the Route B sequence and the data flows are in [`ARCHITETTURA.md`](ARCHITETTURA.md) (in Italian).

## Repository layout

```
SKILL.md                          entry point: roles, phases, checkpoints, closing duties
ARCHITETTURA.md                   diagrams: use cases, lifecycle, sequences, data flows (Italian)
references/
  project-bootstrap.md            how to resolve a vault project and build the project brief
  consensus-protocol.md           the Claude<->Codex planning protocol (rounds, verdicts, escalation)
  model-tiers.md                  who spends which tokens: tiers, delegation, briefs, escalation, accounting
  codex-cli.md                    how to preflight and invoke the Codex CLI (sandbox flags, plan/build models)
  obsidian-logging.md             note templates and conventions for the Sessions folder logs
  vault-lint.md                   the deterministic gate: what it checks, when it runs, check catalogue
  session-history.md              how prior sessions feed back into a new one
  session-journal.md              the complete session record: what to log, when, and its verify gate
  session-recovery.md             state, token budgets, usage limits, pause/wait/resume, progress notebook
  notebooklm.md                   the opt-in NotebookLM source lane: preflight, agents, provenance
scripts/
  vault_common.py                 shared frontmatter parsing and note-name conventions
  vault-lint.py                   frontmatter/wikilink/structure linter for session notes
  session-history.py              extracts priors from prior sessions in the vault
  nblm-import.py                  materializes NotebookLM artifacts into the vault, with provenance
  session_common.py               shared journal/state/token primitives, limit-message parsing
  session-log.py                  journal, file changes, invocations, token ledger, verify, render
  session-state.py                next action, budgets, limits, pause, background wait, resume
  progress-sync.py                publishes the project's progress to its NotebookLM notebook
tests/
  test_session_tools.py           end-to-end tests with fake codex/notebooklm CLIs
  test_review_regressions.py      recovery, source identity, malformed data and YAML round trips
```

## Model tiers

Judgement and execution are not worth the same price. Triage, interview, planning and
review are where a strong model earns its cost; following an approved plan, applying a
project skill to a brief, running a test suite or waiting on a download are not. So the
skill splits the work:

| Tier | Who | Does |
|------|-----|------|
| Script | `scripts/*.py` | lint, history, NotebookLM import — no model tokens at all |
| Mechanical | Claude subagent, `haiku` | runs verification commands verbatim, NotebookLM wait-and-download |
| Executor | Claude subagent, `sonnet` | Route A execution from a self-contained brief |
| Builder | Codex `codex_build` | build steps and fix cycles, on a cheaper model and reasoning effort |
| Critic | Codex `codex_plan` | plan critique, and the escalation target for the builder |
| Orchestrator | Claude main thread | triage, interview, plan, review, logs — never delegated |

Delegation is not free — a subagent starts cold — so tiny tasks and anything that has to
ask the user stay inline. A cheap tier that fails a quality check gets one corrective
re-prompt, then the step **escalates** one tier, is logged as `> [!escalation]`, and the
next step starts cheap again. `session-history.py` collects those escalations, so a vault
where one kind of step keeps failing on the cheap tier says so at the next triage.

Tier assignments live in the `models` block of `~/.claude/duel-vault.config.json`. The
Codex models have no default (they depend on the subscription): they are asked once and
confirmed in one line afterwards. See [`references/model-tiers.md`](references/model-tiers.md).

## Journal, recovery and the progress notebook

Every session, on both routes, owns `<vault>/.duel-vault/<slug>/` with three files:
`journal.jsonl` (every activity, decision, file change with its hash, model invocation and
token figure, appended as it happens), `session.json` (status, phase, step, pending work and
the exact **next action**), and `tokens.json`. Three scripts write them — no model tokens:

```bash
python3 scripts/session-log.py event --vault "$V" --session "$S" --kind decision \
    --summary "Grafici in dark theme" --note "02-plan"
python3 scripts/session-log.py change --vault "$V" --session "$S" --file Notes/report.md --actor codex
python3 scripts/session-log.py invocation --vault "$V" --session "$S" --resource codex \
    --model "$BUILD_MODEL" --id build-step-3 --status ok --total 12400     # exit 3/4: budget warn/over
python3 scripts/session-state.py set --vault "$V" --session "$S" --next "Verify step 3 outputs"
python3 scripts/session-log.py verify --vault "$V" --session "$S"        # the logging gate
```

`verify` checks the record is whole: contiguous journal, no invocation left without an
outcome, every logged file still matching its hash (anything else is an unlogged edit),
every logged decision present as a `> [!decision]` callout. `render` turns the journal into
`Sessions/<slug>/07-activity-log.md`.

**When a resource runs out.** A failed Codex call goes to `session-state.py limit` before
any retry. If it is a usage or rate limit, the session is paused with the reset time parsed
from the message ("try again in 2 hours", "try again at 3:47 PM"), and
`session-state.py wait` — started in the background, costing nothing — sleeps until then,
probes Codex, and exits when it answers; Claude is re-invoked and `session-state.py resume`
prints a brief to continue from. A Claude limit cannot be handled from inside, which is why
`next_action` is written *before* each step: the next session — `claude --continue`, or
"riprendi il progetto X" — finds the open session at Phase 0 and resumes it. A Claude Code
`StopFailure` hook (`session-state.py install-hook`) pauses the session the moment Claude
runs out of quota and schedules a detached notifier that tells you when the limit resets —
a desktop notification on macOS, Linux or Windows, or any command you configure (a phone
push, a mail). Per-session token `budgets`, asked at each project's first Route B and saved
per project, warn at 80% and stop cleanly at 100%.

**The progress notebook.** Each project has its own NotebookLM notebook
(`<project> — avanzamento duel-vault`). `progress-sync.py push`, at every checkpoint and
pause, replaces its current-state source with a fresh snapshot (open sessions, next actions,
blocks, history, recent decisions and changes) and adds a summary of each closed session.
On another machine, `progress-sync.py pull --restore` rebuilds the open sessions from it.
It never blocks: if NotebookLM is unreachable the push is queued locally and retried. The
vault remains the source of truth. See [`references/session-journal.md`](references/session-journal.md)
and [`references/session-recovery.md`](references/session-recovery.md).

## The mechanical gate

Half of what a session log has to get right is decidable by code: frontmatter schema, tag
conventions, wikilinks that resolve, filenames that agree with their own frontmatter,
requirements that are actually covered by the plan. `scripts/vault-lint.py` checks all of
it deterministically — free, instant, no hallucination risk — and runs at three points:
after every Codex build invocation, before the Phase 4 review, and as a closing gate on
both routes. The model's review effort then goes where only judgement works.

```bash
python3 scripts/vault-lint.py --vault "$VAULT"                    # every session
python3 scripts/vault-lint.py --session "$VAULT/Sessions/$SLUG"   # one session
python3 scripts/vault-lint.py --files note.md --json              # one note, machine-readable
```

Exit code 0 means clean. Uses PyYAML when available, falls back to a built-in parser when
not. See [`references/vault-lint.md`](references/vault-lint.md) for the full check catalogue.

## Session memory

The vault is meant to be the memory, but the `Sessions/` folder was write-only: every
session produced findings, arbitrated decisions and deviations that no later session ever
read. `scripts/session-history.py` closes that loop at Phase 0, extracting the priors a new
session should start with — findings that recurred across sessions, decisions already
settled, deviations Codex has made in this vault before, residuals past sessions accepted,
and process stats. The block travels inside every Codex prompt, because Codex has no memory
of prior sessions either.

```bash
python3 scripts/session-history.py --vault "$VAULT"
```

Extraction is structural (callout syntax, severity headings, frontmatter), so it works in
whatever language the vault writes its notes. It does not attempt semantic clustering — that
judgement belongs to the model reading the output. The block is explicitly labelled
**priors, not requirements**. See [`references/session-history.md`](references/session-history.md).

## The NotebookLM lane (opt-in)

NotebookLM is a capability, not a third route: either route can run with the lane open or
closed. It stays **closed unless the user explicitly asks for it** — it costs a
browser-backed login, minutes of wall clock, and an external dependency that sessions
without a source corpus should not pay for.

When it is open, three rules keep it from corrupting the workflow:

- **Nothing blocks.** Every long generation (podcast 10–20 min, video 15–45, deep research
  15–30) is started by the main thread and awaited by a background agent that only waits
  and downloads — it never generates, never re-plans, never writes into the vault.
- **Nothing enters the vault raw.** `notebooklm download` produces files with no
  frontmatter, no tags and no links; `scripts/nblm-import.py import` wraps them into proper
  notes (and turns mind maps into real Obsidian `.canvas` files) so the closing lint gate
  still passes.
- **Nothing is unattributable.** NotebookLM artifacts are not reproducible, so the corpus
  is recorded instead of the result: `nblm-import.py manifest` writes `06-sources.md` from
  the live notebook, and `vault-lint.py` errors when a session declares the lane without it.

```bash
python3 scripts/nblm-import.py import --vault "$V" --session "$S" --notebook "$NB" \
    --into "Ricerche/2026" --canvas .duel-vault/$S/artifacts/mind-map.json
python3 scripts/nblm-import.py manifest --vault "$V" --session "$S" --notebook "$NB" \
    --prompt "Report briefing-doc on <topic>"
```

Codex has no Google authentication in its sandbox, so Claude executes the plan's NotebookLM
steps itself and hands Codex only the materialized files — the one documented exception to
"Codex builds, Claude reviews". Full contract in
[`references/notebooklm.md`](references/notebooklm.md).

## Install

The skill directory must be named `duel-vault` — the name is load-bearing. It is the skill
Claude Code resolves, and it is written into every session note this workflow produces
(the `duel-vault` and `duel-vault/<phase>` tags, `type: duel-vault-moc`), into
`~/.claude/duel-vault.config.json`, and into the `.duel-vault/` working directory inside
each vault. Renaming it orphans existing sessions and config.

```bash
git clone https://github.com/Joestick71/duel-vault.git ~/.claude/skills/duel-vault
```

The repository, the skill and the install directory all carry the same name, so the default
clone directory is already correct.

Optionally, install the hook that pauses a session and notifies you when a Claude usage
limit resets (it adds one `StopFailure` entry to `~/.claude/settings.json`; `--dry-run`
shows it first, `--uninstall` removes it):

```bash
python3 ~/.claude/skills/duel-vault/scripts/session-state.py install-hook
```

## Requirements

- [Claude Code](https://claude.com/claude-code) with this skill installed under `.claude/skills/duel-vault/`
- [Codex CLI](https://github.com/openai/codex) available on `PATH` for Route B (full duet) tasks
- Python 3.10+ for `scripts/` (stdlib only; PyYAML is used when present but is not required)
- [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py) on `PATH` and authenticated — for sessions that open the NotebookLM lane, and for the progress notebook (without it, progress snapshots stay local and are queued)
- One or more Obsidian vaults on disk, each with at least a `CLAUDE.md`, registered under a shared `projects_root` in `~/.claude/duel-vault.config.json`

## Human checkpoints

The workflow never proceeds past two hard gates without explicit approval: the requirements document (end of Phase 1) and the consensus plan (end of Phase 2). Any blocker or major finding in the final review is fixed and re-reviewed (up to 3 cycles) before the session is considered closed.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The session tools are tested end to end against fake `codex` and `notebooklm` executables
placed on `PATH`, so the suite runs offline, costs nothing, and exercises the same
subprocess boundary the scripts use against the real CLIs.
