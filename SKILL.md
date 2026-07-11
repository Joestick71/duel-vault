---
name: vault-duet
description: >-
  Work on a project defined in an Obsidian vault (e.g. Direful, IdeaVerse, Aggeggiors, Casa Mia),
  bootstrapping context from the vault's CLAUDE.md, Home.md and its project-specific skills in
  .claude/skills/, then executing the task either directly (routine work) or through the full
  Claude⇄Codex duet workflow (interview → consensus plan → Codex build → critical review) with
  Obsidian session logs in the vault's Sessions folder. USE THIS SKILL whenever the user names one of their
  Obsidian vault projects and asks to work on it, open a session on it, or build something for it —
  "lavora sul progetto X", "apri una sessione su Direful", "vault duet", "duet sul progetto",
  "nuova sessione di lavoro su IdeaVerse", "fai X per il progetto Y" — even if they don't say
  "duet" or "Obsidian" explicitly. Also trigger when the user asks to run a claude-codex duet
  inside or against one of their vaults.
---

# Vault Duet

A Claude⇄Codex duet anchored to a **project defined in an Obsidian vault**. The vault is the source of truth: its `CLAUDE.md` (binding conventions), `Home.md` (project state) and its project skills in `<vault>/.claude/skills/` are the primary references for every decision. The workflow is **adaptive** — routine tasks execute directly, substantial tasks get the full four-phase duet.

**Roles:**
- **Claude Code** — orchestrator, project-context keeper, interviewer, plan co-author, critical reviewer, and sole executor on the light route
- **Codex** — plan co-author and builder, only on the full route

## Phase 0 — Project bootstrap

Read `references/project-bootstrap.md` and follow it. Summary:

