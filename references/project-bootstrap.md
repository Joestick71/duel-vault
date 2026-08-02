# Project bootstrap — resolving and loading a vault project

The whole point of duel-vault is that the project, not the conversation, is the source of truth. This file describes how to turn "lavora su <nome progetto>" into a loaded, binding project context.

## 1. Config and project resolution

Persistent config: `~/.claude/duel-vault.config.json`

```json
{
  "projects_root": "<absolute path to the directory containing the vaults>",
  "default_fix_cycles": 3,
  "max_consensus_rounds": 5,
  "lint_required_fields": []
}
```

If the file is missing, ask the user for the projects root (the directory whose subdirectories are Obsidian vaults), verify it exists, and save the config.

`lint_required_fields` lists frontmatter fields the vault mandates on every note beyond the duel-vault templates (e.g. `status`, `rank`). When CLAUDE.md declares such fields in step 2 below, record them here so `scripts/vault-lint.py` enforces them without needing `--require-field` on every run (see `references/vault-lint.md`).

Resolution: list the directories under `projects_root` and match the project name the user gave, case-insensitively and tolerantly (a lowercase or partial name matches the vault directory, e.g. "notes" → `Notes`, "field" → `Field Journal`). A directory qualifies as a project if it contains a `CLAUDE.md`. Rules:

- Exactly one match → proceed, stating which vault you resolved to.
- Zero or multiple matches → show the list of qualifying vaults and ask.
- The user may also give an absolute path; verify it contains `CLAUDE.md`.

## 2. What to read, in what order, and why

1. **`<vault>/CLAUDE.md` — binding.** This is the project's contract: identity, structure, operating language, naming and frontmatter conventions, workflows, tool stack, end-of-session duties (some vaults require updating indices or committing — treat those as requirements, not suggestions). Read it in full; if it exceeds 2000 lines, read it in blocks — never truncate.
2. **`<vault>/Home.md` — current state.** The dashboard: what exists, what's in progress, declared next steps. Optional (not every vault has one); when present it tells you where the task fits and which notes/links the deliverable should connect to.
3. **`<vault>/.claude/skills/*/SKILL.md` — the project's own tools.** Read only the YAML frontmatter (name + description) of each. These are capabilities the project already has; a task matching one of them is a strong Route A signal, and a Route B plan that ignores them is a planning bug. CLAUDE.md often documents additional global skills the project relies on (e.g. a "Skill disponibili" table) — treat that list as part of the toolset too.

Do not read the whole vault. CLAUDE.md's structure section tells you what exists; open individual notes only when the task needs them.

## 3. The project brief

Distill what you read into a short brief you keep in working memory and inject (as relevant excerpts, not wholesale) into every Codex prompt:

- **Identity:** what the project is, for whom, one line.
- **Language:** the operating language for notes, filenames, frontmatter (CLAUDE.md decides — never assume English).
- **Conventions that bind this task:** naming, frontmatter fields, folder targets, format preferences (callouts, wikilinks, dark-theme charts, …).
- **Available tools:** project skills + global skills the CLAUDE.md lists, each with when-to-use.
- **End-of-session duties:** indices to update, memory files, commit/push requirements.
- **State hooks:** where in Home.md / indices the new work must be linked.

If CLAUDE.md and the user's request conflict (e.g. the vault says Italian, the user asks in English for an English deliverable), the user wins for the deliverable, but say the conflict out loud and keep session logs in the vault's language.

## 4. Freshness

CLAUDE.md describes the vault as it was last updated, not necessarily as it is. Before relying on a load-bearing fact (a file the task needs, a folder you'll write into), verify it exists. If reality and CLAUDE.md disagree, note the drift in the session log and — if the vault's conventions ask for it — fix CLAUDE.md as part of closing duties.
