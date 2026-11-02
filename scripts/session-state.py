#!/usr/bin/env python3
"""session-state — where a duel-vault session is, and whether it can go on.

`session.json` is kept current after every step (`set --next ...`), so a session cut off at
any moment — Claude or Codex out of quota, a closed terminal — can be resumed exactly where
it stopped. This script owns the transitions:

    session-state.py set     --vault V --session S [--phase P] [--step K] [--next "..."]
    session-state.py guard   --vault V --session S            token budget status
    session-state.py limit   --vault V --session S --resource codex --stderr-file F
    session-state.py pause   --vault V --session S --resource budget --reason "..." [--until T]
    session-state.py wait    --vault V --session S            run in background; exits when free
    session-state.py resume  --vault V [--session S] [--check]
    session-state.py status  --vault V --session S
    session-state.py list    --vault V
    session-state.py budgets --project P [--set codex=N ...|--track-only]
    session-state.py notify-at --vault V --session S [--at T]   notify the user at the reset
    session-state.py on-stop-failure                           Claude Code StopFailure hook
    session-state.py install-hook [--dry-run|--uninstall]      add that hook to settings.json

Exit codes: 0 ok · 1 not a limit / probe error · 2 bad usage · 3 budget warning ·
4 budget exceeded · 5 still blocked · 6 wait gave up.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from session_common import (
    EXIT_BLOCKED,
    EXIT_FAIL,
    EXIT_OK,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    LIMIT_RESOURCES,
    OPEN_STATUSES,
    STATUSES,
    append_event,
    budget_exit,
    check_slug,
    check_vault,
    classify_limit,
    evaluate_budgets,
    iso,
    list_sessions,
    load_events,
    load_state,
    load_tokens,
    new_state,
    now,
    parse_iso,
    parse_when,
    require_state,
    save_state,
    session_dir,
    session_lock,
    NOTIFY_LOG,
    fill_template,
    PROBE_ENV,
    write_json_atomic,
    lookup_claude_session,
    notify,
    pid_alive,
    project_has_budgets,
    resolved_budgets,
    save_config,
    sleep_until,
    spawn_detached,
)
from vault_common import CONFIG_PATH, load_config

SKILL_SCRIPTS = Path(__file__).resolve().parent


def say(text: str) -> None:
    print(text, flush=True)


def warn(text: str) -> None:
    print(f"session-state: {text}", file=sys.stderr, flush=True)


def script(name: str) -> str:
    return shlex.quote(str(SKILL_SCRIPTS / name))


# --------------------------------------------------------------------------
# set / guard / status / list
# --------------------------------------------------------------------------


def cmd_set(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    with session_lock(session_dir(vault, slug)):
        state = require_state(vault, slug)
        changes: list[str] = []
        if a.phase and a.phase != state.get("phase"):
            old = state.get("phase")
            state["phase"] = a.phase
            append_event(vault, slug, "phase", f"{old} → {a.phase}", phase=a.phase, actor="claude")
            changes.append(f"phase {a.phase}")
        if a.step is not None:
            state["step"] = a.step or None
            changes.append(f"step {a.step or '-'}")
        if a.next is not None:
            state["next_action"] = a.next
            changes.append("next action")
        for raw in a.pending_add:
            kind, _, rest = raw.partition(":")
            ident, _, detail = rest.partition(":")
            if not kind or not ident:
                raise SystemExit(f"--pending-add expects KIND:ID[:DETAIL], got {raw!r}")
            state["pending"] = [p for p in state["pending"] if p.get("id") != ident]
            state["pending"].append({"kind": kind, "id": ident, "detail": detail, "since": iso(now())})
            append_event(vault, slug, "state", f"pending + {kind} {ident}", kind_detail=detail)
            changes.append(f"pending +{ident}")
        for ident in a.pending_done:
            before = len(state["pending"])
            state["pending"] = [p for p in state["pending"] if p.get("id") != ident]
            if len(state["pending"]) == before:
                warn(f"no pending item with id {ident!r}")
            else:
                append_event(vault, slug, "state", f"pending − {ident}")
                changes.append(f"pending -{ident}")
        if a.field:
            managed = set(new_state(vault, slug, "B", "", "", "")) | {"legacy"}
            for pair in a.field:
                key, _, value = pair.partition("=")
                key = key.strip()
                if not key or key in managed:
                    raise SystemExit(
                        f"--field cannot set {key!r}: it is managed by session-state "
                        "(use --phase/--step/--next/--pending-*/--status/--budget, pause or limit)"
                    )
                state[key] = value
                changes.append(key)
        for pair in a.budget:
            key, _, value = pair.partition("=")
            key = key.strip()
            if key not in ("codex", "claude_main", "claude_delegated", "warn_ratio"):
                raise SystemExit(f"unknown budget {key!r}")
            if value.strip().lower() in ("", "none", "null"):
                parsed = None
            else:
                try:
                    parsed = float(value) if key == "warn_ratio" else int(value)
                except ValueError:
                    raise SystemExit(f"--budget {key}= needs a number, got {value!r}") from None
            state.setdefault("budgets", {})[key] = parsed
            append_event(vault, slug, "state", f"budget {key} → {parsed}")
            changes.append(f"budget {key}")
        if a.status:
            changes.extend(set_status(vault, slug, state, a.status, a.reason))
        save_state(vault, slug, state)
    say(f"{slug}: " + (", ".join(changes) if changes else "no change"))
    return EXIT_OK


def set_status(vault: Path, slug: str, state: dict, status: str, reason: str | None) -> list[str]:
    if status == state.get("status"):
        return []
    if status == "paused":
        raise SystemExit("use `pause` or `limit` to pause a session — they record why and until when")
    if status == "complete":
        open_items = state.get("pending") or []
        if open_items:
            warn(f"closing with {len(open_items)} pending item(s) — report them as residuals")
        state["blocked_on"] = None
        append_event(vault, slug, "session_end", reason or "session complete", status="complete")
    elif status == "abandoned":
        append_event(vault, slug, "session_end", reason or "session abandoned", status="abandoned")
    elif status == "active":
        state["blocked_on"] = None
        append_event(vault, slug, "resume", reason or "session reactivated")
    state["status"] = status
    return [f"status {status}"]


def cmd_guard(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    level, line = evaluate_budgets(load_tokens(vault, slug)["entries"], state.get("budgets") or {})
    say(line)
    return budget_exit(level)


def describe_block(block: dict | None) -> str:
    if not block:
        return "none"
    until = block.get("resume_after") or "unknown — probe to find out"
    return f"{block.get('resource')} ({block.get('kind', 'manual')}) until {until}: {block.get('reason', '')}"


def cmd_status(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    if a.json:
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return EXIT_OK
    level, line = evaluate_budgets(load_tokens(vault, slug)["entries"], state.get("budgets") or {})
    say(f"{slug} · route {state.get('route')} · {state.get('status')} · phase {state.get('phase')}"
        f"{' · step ' + str(state['step']) if state.get('step') else ''}")
    say(f"next: {state.get('next_action') or '(not set)'}")
    say(f"blocked: {describe_block(state.get('blocked_on'))}")
    for item in state.get("pending") or []:
        say(f"pending: {item.get('kind')} {item.get('id')} {item.get('detail', '')}".rstrip())
    say(line)
    return EXIT_OK


def cmd_list(a: argparse.Namespace) -> int:
    vault = check_vault(a.vault)
    sessions = list_sessions(vault)
    if a.open:
        sessions = [s for s in sessions if s.get("status") in OPEN_STATUSES]
    if a.json:
        print(json.dumps(
            [{k: s.get(k) for k in ("session", "route", "status", "phase", "step", "next_action",
                                    "blocked_on", "updated")} for s in sessions],
            indent=2, ensure_ascii=False))
        return EXIT_OK
    if not sessions:
        say("no sessions" + (" open" if a.open else "") + f" in {vault}")
        return EXIT_OK
    for s in sessions:
        say(f"{s.get('session')} · {s.get('route')} · {s.get('status')} · {s.get('phase')} · "
            f"updated {s.get('updated')}")
    return EXIT_OK


# --------------------------------------------------------------------------
# limit / pause
# --------------------------------------------------------------------------


def wait_hint(vault: Path, slug: str) -> str:
    return (
        f"python3 {script('session-state.py')} wait --vault {shlex.quote(str(vault))} "
        f"--session {shlex.quote(slug)}"
    )


def pause(vault: Path, slug: str, block: dict) -> dict:
    with session_lock(session_dir(vault, slug)):
        state = require_state(vault, slug)
        previous = state.get("blocked_on")
        if previous:  # a new block supersedes the old one; keep the old one on record
            state["blocked_history"] = (state.get("blocked_history") or [])[-19:] + [
                dict(previous, superseded_at=iso(now()))
            ]
        state["status"] = "paused"
        state["blocked_on"] = block
        save_state(vault, slug, state)
        append_event(
            vault,
            slug,
            "pause",
            f"{block['resource']}: {block.get('reason', '')}".strip(),
            phase=state.get("phase"),
            resource=block["resource"],
            limit_kind=block.get("kind"),
            resume_after=block.get("resume_after"),
        )
    return state


def explain_pause(vault: Path, slug: str, state: dict) -> None:
    block = state["blocked_on"]
    say(f"paused: {describe_block(block)}")
    say(f"next action on resume: {state.get('next_action') or '(not set — set it now with `set --next`)'}")
    if block["resource"] in ("codex", "notebooklm") or block.get("resume_after"):
        say("wait without spending tokens — run in the background, you are re-invoked when it exits:")
        say(f"  {wait_hint(vault, slug)}")
    else:
        say("no reset time known: ask the user, or resume later with `session-state.py resume`")


def cmd_limit(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    if state.get("status") not in OPEN_STATUSES:
        raise SystemExit(f"session {slug!r} is {state.get('status')} — nothing to pause")
    if a.stderr_file:
        text = a.stderr_file.expanduser().read_text(encoding="utf-8", errors="replace")
    else:
        text = a.text
    found = classify_limit(text)
    if found is None:
        say("not a usage or rate limit — handle it as an ordinary error (retry once, then surface it)")
        return EXIT_FAIL
    if a.classify_only:
        print(json.dumps(found, indent=2, ensure_ascii=False))
        return EXIT_OK
    block = {
        "resource": a.resource,
        "kind": found["kind"],
        "reason": f"{found['kind']} limit",
        "since": iso(now()),
        "resume_after": found["resume_after"],
        "evidence": found["evidence"],
    }
    state = pause(vault, slug, block)
    explain_pause(vault, slug, state)
    return EXIT_OK


def cmd_pause(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    if state.get("status") not in OPEN_STATUSES:
        raise SystemExit(f"session {slug!r} is {state.get('status')} — nothing to pause")
    until = None
    if a.until:
        when = parse_when(a.until)
        if when is None:
            raise SystemExit(f"cannot read --until {a.until!r} (try ISO, '17:30', '5pm', 'in 2h')")
        until = iso(when)
    block = {
        "resource": a.resource,
        "kind": "manual",
        "reason": a.reason,
        "since": iso(now()),
        "resume_after": until,
    }
    state = pause(vault, slug, block)
    explain_pause(vault, slug, state)
    return EXIT_OK


# --------------------------------------------------------------------------
# wait
# --------------------------------------------------------------------------


def probe_command(resource: str, state: dict, override: str | None) -> list[str] | None:
    if override:
        return shlex.split(override)
    configured = (load_config().get("probes") or {}).get(resource)
    if isinstance(configured, str) and configured.strip():
        return shlex.split(configured)
    if resource == "codex":
        models = state.get("models") or {}
        model = models.get("codex_build") or models.get("codex_plan")
        cmd = ["codex", "exec"]
        if model:
            cmd += ["-m", str(model)]
        return cmd + ["--sandbox", "read-only", "Reply with the single word: ok"]
    if resource == "notebooklm":
        return ["notebooklm", "auth", "check", "--test", "--json"]
    if resource == "claude":
        # Only the detached notifier uses this, never `wait`: while Claude is limited the
        # main thread cannot be the one waiting.
        return ["claude", "-p", "Reply with the single word: ok"]
    return None  # budget, user: the user decides


def run_probe(cmd: list[str], timeout: int, cwd: Path, env: dict | None = None) -> tuple[str, str]:
    """('ok' | 'limited' | 'error', detail).

    'error' is a failure that is *not* a limit — a probe that does not fit the installed
    CLI version, a repo check, a crash. It says nothing about the quota, so the wait must
    not keep polling on it for days: the caller stops and reports it.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    except FileNotFoundError:
        return "error", f"`{cmd[0]}` not on PATH"
    except subprocess.TimeoutExpired:
        return "limited", f"probe timed out after {timeout}s"
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if cmd[:2] == ["notebooklm", "auth"]:
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return "error", "auth check returned non-JSON output"
        ok = data.get("status") == "ok" and (data.get("checks") or {}).get("token_fetch") is True
        return ("ok", "") if ok else ("error", "NotebookLM auth check did not pass — log in again")
    if proc.returncode == 0:
        return "ok", ""
    if classify_limit(out) is not None:
        return "limited", out[-300:]
    return "error", out[-300:]


