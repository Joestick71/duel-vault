#!/usr/bin/env python3
"""progress-sync — publish a project's progress to its own NotebookLM notebook.

Each vault project gets one dedicated notebook ("<project> — avanzamento duel-vault"). It
holds one *current state* source — replaced at every push — plus one summary source per
completed session. A later session, on this machine or another one, can read where the
project stands without the local `.duel-vault/` working dirs, and `pull --restore` can
rebuild the open sessions' state from it.

The vault stays the source of truth. The notebook is a mirror for recovery and for asking
questions about the project's history; nothing here ever blocks a session: when NotebookLM
is unreachable the push is queued locally and retried at the next one.

    progress-sync.py render --vault V                      write the snapshot locally only
    progress-sync.py push   --vault V [--session S]        render + publish (queued on failure)
    progress-sync.py pull   --vault V [--restore]          fetch the snapshot back
    progress-sync.py status --vault V

Exit codes: 0 ok (pushed, unchanged or disabled) · 1 pull failed · 2 bad usage ·
3 push queued (NotebookLM unavailable, or another push still running after --lock-wait;
the local state is on disk and the next push publishes it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from session_common import (
    EXIT_FAIL,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_WARN,
    OPEN_STATUSES,
    LockTimeout,
    SCHEMA,
    append_event,
    check_slug,
    check_vault,
    evaluate_budgets,
    file_lock,
    iso,
    list_sessions,
    load_events,
    load_state,
    load_tokens,
    now,
    progress_dir,
    read_json,
    save_state,
    session_dir,
    tokens_path,
    write_json_atomic,
    write_text_atomic,
)
from vault_common import MOC_RE, ROUTE_A_RE, SKIP_DIRS, load_config, parse_frontmatter

STATE_MARKER = "<!-- duel-vault:state v1 -->"
PUSH_LOCK = ".push.lock"  # in <vault>/.duel-vault/progress/
STAMP_PREFIX = "> Snapshot time:"
FLAG_ERROR_RE = re.compile(r"no such option|unrecognized arguments|unknown option", re.I)
CLI_TIMEOUT = 120


class CliError(RuntimeError):
    pass


def say(text: str) -> None:
    print(text)


def warn(text: str) -> None:
    print(f"progress-sync: {text}", file=sys.stderr)


def settings() -> dict:
    raw = load_config().get("progress_notebook")
    cfg = {"enabled": True, "title": "{project} — avanzamento duel-vault"}
    if isinstance(raw, dict):
        cfg.update(raw)
    return cfg


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "project"


def summary_stem(slug: str) -> str:
    # Keep existing simple names; preserve identity when normalisation is lossy.
    normal = slugify(slug)
    if normal == slug:
        return normal
    return f"{normal[:80]}.{hashlib.sha256(slug.encode('utf-8')).hexdigest()[:16]}"


# --------------------------------------------------------------------------
# notebooklm CLI
# --------------------------------------------------------------------------


def cli(args: list[str], *, want_json: bool = True, answer_yes: bool = False) -> str:
    """Run `notebooklm <args>`; return stdout or raise CliError.

    The notebook flag is spelled `--notebook` or `-n` depending on the subcommand; only a
    failure that looks like an unknown flag is retried with the other spelling.
    """
    variants = [args]
    if "--notebook" in args:
        variants.append(["-n" if a == "--notebook" else a for a in args])
    last = ""
    for i, variant in enumerate(variants):
        cmd = ["notebooklm", *variant]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT,
                input="y\n" if answer_yes else None,
            )
        except FileNotFoundError:
            raise CliError("the `notebooklm` CLI is not on PATH") from None
        except subprocess.TimeoutExpired:
            raise CliError(f"`{' '.join(cmd)}` timed out after {CLI_TIMEOUT}s") from None
        if proc.returncode == 0:
            if want_json:
                try:
                    json.loads(proc.stdout)
                except ValueError:
                    raise CliError(f"`{' '.join(cmd)}` returned non-JSON output") from None
            return proc.stdout
        last = (proc.stderr or proc.stdout).strip()[:400]
        if i < len(variants) - 1 and (proc.returncode == 2 or FLAG_ERROR_RE.search(last)):
            continue
        break
    raise CliError(last or "unknown notebooklm failure")


def find_id(payload: object, *containers: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in containers:
        inner = payload.get(key)
        if isinstance(inner, dict) and isinstance(inner.get("id"), str):
            return inner["id"]
    value = payload.get("id")
    return value if isinstance(value, str) else None


def list_of(payload: object, key: str) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return [x for x in payload[key] if isinstance(x, dict)]
    return []


def ensure_notebook(meta: dict, title: str) -> str:
    if meta.get("notebook_id"):
        return meta["notebook_id"]
    # Another machine may already have created it: look it up by title before creating.
    try:
        listing = json.loads(cli(["list", "--json"]))
        for nb in list_of(listing, "notebooks"):
            if nb.get("title") == title and isinstance(nb.get("id"), str):
                meta["notebook_id"] = nb["id"]
                return nb["id"]
    except CliError:
        pass
    created = json.loads(cli(["create", title, "--json"]))
    nb_id = find_id(created, "notebook")
    if not nb_id:
        raise CliError("`notebooklm create` returned no notebook id")
    meta["notebook_id"] = nb_id
    meta["created"] = iso(now())
    return nb_id


def add_source(nb: str, path: Path) -> str:
    out = json.loads(cli(["source", "add", str(path), "--notebook", nb, "--json"]))
    sid = find_id(out, "source")
    if not sid:
        raise CliError(f"`source add {path.name}` returned no source id")
    return sid


def delete_source(nb: str, sid: str) -> bool:
    try:
        cli(["source", "delete", sid, "--notebook", nb], want_json=False, answer_yes=True)
        return True
    except CliError as exc:
        warn(f"could not delete stale source {sid}: {exc}")
        return False


def fetch_source_text(nb: str, sid: str) -> str:
    errors = []
    for sub in (["source", "fulltext", sid], ["source", "get", sid]):
        try:
            raw = cli([*sub, "--notebook", nb, "--json"])
        except CliError as exc:
            errors.append(str(exc)[:120])
            continue
        data = json.loads(raw)
        for holder in (data, data.get("source") if isinstance(data, dict) else None):
            if isinstance(holder, dict):
                for key in ("fulltext", "content", "text"):
                    if isinstance(holder.get(key), str) and holder[key].strip():
                        return holder[key]
        errors.append(f"`{' '.join(sub[:2])}` returned no text field")
    raise CliError("; ".join(errors) or "no way to read the source text")


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------


def project_name(vault: Path, sessions: list[dict]) -> str:
    for s in sessions:
        if s.get("project"):
            return str(s["project"])
    return vault.name


def obsidian_sessions(vault: Path) -> list[dict]:
    """Sessions as the vault's notes record them — covers sessions older than the journal."""
    out: list[dict] = []
    root = vault / "Sessions"
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue
        if entry.is_dir():
            moc = next((p for p in entry.glob("*.md") if MOC_RE.match(p.stem)), None)
            data = {}
            if moc:
                data, _e, _b = parse_frontmatter(moc.read_text(encoding="utf-8", errors="replace"))
                data = data or {}
            out.append({"session": entry.name, "route": "B", "status": str(data.get("status", "?")),
                        "date": str(data.get("date", "?"))})
        elif entry.suffix == ".md":
            m = ROUTE_A_RE.match(entry.stem)
            if m:
                out.append({"session": m.group("slug"), "route": "A", "status": "complete",
                            "date": m.group("date")})
    return out


