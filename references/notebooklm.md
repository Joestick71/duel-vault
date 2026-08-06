# NotebookLM — the opt-in source lane

NotebookLM is a **capability, not a route**. Routes A and B decide how much process a task
gets; this lane decides whether the session is allowed to build a source corpus in
NotebookLM and turn it into vault deliverables. Either route can run with the lane on or
off.

The lane is **off by default and opens only on an explicit request from the user** —
"usa NotebookLM", "fammi un podcast da queste fonti", "/notebooklm", or a task the user
frames as NotebookLM work. Do not open it because a task merely looks source-heavy: it
costs a browser-backed authentication, minutes of wall clock, and artifacts nobody asked
for. If you believe the lane would help and the user has not asked, say so in one line
during triage and let them decide.

**One exception, in the other direction:** if the vault's own skills already drive
NotebookLM (a project skill whose description mentions it — e.g. a YouTube market-analysis
skill), and the task selects that skill, the skill owns the notebook. Do not open a
parallel lane and do not re-plan what the skill already does — `project-bootstrap.md` §2 is
explicit that a plan ignoring the project's skills is a planning bug. duel-vault's job in
that case is limited to provenance (`06-sources.md`), lint and accounting.

## Preflight — only once the lane is open

Symmetric to the Codex preflight in `codex-cli.md`, and run at the same moment: after
triage, before any planning that assumes the lane exists.

```bash
notebooklm auth check --test --json
```

Accept the lane as available only when the output has **both** `"status": "ok"` **and**
`"checks": {"token_fetch": true}`. Bare `--json` without `--test` only proves the cookie
file parses — a stale cookie file passes it. That is a documented false positive; do not
take the shortcut.

If the check fails:

1. `notebooklm login` (browser OAuth) is the primary path — it needs a display and a human.
2. `notebooklm auth refresh` is the cheap path when the profile merely went stale.
3. If neither is possible right now, **say so and stop the lane**. Present the user two
   options: proceed with the lane closed (and re-plan the steps that depended on it), or
   pause the session until they can log in. Never fabricate an artifact, a source list or
   an answer that NotebookLM did not produce.

Record the preflight result in the session log the same way the Codex preflight is
recorded.

## Notebook identity — always explicit

**Always pass `-n <notebook_id>` (wait/download commands) or `--notebook <notebook_id>`
(everything else). Never use `notebooklm use`.** The CLI stores the "current notebook" in a
per-profile context file, and this lane spawns background agents that run concurrently with
the main thread — a shared context is exactly the thing two concurrent callers overwrite.
Capture the id once, at creation:

```bash
NB=$(notebooklm create "<session title>" --json | jq -r .notebook.id)
```

Write `NB` into `session.json` immediately. An interrupted session that has lost the
notebook id has lost the work.

## Long operations run in a background agent

Nothing in this lane blocks the main conversation. Generation is minutes to tens of
minutes, and the workflow's checkpoints are interactive.

| Operation | Typical | Timeout to pass |
|-----------|---------|-----------------|
| Source processing | 30 s – 10 min | 600 |
| Research, `--mode fast` | 30 s – 2 min | 180 |
| Research, `--mode deep` | 15 – 30+ min | 1800 |
| Report, data table, quiz, flashcards | 5 – 15 min | 900 |
| Audio (podcast) | 10 – 20 min | 1200 |
| Video | 15 – 45 min | 2700 |
| Mind map, notes | instant | — |

The pattern is always the same: **the main thread starts the generation and returns
immediately; a background agent waits for it and downloads the result.**

```
1. main thread:  notebooklm generate report --notebook $NB --json     → task_id
2. main thread:  record {artifact, task_id, status: pending} in session.json
3. main thread:  Agent(subagent_type="general-purpose", run_in_background=true, prompt=…)
4. main thread:  continues the session (interview, planning, other steps)
5. agent done:   the task notification arrives → materialize (see below)
```