def same_block(a: dict | None, b: dict | None) -> bool:
    return bool(a and b and a.get("resource") == b.get("resource") and a.get("since") == b.get("since"))


def cmd_wait(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    block = state.get("blocked_on")
    if not block:
        say("not blocked — nothing to wait for")
        return EXIT_OK
    deadline = now() + timedelta(seconds=a.max_wait)
    resume_after = parse_iso(block.get("resume_after"))
    if resume_after and resume_after > deadline:
        say(f"reset at {iso(resume_after)} is beyond --max-wait; not waiting")
        return EXIT_TIMEOUT
    if resume_after and resume_after > now():
        say(f"sleeping until {iso(resume_after)} (+{a.grace}s grace)")
        sleep_until(resume_after + timedelta(seconds=a.grace))

    resource = block.get("resource", "")
    cmd = None if a.no_probe or (resource == "claude" and not a.probe_cmd) else \
        probe_command(resource, state, a.probe_cmd)
    if cmd is None and not resume_after:
        say("no reset time and no probe for this resource — cannot tell when it is back")
        return EXIT_TIMEOUT
    attempts = 0
    while cmd is not None:
        attempts += 1
        outcome, detail = run_probe(cmd, a.probe_timeout, vault)
        if outcome == "ok":
            break
        if outcome == "error":
            say(f"the probe failed for a reason that is not a limit — stopping the wait: {detail[:300]}")
            say("fix the probe (`--probe-cmd`, or `probes` in the config) or check the resource by hand, "
                "then `resume --force` once it answers")
            return EXIT_FAIL
        if now() + timedelta(seconds=a.poll) > deadline:
            say(f"still limited after {attempts} probe(s): {detail[:200]}")
            return EXIT_TIMEOUT
        again = classify_limit(detail)
        hint = parse_iso(again.get("resume_after")) if again else None
        pause_for = float(a.poll)
        if hint and hint > now():
            pause_for = min(max(pause_for, (hint - now()).total_seconds() + a.grace),
                            (deadline - now()).total_seconds())
        say(f"probe {attempts}: still limited; next in {int(pause_for)}s")
        sleep_until(now() + timedelta(seconds=max(0.0, pause_for)), step=min(30.0, max(1.0, pause_for)))

    with session_lock(session_dir(vault, slug)):
        state = require_state(vault, slug)
        current = state.get("blocked_on")
        if not same_block(current, block):
            # Resumed by hand meanwhile, or blocked again on something else: not ours to clear.
            say(f"{block.get('resource')} is available, but the session's block changed while "
                f"waiting (now: {describe_block(current)}) — left as is")
            return EXIT_OK
        cleared = dict(block, cleared_at=iso(now()), probes=attempts)
        state["blocked_history"] = (state.get("blocked_history") or [])[-19:] + [cleared]
        state["blocked_on"] = None
        save_state(vault, slug, state)
        append_event(
            vault,
            slug,
            "resource_available",
            f"{block.get('resource')} available again",
            resource=block.get("resource"),
            probes=attempts,
        )
    say(f"{block.get('resource')} available — resume with: python3 {script('session-state.py')} "
        f"resume --vault {shlex.quote(str(vault))} --session {shlex.quote(slug)}")
    return EXIT_OK


# --------------------------------------------------------------------------
# resume
# --------------------------------------------------------------------------


def brief(vault: Path, state: dict, events: list[dict], budget_line: str) -> str:
    slug = state["session"]
    lines = [
        f"## Resume — {state.get('title') or slug}",
        "",
        f"- Session: `{slug}` · route {state.get('route')} · phase **{state.get('phase')}**"
        + (f" · step {state['step']}" if state.get("step") else ""),
        f"- Task: {state.get('task') or '(not recorded)'}",
        f"- **Next action:** {state.get('next_action') or '(not recorded — reconstruct it from the last events)'}",
    ]
    if state.get("pending"):
        lines.append("- Pending:")
        for item in state["pending"]:
            lines.append(f"  - {item.get('kind')} `{item.get('id')}` {item.get('detail', '')}".rstrip())
    history = state.get("blocked_history") or []
    if history:
        last = history[-1]
        lines.append(f"- Last block: {describe_block(last)} (cleared {last.get('cleared_at', '?')})")
    models = state.get("models") or {}
    lines.append(
        "- Models: " + " · ".join(f"{k} `{v}`" for k, v in models.items() if v)
    )
    lines.append(f"- {budget_line}")
    lines.append("")
    lines.append("### Last events")
    for event in events[-10:]:
        lines.append(f"- #{event.get('seq')} {event.get('ts', '')[5:16]} {event.get('kind')}: "
                     f"{event.get('summary')}")
    lines.append("")
    lines.append("### Before acting")
    where = f"`Sessions/{slug}/`" if state.get("route") == "B" else f"the Route A log `Sessions/<date> {slug}.md`"
    lines.append("- Re-read the vault's `CLAUDE.md` (it may have changed) and the latest phase notes"
                 f" in {where}")
    lines.append("- Re-run `session-history.py`; check that files the next action depends on exist")
    return "\n".join(lines)


def cmd_resume(a: argparse.Namespace) -> int:
    vault = check_vault(a.vault)
    if a.session:
        slug = check_slug(a.session)
        state = require_state(vault, slug)
    else:
        candidates = [s for s in list_sessions(vault) if s.get("status") in OPEN_STATUSES]
        if not candidates:
            say("no open session in this vault — start a new one")
            return EXIT_OK
        if len(candidates) > 1:
            say("several open sessions — ask the user which one, then pass --session:")
            for s in candidates:
                say(f"- {s.get('session')} · {s.get('status')} · {s.get('phase')} · updated {s.get('updated')}")
            return EXIT_OK
        state = candidates[0]
        slug = state["session"]

    if state.get("status") not in OPEN_STATUSES:
        say(f"{slug} is {state.get('status')} — nothing to resume")
        return EXIT_OK
    block = state.get("blocked_on")
    if block and block.get("resource") in ("codex", "notebooklm"):
        # Only external resources can still be blocked. A Claude block is lifted by the very
        # fact that Claude is running this; a budget or user pause by the user's decision.
        until = parse_iso(block.get("resume_after"))
        if until is None or until > now():
            say(f"{slug} is still blocked: {describe_block(block)}")
            say(f"wait in the background: {wait_hint(vault, slug)}")
            if not a.force:
                return EXIT_BLOCKED
            say("--force: resuming anyway (the user says the resource is back)")
    elif block and block.get("resource") == "budget":
        level, line = evaluate_budgets(load_tokens(vault, slug)["entries"], state.get("budgets") or {})
        if level == "over" and not a.force:
            say(f"{slug} was paused on its token budget and is still over it: {line}")
            say("raise it first (`set --budget codex=N`) or confirm with the user and pass --force")
            return EXIT_BLOCKED

    events = load_events(vault, slug)
    _level, budget_line = evaluate_budgets(load_tokens(vault, slug)["entries"], state.get("budgets") or {})
    if not a.check:
        with session_lock(session_dir(vault, slug)):
            state = require_state(vault, slug)
            previous = state.get("status")
            if state.get("blocked_on"):
                history = (state.get("blocked_history") or [])[-19:]
                state["blocked_history"] = history + [dict(state["blocked_on"], cleared_at=iso(now()))]
            state["blocked_on"] = None
            state["status"] = "active"
            save_state(vault, slug, state)
            append_event(vault, slug, "resume", f"resumed (was {previous})", phase=state.get("phase"))
        events = load_events(vault, slug)
    print(brief(vault, state, events, budget_line))
    return EXIT_OK


# --------------------------------------------------------------------------
# Claude limits: the StopFailure hook and the notification at reset
# --------------------------------------------------------------------------

DEFAULT_TITLE = "duel-vault · {project}"
DEFAULT_MESSAGE = (
    "{resource} di nuovo disponibile. Riprendi la sessione «{session}» scrivendo "
    "«riprendi il progetto {project}». Prossima azione: {next_action}"
)


def schedule_notification(vault: Path, slug: str, at, probe: bool) -> str:
    """Start (once per block) a detached process that notifies the user at the reset."""
    with session_lock(session_dir(vault, slug)):
        state = require_state(vault, slug)
        block = state.get("blocked_on") or {}
        existing = state.get("notifier") or {}
        if existing.get("block_since") == block.get("since") and pid_alive(existing.get("pid")):
            return f"already scheduled (pid {existing['pid']})"
        argv = [sys.executable, str(Path(__file__).resolve()), "notify-worker",
                "--vault", str(vault), "--session", slug, "--block-since", str(block.get("since", ""))]
        if at:
            argv += ["--at", iso(at)]
        if probe:
            argv += ["--probe"]
        pid = spawn_detached(argv, env=dict(os.environ))
        state["notifier"] = {"pid": pid, "at": iso(at) if at else None, "probe": probe,
                             "block_since": block.get("since"), "started": iso(now())}
        save_state(vault, slug, state)
        when = f"at {iso(at)}" if at else "when Claude answers a probe"
        append_event(vault, slug, "notification", f"notification scheduled {when}",
                     resource=block.get("resource"), at=iso(at) if at else None, pid=pid)
    return f"notification scheduled {when} (pid {pid})"


def cmd_notify_at(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    block = state.get("blocked_on")
    if not block:
        raise SystemExit(f"session {slug!r} is not paused — nothing to notify about")
    at = parse_when(a.at) if a.at else parse_iso(block.get("resume_after"))
    if a.at and at is None:
        raise SystemExit(f"cannot read --at {a.at!r}")
    say(schedule_notification(vault, slug, at, probe=a.probe or at is None))
    return EXIT_OK


def cmd_notify_worker(a: argparse.Namespace) -> int:
    """Detached: wait for the reset, then tell the user — unless they already resumed."""
    vault, slug = check_vault(a.vault), check_slug(a.session)

    def still_blocked() -> dict | None:
        st = load_state(vault, slug)
        if not st or st.get("status") != "paused":
            return None
        block = st.get("blocked_on") or {}
        return st if str(block.get("since", "")) == a.block_since else None

    at = parse_iso(a.at)
    if at:
        sleep_until(at)
    if a.probe:
        state = still_blocked()
        if state is None:
            return EXIT_OK
        cmd = probe_command("claude", state, None)
        env = dict(os.environ, **{PROBE_ENV: "1"})
        deadline = now() + timedelta(seconds=a.max_wait)
        while True:
            if still_blocked() is None:
                return EXIT_OK
            outcome, detail = run_probe(cmd, 300, Path.home(), env=env)
            if outcome == "ok":
                break
            if outcome == "error" or now() > deadline:
                state = still_blocked()
                if state is not None:
                    notify("duel-vault · " + str(state.get("project")),
                           f"Limite Claude: orario di reset sconosciuto ({detail[:80]}). "
                           f"Prova a riprendere la sessione «{slug}» più tardi.")
                return EXIT_OK
            hint = classify_limit(detail, rate_default=False)
            nxt = parse_iso(hint.get("resume_after")) if hint else None
            poll = int((load_config().get("notify") or {}).get("poll_seconds") or a.poll)
            sleep_until(nxt if nxt and nxt > now() else now() + timedelta(seconds=poll),
                        step=min(30.0, float(poll)))

    state = still_blocked()
    if state is None:
        return EXIT_OK  # resumed meanwhile: nothing to say
    settings = load_config().get("notify") or {}
    values = {
        "project": state.get("project") or vault.name,
        "session": slug,
        "resource": {"claude": "Claude", "codex": "Codex"}.get(
            (state.get("blocked_on") or {}).get("resource"), "La risorsa"),
        "next_action": (state.get("next_action") or "vedi il brief di ripresa")[:140],
    }
    title = fill_template(str(settings.get("title") or DEFAULT_TITLE), values)
    message = fill_template(str(settings.get("message") or DEFAULT_MESSAGE), values)
    channel = notify(title, message)
    with session_lock(session_dir(vault, slug)):
        state = require_state(vault, slug)
        if state.get("notifier"):
            state["notifier"]["sent"] = iso(now())
            state["notifier"]["channel"] = channel
        save_state(vault, slug, state)
        append_event(vault, slug, "notification", f"user notified ({channel})", channel=channel)
    return EXIT_OK


def cmd_on_stop_failure(a: argparse.Namespace) -> int:
    """Claude Code `StopFailure` hook: a turn ended on an API error.

    Claude itself can run nothing at this point, so this is where the session is paused
    and the notification for the reset is scheduled. Never fails loudly: hook output is
    ignored, and this hook fires for every Claude Code session, duel-vault or not.
    """
    if os.environ.get(PROBE_ENV):
        return EXIT_OK  # one of our own probes hitting the limit — not a session event
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict) or data.get("error") != "rate_limit":
            return EXIT_OK
        found = lookup_claude_session(str(data.get("session_id") or ""))
        if not found:
            return EXIT_OK
        vault, slug = found
        state = load_state(vault, slug)
        if not state or state.get("legacy") or state.get("status") not in OPEN_STATUSES:
            return EXIT_OK
        text = "\n".join(str(data.get(k) or "") for k in ("last_assistant_message", "error_details"))
        limit = classify_limit(text, rate_default=False)
        resume_after = limit["resume_after"] if limit else None
        pause(vault, slug, {
            "resource": "claude",
            "kind": limit["kind"] if limit else "usage",
            "reason": f"Claude API error: {data.get('error')}",
            "since": iso(now()),
            "resume_after": resume_after,
            "evidence": text.strip()[-300:],
        })
        schedule_notification(vault, slug, parse_iso(resume_after), probe=resume_after is None)
        spawn_detached([sys.executable, str(SKILL_SCRIPTS / "progress-sync.py"), "push",
                        "--vault", str(vault), "--session", slug], env=dict(os.environ))
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - a hook must never break Claude Code
        try:
            with open(NOTIFY_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"{iso(now())}\ton-stop-failure error\t{exc!r}\n")
        except OSError:
            pass
    return EXIT_OK


def hook_command() -> str:
    return f"\"{sys.executable}\" \"{Path(__file__).resolve()}\" on-stop-failure"


def cmd_install_hook(a: argparse.Namespace) -> int:
    path = (a.settings or (CONFIG_PATH.parent / "settings.json")).expanduser()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise SystemExit(f"{path} is not valid JSON ({exc}) — not touching it")
        if not isinstance(data, dict):
            raise SystemExit(f"{path} does not hold a JSON object — not touching it")
    else:
        data = {}
    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault("StopFailure", [])
    ours = [g for g in groups if any("on-stop-failure" in str(h.get("command", ""))
                                     and "session-state.py" in str(h.get("command", ""))
                                     for h in g.get("hooks", []))]
    for g in ours:
        groups.remove(g)
    if a.uninstall:
        action = "removed" if ours else "not installed — nothing to remove"
        if not groups:
            hooks.pop("StopFailure")
        if not hooks:
            data.pop("hooks")
    else:
        groups.append({"matcher": "rate_limit",
                       "hooks": [{"type": "command", "command": hook_command(), "timeout": 60}]})
        action = "updated" if ours else "installed"
    if a.dry_run:
        print(json.dumps(data.get("hooks", {}), indent=2, ensure_ascii=False))
        say(f"(dry run — {path} not written; would be {action})")
        return EXIT_OK
    if a.uninstall and not ours:
        say(action)
        return EXIT_OK
    write_json_atomic(path, data)
    say(f"StopFailure hook {action} in {path}")
    return EXIT_OK


# --------------------------------------------------------------------------
# budgets per project
# --------------------------------------------------------------------------


def cmd_budgets(a: argparse.Namespace) -> int:
    if CONFIG_PATH.exists():
        try:
            json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SystemExit(f"{CONFIG_PATH} is not valid JSON ({exc}) — fix it first")
    cfg = load_config()
    if a.set or a.track_only:
        entry: dict = {k: None for k in ("codex", "claude_main", "claude_delegated")}
        for pair in a.set:
            key, _, value = pair.partition("=")
            key = key.strip()
            if key not in ("codex", "claude_main", "claude_delegated", "warn_ratio"):
                raise SystemExit(f"unknown budget {key!r}")
            try:
                entry[key] = None if value.strip().lower() in ("", "none", "null") else (
                    float(value) if key == "warn_ratio" else int(value))
            except ValueError:
                raise SystemExit(f"--set {key}= needs a number, got {value!r}") from None
        cfg.setdefault("budgets_by_project", {})[a.project] = entry
        save_config(cfg)
        say(f"saved budgets for {a.project!r} in {CONFIG_PATH}")
    configured = project_has_budgets(a.project, cfg)
    budgets = resolved_budgets(cfg, a.project)
    say(f"{a.project}: " + ("configured" if configured else
                           "NOT configured — ask the user (Route B), then save with --set or --track-only"))
    say(" · ".join(f"{k} {v if v is not None else '—'}" for k, v in budgets.items()))
    return EXIT_OK


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session-state", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, session_required: bool = True) -> None:
        p.add_argument("--vault", type=Path, required=True)
        p.add_argument("--session", required=session_required)

    p = sub.add_parser("set", help="update phase, step, next action, pending items or status")
    common(p)
    p.add_argument("--phase")
    p.add_argument("--step")
    p.add_argument("--next", help="the exact next action — what a cold resume should do first")
    p.add_argument("--pending-add", action="append", default=[], metavar="KIND:ID[:DETAIL]")
    p.add_argument("--pending-done", action="append", default=[], metavar="ID")
    p.add_argument("--status", choices=[s for s in STATUSES if s != "paused"])
    p.add_argument("--reason")
    p.add_argument("--budget", action="append", default=[], metavar="KEY=N",
                   help="change this session's budget: codex, claude_main, claude_delegated, warn_ratio")
    p.add_argument("--field", action="append", default=[], metavar="K=V",
                   help="extra key in session.json (not one the scripts manage)")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("guard", help="token budget status (exit 3 warn, 4 over)")
    common(p)
    p.set_defaults(func=cmd_guard)

    p = sub.add_parser("limit", help="recognise a usage/rate limit in an error and pause")
    common(p)
    p.add_argument("--resource", choices=LIMIT_RESOURCES, required=True)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--stderr-file", type=Path)
    src.add_argument("--text")
    p.add_argument("--classify-only", action="store_true", help="print the classification, change nothing")
    p.set_defaults(func=cmd_limit)

    p = sub.add_parser("pause", help="pause deliberately (budget, user, known outage)")
    common(p)
    p.add_argument("--resource", choices=(*LIMIT_RESOURCES, "budget", "user"), required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--until", help="ISO time, '17:30', '5pm', 'in 2h'")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("wait", help="block until the resource is back (run in background)")
    common(p)
    p.add_argument("--poll", type=int, default=900, help="seconds between probes (default 900)")
    p.add_argument("--grace", type=int, default=60, help="seconds added after a reset time")
    p.add_argument("--max-wait", type=int, default=6 * 86400, help="give up after N seconds")
    p.add_argument("--probe-cmd", help="command whose exit 0 means available")
    p.add_argument("--probe-timeout", type=int, default=180)
    p.add_argument("--no-probe", action="store_true", help="trust the reset time alone")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("resume", help="reactivate an open session and print its resume brief")
    common(p, session_required=False)
    p.add_argument("--check", action="store_true", help="print the brief, change nothing")
    p.add_argument("--force", action="store_true", help="resume even if still marked blocked")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("notify-at", help="notify the user when the block lifts (detached)")
    common(p)
    p.add_argument("--at", help="time to notify (default: the block's reset time)")
    p.add_argument("--probe", action="store_true", help="probe Claude until it answers instead")
    p.set_defaults(func=cmd_notify_at)

    p = sub.add_parser("notify-worker", help=argparse.SUPPRESS)
    common(p)
    p.add_argument("--block-since", required=True)
    p.add_argument("--at")
    p.add_argument("--probe", action="store_true")
    p.add_argument("--poll", type=int, default=900)
    p.add_argument("--max-wait", type=int, default=2 * 86400)
    p.set_defaults(func=cmd_notify_worker)

    p = sub.add_parser("on-stop-failure", help="Claude Code StopFailure hook handler (reads stdin)")
    p.set_defaults(func=cmd_on_stop_failure)

    p = sub.add_parser("install-hook", help="add (or update/remove) the StopFailure hook in settings.json")
    p.add_argument("--settings", type=Path, help="settings file (default: ~/.claude/settings.json)")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_install_hook)

    p = sub.add_parser("budgets", help="show or save a project's per-session token budgets")
    p.add_argument("--project", required=True)
    p.add_argument("--set", action="append", default=[], metavar="KEY=N")
    p.add_argument("--track-only", action="store_true", help="record the answer 'no budgets'")
    p.set_defaults(func=cmd_budgets)

    p = sub.add_parser("status", help="one session's state")
    common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="sessions in this vault, most recent first")
    p.add_argument("--vault", type=Path, required=True)
    p.add_argument("--open", action="store_true", help="only active or paused sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            warn(exc.code)
            return EXIT_USAGE
        raise
    except OSError as exc:
        warn(str(exc))
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
