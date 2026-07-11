# duel-vault

A Claude Code skill that pairs **Claude** and **Codex CLI** to work on projects defined inside an Obsidian vault. The vault is the source of truth: its `CLAUDE.md` (binding conventions), `Home.md` (current state) and its own project-specific skills drive every decision Claude and Codex make.

## What it does

Point the skill at a named vault project (e.g. "lavora sul progetto Direful") and it:

1. **Bootstraps context** — reads the vault's `CLAUDE.md`, `Home.md`, and the frontmatter of its `.claude/skills/*/SKILL.md` files to learn the project's conventions, state, and existing tooling.
2. **Triages the task** into one of two routes:
   - **Route A — direct execution.** Routine, small tasks that match an existing project skill or workflow. Claude executes directly and writes a single compact session log.
   - **Route B — full duet.** Substantial, multi-step deliverables. Runs the complete four-phase workflow below.
3. **Runs the duet** (Route B only):
   - **Phase 1 — Interview.** Claude interviews the user one question at a time until requirements are fully pinned down and confirmed.
   - **Phase 2 — Consensus planning.** Claude drafts a plan, Codex critiques it via `codex exec` in a read-only sandbox, and the two iterate until both approve the same plan version.
   - **Phase 3 — Build.** Codex executes the approved plan step by step in a write-enabled sandbox; Claude orchestrates but does not write code.
   - **Phase 4 — Critical review.** Claude reviews the result against every requirement and the vault's own conventions, loops fixes back through Codex when needed, and writes a final report.
4. **Logs everything in Obsidian** — every phase produces frontmatter-tagged notes (with wikilinks, decision/warning callouts) in the vault's `Sessions` folder, written in the vault's own operating language.
5. **Tracks tokens** — Codex usage is measured per invocation, Claude usage is estimated, and a running token table is kept throughout the session.

## Repository layout

```
SKILL.md                          entry point: roles, phases, checkpoints, closing duties
references/
  project-bootstrap.md            how to resolve a vault project and build the project brief
  consensus-protocol.md           the Claude<->Codex planning protocol (rounds, verdicts, escalation)
  codex-cli.md                    how to preflight and invoke the Codex CLI (sandbox flags, model choice)
  obsidian-logging.md             note templates and conventions for the Sessions folder logs
```

## Requirements

- [Claude Code](https://claude.com/claude-code) with this skill installed under `.claude/skills/vault-duet/`
- [Codex CLI](https://github.com/openai/codex) available on `PATH` for Route B (full duet) tasks
- One or more Obsidian vaults on disk, each with at least a `CLAUDE.md`, registered under a shared `projects_root` in `~/.claude/vault-duet.config.json`

## Human checkpoints

The workflow never proceeds past two hard gates without explicit approval: the requirements document (end of Phase 1) and the consensus plan (end of Phase 2). Any blocker or major finding in the final review is fixed and re-reviewed (up to 3 cycles) before the session is considered closed.