The agent prompt must be self-contained and narrow:

```
Wait for NotebookLM artifact <artifact_id> in notebook <notebook_id> and download it.

  notebooklm artifact wait <artifact_id> -n <notebook_id> --timeout <seconds>
  notebooklm download <type> <abs path under .duel-vault/<slug>/artifacts/> -n <notebook_id>

Report: final status, the exact path written, and the raw stderr if either command failed.
Do not re-generate, do not retry a failed generation, do not write anything into the vault
itself, do not edit any session note.
```

Rules for the agent lane:

- **The agent waits and downloads. It never generates, never re-plans, never touches the
  vault.** Materialization into the vault is the main thread's job, because that is where
  the project brief and the vault's conventions live.
- Downloads land in `<vault>/.duel-vault/<session-slug>/artifacts/` — the dot-folder
  Obsidian ignores — never straight into the vault tree.
- `artifact wait` **exit code 2 means timeout, not failure.** The generation may still be
  running; re-check with `notebooklm artifact list -n <id> --json` before concluding
  anything. Exit 1 is a real error.
- Every pending artifact stays in `session.json` until it is either materialized or
  explicitly abandoned. A session must never close with a pending artifact nobody
  mentioned: if one is still running at closing time, record it in the final report as a
  residual with its artifact id.
- Do not fan out more than a couple of generations at once. Google rate-limits, and
  `GENERATION_FAILED` on a rate limit costs more wall clock than the serialization saved.

## Where the lane may act, phase by phase

**Phase 0.** NotebookLM is inventoried as an available tool if the vault's `CLAUDE.md`
lists it. Inventory only — no preflight, no notebook, until the user opens the lane.

**Phase 1 (interview).** `source add-research --mode fast` is allowed to establish
*external facts* that the interview needs — this is the "facts are looked up, decisions are
asked" rule of `SKILL.md`, applied to the world outside the vault. What comes back becomes
a **question for the user**, never a settled requirement. `--mode deep` is not allowed
here: a 30-minute wait does not belong inside an interactive interview.

**Phase 2 (planning).** A plan step that produces a NotebookLM artifact must name, in the
step itself: the notebook id, the sources it draws on, the artifact type and its options,
the vault path where the result lands, and the frontmatter it will carry. A step that says
"generate a report" and leaves the landing path implicit is not verifiable and Codex is
right to critique it.

NotebookLM is **not** an author in the consensus protocol. The protocol is two authors
alternating turns until both APPROVE the same `plan_version` (`consensus-protocol.md`); a
third voice has no verdict semantics there, and a source-grounded assistant is not a
reviewer of build plans. Do not ask it to critique the plan.

**Phase 3 (build) — the exception to "Claude does not write".** Codex runs in a sandbox
with no Google authentication, so **Claude executes the NotebookLM steps of the plan
itself**, records each one in `03-build-log.md` exactly like a Codex invocation, and hands
Codex only the materialized files. This is not a licence to write code: everything the plan
implements in code still goes to Codex. Mark these entries clearly, e.g.
`## Invocation 4 — NotebookLM (Claude) — Step 5`.

**Phase 4 (review).** Optional, and only for content deliverables: load the produced note
back as a source and ask targeted verification questions with citations —

```bash
notebooklm ask "Does <claim> appear in the sources? Quote it." --notebook $NB --json
```

Treat every answer as a **candidate finding you must verify yourself** against the cited
source before it enters `04-review-cycle-N.md`. NotebookLM's answers are model output, not
a deterministic gate — the deterministic gate is `vault-lint.py`, and it runs first,
unchanged.

**Closing.** Same gate as always: `vault-lint.py` must exit 0, and every artifact that
entered the vault must have gone through materialization (below). `06-sources.md` must
exist, or the session's provenance is lost.

## Materialization — the contract

**Nothing NotebookLM produced enters the vault except through
`scripts/nblm-import.py`.** A raw `notebooklm download report ./x.md` writes a file with no
frontmatter, no tags and no links: Obsidian will not see its properties, and the closing
lint gate will fail on it.

