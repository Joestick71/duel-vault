#!/usr/bin/env python3
"""vault-lint — deterministic checks for duel-vault session notes.

Everything here is mechanical: frontmatter schema, tag conventions, wikilink
resolution, filename conventions, session-folder completeness, requirement
coverage. No model involved, no hallucination risk, no tokens spent.

Run it as a gate *before* the Phase 4 review, and as a self-check at the end of
every build step. Exit code 0 = no errors, 1 = errors, 2 = usage/IO problem.

    python3 scripts/vault-lint.py --vault ~/vaults/Direful
    python3 scripts/vault-lint.py --session ~/vaults/Direful/Sessions/my-session
    python3 scripts/vault-lint.py --files note-a.md note-b.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Any, Callable, Iterable

from vault_common import (
    MOC_RE,
    REVIEW_RE,
    ROUND_RE,
    ROUTE_A_RE,
    SESSION_NOTE_RE,
    SKIP_DIRS,
    fm_line,
    live_lines,
    parse_frontmatter,
)

ERROR = "ERROR"
WARN = "WARN"

CONFIG_PATH = Path.home() / ".claude" / "duel-vault.config.json"


# --------------------------------------------------------------------------
# issues
# --------------------------------------------------------------------------


@dataclass
class Issue:
    level: str
    code: str
    path: str
    line: int | None
    message: str

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "path": self.path,
            "line": self.line,
            "message": self.message,
        }


class Report:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def add(self, level: str, code: str, path: Path | str, line: int | None, message: str) -> None:
        self.issues.append(Issue(level, code, str(path), line, message))

    def error(self, code: str, path: Path | str, line: int | None, message: str) -> None:
        self.add(ERROR, code, path, line, message)

    def warn(self, code: str, path: Path | str, line: int | None, message: str) -> None:
        self.add(WARN, code, path, line, message)

    @property
    def n_errors(self) -> int:
        return sum(1 for i in self.issues if i.level == ERROR)

    @property
    def n_warnings(self) -> int:
        return sum(1 for i in self.issues if i.level == WARN)


# --------------------------------------------------------------------------
# field validators
# --------------------------------------------------------------------------

Validator = Callable[[Any], str | None]


def v_str(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "expected a non-empty string"
    return None


def v_date(value: Any) -> str | None:
    if isinstance(value, _date):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return "expected a YYYY-MM-DD date"
    try:
        y, m, d = (int(p) for p in value.strip().split("-"))
        _date(y, m, d)
    except ValueError:
        return f"{value!r} is not a real calendar date"
    return None


def v_bool(value: Any) -> str | None:
    return None if isinstance(value, bool) else "expected true or false"


def v_int(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return "expected a non-negative integer"
    return None


def v_tokens(value: Any) -> str | None:
    if isinstance(value, str) and value.strip() == "unavailable":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 'expected a non-negative integer or the string "unavailable"'
    return None


def v_tags(value: Any) -> str | None:
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        return "expected a list of tag strings"
    return None


def v_list(value: Any) -> str | None:
    return None if isinstance(value, list) else "expected a list"


def v_plan_version(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return None
    if isinstance(value, str) and value.strip():
        return None
    return "expected an integer version or an arbitration string"


def enum_of(*allowed: str) -> Validator:
    def check(value: Any) -> str | None:
        text = str(value).strip() if not isinstance(value, bool) else str(value).lower()
        if text not in allowed:
            return f"expected one of {', '.join(allowed)} (found {value!r})"
        return None

    return check


# --------------------------------------------------------------------------
# note kinds
# --------------------------------------------------------------------------

# fields: name -> (validator, required?)
NOTE_SPECS: dict[str, dict[str, Any]] = {
    "moc": {
        "label": "MOC",
        "fields": {
            "project": (v_str, True),
            "session": (v_str, True),
            "type": (enum_of("duel-vault-moc"), True),
            "date": (v_date, True),
            "status": (enum_of("in-progress", "complete", "escalated"), True),
            "tags": (v_tags, True),
            "model": (v_str, False),
            "notebooklm": (v_bool, False),
        },
        "tag_suffix": "moc",
        "sections": ["Phases"],
    },
    "requirements": {
        "label": "requirements",
        "fields": {
            "project": (v_str, True),
            "session": (v_str, True),
            "phase": (enum_of("interview"), True),
            "date": (v_date, True),
            "approved_by_user": (v_bool, True),
            "tokens_codex": (v_tokens, True),
            "tokens_claude_est": (v_tokens, True),
            "tags": (v_tags, True),
        },
        "tag_suffix": "interview",
        "sections": ["Goal", "Requirements", "Token usage"],
    },
    "plan": {
        "label": "plan",
        "fields": {
            "project": (v_str, True),
            "session": (v_str, True),
            "phase": (enum_of("planning"), True),
            "date": (v_date, True),
            "consensus_rounds": (v_int, True),
            "plan_version": (v_plan_version, True),
            "approved_by_user": (v_bool, True),
            "tokens_codex": (v_tokens, True),
            "tokens_claude_est": (v_tokens, True),
            "tags": (v_tags, True),
        },
        "tag_suffix": "plan",
        "sections": ["Steps", "Requirements coverage", "Token usage"],
    },
    "build": {
        "label": "build log",
        "fields": {
            "project": (v_str, True),
            "session": (v_str, True),
            "phase": (enum_of("build"), True),
            "date": (v_date, True),
            "tokens_codex": (v_tokens, True),
            "tokens_claude_est": (v_tokens, True),
            "tags": (v_tags, True),
        },
        "tag_suffix": "build",
        "sections": ["Token usage"],
    },
    "review": {
        "label": "review cycle",
        "fields": {
            "project": (v_str, True),
            "session": (v_str, True),
            "phase": (enum_of("review"), True),
            "cycle": (v_int, True),
            "date": (v_date, True),
            "tokens_codex": (v_tokens, True),
            "tokens_claude_est": (v_tokens, True),
            "tags": (v_tags, True),
        },
        "tag_suffix": "review",
        "sections": ["Findings", "Verdict", "Token usage"],
    },
    "final": {
        "label": "final report",
        "fields": {
            "project": (v_str, True),
            "session": (v_str, True),
            "phase": (enum_of("final"), True),
            "date": (v_date, True),
            "fix_cycles_used": (v_int, True),
            "tokens_codex_total": (v_tokens, True),
            "tokens_claude_est_total": (v_tokens, True),
            "tags": (v_tags, True),
        },
        "tag_suffix": "final",
        "sections": ["Requirements outcome", "Token consumption", "Trail"],
    },
    "sources": {
        "label": "NotebookLM sources",
        "fields": {
            "project": (v_str, True),
            "session": (v_str, True),
            "phase": (enum_of("sources"), True),
            "date": (v_date, True),
            "notebook_id": (v_str, True),
            "sources": (v_int, True),
            "artifacts": (v_int, True),
            "tags": (v_tags, True),
        },
        "tag_suffix": "notebooklm",
        "sections": ["Sources", "Artifacts", "Prompts"],
    },
    "route_a": {
        "label": "Route A session log",
        "fields": {
            "project": (v_str, True),
            "session": (v_str, True),
            "route": (enum_of("A"), True),
            "date": (v_date, True),
            "skills_used": (v_list, True),
            "notebooklm": (v_bool, False),
            "tags": (v_tags, True),
        },
        "tag_suffix": "session",
        "sections": [],
    },
    "round": {
        "label": "consensus round",
        "protocol": True,
        "fields": {
            "protocol": (enum_of("duet-consensus/v1"), True),
            "round": (v_int, True),
            "author": (enum_of("claude", "codex"), True),
            "verdict": (enum_of("PROPOSE", "REVISE", "APPROVE"), True),
            "plan_version": (v_plan_version, True),
        },
        "sections": [],
    },
    "disagreement": {
        "label": "plan disagreement",
        "fields": {},
        "sections": [],
    },
}


def classify(path: Path, session_dir: Path | None) -> str | None:
    stem = path.stem
    if path.parent.name == "02a-consensus-rounds" or ROUND_RE.match(stem):
        return "round" if ROUND_RE.match(stem) else None
    if MOC_RE.match(stem):
        return "moc"
    if stem == "01-requirements":
        return "requirements"
    if stem == "02-plan":
        return "plan"
    if stem == "03-build-log":
        return "build"
    if REVIEW_RE.match(stem):
        return "review"
    if stem == "05-final-report":
        return "final"
    if stem == "06-sources":
        return "sources"
    if stem == "plan-disagreement":
        return "disagreement"
    if session_dir is None and ROUTE_A_RE.match(stem):
        return "route_a"
    return None


# --------------------------------------------------------------------------
# body scanning
# --------------------------------------------------------------------------

WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\n|]+)(?:\|([^\]\n]*))?\]\]")
MDLINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)")
CALLOUT_RE = re.compile(r"^\s*>\s*\[!(?P<body>[^\]\n]*)\]")
PLACEHOLDER_RE = re.compile(r"<[A-Za-z][A-Za-z0-9 _/|.,+#-]{0,40}>")
HEADING2_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
REQ_DECL_RE = re.compile(r"^#{2,4}\s*(?:R(?P<id>\d+))\b")
REQ_REF_RE = re.compile(r"\bR(\d+)\b")


# --------------------------------------------------------------------------
# vault index
# --------------------------------------------------------------------------


class VaultIndex:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.by_relpath: dict[str, str] = {}  # lowercased relpath w/o .md -> relpath
        self.by_basename: dict[str, list[str]] = {}
        self.assets: dict[str, list[str]] = {}
        self._build()

    def _build(self) -> None:
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(self.root).parts[:-1]):
                continue
            rel = path.relative_to(self.root).as_posix()
            if path.suffix.lower() == ".md":
                key = rel[: -len(".md")].lower()
                self.by_relpath.setdefault(key, rel)
                self.by_basename.setdefault(path.stem.lower(), []).append(rel)
            else:
                self.assets.setdefault(path.name.lower(), []).append(rel)

    def resolve(self, target: str, note_rel: str) -> tuple[str, list[str]]:
        """Return (status, candidates) where status is ok | ambiguous | broken."""
        target = target.strip().strip("/")
        if not target:
            return "broken", []
        if Path(target).suffix and Path(target).suffix.lower() != ".md":
            hits = self.assets.get(Path(target).name.lower(), [])
            if len(hits) == 1:
                return "ok", hits
            return ("ambiguous", hits) if hits else ("broken", [])
        if target.lower().endswith(".md"):
            target = target[: -len(".md")]
        note_dir = Path(note_rel).parent.as_posix()
        candidates = [target]
        if note_dir not in ("", "."):
            candidates.append(f"{note_dir}/{target}")
        for cand in candidates:
            key = Path(cand).as_posix().lower()
            if key in self.by_relpath:
                return "ok", [self.by_relpath[key]]
        hits = self.by_basename.get(Path(target).name.lower(), [])
        if len(hits) == 1:
            return "ok", hits
        if hits:
            return "ambiguous", hits
        return "broken", []


def find_vault_root(start: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    for candidate in [current, *current.parents]:
        if (candidate / "CLAUDE.md").is_file():
            return candidate
        if candidate.name == "Sessions" and (candidate.parent / "CLAUDE.md").is_file():
            return candidate.parent
    return None


# --------------------------------------------------------------------------
# per-note checks
# --------------------------------------------------------------------------


def check_note(
    path: Path,
    kind: str,
    report: Report,
    index: VaultIndex | None,
    *,
    session_dir: Path | None,
    session_in_progress: bool,
    extra_fields: Iterable[str],
    check_sections: bool,
) -> dict:
    """Lint a single note. Returns the parsed frontmatter (possibly empty)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report.error("VD000", path, None, f"cannot read file: {exc}")
        return {}

    spec = NOTE_SPECS[kind]
    data, fm_error, body_start = parse_frontmatter(text)

    if data is None:
        if fm_error == "missing":
            report.error("VD001", path, 1, f"{spec['label']} note has no YAML frontmatter")
        else:
            report.error("VD002", path, 1, f"frontmatter could not be parsed: {fm_error}")
        data = {}
    else:
        if fm_error:
            report.error(
                "VD002",
                path,
                1,
                f"frontmatter is {fm_error} — Obsidian will not read these properties",
            )
        _check_fields(path, kind, spec, data, text, report, extra_fields)

    _check_body(
        path,
        kind,
        spec,
        text,
        body_start,
        report,
        index,
        session_in_progress=session_in_progress,
        check_sections=check_sections,
    )
    _check_filename_agreement(path, kind, data, text, report, session_dir)
    return data


