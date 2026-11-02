# Session journal — the complete record

The Obsidian notes are the **curated** record of a session: requirements, plan, findings,
the decisions worth a callout. The journal is the **exhaustive** one: every activity,
decision, file change, model invocation and token figure, appended the moment it happens.
It is what makes a session auditable after the fact and resumable after a cut-off — a
Claude or Codex usage limit, a closed terminal — because nothing lives only in the
conversation.

Both routes keep one. All of it is written by `scripts/session-log.py` (Tier 0 — a script,
no model tokens), never by hand, so the format cannot drift.

## Files

`<vault>/.duel-vault/<session-slug>/` — the dot-folder Obsidian ignores:

| File | Written by | Holds |
|------|------------|-------|
| `journal.jsonl` | `session-log.py`, `session-state.py`, `progress-sync.py` | one JSON event per line, append-only, numbered `seq` 1..n |
| `session.json` | `session-log.py init`, `session-state.py` | where the session is — see `session-recovery.md` |
| `tokens.json` | `session-log.py invocation` / `tokens add` | every token figure, one entry each, with phase, resource and model |

Rendered into the vault at closing (and whenever useful):
`Sessions/<slug>/07-activity-log.md` on Route B, an `## Activity` block in the log on
Route A.

## What to log, and when

Log **as it happens**, never reconstructed at the end — the point is that a session cut off
mid-step has its record up to the last thing it did.

| Moment | Command |
|--------|---------|
| Session opens (after triage, both routes) | `init --route A\|B --task "..." [--title ...]` |
| Phase or step changes | `session-state.py set --phase P --step K --next "..."` (logs a `phase` event) |
| A pre-journal session (a `session.json` written by hand before this existed) is reopened | `init` on it upgrades it in place; until then every other command refuses it |
| Anything done that is not one of the below: prompt written, notes read, check run, question asked and answered | `event --kind activity --summary "..."` |
| A decision — yours, the user's, or arbitrated | `event --kind decision --summary "..." --note "<note that carries the callout>"` |
| A human checkpoint passed (1, 2, closing) | `event --kind checkpoint --summary "..."` |
| A model-tier escalation | `event --kind escalation --summary "..." --note "<note>"` |
| A deviation, a recoverable problem | `event --kind warning` / `--kind error` |
| Any file created, modified or deleted — by Claude, Codex or a subagent | `change --file <path> [--file ...] --actor codex --step K` |
| A Codex call, a subagent, a NotebookLM generation | `invocation --resource codex\|claude_delegated\|notebooklm --id ... --status ...` |
| The main thread's own estimate, at each checkpoint | `tokens add --resource claude_main --total N` |

All commands take `--vault <vault> --session <slug>`.

**Decisions go in both places.** The journal has every decision with its time and actor;
the note keeps the `> [!decision]` callout that explains it. `--note` names the note, and
`verify` checks the callouts exist.

**Changes are hashed.** `change` records each file's sha256 and size. Run it after every
build invocation for the files Codex reports (the same list you already verify exists), and
after every edit you or a subagent make. A file logged twice with no new content is skipped.
Relative paths are resolved against the vault — pass `--base <dir>` when they are relative
to somewhere else (Codex reports paths relative to its `-C` directory). A path that does not
exist is refused, never logged as a deletion: deletions need an explicit `--action deleted`.
At `verify`, a file whose current hash differs from its last logged one is an **unlogged
edit** — something touched the deliverable without the record knowing.

**Invocations carry their tokens.** `invocation` writes the event *and* the `tokens.json`
entry *and* evaluates the budget in one call:

```bash
python3 scripts/session-log.py invocation --vault "$V" --session "$S" \
  --resource codex --model "$BUILD_MODEL" --effort "$BUILD_EFFORT" --id build-step-3 \
  --step 3 --status ok --input 9800 --output 2600 --total 12400
```

`--status started` before a long call, then the outcome (`ok`, `failed`, `limit`,
`timeout`) after it, makes a cut-off visible: `verify` flags a start with no outcome.
A missing `--total` on a measured resource is recorded as `"unavailable"` — never
estimated. Exit code 3 / 4 means the budget is at warning / exceeded — see
`session-recovery.md`.

## The gate — `verify`

```bash
python3 scripts/session-log.py verify --vault "$V" --session "$S" [--json] [--strict]
```

Runs at every checkpoint and as a closing gate on both routes, next to `vault-lint.py`.
Exit 0 means no errors. At closing it runs *after* `set --status complete` and `render`, so
it checks the record as it will stay.

| Code | Level | Check |
|------|-------|-------|
| `SL001` | ERROR | `session.json` missing or not a JSON object |
| `SL002` | ERROR | journal missing, a line that is not a JSON object, a missing `seq`/`ts`/`kind`/`summary`, a bad timestamp |
| `SL003` | WARN | unknown event kind |
| `SL004` | ERROR/WARN | sequence numbers not contiguous from 1 (lines lost or edited) / timestamps going backwards |
| `SL005` | ERROR | journal does not start with `session_start` |
| `SL006` | ERROR/WARN | bad status; paused with no `blocked_on` / open session with no `next_action` |
| `SL007` | ERROR/WARN | status and journal disagree on whether the session is closed (the last `session_end` vs the last `session_start`/`resume`); complete with pending items |
| `SL008` | WARN/ERROR | an invocation started and never got an outcome (ERROR once the session is closed) |
| `SL009` | ERROR/WARN | malformed `tokens.json` entry / an invocation with no ledger entry |
| `SL010` | WARN | a logged file changed afterwards (unlogged edit), vanished, or came back after deletion |
| `SL011` | WARN | more `decision`/`escalation` events than `> [!decision]`/`> [!escalation]` callouts in the session notes |
| `SL012` | WARN | the progress snapshot is queued and not yet in NotebookLM |

What it does not check: whether a summary is *true*, whether a decision was right, whether
an activity was worth logging. That stays judgement.

## Rendering — `07-activity-log.md`

```bash
python3 scripts/session-log.py render --vault "$V" --session "$S"                 # Route B
python3 scripts/session-log.py render --vault "$V" --session "$S" --format block  # Route A
```

Route B writes `Sessions/<slug>/07-activity-log.md`: frontmatter (`phase: activity`,
`events`, tags `[duel-vault, duel-vault/activity]`), then the timeline, decisions, file
changes and the token table. `vault-lint.py` knows the note. It is generated — regenerate
it, never edit it. Route A has no session folder: render a block with its heading in the
vault's language (`--format block --heading Attività`) and paste it into the log.

`session-log.py tokens summary` prints the same per-phase token table on its own — paste
it into `05-final-report.md` instead of adding figures by hand.

## Cost

Every command is a script run with a one-line answer. Log liberally: an activity event
costs the main thread one short tool call; a session that could not be reconstructed costs
the whole session again.