1. Load or create `~/.claude/vault-duet.config.json` (`projects_root`, default `/mnt/d/Obsidian`). Resolve the project the user named to a vault directory; if ambiguous or missing, list available vaults and ask.
2. Read, in this order: `<vault>/CLAUDE.md` (binding — conventions, language, structure, end-of-session duties), `<vault>/Home.md` (current state, if present), and the frontmatter of every `<vault>/.claude/skills/*/SKILL.md` (the project's own tools).
3. Distill a **project brief** (in memory, a few lines): what the project is, operating language, relevant conventions, which project skills match the task, end-of-session obligations. Everything you do and everything you send to Codex must respect this brief — Codex has no memory, so relevant excerpts travel inside every prompt.

## Triage — pick the route

Classify the task and tell the user which route you chose and why (one line — they can override):

- **Route A — direct execution.** The task is routine or small: it matches an existing project skill (e.g. a monthly analysis, a script in the project's voice), touches few files, or follows a workflow the vault already documents. No interview, no consensus, no Codex. Execute it yourself using the project's skills and conventions, then write a single compact session log (template in `references/obsidian-logging.md`).
- **Route B — full duet.** The task is substantial: new multi-step deliverables, code to build, anything where getting requirements and plan wrong is expensive. Run Phases 1–4 below. Preflight Codex now (see `references/codex-cli.md`) and ask the user which Codex model to use — never default silently.

When in doubt, say so and let the user pick. A task that starts on Route A and grows can be upgraded: stop, say why, and enter Phase 1 with what you've learned so far.

**Route B setup:** working dir `<vault>/.vault-duet/<session-slug>/` (dot-folder — Obsidian ignores it) for message-passing files, prompts, raw Codex output, `session.json` and `tokens.json`; vault log dir `<vault>/Sessions/<session-slug>/` for all Obsidian notes, starting with the MOC `00 - <session-slug> MOC.md` (templates in `references/obsidian-logging.md`).

## Phase 1 — Interview (Route B, Claude)

Interview the user **relentlessly** about every aspect of the task until you reach a shared understanding. Walk down each branch of the design tree — goal & context, scope & non-goals, functional requirements (numbered `R1..Rn`, each independently verifiable), constraints, inputs/outputs, edge cases, acceptance criteria — resolving dependencies between decisions one by one: settle the decisions a branch hinges on before descending into it. Pin vague words ("fast", "simple") to something measurable.

Rules of engagement:

- **One question at a time.** Ask a single question, wait for the answer, then continue. Asking multiple questions at once is bewildering. (`AskUserQuestion` with one question per call is fine.)
- **Recommend an answer with every question.** State your recommended option and why in one line; the user can accept it or override.
- **Facts are looked up, decisions are asked.** If something can be established by exploring the vault, the codebase, or the project skills (CLAUDE.md, Home.md, existing files, prior sessions), go find it instead of asking — confirm it in one line at most. Every genuine *decision*, however, is the user's: put each one to them and wait for the answer, even when the recommendation seems obvious.
- **Do not proceed until shared understanding is confirmed.** No planning, no Codex, no execution before the user explicitly confirms the requirements reflect a shared understanding.

Write `01-requirements.md` to the vault log dir. **CHECKPOINT 1:** present it and get explicit approval — this is the confirmation of shared understanding that unlocks Phase 2.

## Phase 2 — Consensus planning (Route B, Claude ⇄ Codex)

Read `references/consensus-protocol.md` and follow it. Claude drafts the plan (each step mapped to requirements, with deliverable and verification), Codex critiques/counter-proposes via `codex exec` in read-only sandbox, iterate until both emit `verdict: APPROVE` on the same plan version. Hard cap 5 rounds, then escalate to the user as tie-breaker.

Every planning prompt to Codex must embed: the requirements, the protocol rules, **and the project brief** — vault path, conventions from CLAUDE.md, the project skills that exist (so Codex doesn't plan to rebuild what a skill already does).

Write the agreed plan as `02-plan.md` plus round files under `02a-consensus-rounds/`. **CHECKPOINT 2:** present it and get explicit approval. Last human gate.

## Phase 3 — Build (Route B, Codex)

Codex executes the plan; Claude orchestrates and does not write code. Small plans (≤5 steps) can go whole; larger ones go step-by-step so failures are isolated. Each prompt is self-contained: relevant plan steps verbatim, the requirements they map to, workspace state, project-brief excerpts, and the instruction to report files touched and deviations. Use write-enabled sandbox flags with `-C` pointed at the directory CLAUDE.md designates for this kind of work (default: the vault).

After each invocation: verify claimed files exist, run the plan's smoke check for that step, append the entry to `03-build-log.md` **as you go**. Deviations: acceptable improvement (keep, document why) or violation (re-prompt with correction) — never silently accept a requirement violation.

## Phase 4 — Critical review (Route B, Claude) with loop-back

Review everything yourself — read it, run it, test it — against every `R#` and every plan step, **and against the vault's conventions** (frontmatter validity, wikilinks that resolve, language, naming — CLAUDE.md violations are findings too). Classify findings BLOCKER / MAJOR / MINOR in `04-review-cycle-N.md`. Any BLOCKER or MAJOR → fix-brief to Codex (Phase 3 machinery), re-review; max 3 fix cycles. Then write `05-final-report.md` (outcome per requirement, residual issues, token table) and update the MOC.

## Closing duties (both routes)

Before declaring the session done:

1. **Honor the vault's end-of-session obligations** from CLAUDE.md — e.g. update indices (`Indice Generale.md`), memory files, dashboards, or commit+push if the vault demands it. These are not optional; they're part of the project's contract.
2. Update `Home.md` only if the vault's own conventions say the dashboard tracks this kind of output.
3. Present the final report (Route B) or the session log (Route A) to the user, with the token line.

## Token accounting

Route B only (Route A has no Codex and needs just a one-line estimate in its log). Same rules as the duet: Codex tokens **measured** per invocation (capture method verified at preflight — `references/codex-cli.md`), Claude tokens **estimated** as `ceil(chars/4)` and always labeled *est.*, `tokens.json` updated at the same moments you write the logs, one-line token status at each checkpoint, full per-phase table in the final report. If capture fails, record `"unavailable"` — never invent numbers.

## Logging rules

Every vault note follows `references/obsidian-logging.md`: YAML frontmatter with `project`, `session`, `phase`, tags `[vault-duet, vault-duet/<phase>]`, wikilinks to siblings, `> [!decision]` for every decision, `> [!warning]` for deviations. Notes are written **in the vault's operating language** (from CLAUDE.md — for this user usually Italian) and **during** the work, never reconstructed after.

## Failure handling

- Codex errors (auth, rate limit, timeout): retry once, then surface the raw error. Never fabricate a Codex response.
- Vault path unwritable mid-session: fall back to `~/.claude/vault-duet-staging/<session>/` and tell the user to copy manually.
- User interrupts are honored; on resume re-read `session.json`, the project brief inputs (CLAUDE.md may have changed) and the latest phase files.