def _check_fields(
    path: Path,
    kind: str,
    spec: dict,
    data: dict,
    text: str,
    report: Report,
    extra_fields: Iterable[str],
) -> None:
    fields: dict[str, tuple[Validator, bool]] = spec["fields"]
    for name, (validator, required) in fields.items():
        if name not in data:
            if required:
                report.error("VD003", path, 1, f"missing required frontmatter field `{name}`")
            continue
        problem = validator(data[name])
        if problem:
            report.error("VD004", path, fm_line(text, name), f"field `{name}`: {problem}")

    # Round files carry the consensus protocol header verbatim — a vault's
    # extra note fields do not apply to them.
    for name in (extra_fields if not spec.get("protocol") else []):
        if name not in data:
            report.error(
                "VD003", path, 1, f"missing frontmatter field `{name}` required by this vault"
            )

    for name, value in data.items():
        if isinstance(value, str) and PLACEHOLDER_RE.search(value):
            report.error(
                "VD005",
                path,
                fm_line(text, name),
                f"field `{name}` still contains an unfilled template placeholder: {value!r}",
            )

    if spec.get("protocol"):
        return

    tags = data.get("tags")
    if isinstance(tags, list):
        line = fm_line(text, "tags")
        if "duel-vault" not in tags:
            report.error("VD006", path, line, "tags must include `duel-vault`")
        suffix = spec.get("tag_suffix")
        if suffix and f"duel-vault/{suffix}" not in tags:
            report.error("VD006", path, line, f"tags must include `duel-vault/{suffix}`")


