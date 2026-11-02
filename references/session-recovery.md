# Session recovery — state, token guard, limits, progress notebook

A session must survive running out of resources. Codex and Claude both have usage limits;
when one is hit mid-session, the work so far must be intact and the session must pick up
exactly where it stopped as soon as the resource is back — without anyone reconstructing
it from memory.

Three mechanisms, all Tier 0 scripts:

1. **State kept current** — `session.json` always says what to do next (`session-state.py`).
2. **Token guard and limit handling** — budgets warn before a limit, limit messages pause
   the session cleanly, a background wait resumes it (`session-state.py`).
3. **Progress notebook** — a snapshot of the project's state in its own NotebookLM notebook,
   for later sessions and other machines (`progress-sync.py`).

## 1. State — `session.json`

Created by `session-log.py init`. The fields that matter for recovery:

| Field | Meaning |
|-------|---------|
| `status` | `active` · `paused` · `complete` · `abandoned` |
| `phase`, `step` | where in the workflow |
| `next_action` | **the exact next thing to do**, written for someone with no memory of this conversation |
| `pending` | started work not yet collected: a Codex step, a NotebookLM generation, a subagent |
| `blocked_on` | when paused: resource, kind, reason, `resume_after`, evidence |
| `models`, `budgets` | snapshots taken at init, so a resumed session runs on the same tiers and limits |

**The discipline that makes recovery work:** update `next_action` *before* each step
starts, not after it ends.

```bash
python3 scripts/session-state.py set --vault "$V" --session "$S" \
  --phase build --step 3 --next "Run build step 3 (prompt: prompts/build-step-3.txt) on codex_build, then verify"
python3 scripts/session-state.py set ... --pending-add "codex:build-step-3:running"
# ... invocation ...
python3 scripts/session-state.py set ... --pending-done build-step-3 --next "Verify step 3 outputs"
```

A Claude limit gives no warning and no chance to save — the turn simply stops. Whatever
`next_action` said at that moment is what the next session does. If it is always written
first, a hard cut-off loses at most the step in flight, which `pending` names.

## 2. Token guard

### Budgets

Per-session caps, **per project**, in `~/.claude/duel-vault.config.json`:

```json
"budgets": { "warn_ratio": 0.8 },
"budgets_by_project": {
  "Direful": { "codex": 400000, "claude_main": 150000, "claude_delegated": 200000 },
  "Notes":   { "codex": null,   "claude_main": null,   "claude_delegated": null }
}
```

`budgets` holds the defaults every project inherits; `budgets_by_project` overrides them.
`null` means track only. A project's entry is created the first time it runs Route B:

```bash
python3 scripts/session-state.py budgets --project "<project>"        # configured or not?
python3 scripts/session-state.py budgets --project "<project>" --set codex=400000 --set claude_main=150000
python3 scripts/session-state.py budgets --project "<project>" --track-only   # the answer "no caps"
```

When it says **NOT configured**, ask the user at Route B triage — alongside the Codex
models, before `session-log.py init` (which snapshots the budgets into the session) — with
a recommendation sized to the plan's scale, and save the answer, even "track only", so it is
never asked again. Afterwards, confirm the saved values in one line at triage. Every `session-log.py invocation` / `tokens add`
evaluates them and prints one status line; `session-state.py guard` does the same on
demand. Exit codes: 0 ok · 3 warning (≥ `warn_ratio`) · 4 exceeded.

- **3 — warning:** tell the user in one line at the next natural pause (a checkpoint, the
  end of a step). Do not interrupt a step for it.
- **4 — exceeded:** finish the step in flight (never leave a half-applied change), then
  checkpoint and stop: `pause --resource budget`, push progress, and ask the user whether
  to raise the budget or resume later. Spending past a budget is the user's call.

Claude's main-thread figure is an estimate (`ceil(chars/4)`), so a `claude_main` budget is a
proxy. It is still the only early warning available: Claude Code does not expose how much
of the user's plan limit is left. If the user knows their limit is close, they can say so —
`pause --resource claude --until <reset time>` is the clean way out.

### Limits — when a call fails

When a Codex call exits non-zero, **classify before retrying**:

```bash
python3 scripts/session-state.py limit --vault "$V" --session "$S" \
  --resource codex --stderr-file "$V/.duel-vault/$S/raw/build-step-3.stderr"
```

