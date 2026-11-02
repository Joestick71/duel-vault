---
name: duel-vault
description: >-
  Work on a project defined in an Obsidian vault,
  bootstrapping context from the vault's CLAUDE.md, Home.md and its project-specific skills in
  .claude/skills/, then executing the task either directly (routine work) or through the full
  Claude⇄Codex duet workflow (interview → consensus plan → Codex build → critical review) with
  Obsidian session logs in the vault's Sessions folder. USE THIS SKILL whenever the user names one of their
  Obsidian vault projects and asks to work on it, open a session on it, or build something for it —
  "lavora sul progetto X", "apri una sessione su <progetto>", "duel vault", "vault duet",
  "duet sul progetto", "duello sul progetto", "nuova sessione di lavoro su <progetto>",
  "fai X per il progetto Y" — even if they don't say "duel", "duet" or "Obsidian" explicitly.
  Also trigger when the user asks to run a claude-codex duel or duet inside or against one
  of their vaults, or to resume an interrupted one — "riprendi il progetto X", "riprendi la
  sessione", "continua dove eravamo rimasti su <progetto>", "Codex è di nuovo disponibile".
---

# Duel Vault

A Claude⇄Codex duet anchored to a **project defined in an Obsidian vault**. The vault is the source of truth: its `CLAUDE.md` (binding conventions), `Home.md` (project state) and its project skills in `<vault>/.claude/skills/` are the primary references for every decision. The workflow is **adaptive** — routine tasks execute directly, substantial tasks get the full four-phase duet.

**Roles:**
- **Claude Code (main thread)** — orchestrator, project-context keeper, interviewer, plan co-author, critical reviewer. Keeps every *judgement*; hands *execution* down a tier.
- **Claude subagents** — cheap executors that never decide: the executor tier (default `sonnet`) carries out Route A tasks from a self-contained brief; the mechanical tier (default `haiku`) runs verification commands and NotebookLM wait-and-download, and reports back.
- **Codex** — plan critic on a strong model (`codex_plan`) and builder on a cheaper one (`codex_build`), only on the full route

**Model tiers.** Read `references/model-tiers.md` once per session and follow it: what to delegate and what stays inline, the brief templates, when a cheap tier escalates, and how each tier is accounted. In one line: **judgement stays on the main thread, execution goes down a tier, deterministic work goes to scripts.**

**Record and recovery.** Every session — both routes — keeps a complete journal of activities, decisions, file changes and tokens (`references/session-journal.md`) and a `session.json` whose `next_action` is always current, publishes its progress to the project's own NotebookLM notebook, and pauses cleanly when Codex or Claude runs out of quota so it can resume the moment the resource is back (`references/session-recovery.md`). All of it is Tier 0: scripts, no model tokens.

## Phase 0 — Project bootstrap

Read `references/project-bootstrap.md` and follow it. Summary:

1. Load or create `~/.claude/duel-vault.config.json` (`projects_root` — ask the user for it on first run, no built-in default; `models` — the tier assignments, defaults in `references/model-tiers.md`; optional `budgets` and `progress_notebook` — `references/session-recovery.md`). Resolve the project the user named to a vault directory; if ambiguous or missing, list available vaults and ask.
   - **Open sessions first.** Run `scripts/session-state.py list --vault <vault> --open`. If a session is active or paused, tell the user (slug, phase, next action, what it is blocked on) and offer to resume it — `session-state.py resume` prints the brief to continue from — or to close it (`set --status abandoned`) and start fresh. Never start a second session over an open one silently. If the vault has no `.duel-vault/` folder but the user says work was in progress elsewhere, `scripts/progress-sync.py pull --vault <vault> --restore` rebuilds it from the progress notebook.