def _check_body(
    path: Path,
    kind: str,
    spec: dict,
    text: str,
    body_start: int,
    report: Report,
    index: VaultIndex | None,
    *,
    session_in_progress: bool,
    check_sections: bool,
) -> None:
    lines = live_lines(text, from_line=body_start)
    note_rel = ""
    if index is not None:
        try:
            note_rel = path.resolve().relative_to(index.root.resolve()).as_posix()
        except ValueError:
            note_rel = path.name

    headings: list[str] = []
    for lineno, line in lines:
        heading = HEADING2_RE.match(line)
        if heading:
            headings.append(heading.group("title").strip())

        for embed, target, _alias in WIKILINK_RE.findall(line):
            base = target.split("#", 1)[0].split("^", 1)[0].strip()
            if not base:
                continue  # pure in-note anchor
            if index is None:
                continue
            status, hits = index.resolve(base, note_rel)
            if status == "ok":
                continue
            if status == "ambiguous":
                report.warn(
                    "VD011",
                    path,
                    lineno,
                    f"wikilink [[{target}]] is ambiguous: {', '.join(hits[:4])}",
                )
                continue
            pending = session_in_progress and SESSION_NOTE_RE.match(Path(base).name) is not None
            level = report.warn if pending else report.error
            kind_word = "embed" if embed else "wikilink"
            note = " (session still in progress)" if pending else ""
            level("VD010", path, lineno, f"broken {kind_word} [[{target}]]{note}")

        for target in MDLINK_RE.findall(line):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                continue
            if target.lower().endswith(".md"):
                report.error(
                    "VD012",
                    path,
                    lineno,
                    f"relative markdown link to `{target}` — use a wikilink instead",
                )

        callout = CALLOUT_RE.match(line)
        if callout:
            body = callout.group("body")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", body.strip()):
                report.warn("VD013", path, lineno, f"malformed callout header `[!{body}]`")

        for match in PLACEHOLDER_RE.finditer(line):
            token = match.group(0)
            if re.fullmatch(r"<\d+>", token):
                continue
            report.warn(
                "VD031", path, lineno, f"unfilled template placeholder in body: {token}"
            )

    if check_sections:
        for wanted in spec.get("sections", []):
            if not any(h.lower().startswith(wanted.lower()) for h in headings):
                report.warn("VD030", path, None, f"missing expected section `## {wanted}`")


