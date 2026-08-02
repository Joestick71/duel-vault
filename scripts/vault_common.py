"""Shared primitives for the duel-vault scripts.

Frontmatter parsing and fenced-code-aware line scanning are used by both
`vault-lint.py` and `session-history.py`. They live here so the two never drift
apart — in particular the strict-YAML recovery path, which matters because
PyYAML raises a bare ValueError on frontmatter like `date: 2026-13-45`.
"""

from __future__ import annotations

import re
from typing import Any

try:  # PyYAML is nicer but not required
    import yaml  # type: ignore
except Exception:  # pragma: no cover - depends on environment
    yaml = None

SKIP_DIRS = {".git", ".obsidian", ".duel-vault", ".trash", "node_modules", "__pycache__"}

FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)

# Session-note filename conventions (references/obsidian-logging.md). Both the
# linter and the history scanner key off these, so they live in one place.
MOC_RE = re.compile(r"^00 - (?P<slug>.+) MOC$")
REVIEW_RE = re.compile(r"^04-review-cycle-(?P<cycle>\d+)$")
ROUND_RE = re.compile(r"^plan-round-(?P<round>\d+)-(?P<author>claude|codex)$")
ROUTE_A_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<slug>.+)$")

SESSION_NOTE_RE = re.compile(
    r"^(00 - .+ MOC|01-requirements|02-plan|03-build-log"
    r"|04-review-cycle-\d+|05-final-report|plan-disagreement"
    r"|plan-round-\d+-(claude|codex))$"
)


def scalar(raw: str) -> Any:
    val = raw.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    low = val.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    return val


def mini_yaml(block: str) -> tuple[dict, list[str]]:
    """Tolerant parser for the flat frontmatter these templates produce.

    Returns (data, bad_lines). Unparseable lines are skipped rather than fatal,
    so one broken value never hides the rest of the schema errors.
    """
    data: dict[str, Any] = {}
    bad: list[str] = []
    key: str | None = None
    for raw in block.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if key is not None:
                bucket = data.setdefault(key, [])
                if isinstance(bucket, list):
                    bucket.append(scalar(stripped[2:]))
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*:\s*(.*)$", line)
        if not m:
            bad.append(line.strip())
            continue
        key, val = m.group(1), m.group(2).strip()
        if val == "":
            data[key] = []
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            data[key] = [scalar(x) for x in inner.split(",") if x.strip()] if inner else []
        else:
            data[key] = scalar(val)
    return data, bad


def parse_frontmatter(text: str) -> tuple[dict | None, str | None, int]:
    """Return (data, error, body_start_line).

    `data is None` means the frontmatter is missing or wholly unreadable. A
    non-None `error` alongside non-None `data` means the block was recovered
    after a strict-YAML failure — report the error, then keep checking fields.
    """
    m = FM_RE.match(text)
    if not m:
        return None, "missing", 1
    block = m.group(1)
    body_start = text[: m.end()].count("\n") + 1

    strict_error: str | None = None
    if yaml is not None:
        try:
            data = yaml.safe_load(block)
        except Exception as exc:
            # PyYAML raises bare ValueError for things like `date: 2026-13-45`.
            detail = str(exc).splitlines()[0] if str(exc).strip() else exc.__class__.__name__
            strict_error = f"not valid YAML ({detail})"
        else:
            if data is None:
                return {}, None, body_start
            if isinstance(data, dict):
                return data, None, body_start
            return None, "frontmatter is not a mapping", body_start

    recovered, bad_lines = mini_yaml(block)
    if bad_lines:
        detail = "unparseable line(s): " + "; ".join(repr(b) for b in bad_lines[:3])
        strict_error = f"{strict_error}; {detail}" if strict_error else detail
    if not recovered and strict_error:
        return None, strict_error, body_start
    return recovered, strict_error, body_start


def fm_line(text: str, key: str) -> int | None:
    """1-indexed line of `key:` inside the frontmatter block, if present."""
    m = FM_RE.match(text)
    if not m:
        return None
    for offset, raw in enumerate(m.group(1).split("\n"), start=2):
        if re.match(rf"^{re.escape(key)}\s*:", raw):
            return offset
    return None


def live_lines(text: str, from_line: int = 1) -> list[tuple[int, str]]:
    """Lines outside fenced code blocks, with inline code spans stripped."""
    out: list[tuple[int, str]] = []
    fence: str | None = None
    for lineno, raw in enumerate(text.split("\n"), start=1):
        stripped = raw.strip()
        m = re.match(r"^(`{3,}|~{3,})", stripped)
        if m:
            token = m.group(1)[0] * 3
            if fence is None:
                fence = token
            elif stripped.startswith(fence):
                fence = None
            continue
        if fence is not None:
            continue
        if lineno < from_line:
            continue
        out.append((lineno, re.sub(r"`[^`\n]*`", "", raw)))
    return out