2. Read, in this order: `<vault>/CLAUDE.md` (binding — conventions, language, structure, end-of-session duties), `<vault>/Home.md` (current state, if present), and the frontmatter of every `<vault>/.claude/skills/*/SKILL.md` (the project's own tools).
3. Distill a **project brief** (in memory, a few lines): what the project is, operating language, relevant conventions, which project skills match the task, end-of-session obligations. Everything you do and everything you send to Codex must respect this brief — Codex has no memory, so relevant excerpts travel inside every prompt.
4. Run `scripts/session-history.py --vault <vault>` and keep its **known failure modes** block alongside the brief (`references/session-history.md`). It extracts what prior sessions in this vault learned: findings that recurred, decisions already arbitrated, deviations Codex has made before, residuals past sessions accepted, steps a cheap model tier could not finish. If there are no prior sessions, say so in one line and move on. These are **priors, not requirements** — hypotheses to check, never on their own a reason to change the plan.

## Triage — pick the route

Classify the task and tell the user which route you chose and why (one line — they can override):

- **Route A — direct execution.** The task is routine or small: it matches an existing project skill (e.g. a monthly analysis, a script in the project's voice), touches few files, or follows a workflow the vault already documents. No interview, no consensus, no Codex. Write an **execution brief** (template in `references/model-tiers.md`: task, project-brief excerpts, the project skill to follow, files it may touch, report format) and delegate the execution to the executor tier (`models.claude_executor`). Keep inline only what is not worth a cold start — one or two tool calls, a small edit — and any step that has to ask the user something. Check what comes back against the task and the vault's conventions, then write a single compact session log yourself (template in `references/obsidian-logging.md`) and lint it before closing. Name the executor in the triage line (e.g. "Route A — esecuzione delegata a `sonnet`").
- **Route B — full duet.** The task is substantial: new multi-step deliverables, code to build, anything where getting requirements and plan wrong is expensive. Run Phases 1–4 below. Preflight Codex now (see `references/codex-cli.md`) and settle the two Codex models: `codex_plan` (critique — the strongest model) and `codex_build` (build and fixes — a cheaper one), plus `codex_build_effort`. Then the token budgets: `scripts/session-state.py budgets --project <name>` — if it says NOT configured, ask the user for this project's per-session caps (recommend values sized to the task; "track only" is a valid answer) and save them with `--set` / `--track-only` before `session-log.py init`, which snapshots them (`references/session-recovery.md`). Ask each of these on the first run and save it; afterwards confirm the saved values in one line. Never default silently.

When in doubt, say so and let the user pick. A task that starts on Route A and grows can be upgraded: stop, say why, and enter Phase 1 with what you've learned so far.

**NotebookLM lane (orthogonal, opt-in).** Either route can additionally run a NotebookLM source lane — a corpus of external sources turned into vault deliverables (report, podcast, mind map, infographic, …). It is **off unless the user explicitly asks for it**; if you think it would help, say so in one line at triage and let them decide. When it opens, read `references/notebooklm.md` and follow it: preflight `notebooklm auth check --test --json` alongside the Codex preflight, always address the notebook by explicit id, run every long generation through a background agent on the mechanical tier that only waits and downloads, and let nothing reach the vault except through `scripts/nblm-import.py`. If a project skill already drives NotebookLM for this kind of task, that skill owns the notebook — don't open a parallel lane.

**Session setup (both routes), right after triage:** `scripts/session-log.py init --vault <vault> --session <slug> --route A|B --task "<one line>"` creates the working dir `<vault>/.duel-vault/<session-slug>/` (dot-folder — Obsidian ignores it) with `session.json` (status, phase, next action, a snapshot of the resolved `models` and `budgets`), `journal.jsonl` and `tokens.json`; Route B also keeps message-passing files, prompts and raw Codex output there. Log the triage decision as the first `decision` event. Route B's vault log dir is `<vault>/Sessions/<session-slug>/` for all Obsidian notes, starting with the MOC `00 - <session-slug> MOC.md` (templates in `references/obsidian-logging.md`). Preflight `notebooklm auth check --test --json` once for the progress notebook; if it fails, say so in one line — pushes will queue, nothing blocks.

## Journal, state and token guard (both routes, throughout)

Read `references/session-journal.md` and `references/session-recovery.md` once per session. The rules, all via scripts:

- **Before each step:** `session-state.py set --phase … --step … --next "<the exact next action>"` — written for a reader with no memory of this conversation. A Claude limit stops the turn without warning; `next_action` is what the next session will do.
- **As it happens:** `session-log.py event` for every activity, decision (with `--note` naming the note that carries its callout), checkpoint, escalation and warning; `session-log.py change --file …` for every file created, modified or deleted — by you, Codex or a subagent; `session-log.py invocation` for every Codex call, subagent and NotebookLM generation, with its tokens. Started-but-unfinished work goes in `pending` (`set --pending-add` / `--pending-done`).
- **Token guard:** `invocation` and `tokens add` evaluate the session budgets. Exit 3 → warn the user at the next natural pause. Exit 4 → finish the step in flight, `session-state.py pause --resource budget`, push progress, ask the user.
- **Limits:** when Codex (or NotebookLM) fails, pass its stderr to `session-state.py limit` **before** any retry. Exit 1 → ordinary error, normal retry rule. Exit 0 → the session is paused with the reset time; log the invocation `--status limit`, push progress, start `session-state.py wait` **in the background** (no tokens while waiting; you are re-invoked when the resource is back), keep doing whatever does not need it, then `session-state.py resume`.
- **Checkpoints:** at each human checkpoint and every pause, add the main-thread estimate (`session-log.py tokens add --resource claude_main`), run `session-log.py verify`, and `scripts/progress-sync.py push --vault <vault> --session <slug>`.

## Phase 1 — Interview (Route B, Claude)

Interview the user **relentlessly** about every aspect of the task until you reach a shared understanding. Walk down each branch of the design tree — goal & context, scope & non-goals, functional requirements (numbered `R1..Rn`, each independently verifiable), constraints, inputs/outputs, edge cases, acceptance criteria — resolving dependencies between decisions one by one: settle the decisions a branch hinges on before descending into it. Pin vague words ("fast", "simple") to something measurable.

Rules of engagement:

- **One question at a time.** Ask a single question, wait for the answer, then continue. Asking multiple questions at once is bewildering. (`AskUserQuestion` with one question per call is fine.)
- **Recommend an answer with every question.** State your recommended option and why in one line; the user can accept it or override.
- **Mine the history for questions, not for constraints.** A finding that recurred in this vault is often a requirement the user never thought to state — put it to them as a question. Never let a prior silently narrow the scope before the requirements are settled.
- **Facts are looked up, decisions are asked.** If something can be established by exploring the vault, the codebase, or the project skills (CLAUDE.md, Home.md, existing files, prior sessions), go find it instead of asking — confirm it in one line at most. Every genuine *decision*, however, is the user's: put each one to them and wait for the answer, even when the recommendation seems obvious.
- **Do not proceed until shared understanding is confirmed.** No planning, no Codex, no execution before the user explicitly confirms the requirements reflect a shared understanding.

Write `01-requirements.md` to the vault log dir. **CHECKPOINT 1:** present it and get explicit approval — this is the confirmation of shared understanding that unlocks Phase 2.

## Phase 2 — Consensus planning (Route B, Claude ⇄ Codex)

Read `references/consensus-protocol.md` and follow it. Claude drafts the plan (each step mapped to requirements, with deliverable and verification), Codex critiques/counter-proposes via `codex exec` on the critic model (`codex_plan`) in read-only sandbox, iterate until both emit `verdict: APPROVE` on the same plan version. Hard cap 5 rounds, then escalate to the user as tie-breaker.

Every planning prompt to Codex must embed: the requirements, the protocol rules, **the project brief** — vault path, conventions from CLAUDE.md, the project skills that exist (so Codex doesn't plan to rebuild what a skill already does) — **and the known-failure-modes block** from Phase 0. Codex has no memory of prior sessions either; without that block the vault rebuilds the same defect every session.

Write the agreed plan as `02-plan.md` plus round files under `02a-consensus-rounds/`. **CHECKPOINT 2:** present it and get explicit approval. Last human gate.

## Phase 3 — Build (Route B, Codex)

Codex executes the plan on the builder model (`codex_build`, with `codex_build_effort`); Claude orchestrates and does not write code. Small plans (≤5 steps) can go whole; larger ones go step-by-step so failures are isolated. Each prompt is self-contained: relevant plan steps verbatim, the requirements they map to, workspace state, project-brief excerpts, the known-failure-modes block (especially the deviations Codex has made in this vault before), and the instruction to report files touched and deviations. Use write-enabled sandbox flags with `-C` pointed at the directory CLAUDE.md designates for this kind of work (default: the vault).

**Exception, NotebookLM lane only:** Codex has no Google authentication in its sandbox, so Claude executes the plan's NotebookLM steps itself and hands Codex only the materialized files. Log them in `03-build-log.md` like any other invocation, marked as NotebookLM/Claude. This is not a licence to write code — everything the plan implements in code still goes to Codex.

After each invocation: verify claimed files exist, run the plan's smoke check for that step, run `scripts/vault-lint.py --files <notes touched>` (see `references/vault-lint.md`), append the entry to `03-build-log.md` **as you go**, with the model that ran it. Run a smoke check inline when it is a command or two; when it is longer or noisy (a test suite, a build), hand it to a verifier on the mechanical tier (brief in `references/model-tiers.md`) so the raw output stays in `raw/` and only its PASS/FAIL table reaches you. Include the same lint command in the build prompt so Codex self-checks before reporting back. Deviations: acceptable improvement (keep, document why) or violation (re-prompt with correction) — never silently accept a requirement violation. A step whose verification still fails after one corrective re-prompt **escalates** to `codex_plan` for that step only, logged as `> [!escalation]`; the next step starts on `codex_build` again.

## Phase 4 — Critical review (Route B, Claude) with loop-back

**Run the mechanical gate first:** `scripts/vault-lint.py --session <session dir>` (plus `--require-field` for any frontmatter field the vault's CLAUDE.md mandates — `references/vault-lint.md`). It settles frontmatter validity, wikilink resolution, filename conventions and requirement coverage deterministically, for free. Every `ERROR` it reports is a finding — at least MAJOR, since a note with invalid frontmatter is invisible to Obsidian. Don't spend review effort re-deriving what it already proved.

**Then check the recurring findings from Phase 0 explicitly** — they are this vault's highest-prior-probability defects, and they cost one targeted look each.

**If the NotebookLM lane is open**, you may also ask the notebook to verify claims in a content deliverable against its own sources (`references/notebooklm.md`). Its answers are candidate findings you verify yourself before classifying them — never a gate.

Then review everything yourself — read it, run it, test it — against every `R#` and every plan step, **and against the vault's conventions** the linter can't judge (language, naming sense, whether a requirement is genuinely satisfied — CLAUDE.md violations are findings too). Running the plan's verification commands is execution and can go to the mechanical-tier verifier like in Phase 3; reading the deliverable and judging the results is review and stays with you. Classify findings BLOCKER / MAJOR / MINOR in `04-review-cycle-N.md`. Any BLOCKER or MAJOR → fix-brief to Codex on `codex_build` (Phase 3 machinery), re-review; max 3 fix cycles. A BLOCKER or MAJOR that survives a fix cycle goes to `codex_plan` in the next one (`> [!escalation]`). Then write `05-final-report.md` (outcome per requirement, residual issues, token table) and update the MOC.

## Closing duties (both routes)

Before declaring the session done:

0. **The session must lint clean.** `scripts/vault-lint.py --session <dir>` (Route B) or `--files <log>` (Route A) must exit 0. Fix what it reports; if an error is a deliberate deviation from the templates, say so explicitly in the final report rather than leaving it unexplained.
   - *NotebookLM lane only:* every artifact that entered the vault went through `scripts/nblm-import.py`, `06-sources.md` records the corpus (Route A: the provenance block in the log), and no artifact is left pending unmentioned — a generation still running at closing time is a residual in the final report, with its id.
1. **Honor the vault's end-of-session obligations** from CLAUDE.md — e.g. update the vault's index notes, memory files, dashboards, or commit+push if the vault demands it. These are not optional; they're part of the project's contract.
2. Update `Home.md` only if the vault's own conventions say the dashboard tracks this kind of output.
3. **Close the record**, in this order: `session-state.py set --status complete` (logs `session_end`); `session-log.py render` (Route B: writes `07-activity-log.md`; Route A: `--format block --heading <"Activity" in the vault's language>`, pasted into the log, then re-lint the log); `session-log.py verify` must exit 0 — if a fix needs new events, reopen with `set --status active`, fix, and close again; then `progress-sync.py push --session <slug>` (adds the session's summary to the progress notebook; a queued push is fine — say so).
4. Present the final report (Route B) or the session log (Route A) to the user, with the token line.

## Token accounting

Route B in full; Route A needs just one line in its log (main thread estimate, plus the executor's figure if it ran). Same rules as the duet: Codex tokens **measured** per invocation (capture method verified at preflight — `references/codex-cli.md`) and attributed to the model that ran them, main-thread Claude tokens **estimated** as `ceil(chars/4)` and always labeled *est.*, delegated Claude tokens (subagents) taken from the usage each subagent's completion reports, as `tokens_claude_delegated` — or `"unavailable"`, never estimated (`references/model-tiers.md`), `tokens.json` written only through `session-log.py invocation` / `tokens add` (never by hand), one-line token status at each checkpoint (`session-state.py guard`), full per-phase table in the final report (`session-log.py tokens summary` prints it). If capture fails, record `"unavailable"` — never invent numbers. NotebookLM exposes no token counts at all: account it as a separate column of *operations* (artifacts generated, sources imported) with tokens `unavailable`, never as an estimate.

## Logging rules

Every vault note follows `references/obsidian-logging.md`: YAML frontmatter with `project`, `session`, `phase`, tags `[duel-vault, duel-vault/<phase>]`, wikilinks to siblings, `> [!decision]` for every decision, `> [!warning]` for deviations, `> [!escalation]` whenever work moves up a model tier. Notes are written **in the vault's operating language** (from CLAUDE.md) and **during** the work, never reconstructed after. `scripts/vault-lint.py` enforces the mechanical half of these rules — see `references/vault-lint.md`. Alongside the notes, the journal records everything exhaustively (`references/session-journal.md`); `session-log.py verify` checks the two agree — every logged decision and escalation has its callout, every logged file still matches its hash.

## Failure handling

- Codex errors: first `session-state.py limit` on the stderr. A usage or rate limit pauses the session and waits for the reset (`references/session-recovery.md`) — never retry into it. Anything else (auth, timeout, crash): retry once, then surface the raw error. Never fabricate a Codex response.
- Claude out of quota: nothing can run, so nothing is lost only if `next_action` was written before the step began. If the `StopFailure` hook is installed (`session-state.py install-hook`, offered on first run), it pauses the session and notifies the user — desktop notification on any OS, or a configured command — when the limit resets. The user resumes with `claude --continue` or "riprendi il progetto <nome>"; Phase 0 finds the paused session and `resume` continues it.
- Quality failures on a cheap tier (a build step that stays red, a subagent whose output fails your check, a verifier report that contradicts its raw output) are not errors to retry blindly: one corrective re-prompt on the same tier, then escalate one tier per `references/model-tiers.md`. A bad verifier report skips the re-prompt — rerun its commands inline.
- Vault path unwritable mid-session: fall back to `~/.claude/duel-vault-staging/<session>/` and tell the user to copy manually.
- User interrupts are honored; on resume run `session-state.py resume` and follow its brief: re-read the project brief inputs (CLAUDE.md may have changed) and the latest phase files, and collect anything left in `pending`.