```bash
python3 scripts/nblm-import.py import \
  --vault "$VAULT" --session "$SLUG" --notebook "$NB" \
  --into "Ricerche/2026" --title "Titolo della nota" \
  --field status=draft --tag ricerca \
  .duel-vault/$SLUG/artifacts/report.md
```

What it guarantees: frontmatter carrying the vault's mandated fields (it reads
`lint_required_fields` from `~/.claude/duel-vault.config.json` and refuses to write a note
that would miss one), a provenance callout naming the notebook, and a wikilink back to the
session. Binary artifacts (mp3, mp4, png, pdf, pptx, csv) are copied into the vault's asset
folder with a companion note that embeds or links them. Mind maps convert to a real
Obsidian `.canvas` with `--canvas`.

Generate the mind map with an explicit `--kind`. The CLI's default flips from
`note-backed` to `interactive` in v0.8.0, and the two produce different JSON.

## Provenance — `06-sources.md`

NotebookLM artifacts are **not reproducible**: the same sources and the same prompt do not
return the same report tomorrow. A session that cannot say what went in is a session whose
output cannot be audited. So the corpus is recorded, not the result:

```bash
python3 scripts/nblm-import.py manifest \
  --vault "$VAULT" --session "$SLUG" --notebook "$NB" \
  --prompt "Report briefing-doc, focus on <topic>" \
  --prompt "Podcast deep-dive, 20 min"
```

It queries `source list --json` and `artifact list --json` and writes
`Sessions/<slug>/06-sources.md`: every source with title, type, status and URL; every
artifact with type, status and id; every generation prompt used, verbatim. On Route A there
is no session folder — use `--format block` and paste the fragment into the single session
log under its own heading.

Write the manifest **when the corpus is complete**, and refresh it if sources are added
later. `vault-lint.py` enforces the link between the two halves: a MOC with
`notebooklm: true` and no `06-sources.md` is an error.

## Accounting

NotebookLM exposes no token counts. The rule "never invent numbers" holds, so the lane is
accounted in **operations**, not tokens, as its own row of the final report's table:

```markdown
| Phase | Codex (measured) | Claude (est.) | NotebookLM |
|-------|------------------|---------------|------------|
| 3 — Build | 12400 | ~8200 | 3 ops (1 report, 1 audio, 6 sources) · tokens unavailable |
```

`tokens_codex` / `tokens_claude_est` in the frontmatter stay what they always were. The
NotebookLM figure is a count of operations and never a token estimate.

## Failure modes

| Symptom | Cause | Action |
|---------|-------|--------|
| `auth check --test` returns `token_fetch: false` | cookies stale | `notebooklm auth refresh`, then re-check; `login` if that fails |
| `GENERATION_FAILED` | Google rate limit | wait, retry once with `--retry 2`; if it fails again, report it and re-plan the step |
| `No result found for RPC ID` | rate limiting | wait 5–10 min before retrying |
| `artifact wait` exits 2 | timeout, generation may still run | `artifact list --json` before concluding; do not re-generate blindly |
| `No notebook context` | someone relied on `use` | pass `-n` / `--notebook` explicitly |
| download fails | generation incomplete | check `artifact list` status; a `pending` artifact has nothing to download |

Two invocations fail, or the same one fails twice: stop the lane, tell the user what the
raw error was, and continue the session with the lane closed rather than burning the
session on retries.

## What this lane must never become

- **A third consensus author.** See Phase 2 above.
- **A replacement for `session-history.py`.** Loading past sessions into a notebook to "ask
  what went wrong before" trades a deterministic, instant, free extraction for a slow,
  non-reproducible one. The priors block stays exactly as it is.
- **A default.** Every session that opens this lane pays authentication, wall clock and an
  external dependency. Sessions that do not need a source corpus must not pay for one.
