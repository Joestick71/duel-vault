#!/usr/bin/env python3
"""session-log — the complete, append-only record of a duel-vault session.

The Obsidian notes are the curated record: requirements, plan, findings, decisions worth a
callout. This is the exhaustive one. Every activity, decision, file change, model
invocation and token figure goes into `<vault>/.duel-vault/<slug>/journal.jsonl` the moment
it happens, so a session cut off mid-step — Claude or Codex out of quota, a closed
terminal — loses nothing, and the next session can see exactly where it stopped.

    session-log.py init       --vault V --session S --route A|B --task "..."
    session-log.py event      --vault V --session S --kind decision --summary "..."
    session-log.py change     --vault V --session S --file path [--file path ...]
    session-log.py invocation --vault V --session S --resource codex --model M --id inv-3 \
                              --status ok --total 12400
    session-log.py tokens add --vault V --session S --resource claude_main --total 4200
    session-log.py tokens summary --vault V --session S
    session-log.py verify     --vault V --session S
    session-log.py render     --vault V --session S [--format note|block]

Exit codes: 0 ok · 1 verify found errors · 2 bad usage · 3 token budget warning ·
4 token budget exceeded.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date as _date
from pathlib import Path

from session_common import (
    EVENT_KINDS,
    EXIT_FAIL,
    EXIT_OK,
    EXIT_USAGE,
    FREE_EVENT_KINDS,
    INVOCATION_RESOURCES,
    INVOCATION_STATUSES,
    OPEN_STATUSES,
    STATUSES,
    TOKEN_RESOURCES,
    add_token_entry,
    append_event,
    budget_exit,
    check_slug,
    check_vault,
    digest,
    display_path,
    evaluate_budgets,
    journal_path,
    load_events,
    load_state,
    load_tokens,
    new_state,
    parse_iso,
    parse_kv,
    parse_total,
    phase_rank,
    progress_dir,
    read_journal,
    read_json,
    require_state,
    save_state,
    session_dir,
    state_path,
    tokens_path,
    write_json_atomic,
    write_text_atomic,
)
from vault_common import ROUTE_A_RE, live_lines, parse_frontmatter

CALLOUT_RE = re.compile(r"^\s*>\s*\[!(?P<type>[A-Za-z][A-Za-z0-9_-]*)\]")


def say(text: str) -> None:
    print(text)


def warn(text: str) -> None:
    print(f"session-log: {text}", file=sys.stderr)


def budget_report(vault: Path, slug: str, state: dict) -> int:
    entries = load_tokens(vault, slug)["entries"]
    level, line = evaluate_budgets(entries, state.get("budgets") or {})
    say(line)
    if level == "over":
        say(
            "budget exceeded — checkpoint now: `session-state.py pause --resource budget "
            "--reason ...`, push progress, and ask the user before spending more"
        )
    elif level == "warn":
        say("budget warning — tell the user at the next natural pause")
    return budget_exit(level)


# --------------------------------------------------------------------------
# init / event / change
# --------------------------------------------------------------------------


def cmd_init(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    existing = load_state(vault, slug)
    if existing is not None and existing.get("legacy"):
        return upgrade_legacy(vault, slug, existing, a)
    if existing is not None:
        say(
            f"{slug}: already initialised — status {existing['status']}, phase "
            f"{existing['phase']}. Nothing changed; use `session-state.py resume` to continue it."
        )
        return EXIT_OK
    state = new_state(vault, slug, a.route, a.project or vault.name, a.title or slug, a.task or "")
    save_state(vault, slug, state)
    if not tokens_path(vault, slug).exists():
        write_json_atomic(tokens_path(vault, slug), {"schema": 1, "entries": []})
    append_event(
        vault,
        slug,
        "session_start",
        f"Route {a.route}: {a.task or a.title or slug}",
        route=a.route,
        phase="triage",
        actor="claude",
    )
    say(f"initialised {session_dir(vault, slug)}")
    return EXIT_OK


def upgrade_legacy(vault: Path, slug: str, old: dict, a: argparse.Namespace) -> int:
    """Adopt a session.json written before the journal existed, keeping what it held."""
    state = new_state(vault, slug, a.route, a.project or vault.name, a.title or slug, a.task or "")
    for key, value in old.items():
        if key in ("legacy", "legacy_status", "status", "schema", "created", "updated"):
            continue
        state[key] = value
    state["route"] = a.route
    state["status"] = "active"
    save_state(vault, slug, state)
    had_journal = journal_path(vault, slug).exists()
    if not tokens_path(vault, slug).exists():
        write_json_atomic(tokens_path(vault, slug), {"schema": 1, "entries": []})
    append_event(
        vault,
        slug,
        "state" if had_journal else "session_start",
        "upgraded a pre-journal session.json — events before this point were not recorded",
        route=a.route,
        phase=state.get("phase"),
        actor="claude",
        upgraded=True,
    )
    say(f"{slug}: upgraded the pre-journal session.json in place (status active)")
    return EXIT_OK


def cmd_event(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    fields = parse_kv(a.field)
    event = append_event(
        vault,
        slug,
        a.kind,
        a.summary,
        phase=a.phase or state.get("phase"),
        actor=a.actor,
        step=a.step,
        note=a.note,
        **fields,
    )
    say(f"#{event['seq']} {a.kind}")
    if a.kind in ("decision", "escalation") and not a.note:
        warn(
            f"{a.kind} logged without --note: say which session note carries its callout, "
            "or `verify` cannot match it"
        )
    return EXIT_OK


def last_changes(events: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for event in events:
        if event.get("kind") == "change" and isinstance(event.get("path"), str):
            out[event["path"]] = event
    return out


def cmd_change(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    prior = last_changes(load_events(vault, slug))
    # Relative paths are relative to --base (default: the vault), never to wherever this
    # script happens to run: Codex reports paths relative to its own -C directory.
    base = a.base.expanduser().resolve() if a.base else vault
    skipped = 0
    for raw in a.file:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base / path
        rel = display_path(vault, path)
        sha = size = None
        if path.is_dir():
            warn(f"{rel} is a directory — log the files inside it")
            skipped += 1
            continue
        if path.is_file():
            if a.action == "deleted":
                warn(f"{rel} still exists — not logged as deleted")
                skipped += 1
                continue
            sha, size = digest(path)
            if not a.action and rel in prior and prior[rel].get("sha256") == sha:
                say(f"unchanged since last logged: {rel}")
                continue
            action = a.action or ("modified" if rel in prior else "written")
        elif a.action == "deleted":
            action = "deleted"
        else:
            # Never infer a deletion: a missing file is far more often a wrong path.
            warn(f"{rel} does not exist — check the path (relative to {base}); "
                 "pass --action deleted if it was removed on purpose")
            skipped += 1
            continue
        event = append_event(
            vault,
            slug,
            "change",
            a.summary or f"{action} {rel}",
            phase=a.phase or state.get("phase"),
            actor=a.actor,
            step=a.step,
            path=rel,
            action=action,
            sha256=sha,
            bytes=size,
        )
        say(f"#{event['seq']} {action} {rel}")
    return EXIT_USAGE if skipped else EXIT_OK


# --------------------------------------------------------------------------
# invocations and tokens
# --------------------------------------------------------------------------


def cmd_invocation(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    total = parse_total(a.total)
    if a.resource in ("codex", "claude_delegated") and a.status != "started" and total is None:
        warn('no --total given — recorded as "unavailable" (never estimate a measured figure)')
        total = "unavailable"
    phase = a.phase or state.get("phase")
    event = append_event(
        vault,
        slug,
        "invocation",
        a.summary or f"{a.resource} {a.model or ''} {a.id}: {a.status}".replace("  ", " "),
        phase=phase,
        actor=a.actor or (f"subagent:{a.model}" if a.resource == "claude_delegated" else a.resource),
        resource=a.resource,
        model=a.model,
        effort=a.effort,
        invocation=a.id,
        step=a.step,
        status=a.status,
        input=a.input,
        output=a.output,
        total=total,
        ops=a.ops,
        escalated=True if a.escalated else None,
    )
    say(f"#{event['seq']} invocation {a.id} ({a.status})")
    if a.resource in TOKEN_RESOURCES and a.status != "started":
        add_token_entry(
            vault,
            slug,
            {
                "phase": phase,
                "resource": a.resource,
                "model": a.model,
                "invocation": a.id,
                "input": a.input,
                "output": a.output,
                "total": total,
            },
        )
    if a.status == "limit":
        say(
            "limit hit — hand the raw error to `session-state.py limit` instead of retrying; "
            "it pauses the session and tells you how to wait"
        )
    return budget_report(vault, slug, state)


def cmd_tokens_add(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    total = parse_total(a.total)
    phase = a.phase or state.get("phase")
    add_token_entry(
        vault,
        slug,
        {
            "phase": phase,
            "resource": a.resource,
            "model": a.model,
            "invocation": a.id,
            "total": total,
            "estimated": True if a.resource == "claude_main" else None,
            "note": a.note,
        },
    )
    append_event(
        vault,
        slug,
        "tokens",
        f"{a.resource} {total} ({phase})",
        phase=phase,
        resource=a.resource,
        total=total,
    )
    return budget_report(vault, slug, state)


def fmt(value: int, unavailable: int, prefix: str = "") -> str:
    text = f"{prefix}{value:,}" if value or not unavailable else ""
    if unavailable:
        text = (text + " + " if text else "") + f"{unavailable} unavailable"
    return text or "0"


def tokens_table(entries: list[dict]) -> tuple[str, dict]:
    by_phase: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: {r: [0, 0] for r in TOKEN_RESOURCES}
    )
    by_model: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for entry in entries:
        resource = entry.get("resource")
        if resource not in TOKEN_RESOURCES:
            continue
        cell = by_phase[str(entry.get("phase") or "unknown")][resource]
        value = entry.get("total")
        known = isinstance(value, int) and not isinstance(value, bool)
        cell[0 if known else 1] += value if known else 1
        if resource == "codex":
            model_cell = by_model[str(entry.get("model") or "?")]
            model_cell[0 if known else 1] += value if known else 1
    lines = [
        "| Phase | Codex (measured) | Claude main (est.) | Claude delegated (measured) |",
        "|-------|------------------|--------------------|-----------------------------|",
    ]
    grand = {r: [0, 0] for r in TOKEN_RESOURCES}
    for phase in sorted(by_phase, key=phase_rank):
        row = by_phase[phase]
        for r in TOKEN_RESOURCES:
            grand[r][0] += row[r][0]
            grand[r][1] += row[r][1]
        lines.append(
            f"| {phase} | {fmt(*row['codex'])} | {fmt(*row['claude_main'], prefix='~')} | "
            f"{fmt(*row['claude_delegated'])} |"
        )
    lines.append(
        f"| **Total** | **{fmt(*grand['codex'])}** | **{fmt(*grand['claude_main'], prefix='~')}** | "
        f"**{fmt(*grand['claude_delegated'])}** |"
    )
    if by_model:
        lines.append("")
        lines.append(
            "Codex by model: "
            + " · ".join(f"`{m}` {fmt(*v)}" for m, v in sorted(by_model.items()))
        )
    summary = {
        "by_phase": {p: {r: {"total": v[0], "unavailable": v[1]} for r, v in row.items()}
                     for p, row in by_phase.items()},
        "totals": {r: {"total": v[0], "unavailable": v[1]} for r, v in grand.items()},
        "codex_by_model": {m: {"total": v[0], "unavailable": v[1]} for m, v in by_model.items()},
    }
    return "\n".join(lines), summary


def cmd_tokens_summary(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    entries = load_tokens(vault, slug)["entries"]
    table, summary = tokens_table(entries)
    level, line = evaluate_budgets(entries, state.get("budgets") or {})
    if a.json:
        summary["budget"] = {"level": level, "line": line}
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        say(table)
        say("")
        say(line)
    return EXIT_OK


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


class Findings:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    def error(self, code: str, msg: str) -> None:
        self.items.append(("ERROR", code, msg))

    def warn(self, code: str, msg: str) -> None:
        self.items.append(("WARN", code, msg))

    @property
    def errors(self) -> int:
        return sum(1 for level, _c, _m in self.items if level == "ERROR")

    @property
    def warnings(self) -> int:
        return sum(1 for level, _c, _m in self.items if level == "WARN")


def session_notes(vault: Path, slug: str, route: str) -> list[Path]:
    sessions = vault / "Sessions"
    if route == "B":
        folder = sessions / slug
        return sorted(folder.rglob("*.md")) if folder.is_dir() else []
    notes = []
    if sessions.is_dir():
        for path in sessions.glob("*.md"):
            m = ROUTE_A_RE.match(path.stem)
            if m and m.group("slug") == slug:
                notes.append(path)
    return notes


def count_callouts(paths: list[Path], wanted: str) -> int:
    total = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _data, _err, body = parse_frontmatter(text)
        for _n, line in live_lines(text, from_line=body, keep_code=True):
            m = CALLOUT_RE.match(line)
            if m and m.group("type").lower() == wanted:
                total += 1
    return total


def verify(vault: Path, slug: str) -> Findings:
    f = Findings()
    sp = state_path(vault, slug)
    raw_state = read_json(sp, None)
    if not isinstance(raw_state, dict):
        f.error("SL001", f"{sp} is missing or not a JSON object")
        return f
    state = load_state(vault, slug) or {}
    if state.get("legacy"):
        # Nothing else is meaningful until it is upgraded — say the one thing to do.
        f.error("SL006", "pre-journal session.json — run `session-log.py init` on it to upgrade")
        return f

    # journal structure
    rows = read_journal(journal_path(vault, slug))
    if not rows:
        f.error("SL002", "journal.jsonl is missing or empty")
        return f
    events: list[dict] = []
    for lineno, event, err in rows:
        if event is None:
            f.error("SL002", f"journal line {lineno}: {err}")
            continue
        for key in ("seq", "ts", "kind", "summary"):
            if key not in event:
                f.error("SL002", f"journal line {lineno}: missing `{key}`")
        if event.get("kind") not in EVENT_KINDS:
            f.warn("SL003", f"journal line {lineno}: unknown kind {event.get('kind')!r}")
        if parse_iso(event.get("ts")) is None:
            f.error("SL002", f"journal line {lineno}: `ts` is not an ISO timestamp")
        events.append(event)
    seqs = [e.get("seq") for e in events]
    if seqs != list(range(1, len(seqs) + 1)):
        f.error("SL004", "journal sequence numbers are not contiguous from 1 — lines lost or edited")
    stamps = [parse_iso(e.get("ts")) for e in events]
    for prev, cur, event in zip(stamps, stamps[1:], events[1:]):
        if prev and cur and cur < prev:
            f.warn("SL004", f"event #{event.get('seq')} is timestamped before the one it follows")
            break
    if events and events[0].get("kind") != "session_start":
        f.error("SL005", "journal does not start with `session_start`")

    # state
    status = state.get("status")
    if status not in STATUSES:
        f.error("SL006", f"session.json status {status!r} is not one of {', '.join(STATUSES)}")
    if status == "paused" and not state.get("blocked_on"):
        f.error("SL006", "session is paused but `blocked_on` says neither why nor until when")
    if status in OPEN_STATUSES and not str(state.get("next_action") or "").strip():
        f.warn(
            "SL006",
            "session is open but `next_action` is empty — a cut-off session could not be resumed",
        )
    kinds = [e.get("kind") for e in events]
    # A session can be closed and reopened: what counts is the last transition.
    last_end = max((i for i, k in enumerate(kinds) if k == "session_end"), default=-1)
    last_open = max((i for i, k in enumerate(kinds) if k in ("session_start", "resume")), default=-1)
    closed_in_journal = last_end > last_open
    if status in ("complete", "abandoned"):
        if not closed_in_journal:
            f.error("SL007", f"session is {status} but the journal's last transition is not `session_end`")
        if status == "complete" and state.get("pending"):
            f.warn("SL007", f"session is complete with {len(state['pending'])} pending item(s)")
    elif closed_in_journal:
        f.error("SL007", f"journal ends the session but session.json status is {status!r}")

    # invocations: every start has an outcome
    open_inv: dict[str, dict] = {}
    for event in events:
        if event.get("kind") != "invocation":
            continue
        key = str(event.get("invocation"))
        if event.get("status") == "started":
            open_inv[key] = event
        else:
            open_inv.pop(key, None)
    for key, event in open_inv.items():
        msg = f"invocation {key} started (#{event.get('seq')}) but no outcome was logged"
        if status in OPEN_STATUSES:
            f.warn("SL008", msg)
        else:
            f.error("SL008", msg)

    # tokens ledger matches the invocations
    tdata = read_json(tokens_path(vault, slug), None)
    if not isinstance(tdata, dict):
        f.error("SL009", "tokens.json is missing or not a JSON object")
    else:
        entries = load_tokens(vault, slug)["entries"]
        valid_entries = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                f.error("SL009", f"tokens.json entry {i}: not a JSON object")
                continue
            value = entry.get("total")
            ok = value == "unavailable" or (
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
            )
            if not ok or entry.get("resource") not in TOKEN_RESOURCES:
                f.error("SL009", f"tokens.json entry {i}: bad resource or total ({entry!r:.120})")
                continue
            valid_entries.append(entry)
        ledger = {(e.get("resource"), str(e.get("invocation"))) for e in valid_entries}
        for event in events:
            if (
                event.get("kind") == "invocation"
                and event.get("resource") in TOKEN_RESOURCES
                and event.get("status") != "started"
                and (event.get("resource"), str(event.get("invocation"))) not in ledger
            ):
                f.warn(
                    "SL009",
                    f"invocation {event.get('invocation')} (#{event.get('seq')}) has no tokens.json entry",
                )

    # file changes: the files still match what was logged
    for rel, event in last_changes(events).items():
        path = Path(rel) if Path(rel).is_absolute() else vault / rel
        if event.get("action") == "deleted":
            if path.exists():
                f.warn("SL010", f"{rel} was logged as deleted but exists — log it again")
            continue
        if not path.is_file():
            f.warn("SL010", f"{rel} was logged as {event.get('action')} but no longer exists")
            continue
        sha, _size = digest(path)
        if event.get("sha256") and sha != event["sha256"]:
            f.warn("SL010", f"{rel} changed after it was last logged (#{event.get('seq')}) — unlogged edit")

    # decisions and escalations reach the Obsidian notes
    notes = session_notes(vault, slug, str(state.get("route", "B")))
    for kind in ("decision", "escalation"):
        logged = kinds.count(kind)
        if not logged:
            continue
        if not notes:
            f.warn("SL011", f"{logged} {kind}(s) in the journal but no session notes found yet")
            continue
        written = count_callouts(notes, kind)
        if written < logged:
            f.warn(
                "SL011",
                f"{logged} {kind} event(s) in the journal but only {written} `> [!{kind}]` "
                "callout(s) in the session notes",
            )

    # the progress snapshot was published, or is knowingly queued
    nb = read_json(progress_dir(vault) / "notebook.json", None)
    if isinstance(nb, dict) and nb.get("pending_push"):
        f.warn(
            "SL012",
            f"progress snapshot not yet in NotebookLM (queued: {nb.get('last_error') or 'unknown'})",
        )
    return f


def cmd_verify(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    findings = verify(vault, slug)
    if a.json:
        print(
            json.dumps(
                {
                    "errors": findings.errors,
                    "warnings": findings.warnings,
                    "issues": [
                        {"level": lv, "code": c, "message": m} for lv, c, m in findings.items
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        for level, code, msg in findings.items:
            say(f"{level:5} {code}  {msg}")
        say(f"session-log verify: {findings.errors} error(s), {findings.warnings} warning(s)")
    if findings.errors or (a.strict and findings.warnings):
        return EXIT_FAIL
    return EXIT_OK


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------


def cell(text: object) -> str:
    out = re.sub(r"\s+", " ", str(text if text is not None else "")).strip()
    out = out.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
    out = out.replace("[[", "[​[")  # never create a wikilink by accident
    return out


def event_detail(event: dict) -> str:
    kind = event.get("kind")
    bits: list[str] = []
    if kind == "invocation":
        for key in ("model", "status"):
            if event.get(key):
                bits.append(str(event[key]))
        if event.get("total") is not None:
            bits.append(f"{event['total']} tok")
    elif kind == "change":
        bits.append(f"`{cell(event.get('path'))}`")
    elif kind in ("pause", "resource_available"):
        if event.get("resume_after"):
            bits.append(f"until {event['resume_after']}")
    if event.get("step") is not None:
        bits.insert(0, f"step {event['step']}")
    return " · ".join(bits)


def render(vault: Path, slug: str, fmt_kind: str, heading: str = "Activity") -> str:
    state = require_state(vault, slug)
    events = load_events(vault, slug)
    entries = load_tokens(vault, slug)["entries"]
    table, _summary = tokens_table(entries)
    level, budget_line = evaluate_budgets(entries, state.get("budgets") or {})

    timeline = ["| # | Time | Phase | Kind | Actor | Summary | Detail |",
                "|---|------|-------|------|-------|---------|--------|"]
    for event in events:
        ts = parse_iso(event.get("ts"))
        when = ts.strftime("%m-%d %H:%M") if ts else "?"
        timeline.append(
            f"| {event.get('seq')} | {when} | {cell(event.get('phase', ''))} | {cell(event.get('kind'))} "
            f"| {cell(event.get('actor', ''))} | {cell(event.get('summary'))} | {event_detail(event)} |"
        )

    changes = last_changes(events)
    change_lines = [
        f"- `{cell(rel)}` — {cell(ev.get('action'))} (#{ev.get('seq')}, {cell(ev.get('actor', ''))})"
        for rel, ev in sorted(changes.items())
    ] or ["- (none)"]
    decisions = [
        f"- #{e.get('seq')} {cell(e.get('summary'))}" for e in events if e.get("kind") == "decision"
    ] or ["- (none)"]
    counts = defaultdict(int)
    for e in events:
        counts[e.get("kind")] += 1
    stats = " · ".join(f"{k}: {v}" for k, v in sorted(counts.items()))

    body = [
        "## Timeline",
        "",
        *timeline,
        "",
        "## Decisions",
        "",
        *decisions,
        "",
        "## Changes",
        "",
        *change_lines,
        "",
        "## Tokens",
        "",
        table,
        "",
        budget_line,
        "",
        f"Events: {len(events)} ({stats})",
        "",
    ]
    if fmt_kind == "block":
        nested = [ln.replace("## ", "### ", 1) if ln.startswith("## ") else ln for ln in body]
        return "\n".join([f"## {heading}", "", *nested])

    today = _date.today().isoformat()
    created = parse_iso(state.get("created"))
    front = [
        "---",
        f"project: {json.dumps(state.get('project') or vault.name, ensure_ascii=False)}",
        f"session: {json.dumps(slug, ensure_ascii=False)}",
        "phase: activity",
        f"date: {created.date().isoformat() if created else today}",
        f"events: {len(events)}",
        f"status: {state.get('status')}",
        "tags: [duel-vault, duel-vault/activity]",
        "---",
        "",
        f"# Activity log — {state.get('title') or slug}",
        "",
        f"Generated from `.duel-vault/{slug}/journal.jsonl` by `scripts/session-log.py render`. "
        "Do not edit by hand: regenerate it.",
        "",
        f"MOC: [[00 - {slug} MOC]]",
        "",
    ]
    return "\n".join(front + body)


def cmd_render(a: argparse.Namespace) -> int:
    vault, slug = check_vault(a.vault), check_slug(a.session)
    state = require_state(vault, slug)
    text = render(vault, slug, a.format, a.heading)
    if a.format == "block":
        if a.out:
            write_text_atomic(a.out.expanduser(), text)
            say(f"wrote {a.out}")
        else:
            print(text)
        return EXIT_OK
    if str(state.get("route")) != "B" and not a.out:
        warn("Route A has no session folder — use --format block and paste it into the log")
        return EXIT_USAGE
    out = a.out.expanduser() if a.out else vault / "Sessions" / slug / "07-activity-log.md"
    write_text_atomic(out, text)
    say(f"wrote {out}")
    return EXIT_OK


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session-log", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--vault", type=Path, required=True, help="vault root")
        p.add_argument("--session", required=True, help="session slug")

    p = sub.add_parser("init", help="create the session's working dir, state, ledger and journal")
    common(p)
    p.add_argument("--route", choices=("A", "B"), required=True)
    p.add_argument("--project", help="project name (default: vault dir name)")
    p.add_argument("--title", help="session title")
    p.add_argument("--task", help="one-line task summary")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("event", help="log an activity, decision, checkpoint, escalation, warning or error")
    common(p)
    p.add_argument("--kind", choices=FREE_EVENT_KINDS, required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--phase")
    p.add_argument("--step")
    p.add_argument("--actor", default="claude")
    p.add_argument("--note", help="session note that carries the callout (decision/escalation)")
    p.add_argument("--field", action="append", default=[], metavar="K=V")
    p.set_defaults(func=cmd_event)

    p = sub.add_parser("change", help="log files created, modified or deleted, with their hash")
    common(p)
    p.add_argument("--file", action="append", required=True)
    p.add_argument("--base", type=Path, help="directory relative paths are relative to (default: the vault)")
    p.add_argument("--action", choices=("created", "modified", "deleted", "renamed", "written"))
    p.add_argument("--summary")
    p.add_argument("--phase")
    p.add_argument("--step")
    p.add_argument("--actor", default="claude")
    p.set_defaults(func=cmd_change)

    p = sub.add_parser("invocation", help="log a Codex/subagent/NotebookLM call and its tokens")
    common(p)
    p.add_argument("--resource", choices=INVOCATION_RESOURCES, required=True)
    p.add_argument("--id", required=True, help="invocation id, e.g. build-step-3")
    p.add_argument("--status", choices=INVOCATION_STATUSES, required=True)
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--step")
    p.add_argument("--phase")
    p.add_argument("--actor")
    p.add_argument("--summary")
    p.add_argument("--input", type=int)
    p.add_argument("--output", type=int)
    p.add_argument("--total", help='integer, or "unavailable"')
    p.add_argument("--ops", type=int, help="NotebookLM operations (no tokens)")
    p.add_argument("--escalated", action="store_true", help="this call ran on the higher tier")
    p.set_defaults(func=cmd_invocation)

    p = sub.add_parser("tokens", help="token ledger: add an estimate, or summarise")
    tsub = p.add_subparsers(dest="tokens_command", required=True)
    t = tsub.add_parser("add", help="record a figure not tied to an invocation (main-thread estimate)")
    common(t)
    t.add_argument("--resource", choices=TOKEN_RESOURCES, required=True)
    t.add_argument("--total", required=True)
    t.add_argument("--phase")
    t.add_argument("--model")
    t.add_argument("--id")
    t.add_argument("--note")
    t.set_defaults(func=cmd_tokens_add)
    t = tsub.add_parser("summary", help="per-phase table for the final report, plus budget status")
    common(t)
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_tokens_summary)

    p = sub.add_parser("verify", help="check the session's logs are complete and consistent")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--strict", action="store_true", help="warnings fail too")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("render", help="write 07-activity-log.md (Route B) or a block (Route A)")
    common(p)
    p.add_argument("--format", choices=("note", "block"), default="note")
    p.add_argument("--heading", default="Activity", help="block heading, in the vault's language")
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_render)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