def _check_filename_agreement(
    path: Path,
    kind: str,
    data: dict,
    text: str,
    report: Report,
    session_dir: Path | None,
) -> None:
    stem = path.stem

    if kind == "review":
        expected = int(REVIEW_RE.match(stem).group("cycle"))  # type: ignore[union-attr]
        cycle = data.get("cycle")
        if isinstance(cycle, int) and cycle != expected:
            report.error(
                "VD021",
                path,
                fm_line(text, "cycle"),
                f"frontmatter `cycle: {cycle}` disagrees with filename cycle {expected}",
            )

    if kind == "round":
        m = ROUND_RE.match(stem)
        assert m is not None
        rnd, author = int(m.group("round")), m.group("author")
        if isinstance(data.get("round"), int) and data["round"] != rnd:
            report.error(
                "VD021",
                path,
                fm_line(text, "round"),
                f"frontmatter `round: {data['round']}` disagrees with filename round {rnd}",
            )
        if isinstance(data.get("author"), str) and data["author"].strip() != author:
            report.error(
                "VD021",
                path,
                fm_line(text, "author"),
                f"frontmatter `author: {data['author']}` disagrees with filename author {author}",
            )
        if data.get("verdict") == "PROPOSE" and (rnd != 1 or author != "claude"):
            report.error(
                "VD021",
                path,
                fm_line(text, "verdict"),
                "verdict PROPOSE is only legal for round 1, author claude",
            )

    if kind == "route_a":
        m = ROUTE_A_RE.match(stem)
        assert m is not None
        fm_date = data.get("date")
        fm_date = fm_date.isoformat() if isinstance(fm_date, _date) else fm_date
        if isinstance(fm_date, str) and fm_date.strip() != m.group("date"):
            report.error(
                "VD021",
                path,
                fm_line(text, "date"),
                f"frontmatter `date: {fm_date}` disagrees with filename date {m.group('date')}",
            )

    if kind == "moc" and session_dir is not None:
        slug = MOC_RE.match(stem).group("slug")  # type: ignore[union-attr]
        if slug != session_dir.name:
            report.error(
                "VD021",
                path,
                None,
                f"MOC filename slug `{slug}` disagrees with session folder `{session_dir.name}`",
            )

    if session_dir is not None and kind != "round":
        session = data.get("session")
        if isinstance(session, str) and session.strip() != session_dir.name:
            report.warn(
                "VD007",
                path,
                fm_line(text, "session"),
                f"frontmatter `session: {session}` disagrees with folder `{session_dir.name}`",
            )


