# vault-lint — the mechanical gate

`scripts/vault-lint.py` checks everything about a session's notes that can be checked
deterministically: frontmatter schema, tag conventions, wikilink resolution, filename
agreement, session-folder completeness, requirement coverage.

These are exactly the checks that do not need a model. Running them as code makes them
free, instant and impossible to hallucinate — and it keeps the Phase 4 review focused on
the things only judgement can catch (is the deliverable actually correct? does it satisfy
the requirement's intent?).

**The linter never replaces the review. It removes the mechanical half of it.**

## Invocation

```bash
# whole vault — every session under <vault>/Sessions/
python3 scripts/vault-lint.py --vault "$VAULT"

# one session folder
python3 scripts/vault-lint.py --session "$VAULT/Sessions/$SESSION"

# individual notes (what Codex runs as a self-check)
python3 scripts/vault-lint.py --files "$VAULT/Sessions/$SESSION/03-build-log.md"
```

Flags:

| Flag | Effect |
|------|--------|
| `--require-field NAME` | extra frontmatter field this vault's `CLAUDE.md` mandates for all notes (repeatable) |
| `--check-sections` | also warn on missing template sections — **English heading names only**, so leave it off for vaults that write notes in another language |
| `--json` | machine-readable output (`{errors, warnings, issues[]}`) |
| `--strict` | warnings count toward the exit code |
| `--quiet` | summary line only |

Exit codes: `0` clean · `1` errors present (or warnings under `--strict`) · `2` bad usage or unreadable path.

The vault's mandated fields can also live in `~/.claude/duel-vault.config.json` as
`"lint_required_fields": ["status", "rank"]`, so you don't have to pass them every run.
Fields listed there are **not** applied to consensus round files, which carry the
protocol header verbatim.

PyYAML is used when installed and a built-in fallback parser is used when it isn't. When
strict YAML parsing fails, the linter recovers what it can and reports both the YAML
error and every field problem it can still see — one bad value never hides the rest.

## Where it runs in the workflow

1. **Phase 3, after every Codex invocation.** Run `--files` over the notes that
   invocation touched. Catches a malformed frontmatter block at the moment it is written,
   not three phases later. Put the same command in the build prompt so Codex self-checks
   before reporting back.
2. **Phase 4, before you read anything.** Run `--session` first. Every `ERROR` becomes a
   finding in `04-review-cycle-N.md` — schema and link breakage is at least MAJOR, since
   a note with invalid frontmatter is invisible to Obsidian's property queries. Then do
   the semantic review on top of a structurally clean session.
3. **Phase 4 exit condition.** The session is not clean while the linter reports errors.
4. **Route A closing.** Run `--files` on the single session log before declaring done.

## Check catalogue

| Code | Level | Check |
|------|-------|-------|
| `VD000` | ERROR | file missing or unreadable |
| `VD001` | ERROR | note has no YAML frontmatter |
| `VD002` | ERROR | frontmatter is not valid YAML (Obsidian will not read the properties) |
| `VD003` | ERROR | required frontmatter field missing |
| `VD004` | ERROR | field has the wrong type, format or enum value |
| `VD005` | ERROR | unfilled `<template placeholder>` left in frontmatter |
| `VD006` | ERROR | `tags` missing `duel-vault` or `duel-vault/<phase>` |
| `VD007` | WARN | `session:` disagrees with the session folder name |
| `VD010` | ERROR | broken wikilink or embed (WARN while the MOC status is not `complete` and the target is an expected session note) |
| `VD011` | WARN | wikilink resolves ambiguously to several notes |
| `VD012` | ERROR | relative markdown link to a note — the convention is wikilinks |
| `VD013` | WARN | malformed callout header |
| `VD020` | WARN | unexpected filename in the session folder / loose note in `Sessions/` |
| `VD021` | ERROR | filename disagrees with frontmatter (review cycle, round number, round author, Route A date, MOC slug) or an illegal `PROPOSE` verdict |
| `VD022` | ERROR/WARN | session structure: missing MOC, finished session missing an earlier phase note, non-contiguous review cycles, a codex round with no claude round |
| `VD030` | WARN | expected template section missing (only with `--check-sections`) |
| `VD031` | WARN | unfilled `<template placeholder>` left in the body |
| `VD040` | ERROR | a requirement declared in `01-requirements.md` is never referenced by `02-plan.md` |
| `VD041` | ERROR | a note references an `R#` that does not exist in `01-requirements.md` |
| `VD042` | WARN | a requirement is missing from `05-final-report.md` |

Requirement coverage is matched on bare `R<n>` tokens, so it works regardless of the
language the notes are written in. Fenced code blocks and inline code spans are excluded
from every body scan, so example snippets never produce findings.

## What it deliberately does not check

- **Language.** Whether a note is in the vault's operating language is not mechanically
  decidable, and heuristics here produce false positives. That stays a review finding.
- **Content quality.** Whether a requirement is actually satisfied, whether a plan step is
  sensible, whether a finding was correctly classified.
- **Token arithmetic.** Figures are checked for type (`int` or `"unavailable"`), never for
  whether the totals add up — that would encode assumptions about phases that legitimately
  vary.

## Extending it

Add a note kind to `NOTE_SPECS` (fields with validators, expected tag suffix, expected
sections) and teach `classify()` its filename pattern. Add a vault-wide field rule via
`--require-field` rather than by editing the specs — the specs describe the duel-vault
contract, which is the same in every vault; the vault's own conventions stack on top.