- **Exit 1 — not a limit:** an ordinary error. The usual rule applies: retry once, then
  surface it. Only the last 40 lines are read, and the patterns are specific ("usage
  limit", "too many requests", "status: 429"…): Codex's stderr also carries the output of
  the commands it ran, and a project that is *about* rate limiting must not pause itself.
- **Exit 0 — a limit:** the session is now `paused` with `blocked_on` filled in: `kind`
  (`usage` — quota, long wait; `rate` — transient, minutes), `resume_after` parsed from the
  message when it names one ("try again in 2 hours", "try again at 3:47 PM",
  "resets 5pm (Europe/Rome)"), and the raw evidence. **Do not retry**: retrying into a usage
  limit burns wall clock and sometimes tokens. Log the invocation with `--status limit`.

Then, for Codex (and NotebookLM): **wait in the background**, which costs no tokens —

```bash
python3 scripts/session-state.py wait --vault "$V" --session "$S"     # run_in_background
```

It sleeps until `resume_after` (plus a grace minute), then probes the resource with a
trivial call (the same one the Codex preflight uses, run from the vault) every `--poll`
seconds (default 900) while the answer is still a limit; it exits 0 when the resource is
back (`resource_available` in the journal), 6 after `--max-wait` (default 6 days), or 1 at
once if the probe fails for a reason that is *not* a limit (a flag the installed version
does not know, a repo check, a crash) — that says nothing about the quota, so polling it for
days would be pointless. Fix the probe or check by hand, then `resume --force`. If the
session was resumed by hand or blocked on something else while it waited, it leaves the
new state alone. Started with the Bash tool's
`run_in_background`, it re-invokes Claude when it exits: run `resume` and carry on. While it
waits, keep doing whatever does not need the blocked resource — interview questions,
review of finished steps, logs — and tell the user the session is waiting and until when.

Override the probe per resource in the config (`"probes": {"codex": "codex exec -m X ..."}`)
if the default does not fit the installed version.

**Claude limits** cannot be handled from inside: when the main thread is out of quota it
cannot run anything. Two things cover it:

1. **The state discipline above** — whatever `next_action` said is where the next session
   starts. Nothing else is needed for correctness.
2. **A notification at the reset**, so the user knows when to come back. Claude Code fires a
   `StopFailure` hook when a turn ends on an API error; duel-vault's handler
   (`session-state.py on-stop-failure`) runs for `rate_limit`:
   - finds the duel-vault session driven by that Claude session — every script records
     `CLAUDE_CODE_SESSION_ID` in a small registry (`~/.claude/duel-vault.sessions.json`),
     and the hook receives the same id, so the match is exact; any other Claude session is
     ignored;
   - pauses it (`resource: claude`), reading the reset time from the error text when it has
     one ("resets 5pm (Europe/Rome)", "reset at 3:47 PM");
   - starts a **detached** notifier — it survives closing the terminal — that sleeps until
     the reset and then notifies the user, unless the session was resumed in the meantime;
   - when the text names no time, the notifier instead runs a minimal `claude -p` probe
     every `notify.poll_seconds` (default 900) until Claude answers. Each probe runs with
     `DUEL_VAULT_PROBE=1`, which this hook ignores, so a probe that hits the limit cannot
     trigger another pause;
   - pushes the progress snapshot in the background.

   Install the hook once per machine (it edits `~/.claude/settings.json`, so ask first):

   ```bash
   python3 scripts/session-state.py install-hook --dry-run    # show what would be added
   python3 scripts/session-state.py install-hook              # add or update it
   python3 scripts/session-state.py install-hook --uninstall
   ```

   **Channels, on any OS.** macOS notifications (`osascript`), Linux `notify-send`, Windows
   balloon tips (PowerShell) — or any command you prefer, which replaces the desktop
   channel: a phone push, a mail, a chat webhook. Every notification is also appended to
   `~/.claude/duel-vault-notifications.log`.

   ```json
   "notify": {
     "command": ["curl", "-s", "-H", "Title: {title}", "-d", "{message}", "https://ntfy.sh/<your-topic>"],
     "title": "duel-vault · {project}",
     "message": "{resource} di nuovo disponibile — sessione {session}. Prossima azione: {next_action}",
     "poll_seconds": 900
   }
   ```

   `{title}`/`{message}` are substituted into the command's arguments (no shell, nothing
   to inject); `{project}`, `{session}`, `{resource}`, `{next_action}` into the templates.

   **Limits.** The notifier is a process: a reboot before the reset loses it (the session is
   still found at Phase 0). It only notifies — it never resumes the session by itself. If the
   reset time is known *before* a cut-off (the user says so), `pause --resource claude
   --until <time>` followed by `notify-at` schedules the same notification by hand.