# --------------------------------------------------------------------------
# session-level checks
# --------------------------------------------------------------------------


def session_notes(session_dir: Path) -> list[Path]:
    notes = sorted(p for p in session_dir.glob("*.md"))
    rounds_dir = session_dir / "02a-consensus-rounds"
    if rounds_dir.is_dir():
        notes.extend(sorted(rounds_dir.glob("*.md")))
    return notes


def requirement_ids(path: Path) -> set[int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    _, _, body_start = parse_frontmatter(text)
    ids = set()
    for _lineno, line in live_lines(text, from_line=body_start):
        m = REQ_DECL_RE.match(line.strip())
        if m:
            ids.add(int(m.group("id")))
    return ids


def referenced_ids(path: Path) -> set[int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    _, _, body_start = parse_frontmatter(text)
    ids = set()
    for _lineno, line in live_lines(text, from_line=body_start):
        ids.update(int(n) for n in REQ_REF_RE.findall(line))
    return ids


def check_session(
    session_dir: Path,
    report: Report,
    index: VaultIndex | None,
    *,
    extra_fields: Iterable[str],
    check_sections: bool,
) -> None:
    notes = session_notes(session_dir)
    if not notes:
        report.warn("VD022", session_dir, None, "session folder contains no notes")
        return

    by_kind: dict[str, list[Path]] = {}
    for path in notes:
        kind = classify(path, session_dir)
        if kind is None:
            report.warn(
                "VD020",
                path,
                None,
                "unexpected file in session folder — not a duel-vault note name",
            )
            continue
        by_kind.setdefault(kind, []).append(path)

    moc_paths = by_kind.get("moc", [])
    moc_fm: dict = {}
    if not moc_paths:
        report.error("VD022", session_dir, None, "session folder has no `00 - <slug> MOC.md`")
        in_progress = True
    else:
        if len(moc_paths) > 1:
            report.error(
                "VD022", session_dir, None, f"session folder has {len(moc_paths)} MOC notes"
            )
        moc_data, _, _ = parse_frontmatter(moc_paths[0].read_text(encoding="utf-8", errors="replace"))
        moc_fm = moc_data or {}
        status = moc_fm.get("status")
        in_progress = str(status).strip() != "complete"

    for kind, paths in by_kind.items():
        for path in paths:
            check_note(
                path,
                kind,
                report,
                index,
                session_dir=session_dir,
                session_in_progress=in_progress,
                extra_fields=extra_fields,
                check_sections=check_sections,
            )

    # Review cycles must run 1..N with no gaps.
    cycles = sorted(
        int(REVIEW_RE.match(p.stem).group("cycle"))  # type: ignore[union-attr]
        for p in by_kind.get("review", [])
    )
    if cycles and cycles != list(range(1, len(cycles) + 1)):
        report.warn(
            "VD022",
            session_dir,
            None,
            f"review cycles are not contiguous from 1: found {cycles}",
        )

    # A finished session must have the notes its phases were supposed to produce.
    if by_kind.get("final"):
        for kind, label in (
            ("requirements", "01-requirements.md"),
            ("plan", "02-plan.md"),
            ("build", "03-build-log.md"),
        ):
            if not by_kind.get(kind):
                report.error(
                    "VD022",
                    session_dir,
                    None,
                    f"`05-final-report.md` exists but `{label}` is missing",
                )
        if not by_kind.get("review"):
            report.error(
                "VD022",
                session_dir,
                None,
                "`05-final-report.md` exists but no `04-review-cycle-N.md` was written",
            )

    # The NotebookLM lane and its provenance note are two halves of one fact: artifacts are
    # not reproducible, so a session that ran the lane without recording its corpus cannot
    # be audited at all.
    lane_declared = moc_fm.get("notebooklm") is True
    sources_notes = by_kind.get("sources", [])
    if lane_declared and not sources_notes:
        report.error(
            "VD023",
            session_dir,
            None,
            "MOC declares `notebooklm: true` but the session has no `06-sources.md`",
        )
    elif sources_notes and moc_paths and not lane_declared:
        report.warn(
            "VD023",
            moc_paths[0],
            None,
            "`06-sources.md` exists but the MOC does not declare `notebooklm: true`",
        )

    # Consensus rounds must alternate claude -> codex within each round.
    rounds: dict[int, set[str]] = {}
    for path in by_kind.get("round", []):
        m = ROUND_RE.match(path.stem)
        assert m is not None
        rounds.setdefault(int(m.group("round")), set()).add(m.group("author"))
    for rnd, authors in sorted(rounds.items()):
        if "claude" not in authors:
            report.error(
                "VD022",
                session_dir / "02a-consensus-rounds",
                None,
                f"round {rnd} has a codex message but no claude message",
            )

    # Requirement coverage, language-agnostic (R# tokens only).
    req_paths = by_kind.get("requirements", [])
    if req_paths:
        declared = requirement_ids(req_paths[0])
        if not declared:
            report.warn(
                "VD040", req_paths[0], None, "no `R<n>` requirements found in the requirements note"
            )
        for kind, code, level_name in (("plan", "VD040", ERROR), ("final", "VD042", WARN)):
            targets = by_kind.get(kind, [])
            if not targets or not declared:
                continue
            referenced = referenced_ids(targets[0])
            missing = sorted(declared - referenced)
            if missing:
                msg = "requirements never referenced: " + ", ".join(f"R{i}" for i in missing)
                report.add(level_name, code, targets[0], None, msg)
            unknown = sorted(referenced - declared)
            if unknown:
                report.error(
                    "VD041",
                    targets[0],
                    None,
                    "references requirements that do not exist in 01-requirements.md: "
                    + ", ".join(f"R{i}" for i in unknown),
                )


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


def lint_vault(
    vault: Path, report: Report, *, extra_fields: Iterable[str], check_sections: bool
) -> None:
    index = VaultIndex(vault)
    sessions = vault / "Sessions"
    if not sessions.is_dir():
        report.warn("VD022", vault, None, "vault has no `Sessions/` folder — nothing to lint")
        return
    for entry in sorted(sessions.iterdir()):
        if entry.is_dir():
            if entry.name in SKIP_DIRS:
                continue
            check_session(
                entry, report, index, extra_fields=extra_fields, check_sections=check_sections
            )
        elif entry.suffix.lower() == ".md":
            kind = classify(entry, None)
            if kind is None:
                report.warn(
                    "VD020",
                    entry,
                    None,
                    "loose note in Sessions/ does not match `<YYYY-MM-DD> <slug>.md`",
                )
                continue
            check_note(
                entry,
                kind,
                report,
                index,
                session_dir=None,
                session_in_progress=False,
                extra_fields=extra_fields,
                check_sections=check_sections,
            )


def lint_files(
    files: list[Path], report: Report, *, extra_fields: Iterable[str], check_sections: bool
) -> None:
    for path in files:
        if not path.is_file():
            report.error("VD000", path, None, "file does not exist")
            continue
        root = find_vault_root(path)
        index = VaultIndex(root) if root else None
        session_dir = path.parent
        if session_dir.name == "02a-consensus-rounds":
            session_dir = session_dir.parent
        in_session = session_dir.parent.name == "Sessions"
        kind = classify(path, session_dir if in_session else None)
        if kind is None:
            report.warn("VD020", path, None, "not a recognised duel-vault note name — skipped")
            continue
        check_note(
            path,
            kind,
            report,
            index,
            session_dir=session_dir if in_session else None,
            session_in_progress=True,
            extra_fields=extra_fields,
            check_sections=check_sections,
        )


def load_config_fields() -> list[str]:
    if not CONFIG_PATH.is_file():
        return []
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    fields = config.get("lint_required_fields", [])
    return [f for f in fields if isinstance(f, str)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vault-lint",
        description="Deterministic checks for duel-vault session notes.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--vault", type=Path, help="lint every session under <vault>/Sessions/")
    target.add_argument("--session", type=Path, help="lint a single session folder")
    target.add_argument("--files", type=Path, nargs="+", help="lint individual notes")
    parser.add_argument(
        "--require-field",
        action="append",
        default=[],
        metavar="NAME",
        help="extra frontmatter field this vault's CLAUDE.md mandates (repeatable)",
    )
    parser.add_argument(
        "--check-sections",
        action="store_true",
        help="also warn about missing template sections (English heading names only)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument(
        "--strict", action="store_true", help="treat warnings as errors for the exit code"
    )
    parser.add_argument("--quiet", action="store_true", help="print only the summary line")
    args = parser.parse_args(argv)

    extra_fields = list(dict.fromkeys(load_config_fields() + args.require_field))
    report = Report()

    try:
        if args.vault:
            vault = args.vault.expanduser()
            if not vault.is_dir():
                print(f"vault-lint: not a directory: {vault}", file=sys.stderr)
                return 2
            lint_vault(
                vault, report, extra_fields=extra_fields, check_sections=args.check_sections
            )
        elif args.session:
            session = args.session.expanduser()
            if not session.is_dir():
                print(f"vault-lint: not a directory: {session}", file=sys.stderr)
                return 2
            root = find_vault_root(session)
            index = VaultIndex(root) if root else None
            check_session(
                session,
                report,
                index,
                extra_fields=extra_fields,
                check_sections=args.check_sections,
            )
        else:
            lint_files(
                [p.expanduser() for p in args.files],
                report,
                extra_fields=extra_fields,
                check_sections=args.check_sections,
            )
    except OSError as exc:
        print(f"vault-lint: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "errors": report.n_errors,
                    "warnings": report.n_warnings,
                    "issues": [i.as_dict() for i in report.issues],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        if not args.quiet:
            for issue in sorted(report.issues, key=lambda i: (i.path, i.line or 0, i.code)):
                where = f"{issue.path}:{issue.line}" if issue.line else issue.path
                print(f"{issue.level:5} {issue.code}  {where}: {issue.message}")
        print(f"vault-lint: {report.n_errors} error(s), {report.n_warnings} warning(s)")

    if report.n_errors or (args.strict and report.n_warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
