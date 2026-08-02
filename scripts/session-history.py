#!/usr/bin/env python3
"""session-history — turn the Sessions folder back into an input.

The duel-vault workflow writes richly structured session notes and then never
reads them again. This script closes that loop: it scans prior sessions and
extracts what a new session should know before it starts — findings that keep
recurring, decisions already arbitrated, deviations Codex has made before, and
the residual issues a past session chose to accept.

The extraction is entirely mechanical (callout syntax, `F<n> · SEVERITY ·`
headings, frontmatter fields), so it works whatever language the notes are
written in. It deliberately does *not* try to cluster findings semantically —
that judgement belongs to the model reading the output.

    python3 scripts/session-history.py --vault ~/vaults/Direful
    python3 scripts/session-history.py --vault ~/vaults/Direful --json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any

from vault_common import (
    MOC_RE,
    REVIEW_RE,
    ROUTE_A_RE,
    SKIP_DIRS,
    live_lines,
    parse_frontmatter,
)

SEVERITIES = ("BLOCKER", "MAJOR", "MINOR")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# `### F1 · BLOCKER · title  [R2]` — separator varies with the vault's typography.
SEP = r"[·•|:\-—–]"
FINDING_RE = re.compile(
    rf"^#{{2,4}}\s*F\d+\s*{SEP}\s*(?P<sev>BLOCKER|MAJOR|MINOR)\s*{SEP}\s*(?P<title>.+?)\s*$",
    re.I,
)
CALLOUT_START_RE = re.compile(r"^\s*>\s*\[!(?P<type>[A-Za-z][A-Za-z0-9_-]*)\][+-]?\s*(?P<rest>.*)$")
CALLOUT_CONT_RE = re.compile(r"^\s*>\s?(?P<rest>.*)$")
WORD_RE = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def truncate(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def normalize_title(title: str) -> str:
    """Grouping key: case- and punctuation-insensitive, requirement refs dropped."""
    text = re.sub(r"\[?R\d+(\s*,\s*R?\d+)*\]?", " ", title, flags=re.I)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip().lower()


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


@dataclass
class Finding:
    severity: str
    title: str
    session: str
    cycle: int
    date: str


@dataclass
class Callout:
    type: str
    text: str
    session: str
    note: str


@dataclass
class Session:
    slug: str
    path: Path
    route: str
    date: str
    status: str = "unknown"
    consensus_rounds: int | None = None
    fix_cycles: int | None = None
    tokens_codex: Any = None
    findings: list[Finding] = field(default_factory=list)
    decisions: list[Callout] = field(default_factory=list)
    deviations: list[Callout] = field(default_factory=list)
    residuals: list[Finding] = field(default_factory=list)
    last_cycle: int = 0


def read(path: Path) -> tuple[dict, list[tuple[int, str]]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, []
    data, _error, body_start = parse_frontmatter(text)
    return (data or {}), live_lines(text, from_line=body_start)


def extract_callouts(lines: list[tuple[int, str]], wanted: set[str]) -> list[tuple[str, str]]:
    """Return (type, text) for each callout of a wanted type, continuations joined."""
    out: list[tuple[str, str]] = []
    current: tuple[str, list[str]] | None = None
    for _lineno, line in lines:
        start = CALLOUT_START_RE.match(line)
        if start:
            if current:
                out.append((current[0], " ".join(current[1]).strip()))
                current = None
            ctype = start.group("type").lower()
            if ctype in wanted:
                current = (ctype, [start.group("rest").strip()])
            continue
        if current is not None:
            cont = CALLOUT_CONT_RE.match(line)
            if cont and line.strip().startswith(">"):
                current[1].append(cont.group("rest").strip())
            else:
                out.append((current[0], " ".join(current[1]).strip()))
                current = None
    if current:
        out.append((current[0], " ".join(current[1]).strip()))
    return [(t, txt) for t, txt in out if txt]


def as_date_str(value: Any) -> str | None:
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return value.strip()
    return None


def scan_route_b(session_dir: Path) -> Session | None:
    notes = {p.stem: p for p in session_dir.glob("*.md")}
    moc_path = next((p for stem, p in notes.items() if MOC_RE.match(stem)), None)

    session = Session(slug=session_dir.name, path=session_dir, route="B", date="")

    if moc_path is not None:
        data, lines = read(moc_path)
        session.status = str(data.get("status", "unknown"))
        session.date = as_date_str(data.get("date")) or ""
        for _t, text in extract_callouts(lines, {"decision"}):
            session.decisions.append(Callout("decision", text, session.slug, moc_path.stem))

    if "02-plan" in notes:
        data, lines = read(notes["02-plan"])
        rounds = data.get("consensus_rounds")
        session.consensus_rounds = rounds if isinstance(rounds, int) else None
        session.date = session.date or as_date_str(data.get("date")) or ""
        for _t, text in extract_callouts(lines, {"decision"}):
            session.decisions.append(Callout("decision", text, session.slug, "02-plan"))

    if "03-build-log" in notes:
        _data, lines = read(notes["03-build-log"])
        for _t, text in extract_callouts(lines, {"warning"}):
            session.deviations.append(Callout("warning", text, session.slug, "03-build-log"))

    if "05-final-report" in notes:
        data, _lines = read(notes["05-final-report"])
        cycles = data.get("fix_cycles_used")
        session.fix_cycles = cycles if isinstance(cycles, int) else None
        session.tokens_codex = data.get("tokens_codex_total")
        session.date = session.date or as_date_str(data.get("date")) or ""

    for stem, path in sorted(notes.items()):
        m = REVIEW_RE.match(stem)
        if not m:
            continue
        cycle = int(m.group("cycle"))
        session.last_cycle = max(session.last_cycle, cycle)
        _data, lines = read(path)
        for _lineno, line in lines:
            found = FINDING_RE.match(line)
            if found:
                session.findings.append(
                    Finding(
                        severity=found.group("sev").upper(),
                        title=truncate(found.group("title"), 120),
                        session=session.slug,
                        cycle=cycle,
                        date=session.date,
                    )
                )

    if not session.date:
        session.date = _date.fromtimestamp(session_dir.stat().st_mtime).isoformat()

    # MINORs surviving in the last review cycle are what the session accepted.
    session.residuals = [
        f for f in session.findings if f.cycle == session.last_cycle and f.severity == "MINOR"
    ]
    return session


def scan_route_a(note: Path) -> Session:
    data, lines = read(note)
    m = ROUTE_A_RE.match(note.stem)
    session = Session(
        slug=str(data.get("session") or (m.group("slug") if m else note.stem)),
        path=note,
        route="A",
        date=as_date_str(data.get("date")) or (m.group("date") if m else ""),
        status="complete",
    )
    if not session.date:
        session.date = _date.fromtimestamp(note.stat().st_mtime).isoformat()
    for _t, text in extract_callouts(lines, {"decision"}):
        session.decisions.append(Callout("decision", text, session.slug, note.stem))
    for _t, text in extract_callouts(lines, {"warning"}):
        session.deviations.append(Callout("warning", text, session.slug, note.stem))
    return session


def scan_vault(vault: Path, limit: int | None) -> list[Session]:
    sessions_dir = vault / "Sessions"
    if not sessions_dir.is_dir():
        return []
    sessions: list[Session] = []
    for entry in sorted(sessions_dir.iterdir()):
        if entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue
        if entry.is_dir():
            found = scan_route_b(entry)
            if found:
                sessions.append(found)
        elif entry.suffix.lower() == ".md" and ROUTE_A_RE.match(entry.stem):
            sessions.append(scan_route_a(entry))
    sessions.sort(key=lambda s: (s.date, s.slug), reverse=True)
    return sessions[:limit] if limit else sessions


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------


def aggregate(sessions: list[Session], max_items: int) -> dict:
    all_findings = [f for s in sessions for f in s.findings]

    groups: dict[str, list[Finding]] = defaultdict(list)
    for finding in all_findings:
        groups[normalize_title(finding.title)].append(finding)

    recurring = []
    for key, items in groups.items():
        distinct = {f.session for f in items}
        if len(distinct) < 2:
            continue
        worst = min(items, key=lambda f: SEVERITY_RANK.get(f.severity, 9))
        recurring.append(
            {
                "title": worst.title,
                "worst_severity": worst.severity,
                "occurrences": len(items),
                "sessions": sorted(distinct),
            }
        )
    recurring.sort(key=lambda r: (-len(r["sessions"]), SEVERITY_RANK.get(r["worst_severity"], 9)))

    recurring_keys = {
        normalize_title(r["title"]) for r in recurring
    }
    high_severity = [
        {
            "severity": f.severity,
            "title": f.title,
            "session": f.session,
            "date": f.date,
        }
        for f in all_findings
        if f.severity in ("BLOCKER", "MAJOR") and normalize_title(f.title) not in recurring_keys
    ]
    high_severity.sort(key=lambda f: (f["date"], SEVERITY_RANK.get(f["severity"], 9)), reverse=True)

    # A word counts as a theme only if it appears in findings from >= 2 sessions,
    # which removes the need for a language-specific stopword list.
    word_sessions: dict[str, set[str]] = defaultdict(set)
    word_counts: Counter[str] = Counter()
    for finding in all_findings:
        if finding.severity == "MINOR":
            continue
        for word in set(WORD_RE.findall(finding.title.lower())):
            word_sessions[word].add(finding.session)
            word_counts[word] += 1
    themes = [
        {"word": w, "sessions": len(s), "occurrences": word_counts[w]}
        for w, s in word_sessions.items()
        if len(s) >= 2
    ]
    themes.sort(key=lambda t: (-t["sessions"], -t["occurrences"], t["word"]))

    route_b = [s for s in sessions if s.route == "B"]
    rounds = [s.consensus_rounds for s in route_b if isinstance(s.consensus_rounds, int)]
    fixes = [s.fix_cycles for s in route_b if isinstance(s.fix_cycles, int)]
    tokens = [s.tokens_codex for s in route_b if isinstance(s.tokens_codex, int)]

    def collect(attr: str) -> list[dict]:
        """Dedupe by normalized text — the same decision recurring in five sessions
        is one prior with a weight, not five entries eating the budget."""
        buckets: dict[str, dict] = {}
        for session in sessions:  # already sorted most-recent first
            for callout in getattr(session, attr):
                key = normalize_title(callout.text)
                bucket = buckets.setdefault(
                    key,
                    {"text": truncate(callout.text), "note": callout.note, "sessions": []},
                )
                if callout.session not in bucket["sessions"]:
                    bucket["sessions"].append(callout.session)
        items = sorted(buckets.values(), key=lambda b: -len(b["sessions"]))
        return items[:max_items]

    residual_buckets: dict[str, dict] = {}
    for session in sessions:
        for finding in session.residuals:
            key = normalize_title(finding.title)
            bucket = residual_buckets.setdefault(
                key, {"severity": finding.severity, "title": finding.title, "sessions": []}
            )
            if finding.session not in bucket["sessions"]:
                bucket["sessions"].append(finding.session)

    return {
        "sessions_scanned": len(sessions),
        "date_range": [sessions[-1].date, sessions[0].date] if sessions else [],
        "recurring_findings": recurring[:max_items],
        "recent_high_severity": high_severity[:max_items],
        "themes": themes[:max_items],
        "settled_decisions": collect("decisions"),
        "past_deviations": collect("deviations"),
        "accepted_residuals": sorted(
            residual_buckets.values(), key=lambda b: -len(b["sessions"])
        )[:max_items],
        "stats": {
            "route_a": len(sessions) - len(route_b),
            "route_b": len(route_b),
            "consensus_rounds_median": statistics.median(rounds) if rounds else None,
            "consensus_rounds_max": max(rounds) if rounds else None,
            "sessions_needing_fix_cycle": sum(1 for f in fixes if f > 0),
            "sessions_with_fix_data": len(fixes),
            "codex_tokens_median": int(statistics.median(tokens)) if tokens else None,
        },
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def render(summary: dict) -> str:
    if not summary["sessions_scanned"]:
        return (
            "## Prior sessions in this vault\n\n"
            "None found — this is the first duel-vault session here. No priors to apply.\n"
        )

    lo, hi = summary["date_range"]
    out: list[str] = [
        "## Known failure modes in this vault",
        "",
        f"Extracted mechanically from the {summary['sessions_scanned']} most recent session(s) "
        f"({lo} → {hi}) by `scripts/session-history.py`.",
        "",
        "> [!info] These are **priors, not requirements**. A past finding is a hypothesis worth",
        "> checking against this task, never on its own a reason to change the plan. A settled",
        "> decision should not be re-litigated unless this task gives a concrete new reason.",
        "",
    ]

    if summary["recurring_findings"]:
        out.append("### Findings that recurred across sessions")
        for item in summary["recurring_findings"]:
            sessions = ", ".join(item["sessions"])
            out.append(
                f"- **×{item['occurrences']} · worst {item['worst_severity']}** — {item['title']} "
                f"_(sessions: {sessions})_"
            )
        out.append("")

    if summary["recent_high_severity"]:
        out.append("### Recent BLOCKER/MAJOR findings (not yet recurring)")
        for item in summary["recent_high_severity"]:
            out.append(
                f"- **{item['severity']}** — {item['title']} _({item['session']}, {item['date']})_"
            )
        out.append("")

    if summary["themes"]:
        out.append("### Recurring vocabulary in high-severity findings")
        out.append(
            " · ".join(
                f"`{t['word']}` ({t['sessions']} sessions)" for t in summary["themes"]
            )
        )
        out.append("")

    def provenance(sessions_list: list[str], note: str | None = None) -> str:
        if len(sessions_list) > 1:
            label = f"{len(sessions_list)} sessions: {', '.join(sessions_list[:4])}"
            if len(sessions_list) > 4:
                label += ", …"
        else:
            label = sessions_list[0] if sessions_list else "?"
        return f"_({label}{' · ' + note if note else ''})_"

    if summary["settled_decisions"]:
        out.append("### Settled decisions — do not re-litigate")
        for item in summary["settled_decisions"]:
            out.append(f"- {item['text']} {provenance(item['sessions'], item['note'])}")
        out.append("")

    if summary["past_deviations"]:
        out.append("### Build deviations already seen")
        for item in summary["past_deviations"]:
            out.append(f"- {item['text']} {provenance(item['sessions'])}")
        out.append("")

    if summary["accepted_residuals"]:
        out.append("### Residual issues past sessions chose to accept")
        for item in summary["accepted_residuals"]:
            out.append(
                f"- **{item['severity']}** — {item['title']} {provenance(item['sessions'])}"
            )
        out.append("")

    stats = summary["stats"]
    bits = [f"{stats['route_b']} Route B, {stats['route_a']} Route A"]
    if stats["consensus_rounds_median"] is not None:
        bits.append(
            f"consensus rounds median {stats['consensus_rounds_median']:g} "
            f"(max {stats['consensus_rounds_max']})"
        )
    if stats["sessions_with_fix_data"]:
        bits.append(
            f"fix cycle needed in {stats['sessions_needing_fix_cycle']}/"
            f"{stats['sessions_with_fix_data']} sessions"
        )
    if stats["codex_tokens_median"] is not None:
        bits.append(f"Codex tokens median {stats['codex_tokens_median']:,}")
    out.append("### Process stats")
    out.append(" · ".join(bits))
    out.append("")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="session-history",
        description="Extract priors from prior duel-vault sessions.",
    )
    parser.add_argument("--vault", type=Path, required=True, help="vault root")
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="how many of the most recent sessions to scan (default 10, 0 = all)",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=8,
        help="max entries per section, to keep the injected block small (default 8)",
    )
    parser.add_argument("--json", action="store_true", help="emit the raw summary instead")
    parser.add_argument("--out", type=Path, help="write the rendered block to this file too")
    args = parser.parse_args(argv)

    vault = args.vault.expanduser()
    if not vault.is_dir():
        print(f"session-history: not a directory: {vault}", file=sys.stderr)
        return 2

    sessions = scan_vault(vault, args.limit or None)
    summary = aggregate(sessions, args.max_items)

    text = json.dumps(summary, indent=2, ensure_ascii=False) if args.json else render(summary)
    print(text)
    if args.out:
        try:
            args.out.expanduser().write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"session-history: cannot write {args.out}: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