def oneline(text: object, limit: int = 200) -> str:
    out = re.sub(r"\s+", " ", str(text or "")).strip()
    return out if len(out) <= limit else out[: limit - 1] + "…"


def render_state(vault: Path) -> tuple[str, str]:
    sessions = list_sessions(vault)
    project = project_name(vault, sessions)
    open_sessions = [s for s in sessions if s.get("status") in OPEN_STATUSES]
    lines = [
        f"# Project progress — {project}",
        "",
        "> Snapshot published by duel-vault (`scripts/progress-sync.py`). The vault is the source",
        f"> of truth: `{vault}`, with session working dirs under `.duel-vault/`.",
        f"{STAMP_PREFIX} {iso(now())}",
        "",
        "## Open sessions",
        "",
    ]
    if not open_sessions:
        lines += ["None — every session is closed.", ""]
    for s in open_sessions:
        slug = s["session"]
        # Our own sync events are excluded: a push that changed the snapshot it pushes
        # would make every later push look like new progress.
        events = [e for e in load_events(vault, slug) if e.get("kind") != "sync"]
        _lvl, budget = evaluate_budgets(load_tokens(vault, slug)["entries"], s.get("budgets") or {})
        block = s.get("blocked_on")
        lines += [
            f"### {slug} — {s.get('status')}",
            "",
            f"- Route {s.get('route')} · phase {s.get('phase')}"
            + (f" · step {s['step']}" if s.get("step") else ""),
            f"- Task: {oneline(s.get('task')) or '(not recorded)'}",
            f"- Next action: {oneline(s.get('next_action'), 400) or '(not recorded)'}",
        ]
        if block and block.get("cleared_at"):
            lines.append(
                f"- {block.get('resource')} available again since {block['cleared_at']} — waiting "
                f"for `resume` (was: {oneline(block.get('reason'))})"
            )
        elif block:
            lines.append(
                f"- Blocked on {block.get('resource')} ({block.get('kind')}) until "
                f"{block.get('resume_after') or 'unknown'}: {oneline(block.get('reason'))}"
            )
        for item in s.get("pending") or []:
            lines.append(f"- Pending {item.get('kind')} `{item.get('id')}` {oneline(item.get('detail'))}".rstrip())
        lines.append(f"- {budget}")
        lines.append("- Last activity:")
        for e in events[-6:]:
            lines.append(f"  - {str(e.get('ts', ''))[:16]} {e.get('kind')}: {oneline(e.get('summary'), 160)}")
        lines.append("")

    lines += ["## Session history", "", "| Session | Route | Status | Date |", "|---|---|---|---|"]
    known = {s["session"]: s for s in sessions}
    rows = {o["session"]: o for o in obsidian_sessions(vault)}
    for slug, s in known.items():
        rows[slug] = {"session": slug, "route": s.get("route"), "status": s.get("status"),
                      "date": str(s.get("created", ""))[:10]}
    for row in sorted(rows.values(), key=lambda r: str(r.get("date")), reverse=True)[:30]:
        lines.append(f"| {row['session']} | {row['route']} | {row['status']} | {row['date']} |")
    lines.append("")

    decisions: list[tuple[str, str, str]] = []
    changes: list[tuple[str, str, str]] = []
    for s in sessions:
        for e in load_events(vault, s["session"]):
            if e.get("kind") == "decision":
                decisions.append((str(e.get("ts")), s["session"], oneline(e.get("summary"))))
            elif e.get("kind") == "change":
                changes.append((str(e.get("ts")), s["session"], f"`{e.get('path')}` {e.get('action')}"))
    lines += ["## Recent decisions", ""]
    lines += [f"- [{slug}] {text}" for _ts, slug, text in sorted(decisions, reverse=True)[:15]] or ["- (none)"]
    lines += ["", "## Recent changes", ""]
    lines += [f"- [{slug}] {text}" for _ts, slug, text in sorted(changes, reverse=True)[:20]] or ["- (none)"]
    lines += [
        "",
        "## How to resume",
        "",
        f"- In Claude Code: \"riprendi il progetto {project}\" — duel-vault runs "
        "`session-state.py resume` and continues from the next action above.",
        "- On a machine without the vault's `.duel-vault/` folder: `progress-sync.py pull "
        "--restore` rebuilds the open sessions' state from the block below.",
        "",
        "## Machine-readable state",
        "",
        STATE_MARKER,
        "```json",
        json.dumps({"schema": SCHEMA, "project": project,
                    "open_sessions": [publishable(s) for s in open_sessions]},
                   indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    return project, "\n".join(lines)


def publishable(state: dict) -> dict:
    """The state as it may leave the machine: raw stderr excerpts stay local — they can
    carry paths, tokens or anything else a failing command printed."""
    out = {k: v for k, v in state.items() if k not in ("legacy",)}
    for key in ("blocked_on",):
        if isinstance(out.get(key), dict):
            out[key] = {k: v for k, v in out[key].items() if k != "evidence"}
    out["blocked_history"] = [
        {k: v for k, v in b.items() if k != "evidence"}
        for b in (out.get("blocked_history") or [])[-5:]
        if isinstance(b, dict)
    ]
    return out


def content_sha(text: str) -> str:
    stable = "\n".join(ln for ln in text.splitlines() if not ln.startswith(STAMP_PREFIX))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def render_session_summary(vault: Path, slug: str) -> str:
    state = load_state(vault, slug) or {}
    events = load_events(vault, slug)
    _lvl, budget = evaluate_budgets(load_tokens(vault, slug)["entries"], state.get("budgets") or {})
    lines = [
        f"# Session {slug} — {state.get('title') or slug}",
        "",
        f"- Project: {state.get('project')} · route {state.get('route')} · status {state.get('status')}",
        f"- Task: {oneline(state.get('task'), 400)}",
        f"- {budget}",
        "",
        "## Events",
        "",
    ]
    for e in events:
        lines.append(f"- {str(e.get('ts', ''))[:16]} {e.get('kind')}: {oneline(e.get('summary'), 240)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def meta_path(vault: Path) -> Path:
    return progress_dir(vault) / "notebook.json"


def state_file(vault: Path, project: str) -> Path:
    return progress_dir(vault) / f"duel-vault-state-{slugify(project)}.md"


def cmd_render(a: argparse.Namespace) -> int:
    vault = check_vault(a.vault)
    project, text = render_state(vault)
    path = state_file(vault, project)
    write_text_atomic(path, text)
    say(f"wrote {path}")
    return EXIT_OK


def log_sync(vault: Path, slug: str | None, summary: str, **fields: object) -> None:
    if slug and (session_dir(vault, slug) / "session.json").is_file():
        append_event(vault, slug, "sync", summary, actor="script", **fields)


def cmd_push(a: argparse.Namespace) -> int:
    """Pushes are serialised per vault: two at once would each create the notebook, add the
    same session summary, or orphan each other's state source. A push waits up to
    `--lock-wait` for one already running, then gives up like a queued push — the state is
    on disk, and the next push publishes whatever changed."""
    vault = check_vault(a.vault)
    slug = check_slug(a.session) if a.session else None
    try:
        with file_lock(progress_dir(vault) / PUSH_LOCK, timeout=a.lock_wait):
            return push_locked(vault, slug, a)
    except LockTimeout:
        say(f"another progress push is still running after {a.lock_wait:g}s — this one is "
            "skipped; the next push publishes the latest state")
        log_sync(vault, slug, "progress push skipped: another push in progress", status="queued")
        return EXIT_WARN


def push_locked(vault: Path, slug: str | None, a: argparse.Namespace) -> int:
    cfg = settings()
    project, text = render_state(vault)
    path = state_file(vault, project)
    write_text_atomic(path, text)
    if not cfg.get("enabled", True):
        say(f"progress notebook disabled in config — snapshot written locally: {path}")
        return EXIT_OK

    meta = read_json(meta_path(vault), {})
    meta = meta if isinstance(meta, dict) else {}
    meta.setdefault("session_sources", {})
    title = str(cfg.get("title", "{project}")).replace("{project}", project)
    sha = content_sha(text)

    # Every closed session not yet in the notebook gets its summary — not only the one
    # named on this push, so a summary whose push failed is caught up by the next one.
    summaries: dict[str, Path] = {}
    for closed in list_sessions(vault):
        done = closed.get("session")
        if closed.get("status") not in ("complete", "abandoned") or done in meta["session_sources"]:
            continue
        spath = progress_dir(vault) / "sessions" / f"duel-vault-session-{summary_stem(done)}.md"
        write_text_atomic(spath, render_session_summary(vault, done))
        summaries[done] = spath
        if len(summaries) >= 10:
            break

    if sha == meta.get("state_sha") and not summaries and not meta.get("pending_push") and not a.force:
        say("progress unchanged since the last push — nothing sent")
        return EXIT_OK
    if a.dry_run:
        say(f"would push {path.name} to notebook '{title}'"
            + (f" plus {len(summaries)} session summary" if summaries else ""))
        return EXIT_OK

    try:
        nb = ensure_notebook(meta, title)
        meta["title"] = title
        write_json_atomic(meta_path(vault), meta)
        for done_slug, spath in summaries.items():
            meta["session_sources"][done_slug] = add_source(nb, spath)
            write_json_atomic(meta_path(vault), meta)
        new_sid = add_source(nb, path)  # add first: a failure never leaves zero state sources
        old_sid = meta.get("state_source_id")
        meta.update(state_source_id=new_sid, state_sha=sha, last_push=iso(now()),
                    pending_push=False, last_error=None)
        write_json_atomic(meta_path(vault), meta)
        stale = [s for s in (meta.get("stale_sources") or []) if s != new_sid]
        if old_sid and old_sid != new_sid:
            stale.append(old_sid)
        meta["stale_sources"] = [s for s in stale if not delete_source(nb, s)]
        write_json_atomic(meta_path(vault), meta)
    except CliError as exc:
        meta.update(pending_push=True, last_error=str(exc)[:300], last_attempt=iso(now()))
        write_json_atomic(meta_path(vault), meta)
        log_sync(vault, slug, "progress push queued", status="queued", error=str(exc)[:200])
        say(f"NotebookLM unavailable ({exc}) — snapshot kept locally at {path}; "
            "the next push retries it")
        return EXIT_WARN
    log_sync(vault, slug, "progress pushed to NotebookLM", status="ok", notebook=meta["notebook_id"],
             source=meta["state_source_id"])
    say(f"pushed to '{title}' (notebook {meta['notebook_id']}, source {meta['state_source_id']})")
    return EXIT_OK


def extract_state_block(text: str) -> dict | None:
    """The machine-readable block, tolerating what a round trip through NotebookLM may do
    to markdown: the HTML-comment marker or the code fence can be stripped, so the search
    anchors on the marker, else on the heading, and decodes the first JSON object after it."""
    idx = text.find(STATE_MARKER)
    if idx < 0:
        idx = text.find("Machine-readable state")
    if idx < 0:
        return None
    decoder = json.JSONDecoder()
    pos = text.find("{", idx)
    while pos >= 0:
        try:
            data, _end = decoder.raw_decode(text, pos)
        except ValueError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(data, dict) and "open_sessions" in data:
            return data
        pos = text.find("{", pos + 1)
    return None


def restore(vault: Path, text: str) -> int:
    data = extract_state_block(text)
    if data is None:
        warn("the snapshot has no readable machine state block — nothing restored")
        return EXIT_FAIL
    states = data.get("open_sessions")
    if data.get("schema") != SCHEMA or not isinstance(states, list):
        warn("unsupported snapshot schema or invalid open_sessions list — nothing restored")
        return EXIT_FAIL
    # Validate the entire remote payload before creating any local session.
    seen = set()
    for state in states:
        if not isinstance(state, dict) or not isinstance(state.get("session"), str):
            warn("invalid session in snapshot — nothing restored")
            return EXIT_FAIL
        slug = state["session"]
        try:
            check_slug(slug)
        except SystemExit:
            warn("invalid session slug in snapshot — nothing restored")
            return EXIT_FAIL
        if (slug in seen or state.get("schema") != SCHEMA
                or state.get("status") not in OPEN_STATUSES
                or state.get("route") not in ("A", "B")
                or not isinstance(state.get("budgets", {}), dict)
                or not isinstance(state.get("models", {}), dict)
                or (state.get("blocked_on") is not None
                    and not isinstance(state["blocked_on"], dict))
                or any(not isinstance(state.get(key, []), list)
                       or any(not isinstance(item, dict) for item in state.get(key, []))
                       for key in ("pending", "blocked_history"))):
            warn("invalid session state in snapshot — nothing restored")
            return EXIT_FAIL
        seen.add(slug)
    restored = 0
    for state in states:
        slug = state.get("session")
        if not isinstance(slug, str) or (session_dir(vault, slug) / "session.json").exists():
            continue
        check_slug(slug)
        state["vault"] = str(vault)
        save_state(vault, slug, state)
        if not tokens_path(vault, slug).exists():
            write_json_atomic(tokens_path(vault, slug), {"schema": SCHEMA, "entries": []})
        append_event(vault, slug, "session_start",
                     "restored from the NotebookLM progress snapshot — earlier journal and token "
                     "entries are not available on this machine",
                     route=state.get("route"), phase=state.get("phase"), restored=True)
        say(f"restored {slug} ({state.get('status')}, phase {state.get('phase')})")
        restored += 1
    if not restored:
        say("no open session needed restoring — local state already present")
    return EXIT_OK


def cmd_pull(a: argparse.Namespace) -> int:
    vault = check_vault(a.vault)
    cfg = settings()
    meta = read_json(meta_path(vault), {})
    meta = meta if isinstance(meta, dict) else {}
    try:
        nb = a.notebook or meta.get("notebook_id")
        if not nb:
            project = a.project or project_name(vault, list_sessions(vault))
            title = str(cfg.get("title", "{project}")).replace("{project}", project)
            listing = json.loads(cli(["list", "--json"]))
            nb = next((n["id"] for n in list_of(listing, "notebooks")
                       if n.get("title") == title and isinstance(n.get("id"), str)), None)
            if not nb:
                warn(f"no notebook titled '{title}' — pass --project (the name used when the "
                     "sessions were initialised) or --notebook <id>")
                return EXIT_FAIL
        # A different machine can replace the source, and --notebook can select a
        # different notebook entirely. Never trust a cached source id on its own.
        sources = list_of(json.loads(cli(["source", "list", "--notebook", nb, "--json"])), "sources")
        sid = meta.get("state_source_id") if nb == meta.get("notebook_id") else None
        if not any(s.get("id") == sid for s in sources):
            matches = [s for s in sources if str(s.get("title", "")).startswith("duel-vault-state")
                       and isinstance(s.get("id"), str)]
            if not matches:
                warn("the notebook has no duel-vault state source")
                return EXIT_FAIL
            sid = matches[-1]["id"]
        text = fetch_source_text(nb, sid)
    except CliError as exc:
        warn(f"cannot read the snapshot from NotebookLM: {exc}")
        local = sorted(progress_dir(vault).glob("duel-vault-state-*.md"))
        if local:
            warn(f"the last local snapshot is still at {local[-1]}")
        return EXIT_FAIL
    out = a.out.expanduser() if a.out else progress_dir(vault) / "state.pulled.md"
    write_text_atomic(out, text)
    say(f"wrote {out}")
    if a.restore:
        return restore(vault, text)
    return EXIT_OK


def cmd_status(a: argparse.Namespace) -> int:
    vault = check_vault(a.vault)
    cfg = settings()
    meta = read_json(meta_path(vault), {})
    say(f"enabled: {cfg.get('enabled', True)}")
    if not isinstance(meta, dict) or not meta:
        say("never pushed")
        return EXIT_OK
    for key in ("title", "notebook_id", "state_source_id", "last_push", "pending_push",
                "last_error", "stale_sources"):
        if meta.get(key) not in (None, [], ""):
            say(f"{key}: {meta[key]}")
    say(f"session summaries: {len(meta.get('session_sources') or {})}")
    _project, text = render_state(vault)
    say("local progress differs from last push" if content_sha(text) != meta.get("state_sha")
        else "local progress matches last push")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="progress-sync", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("render", help="write the progress snapshot locally")
    p.add_argument("--vault", type=Path, required=True)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("push", help="render and publish to the project's notebook")
    p.add_argument("--vault", type=Path, required=True)
    p.add_argument("--session", help="session being checkpointed (logged; summary added when closed)")
    p.add_argument("--force", action="store_true", help="push even if unchanged")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--lock-wait", type=float, default=60.0, metavar="SECONDS",
                   help="how long to wait for a push already running (default 60), then exit 3")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("pull", help="fetch the snapshot back from NotebookLM")
    p.add_argument("--vault", type=Path, required=True)
    p.add_argument("--notebook", help="notebook id (default: from notebook.json, else by title)")
    p.add_argument("--project", help="project name used in the notebook title")
    p.add_argument("--out", type=Path)
    p.add_argument("--restore", action="store_true", help="rebuild missing open sessions' state")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("status", help="what was pushed, when, and whether anything is queued")
    p.add_argument("--vault", type=Path, required=True)
    p.set_defaults(func=cmd_status)
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
