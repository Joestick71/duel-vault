"""Shared primitives for a duel-vault session's working directory.

Every session — Route A and Route B — owns `<vault>/.duel-vault/<slug>/`:

    session.json   where the session is: status, phase, step, next action, blocks
    journal.jsonl  append-only record of everything that happened, one event per line
    tokens.json    every token figure, one entry per invocation or estimate

`session-log.py` writes the journal and the token ledger, `session-state.py` moves the
state, `progress-sync.py` publishes a snapshot of it. They share these primitives so the
three files can never disagree on format.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

from vault_common import CONFIG_PATH, load_config

WORKDIR = ".duel-vault"
PROGRESS_DIR = "progress"  # <vault>/.duel-vault/progress/ — not a session
SCHEMA = 1

STATUSES = ("active", "paused", "complete", "abandoned")
OPEN_STATUSES = ("active", "paused")
TOKEN_RESOURCES = ("codex", "claude_main", "claude_delegated")
INVOCATION_RESOURCES = ("codex", "claude_delegated", "notebooklm")
LIMIT_RESOURCES = ("codex", "claude", "notebooklm")
INVOCATION_STATUSES = ("started", "ok", "failed", "limit", "timeout")

# Every kind the journal may hold. `session-log.py event` accepts only the first group;
# the rest are written by the command that owns the transition.
FREE_EVENT_KINDS = ("activity", "decision", "checkpoint", "escalation", "warning", "error")
OWNED_EVENT_KINDS = (
    "session_start",
    "phase",
    "state",
    "change",
    "invocation",
    "tokens",
    "pause",
    "resume",
    "resource_available",
    "sync",
    "notification",
    "session_end",
)
EVENT_KINDS = FREE_EVENT_KINDS + OWNED_EVENT_KINDS

PHASE_ORDER = ("triage", "interview", "planning", "build", "review", "final", "closing")

DEFAULT_MODELS: dict[str, Any] = {
    "claude_mechanical": "haiku",
    "claude_executor": "sonnet",
    "codex_plan": None,
    "codex_build": None,
    "codex_build_effort": None,
}
DEFAULT_BUDGETS: dict[str, Any] = {
    "codex": None,
    "claude_main": None,
    "claude_delegated": None,
    "warn_ratio": 0.8,
}

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_WARN = 3  # budget warning / push queued
EXIT_OVER = 4  # budget exceeded
EXIT_BLOCKED = 5  # resume refused: the resource is still blocked
EXIT_TIMEOUT = 6  # wait gave up

SLUG_RE = re.compile(r"^[^/\\\0]+$")


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


UNIT_SECONDS = {
    "d": 86400,
    "h": 3600,
    "m": 60,
    "s": 1,
}
DURATION_RE = re.compile(
    r"(?P<n>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>days?|giorn[oi]|d|hours?|hrs?|or[ae]|h|minutes?|mins?|minut[oi]|m|"
    r"seconds?|secs?|second[oi]|s)(?![a-z])",
    re.I,
)
CLOCK_RE = re.compile(
    r"(?<![\d:])(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?(?![\d:])", re.I
)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
DATE_RE = re.compile(
    r"\b(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(?P<year>\d{4}))?",
    re.I,
)
ZONE_RE = re.compile(r"\((?P<zone>[A-Za-z_]+/[A-Za-z_+\-/]+)\)")


def _unit(raw: str) -> str:
    raw = raw.lower()
    if raw.startswith(("d", "g")):
        return "d"
    if raw.startswith(("h", "o")):
        return "h"
    if raw.startswith("m"):
        return "m"
    return "s"


def parse_when(text: str, ref: datetime | None = None) -> datetime | None:
    """A point in time from what limit messages and humans actually write.

    Accepts ISO timestamps, durations ("in 2 days 4 hours", "2h30m", "90 minutes") and
    clock times ("3:47 PM", "5pm", "17:30", "Nov 5th, 2026 9:12 AM", "5pm (Europe/Rome)").
    A clock time with no date means its next occurrence. Returns None when nothing parses.
    """
    ref = ref or now()
    text = (text or "").strip()
    if not text:
        return None
    exact = parse_iso(text)
    if exact:
        return exact

    zone = None
    zm = ZONE_RE.search(text)
    if zm:
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(zm.group("zone"))
        except Exception:
            zone = None
        text = ZONE_RE.sub(" ", text)

    date_m = DATE_RE.search(text)
    rest = DATE_RE.sub(" ", text) if date_m else text
    clock = None
    for cm in CLOCK_RE.finditer(rest):
        # A bare number is a clock time only with ":mm" or am/pm attached.
        if cm.group("m") is None and cm.group("ampm") is None:
            continue
        clock = cm
        break

    if clock is None and date_m is None:
        seconds = 0.0
        found = False
        for dm in DURATION_RE.finditer(text):
            seconds += float(dm.group("n")) * UNIT_SECONDS[_unit(dm.group("unit"))]
            found = True
        return ref + timedelta(seconds=seconds) if found else None

    base = ref.astimezone(zone) if zone else ref
    hour, minute = 0, 0
    if clock is not None:
        hour = int(clock.group("h"))
        minute = int(clock.group("m") or 0)
        ampm = (clock.group("ampm") or "").lower().replace(".", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
    try:
        if date_m is not None:
            month = MONTHS.index(date_m.group("mon").lower()[:3]) + 1
            day = int(date_m.group("day"))
            year = int(date_m.group("year")) if date_m.group("year") else base.year
            when = base.replace(
                year=year, month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0
            )
            if not date_m.group("year") and when < base - timedelta(days=1):
                when = when.replace(year=year + 1)
        else:
            when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when <= base:
                when += timedelta(days=1)
    except ValueError:
        return None
    return when.astimezone(ref.tzinfo)


# --------------------------------------------------------------------------
# limit messages
# --------------------------------------------------------------------------

# Patterns are deliberately specific. Codex's stderr also carries the output of the
# commands it ran, so a generic word ("overloaded", "429", "limit reached") can come from
# the project being built rather than from the service. `classify_limit` additionally
# only reads the tail of the text, where the fatal error is printed.
USAGE_RE = re.compile(
    r"usage limit|(?:usage|5-hour|weekly|daily|session|plan|message) limit reached|"
    r"hit your (?:usage )?limit|insufficient_quota|quota exceeded|"
    r"exceeded your (?:current )?(?:quota|plan|usage limit)|out of credits|"
    r"limite di utilizzo",
    re.I,
)
RATE_RE = re.compile(
    r"rate[ _-]?limit(?:ed|_error|_exceeded| exceeded| reached)|too many requests|"
    r"(?:last )?status:? 429\b|\bhttp 429\b|overloaded_error|server is overloaded",
    re.I,
)
LIMIT_TAIL_LINES = 40
RESET_RE = re.compile(
    r"(?:try again|retry|resets?|reset|available again|riprova|disponibile)\s*"
    r"(?:at|in|after|on|alle|tra|fra)?\s*(?P<when>[^\n;]{1,80})",
    re.I,
)


def classify_limit(text: str, ref: datetime | None = None, rate_default: bool = True) -> dict | None:
    """Recognise a usage/rate limit in an error message and estimate when it lifts.

    Returns None when the text is not a limit at all — an ordinary error, to be handled
    by the ordinary retry rule. `resume_after` is None when the message names no time.
    """
    ref = ref or now()
    text = "\n".join((text or "").strip().splitlines()[-LIMIT_TAIL_LINES:])
    if USAGE_RE.search(text):
        kind = "usage"
    elif RATE_RE.search(text):
        kind = "rate"
    else:
        return None
    resume_after = None
    for m in RESET_RE.finditer(text):
        # Stop at a sentence end that is not part of a clock time like "3.47".
        candidate = re.split(r"\.(?:\s|$)", m.group("when"))[0]
        resume_after = parse_when(candidate, ref)
        if resume_after:
            break
    if resume_after is None and kind == "rate" and rate_default:
        resume_after = ref + timedelta(minutes=2)
    excerpt = re.sub(r"\s+", " ", text).strip()[-300:]
    return {
        "kind": kind,
        "resume_after": iso(resume_after) if resume_after else None,
        "evidence": excerpt,
    }


# --------------------------------------------------------------------------
# files and locking
# --------------------------------------------------------------------------


def check_vault(raw: Path) -> Path:
    vault = raw.expanduser().resolve()
    if not vault.is_dir():
        raise SystemExit(f"not a directory: {vault}")
    return vault


def check_slug(slug: str) -> str:
    if not slug or slug.startswith(".") or slug == PROGRESS_DIR or not SLUG_RE.match(slug):
        raise SystemExit(f"invalid session slug: {slug!r}")
    return slug


def session_dir(vault: Path, slug: str) -> Path:
    return vault / WORKDIR / slug


def progress_dir(vault: Path) -> Path:
    return vault / WORKDIR / PROGRESS_DIR


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def _target_mode(path: Path) -> int:
    """The mode the file would have had if written normally: its current mode when it
    exists, else 0666 minus the umask. `mkstemp` alone would leave every note 0600."""
    try:
        return path.stat().st_mode & 0o777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = _target_mode(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_json_atomic(path: Path, data: Any) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


_LOCK_DEPTH: dict[str, int] = {}


def session_lock(sdir: Path) -> contextlib.AbstractContextManager[None]:
    """Exclusive lock on one session's working dir, re-entrant within this process.

    Background agents (NotebookLM waits, verifiers) may log while the main thread does;
    the lock keeps journal sequence numbers unique and state writes whole.
    """
    return file_lock(sdir / ".lock")


@contextlib.contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Exclusive inter-process lock on `lock_path`, re-entrant within this process."""
    key = str(lock_path)
    if _LOCK_DEPTH.get(key):
        _LOCK_DEPTH[key] += 1
        try:
            yield
        finally:
            _LOCK_DEPTH[key] -= 1
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        _LOCK_DEPTH[key] = 1
        try:
            yield
        finally:
            _LOCK_DEPTH[key] = 0
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def digest(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def display_path(vault: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(vault).as_posix()
    except ValueError:
        return str(path.resolve())


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------


def journal_path(vault: Path, slug: str) -> Path:
    return session_dir(vault, slug) / "journal.jsonl"


def read_journal(path: Path) -> list[tuple[int, dict | None, str]]:
    """(line number, event or None, error) for every non-empty line."""
    out: list[tuple[int, dict | None, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            out.append((lineno, None, f"not JSON: {exc}"))
            continue
        if not isinstance(event, dict):
            out.append((lineno, None, "not a JSON object"))
            continue
        out.append((lineno, event, ""))
    return out


def load_events(vault: Path, slug: str) -> list[dict]:
    return [e for _n, e, _err in read_journal(journal_path(vault, slug)) if e is not None]


def append_event(vault: Path, slug: str, kind: str, summary: str, **fields: Any) -> dict:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    sdir = session_dir(vault, slug)
    path = journal_path(vault, slug)
    with session_lock(sdir):
        events = load_events(vault, slug)
        seq = max((e.get("seq", 0) for e in events if isinstance(e.get("seq"), int)), default=0) + 1
        event: dict[str, Any] = {"seq": seq, "ts": iso(now()), "kind": kind, "summary": summary}
        event.update({k: v for k, v in fields.items() if v not in (None, "", [], {})})
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def state_path(vault: Path, slug: str) -> Path:
    return session_dir(vault, slug) / "session.json"


def resolved_models(cfg: dict | None = None) -> dict:
    cfg = load_config() if cfg is None else cfg
    models = dict(DEFAULT_MODELS)
    extra = cfg.get("models")
    if isinstance(extra, dict):
        models.update({k: v for k, v in extra.items() if k in DEFAULT_MODELS})
    return models


def resolved_budgets(cfg: dict | None = None, project: str | None = None) -> dict:
    """Global `budgets`, overridden by `budgets_by_project[<project>]` when present."""
    cfg = load_config() if cfg is None else cfg
    budgets = dict(DEFAULT_BUDGETS)
    layers = [cfg.get("budgets")]
    by_project = cfg.get("budgets_by_project")
    if project and isinstance(by_project, dict):
        layers.append(by_project.get(project))
    for layer in layers:
        if isinstance(layer, dict):
            budgets.update({k: v for k, v in layer.items() if k in DEFAULT_BUDGETS})
    return budgets


def project_has_budgets(project: str, cfg: dict | None = None) -> bool:
    """True once the user has answered the budget question for this project — even with
    "track only" (an entry of nulls), so it is never asked twice."""
    cfg = load_config() if cfg is None else cfg
    by_project = cfg.get("budgets_by_project")
    return isinstance(by_project, dict) and isinstance(by_project.get(project), dict)


def save_config(cfg: dict) -> None:
    write_json_atomic(CONFIG_PATH, cfg)


def new_state(vault: Path, slug: str, route: str, project: str, title: str, task: str) -> dict:
    stamp = iso(now())
    return {
        "schema": SCHEMA,
        "project": project,
        "vault": str(vault),
        "session": slug,
        "route": route,
        "title": title,
        "task": task,
        "status": "active",
        "phase": "triage",
        "step": None,
        "next_action": "",
        "blocked_on": None,
        "blocked_history": [],
        "pending": [],
        "models": resolved_models(),
        "budgets": resolved_budgets(project=project),
        "created": stamp,
        "updated": stamp,
    }


def load_state(vault: Path, slug: str) -> dict | None:
    data = read_json(state_path(vault, slug), None)
    if not isinstance(data, dict):
        return None
    # session.json files written by hand before the journal existed have no `schema`.
    # They must never pass for open sessions — not even when they claim `active` or
    # `paused` — or Phase 0 would offer every old session for resumption.
    # `session-log.py init` upgrades one explicitly.
    if "schema" not in data:
        data["legacy"] = True
        if "status" in data:
            data["legacy_status"] = data["status"]
        data["status"] = "legacy"
        if not data.get("updated"):
            # Without it the defaults below would stamp "now", sorting it first.
            with contextlib.suppress(OSError):
                mtime = state_path(vault, slug).stat().st_mtime
                data["updated"] = iso(datetime.fromtimestamp(mtime).astimezone())
    defaults = new_state(vault, slug, str(data.get("route", "B")), vault.name, slug, "")
    for key, value in defaults.items():
        data.setdefault(key, value)
    if not isinstance(data.get("pending"), list):
        data["pending"] = []
    if not isinstance(data.get("blocked_history"), list):
        data["blocked_history"] = []
    return data


def require_state(vault: Path, slug: str) -> dict:
    state = load_state(vault, slug)
    if state is None:
        raise SystemExit(
            f"session {slug!r} is not initialised in {vault} — run "
            f"`session-log.py init --vault <vault> --session {slug} --route A|B` first"
        )
    if state.get("legacy"):
        raise SystemExit(
            f"session {slug!r} has a pre-journal session.json — upgrade it first with "
            f"`session-log.py init --vault <vault> --session {slug} --route A|B`"
        )
    return state


def save_state(vault: Path, slug: str, state: dict) -> None:
    state["updated"] = iso(now())
    claude_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if claude_id and state.get("status") in OPEN_STATUSES:
        state["claude_session_id"] = claude_id
    write_json_atomic(state_path(vault, slug), state)
    if claude_id and state.get("status") in OPEN_STATUSES:
        register_claude_session(claude_id, vault, slug)


# --------------------------------------------------------------------------
# Claude session registry — lets a Claude Code hook find the duel-vault session
# --------------------------------------------------------------------------

REGISTRY_PATH = CONFIG_PATH.parent / "duel-vault.sessions.json"
REGISTRY_MAX_AGE = timedelta(days=30)


def register_claude_session(claude_id: str, vault: Path, slug: str) -> None:
    """Map a Claude Code session id to the duel-vault session it is driving.

    Every script run from Claude Code's Bash tool sees `CLAUDE_CODE_SESSION_ID`, and the
    StopFailure hook receives the same id as `session_id`: the mapping is exact, never a
    guess based on recency or working directory.

    Several Claude sessions can write it at once, so the read-modify-write runs under
    its own lock — always taken last, after any session lock, so the two never deadlock.
    Readers need no lock: the file is replaced atomically.
    """
    try:
        with file_lock(REGISTRY_PATH.with_name(REGISTRY_PATH.name + ".lock")):
            data = read_json(REGISTRY_PATH, {})
            data = data if isinstance(data, dict) else {}
            cutoff = now() - REGISTRY_MAX_AGE
            data = {
                k: v for k, v in data.items()
                if isinstance(v, dict) and (parse_iso(v.get("updated")) or cutoff) > cutoff
            }
            data[claude_id] = {"vault": str(vault), "session": slug, "updated": iso(now())}
            write_json_atomic(REGISTRY_PATH, data)
    except OSError:
        pass  # the registry is a convenience for the hook; never fail a state write on it


def lookup_claude_session(claude_id: str) -> tuple[Path, str] | None:
    entry = read_json(REGISTRY_PATH, {}).get(claude_id) if claude_id else None
    if not isinstance(entry, dict) or not entry.get("vault") or not entry.get("session"):
        return None
    return Path(entry["vault"]), str(entry["session"])


# --------------------------------------------------------------------------
# notifications and detached processes
# --------------------------------------------------------------------------

NOTIFY_LOG = CONFIG_PATH.parent / "duel-vault-notifications.log"
PROBE_ENV = "DUEL_VAULT_PROBE"  # set in every probe we spawn: our own hooks ignore those runs


def fill_template(template: str, values: dict) -> str:
    """`str.format_map` that leaves unknown `{placeholders}` as they are."""
    class Safe(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    return template.format_map(Safe(values))


def notify(title: str, message: str, cfg: dict | None = None) -> str:
    """Send a desktop notification on whatever OS this is; return the channel used.

    `notify.command` in the config replaces the desktop channel with any command — a push
    service, a mail, a chat webhook — with `{title}` and `{message}` substituted into its
    arguments (never through a shell, so the text cannot inject anything). Every
    notification is also appended to ~/.claude/duel-vault-notifications.log.
    """
    cfg = load_config() if cfg is None else cfg
    settings = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
    channel = "log only"
    try:
        command = settings.get("command")
        if command:
            parts = shlex.split(command) if isinstance(command, str) else [str(c) for c in command]
            argv = [fill_template(part, {"title": title, "message": message}) for part in parts]
            subprocess.run(argv, timeout=60, capture_output=True, check=True)
            channel = "command"
        elif sys.platform == "darwin" and shutil.which("osascript"):
            script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
            subprocess.run(["osascript", "-e", script], timeout=30, capture_output=True, check=True)
            channel = "macOS"
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], timeout=30, capture_output=True, check=True)
            channel = "notify-send"
        elif sys.platform == "win32":  # pragma: no cover - Windows
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
                f"$n.ShowBalloonTip(15000, {json.dumps(title)}, {json.dumps(message)}, 'Info'); "
                "Start-Sleep -Seconds 16; $n.Dispose()"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=60,
                           capture_output=True, check=True)
            channel = "Windows"
    except (OSError, subprocess.SubprocessError) as exc:
        channel = f"failed ({exc.__class__.__name__}) — log only"
    try:
        NOTIFY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(NOTIFY_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{iso(now())}\t{channel}\t{title}\t{message}\n")
    except OSError:
        pass
    return channel


def spawn_detached(argv: list[str], env: dict | None = None) -> int:
    """Start a process that outlives this one and the terminal; return its pid."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
    }
    if sys.platform == "win32":  # pragma: no cover - Windows
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs).pid


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - no cheap check without extra packages
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sleep_until(when: datetime, step: float = 30.0) -> None:
    """Sleep until a wall-clock time, in short steps.

    A single long sleep follows the monotonic clock, which stops while a laptop sleeps:
    a 5-hour wait started before closing the lid would end hours late. Re-checking the
    wall clock every few seconds keeps the deadline real.
    """
    while True:
        remaining = (when - now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(step, remaining))


def list_sessions(vault: Path) -> list[dict]:
    root = vault / WORKDIR
    out: list[dict] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name == PROGRESS_DIR:
            continue
        state = load_state(vault, entry.name) if (entry / "session.json").is_file() else None
        if state is not None:
            out.append(state)
    epoch = datetime.fromtimestamp(0).astimezone()
    out.sort(key=lambda s: parse_iso(s.get("updated")) or epoch, reverse=True)
    return out


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------


def tokens_path(vault: Path, slug: str) -> Path:
    return session_dir(vault, slug) / "tokens.json"


def load_tokens(vault: Path, slug: str) -> dict:
    data = read_json(tokens_path(vault, slug), None)
    if not isinstance(data, dict):
        return {"schema": SCHEMA, "entries": []}
    if isinstance(data.get("entries"), list):
        return data
    # Legacy layout: {"<phase>": [{invocation, input, output, total}], "claude_delegated": [...]}
    entries: list[dict] = []
    for phase, items in data.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            resource = "claude_delegated" if phase == "claude_delegated" else "codex"
            entries.append(
                {
                    "phase": "unknown" if phase == "claude_delegated" else phase,
                    "resource": resource,
                    "model": item.get("model"),
                    "invocation": item.get("invocation") or item.get("agent"),
                    "input": item.get("input"),
                    "output": item.get("output"),
                    "total": item.get("total", "unavailable"),
                    "legacy": True,
                }
            )
    return {"schema": SCHEMA, "entries": entries}


def add_token_entry(vault: Path, slug: str, entry: dict) -> dict:
    sdir = session_dir(vault, slug)
    with session_lock(sdir):
        data = load_tokens(vault, slug)
        entry = {"ts": iso(now()), **{k: v for k, v in entry.items() if v is not None}}
        data["entries"].append(entry)
        write_json_atomic(tokens_path(vault, slug), data)
    return data


def parse_total(raw: str | None) -> int | str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "unavailable":
        return text
    if re.fullmatch(r"\d+", text):
        return int(text)
    raise SystemExit(f'token figure must be a non-negative integer or "unavailable": {raw!r}')


def token_totals(entries: list[dict]) -> dict[str, dict[str, int]]:
    totals = {r: {"total": 0, "unavailable": 0, "entries": 0} for r in TOKEN_RESOURCES}
    for entry in entries:
        resource = entry.get("resource")
        if resource not in totals:
            continue
        totals[resource]["entries"] += 1
        value = entry.get("total")
        if isinstance(value, int) and not isinstance(value, bool):
            totals[resource]["total"] += value
        else:
            totals[resource]["unavailable"] += 1
    return totals


LABELS = {"codex": "codex", "claude_main": "claude main (est.)", "claude_delegated": "claude delegated"}


def evaluate_budgets(entries: list[dict], budgets: dict) -> tuple[str, str]:
    """('ok'|'warn'|'over', one status line) for the session's token ledger."""
    totals = token_totals(entries)
    try:
        warn_ratio = float(budgets.get("warn_ratio") or DEFAULT_BUDGETS["warn_ratio"])
    except (TypeError, ValueError):
        warn_ratio = DEFAULT_BUDGETS["warn_ratio"]
    level = "ok"
    bits: list[str] = []
    for resource in TOKEN_RESOURCES:
        used = totals[resource]["total"]
        cap = budgets.get(resource)
        text = f"{LABELS[resource]} {used:,}"
        if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0:
            cap = int(cap)
            ratio = used / cap
            text += f"/{cap:,} ({ratio:.0%})"
            if ratio >= 1:
                level = "over"
                text += " OVER"
            elif ratio >= warn_ratio:
                level = "warn" if level == "ok" else level
                text += " WARN"
        if totals[resource]["unavailable"]:
            text += f" +{totals[resource]['unavailable']} unavailable"
        bits.append(text)
    return level, "tokens — " + " · ".join(bits) + f" → {level}"


def budget_exit(level: str) -> int:
    return {"ok": EXIT_OK, "warn": EXIT_WARN, "over": EXIT_OVER}[level]


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------


RESERVED_EVENT_FIELDS = frozenset({"seq", "ts", "kind", "summary", "phase", "actor", "step", "note"})


def parse_kv(pairs: list[str], reserved: frozenset[str] = RESERVED_EVENT_FIELDS) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"expected KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key or key in reserved:
            raise SystemExit(
                f"field name not allowed: {key!r} (reserved: {', '.join(sorted(reserved))} — "
                "use the dedicated option)"
            )
        value = value.strip()
        out[key] = int(value) if re.fullmatch(r"-?\d+", value) else value
    return out


def phase_rank(phase: str) -> tuple[int, str]:
    return (PHASE_ORDER.index(phase), phase) if phase in PHASE_ORDER else (len(PHASE_ORDER), phase)