The user then resumes with `claude --continue`, or a new session and "riprendi il progetto
<nome>": Phase 0 finds the paused session and `resume` continues it — a Claude pause never
needs `--force`, since Claude running `resume` is the proof it is back.

### Resume

```bash
python3 scripts/session-state.py resume --vault "$V" [--session "$S"] [--check] [--force]
```

- No `--session`: finds the open (active/paused) session; lists them when there are several.
- Blocked on **Codex or NotebookLM** with `resume_after` in the future or unknown: refuses
  with exit 5 and prints the `wait` command. `--force` when the user confirms the resource
  is back.
- Paused on **Claude**: resumes — Claude running `resume` is the proof it is back.
- Paused by the **user**: resumes — resuming is the user's decision.
- Paused on the **budget**: refuses (exit 5) while the session is still over it. Raise it
  with `session-state.py set --budget codex=<N>` (the session keeps the budgets it started
  with; editing the config does not change a running session), or pass `--force` once the
  user has agreed to go on.
- Otherwise: marks the session `active`, logs `resume`, and prints the **resume brief** —
  phase, step, next action, pending items, last block, models, token status, last events,
  and what to re-read before acting. `--check` prints it without changing anything.

**Phase 0 runs `session-state.py list --vault "$V" --open` before triage.** An open session
on the project is offered to the user — resume it, or close it (`set --status abandoned`)
and start fresh — never silently ignored.

On resume, before acting: re-read CLAUDE.md and the latest phase notes (they may have
changed), re-run `session-history.py`, check the files the next action depends on still
exist, and collect anything in `pending` (a Codex step that may have finished, a
NotebookLM artifact still generating — `artifact list -n <id> --json`).

## 3. Progress notebook

Each project gets one dedicated NotebookLM notebook, `"<project> — avanzamento duel-vault"`
(title configurable). It holds:

- **one current-state source**, `duel-vault-state-<project>.md`, replaced at every push:
  open sessions with phase, next action, blocks, pending items and token status; session
  history; recent decisions and file changes; how to resume; and a machine-readable block
  of the open sessions' state;
- **one summary source per closed session**, `duel-vault-session-<slug>.md`, added once.

```bash
python3 scripts/progress-sync.py push --vault "$V" --session "$S"
```

**When to push:** at each human checkpoint, at every pause (budget, limit, user), and at
closing. Not after every step — a push is three NotebookLM calls, and the local state is
already current. Every push also adds the summary of any closed session not yet in the
notebook, so a summary whose push failed is caught up later.

**What leaves the machine:** the snapshot carries task lines, next actions, decision and
activity summaries, file paths and the vault's path — never file contents, and never the raw
stderr excerpts kept as evidence of a limit (those stay in the local `session.json`).

**It never blocks.** Unchanged state is not re-sent. If the CLI is missing or the login has
expired, the snapshot stays at `<vault>/.duel-vault/progress/` and the push exits 3
("queued"); the next push retries it, and `session-log.py verify` warns (`SL012`) until it
lands. A failed push is a line in the log, never a reason to stop the session.

**Reading it back:**

- Same machine: not needed — `session-state.py resume` reads the local state.
- Another machine, or the `.duel-vault/` folder is gone:
  `progress-sync.py pull --vault "$V" --restore` fetches the current-state source and
  rebuilds `session.json` for each open session that is missing locally. The restored
  journal starts with a `session_start` marked `restored`: earlier journal and token
  entries do not travel through NotebookLM.
- Questions about the project's history ("what did we decide about the charts?"):
  `notebooklm ask "..." --notebook <id>`. The answer is model output — check it against the
  vault before acting on it.

**Relationship with the NotebookLM lane.** The progress notebook is not the source lane of
`notebooklm.md`: it never generates artifacts, never waits on long jobs, and uses its own
notebook — never a session's lane notebook. It needs the same login: preflight
`notebooklm auth check --test --json` once at Phase 0; if it fails, say so in one line and
let pushes queue. Turn it off with `"progress_notebook": {"enabled": false}` — the snapshot
is then written locally only.

**Not a source of truth.** The vault is. The notebook is a mirror for recovery and for
asking questions; when the two disagree, the vault wins and the next push corrects the
notebook.

## Exit codes at a glance

| Script | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|--------|---|---|---|---|---|---|---|
| `session-log.py` | ok | verify errors | usage | budget warn | budget over | — | — |
| `session-state.py` | ok / limit recognised | not a limit / probe error (wait) | usage | budget warn | budget over | still blocked | wait gave up |
| `progress-sync.py` | pushed / unchanged / disabled | pull failed | usage | push queued | — | — | — |
