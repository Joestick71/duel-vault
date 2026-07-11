# Codex CLI — invocation reference

Codex CLI evolves quickly. **Always run the preflight before trusting anything in this file.**

## Preflight (mandatory, Phase 0)

```bash
codex --version
codex exec --help
```

Confirm from the help output:
1. The non-interactive subcommand (expected: `codex exec "PROMPT"`)
2. The model flag (expected: `-m` / `--model`)
3. Sandbox/approval flags (expected: `--sandbox read-only|workspace-write|danger-full-access`, and/or `--full-auto`)
4. A way to capture the final message (expected: `--output-last-message <file>`; if absent, capture stdout)
5. Working-directory flag (expected: `-C <dir>` / `--cd <dir>`)

If any differ, adapt the patterns below to what the help actually says and note the adaptation in the session log.

## Listing available models

`codex exec --help` may not list models. Options, in order of preference:
1. Ask the user — they know their subscription.
2. Check `~/.codex/config.toml` for a configured `model`.
3. Try the model the user names; if Codex rejects it, show the error and ask again.

## Standard invocation patterns

**Plan critique (Phase 2 — must not modify files):**
```bash
codex exec -m "$MODEL" --sandbox read-only \
  --output-last-message "$VAULT/.vault-duet/$SESSION/plan-round-${N}-codex.md" \
  "$(cat "$VAULT"/.vault-duet/$SESSION/prompts/plan-round-${N}.txt)"
```

**Build step (Phase 3 — needs write access):**
```bash
codex exec -m "$MODEL" --sandbox workspace-write --full-auto \
  -C "$PROJECT_DIR" \
  --output-last-message "$VAULT/.vault-duet/$SESSION/build-step-${K}-codex.md" \
  "$(cat "$VAULT"/.vault-duet/$SESSION/prompts/build-step-${K}.txt)"
```

**Fix cycle (Phase 4 loop-back):** same as build step, prompt = fix-brief.

## Prompt construction rules

- Write prompts to files under `<vault>/.vault-duet/<session>/prompts/` and `cat` them in — never inline long prompts in the shell command (quoting bugs, no audit trail).
- Every prompt to Codex must be self-contained: Codex has **no memory between invocations**. Include requirements excerpts, relevant plan steps, current workspace state, and the expected response format every time.
- End every Phase 2 prompt with the protocol header requirement (see consensus-protocol.md).
- End every Phase 3 prompt with: "After completing, list every file you created or modified, and state any deviation from the plan steps above with its reason."

## Capturing token usage

Codex reports token usage, but where it reports it varies by version. At preflight, determine which of these works on the installed version, in order of preference, and record the chosen method in `session.json` (`"token_capture": "..."`):

1. **JSON/experimental output mode** — check `codex exec --help` for a `--json` or similar flag that emits structured events; usage events contain exact `input_tokens` / `output_tokens`. Most reliable when available.
2. **Stdout summary** — recent versions print a usage line at the end of `codex exec` output (e.g. `tokens used: N`). Since raw stdout is already saved under `<vault>/.vault-duet/<session>/raw/`, grep it per invocation (pattern like `grep -iE 'tokens? used|token usage' raw/<file>`). Verify the exact wording on this installation during preflight with a trivial call (e.g. `codex exec -m "$MODEL" --sandbox read-only "Reply with the single word: ok"`), and note the pattern.
3. **Session logs fallback** — Codex writes session files under `~/.codex/sessions/` (JSONL). Match the most recent file to your invocation by timestamp and extract usage fields from its records.

If none of the three works, set `"token_capture": "unavailable"`, warn the user once at the end of Phase 0, and record `"unavailable"` per invocation as per SKILL.md rules.

Per invocation, append to `tokens.json` under the current phase: `{ "invocation": "<id>", "input": N, "output": N, "total": N }` (or `"unavailable"`).

## Robustness

- Wrap each invocation with a timeout (e.g. `timeout 900 codex exec ...`) and check the exit code.
- On non-zero exit: log stderr, retry once. On second failure: stop and surface to the user.
- Verify claimed outputs: after a build step, check with `ls`/`test -f` that the files Codex says it wrote actually exist. Trust but verify.
- Keep raw stdout of every invocation in `<vault>/.vault-duet/<session>/raw/` for debugging; only the distilled result goes to the vault log.
