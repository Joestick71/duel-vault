# session-history — closing the loop on the Sessions folder

The workflow writes structured session notes and then never reads them again. The vault
is supposed to be the memory, but for prior *sessions* that memory was write-only:
Codex has no memory between invocations, and Phase 0 loaded `CLAUDE.md` and `Home.md` but
nothing about what previous sessions actually learned.

`scripts/session-history.py` closes that loop. It scans prior sessions and extracts the
priors a new session should start with:

- **Findings that recurred** across two or more sessions — the vault's actual failure modes
- **Recent BLOCKER/MAJOR findings** that haven't recurred yet
- **Settled decisions** (`> [!decision]` callouts from MOCs, plans and Route A logs) — so a
  point arbitrated in session 3 is not re-litigated in session 9
- **Build deviations already seen** (`> [!warning]` from build logs) — what Codex tends to do
- **Residual issues past sessions accepted** — MINORs surviving in each session's last review cycle
- **Process stats** — median consensus rounds, how often a fix cycle was needed, token medians

## Invocation

```bash
python3 scripts/session-history.py --vault "$VAULT"                      # injectable block
python3 scripts/session-history.py --vault "$VAULT" --json               # raw summary
python3 scripts/session-history.py --vault "$VAULT" --out "$WORKDIR/history.md"
```

| Flag | Effect |
|------|--------|
| `--limit N` | scan only the N most recent sessions (default 10, `0` = all) |
| `--max-items N` | cap entries per section (default 8) so the block stays prompt-sized |
| `--json` | raw summary instead of the rendered block |
| `--out FILE` | also write the output to a file, for prompt assembly |

Sessions are ordered by the MOC's `date` (falling back to the plan/final-report date, then
the folder mtime). Route A logs are scanned too — they carry decisions even though they
have no findings.

## How the extraction stays language-agnostic

Everything is keyed off structure, never off vocabulary: callout syntax (`> [!decision]`),
the `F<n> · SEVERITY · title` finding heading (any of `· • | : - — –` works as separator),
and frontmatter fields. Severity words are fixed by the template, so they are the same in
every vault. Fenced code blocks are excluded.

Two aggregations deserve explanation:

- **Recurrence** is exact-match after normalization (lowercase, punctuation and `R#` refs
  stripped). It does *not* attempt semantic clustering — "wikilink rotto" and "wikilink
  rotto verso Home" stay separate. That is deliberate: fuzzy clustering here would produce
  confident nonsense, and the model reading the block does that grouping better anyway.
- **Themes** list words appearing in high-severity findings from **at least two different
  sessions**. Requiring two distinct sessions is what removes the need for a
  language-specific stopword list.

Decisions, deviations and residuals are deduplicated by normalized text, so a decision
that recurs in five sessions is one entry weighted `5 sessions`, not five entries eating
the item budget.

## Where it runs in the workflow

**Phase 0, after the project brief.** Run it once and keep the rendered block with the
brief. If it reports no prior sessions, say so in one line and move on.

**Phase 1.** Use the priors to sharpen the interview — a recurring finding is often a
requirement the user never thought to state. Ask about it rather than assuming it.

**Phase 2 and Phase 3.** The block travels inside every Codex prompt, alongside the
requirements and the project brief. This is the part that matters most: Codex has no
memory, so without it the same failure mode gets rebuilt every session.

**Phase 4.** Check the deliverable against the recurring findings explicitly before doing
anything else — they are this vault's highest-prior-probability defects.

## The honesty rule

The rendered block leads with a callout stating that these are **priors, not requirements**,
and that phrasing is load-bearing — it travels into the Codex prompt. A past finding is a
hypothesis worth checking against the current task, never on its own a reason to change the
plan. A settled decision should not be reopened unless this task gives a concrete new
reason, but "a previous session decided X" is not by itself an argument that X is correct.

Two failure modes to watch for, both of which this framing exists to prevent:

- **Cargo-culting.** Treating a residual issue accepted once as a standing exemption.
- **Anchoring.** Letting past contested points narrow the plan before the requirements are
  even settled. That is why the block enters the interview as questions, not as constraints.

## Interaction with the linter

The two are complementary and deliberately non-overlapping. `vault-lint.py` proves that a
session's notes are *structurally* valid; `session-history.py` reads those notes assuming
they are. If a vault's sessions are old or hand-edited, lint them first — the scanner
skips notes whose frontmatter it cannot read, so structural breakage shows up as missing
history rather than as an error.
