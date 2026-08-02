# duel-vault

A Claude Code skill that pairs **Claude** and **Codex CLI** to work on projects defined inside an Obsidian vault. The vault is the source of truth: its `CLAUDE.md` (binding conventions), `Home.md` (current state) and its own project-specific skills drive every decision Claude and Codex make.

## What it does

Point the skill at a named vault project (e.g. `lavora sul progetto <nome>`) and it:

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
  vault-lint.md                   the deterministic gate: what it checks, when it runs, check catalogue
  session-history.md              how prior sessions feed back into a new one
scripts/
  vault_common.py                 shared frontmatter parsing and note-name conventions
  vault-lint.py                   frontmatter/wikilink/structure linter for session notes
  session-history.py              extracts priors from prior sessions in the vault
```

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

## Requirements

- [Claude Code](https://claude.com/claude-code) with this skill installed under `.claude/skills/duel-vault/`
- [Codex CLI](https://github.com/openai/codex) available on `PATH` for Route B (full duet) tasks
- Python 3.10+ for `scripts/` (stdlib only; PyYAML is used when present but is not required)
- One or more Obsidian vaults on disk, each with at least a `CLAUDE.md`, registered under a shared `projects_root` in `~/.claude/duel-vault.config.json`

## Human checkpoints

The workflow never proceeds past two hard gates without explicit approval: the requirements document (end of Phase 1) and the consensus plan (end of Phase 2). Any blocker or major finding in the final review is fixed and re-reviewed (up to 3 cycles) before the session is considered closed.
