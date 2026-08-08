#!/usr/bin/env python3
"""nblm-import — the only door NotebookLM artifacts use to enter the vault.

`notebooklm download` writes bare files: a report with no frontmatter, a PNG with no
note around it, a mind map as raw JSON. Dropped into a vault as-is they are invisible to
Obsidian's properties, unlinked from the session that produced them, and they fail the
closing `vault-lint.py` gate. Worse, they are unattributable: NotebookLM artifacts are not
reproducible, so a note whose corpus was never recorded cannot be audited later.

This script closes both gaps.

    import    materialize downloaded artifacts as vault notes (frontmatter, provenance,
              backlink to the session; mind maps become real Obsidian .canvas files)
    manifest  write `06-sources.md` — the corpus that produced them, read from the live
              notebook, never from memory

Exit codes: 0 ok · 1 refused (would overwrite, unrecognised mind map, CLI failure) ·
2 usage or IO problem.

    python3 scripts/nblm-import.py import --vault "$V" --session "$S" \
        --notebook "$NB" --into "Ricerche/2026" .duel-vault/$S/artifacts/report.md
    python3 scripts/nblm-import.py manifest --vault "$V" --session "$S" --notebook "$NB"
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any

from vault_common import FM_RE, MOC_RE, ROUTE_A_RE, lint_required_fields, parse_frontmatter

# Obsidian refuses these in filenames; NotebookLM titles contain them routinely.
ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]+')

EMBEDDABLE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".mp3", ".mp4", ".wav"}

# filename stem -> artifact type, for the common `notebooklm download <type>` outputs
TYPE_BY_STEM = {
    "report": "report",
    "audio": "audio",
    "podcast": "audio",
    "video": "video",
    "cinematic-video": "video",
    "infographic": "infographic",
    "slide-deck": "slide-deck",
    "slides": "slide-deck",
    "mind-map": "mind-map",
    "mindmap": "mind-map",
    "data-table": "data-table",
    "quiz": "quiz",
    "flashcards": "flashcards",
}


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------


def fail(message: str, code: int = 2) -> int:
    print(f"nblm-import: {message}", file=sys.stderr)
    return code


def parse_kv(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--field expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"--field has an empty key: {pair!r}")
        out[key] = value.strip()
    return out


def yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if text == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`]", text) or text != text.strip():
        return '"' + text.replace('"', '\\"') + '"'
    return text


def dump_frontmatter(data: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(yaml_scalar(v) for v in value)}]")
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def safe_stem(text: str, fallback: str = "notebooklm") -> str:
    cleaned = ILLEGAL_NAME_CHARS.sub("", text).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or fallback


def cell(text: Any) -> str:
    """Table-cell-safe: a pipe inside a title would split the column."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip() or "—"


def pretty_type(raw: Any) -> str:
    text = str(raw or "").strip()
    if "." in text:  # "SourceType.WEB_PAGE"
        text = text.rsplit(".", 1)[1]
    return text.replace("_", " ").lower() or "—"


def infer_type(path: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    stem = path.stem.lower()
    for key, value in TYPE_BY_STEM.items():
        if stem == key or stem.startswith(key + "-") or stem.endswith("-" + key):
            return value
    return "artifact"


def resolve_backlink(vault: Path, slug: str) -> str | None:
    """The session note this artifact should point back to, if it exists yet.

    Route B sessions are folders with a MOC; Route A is a single dated note. Emitting a
    wikilink to neither is better than emitting a broken one.
    """
    session_dir = vault / "Sessions" / slug
    if session_dir.is_dir():
        for note in sorted(session_dir.glob("*.md")):
            if MOC_RE.match(note.stem):
                return note.stem
        return None
    sessions = vault / "Sessions"
    if sessions.is_dir():
        for note in sorted(sessions.glob("*.md")):
            m = ROUTE_A_RE.match(note.stem)
            if m and m.group("slug") == slug:
                return note.stem
    return None


def inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------


@dataclass
class Context:
    vault: Path
    project: str
    session: str
    notebook: str
    date: str
    tags: list[str]
    extra: dict[str, Any]
    backlink: str | None
    force: bool
    dry_run: bool
    written: list[str] = field(default_factory=list)
    used_stems: set[str] = field(default_factory=set)


def reserve_stem(ctx: Context, base: str) -> str:
    """A note/asset stem unique within this invocation.

    Two files sharing a stem (same --title across a batch, or same source stem with
    different suffixes) must not silently clobber each other's note even under --force,
    which is meant for files left by a *previous* run, not our own batch's outputs.
    """
    stem = base
    n = 2
    while stem in ctx.used_stems:
        stem = f"{base}-{n}"
        n += 1
    ctx.used_stems.add(stem)
    return stem


def merge_frontmatter(existing: dict[str, Any] | None, override: dict[str, Any]) -> dict[str, Any]:
    """`override` (mandated fields, guaranteed tags) always wins; `existing` only fills gaps.

    An artifact's own frontmatter must never be able to shadow a `--field` the operator
    passed to satisfy the vault's mandated fields, or replace the guaranteed tag list.
    """
    data: dict[str, Any] = dict(existing) if existing else {}
    existing_tags = data.get("tags")
    data.update(override)
    if isinstance(existing_tags, list):
        data["tags"] = list(dict.fromkeys([*existing_tags, *override["tags"]]))
    return data


def note_frontmatter(ctx: Context, artifact_type: str, artifact_id: str | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "project": ctx.project,
        "session": ctx.session,
        "date": ctx.date,
        "source": "notebooklm",
        "notebook_id": ctx.notebook,
        "artifact_type": artifact_type,
    }
    if artifact_id:
        data["artifact_id"] = artifact_id
    data.update(ctx.extra)
    data["tags"] = ["notebooklm", "duel-vault/artifact", *ctx.tags]
    return data


def provenance(ctx: Context, artifact_type: str, artifact_id: str | None) -> str:
    bits = [f"notebook `{ctx.notebook}`", f"artifact `{artifact_type}`"]
    if artifact_id:
        bits.append(f"id `{artifact_id}`")
    bits.append(f"imported {ctx.date}")
    block = "\n> [!info] NotebookLM\n> " + " · ".join(bits) + "\n"
    if ctx.backlink:
        block += f"> Sessione: [[{ctx.backlink}]]\n"
    block += "> Not reproducible: the same sources and prompt will not return this artifact again.\n"
    return block


def write_text(ctx: Context, path: Path, text: str) -> int:
    if path.exists() and not ctx.force:
        return fail(f"{path} already exists (use --force to overwrite)", 1)
    if ctx.dry_run:
        print(f"[dry-run] would write {path} ({len(text)} chars)")
        ctx.written.append(str(path))
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    ctx.written.append(str(path))
    return 0


def import_markdown(ctx: Context, src: Path, dest_dir: Path, args: argparse.Namespace) -> int:
    raw = src.read_text(encoding="utf-8", errors="replace")
    existing, _err, _line = parse_frontmatter(raw)
    body = FM_RE.sub("", raw, count=1) if FM_RE.match(raw) else raw

    artifact_type = infer_type(src, args.artifact_type)
    data = merge_frontmatter(existing, note_frontmatter(ctx, artifact_type, args.artifact_id))

    title = args.title or (existing or {}).get("title") or src.stem.replace("-", " ").title()
    heading = f"# {title}\n" if not re.match(r"^\s*#\s", body.lstrip("\n")) else ""
    text = dump_frontmatter(data) + "\n" + heading + body.strip() + "\n" + provenance(
        ctx, artifact_type, args.artifact_id
    )
    stem = reserve_stem(ctx, safe_stem(args.title or src.stem))
    return write_text(ctx, dest_dir / f"{stem}.md", text)


def import_asset(
    ctx: Context, src: Path, dest_dir: Path, assets_dir: Path, args: argparse.Namespace
) -> int:
    artifact_type = infer_type(src, args.artifact_type)
    stem = reserve_stem(ctx, safe_stem(args.title or src.stem))
    asset_name = f"{stem}{src.suffix.lower()}"
    asset_path = assets_dir / asset_name
    note_path = dest_dir / f"{stem}.md"

    for target in (asset_path, note_path):
        if target.exists() and not ctx.force:
            return fail(f"{target} already exists (use --force to overwrite)", 1)

    if ctx.dry_run:
        print(f"[dry-run] would copy {src} -> {asset_path}")
    else:
        assets_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, asset_path)
    ctx.written.append(str(asset_path))

    title = args.title or src.stem.replace("-", " ").title()
    embed = "!" if src.suffix.lower() in EMBEDDABLE else ""
    text = (
        dump_frontmatter(note_frontmatter(ctx, artifact_type, args.artifact_id))
        + f"\n# {title}\n\n{embed}[[{asset_name}]]\n"
        + provenance(ctx, artifact_type, args.artifact_id)
    )
    return write_text(ctx, note_path, text)


def import_canvas(ctx: Context, src: Path, dest_dir: Path, args: argparse.Namespace) -> int:
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return fail(f"cannot read mind map {src}: {exc}", 2)

    title = args.title or src.stem.replace("-", " ").title()
    root = extract_tree(payload, title)
    if root is None:
        return fail(
            f"unrecognised mind-map JSON in {src} — top-level keys: "
            f"{sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__}. "
            "Import it as a plain asset instead (drop --canvas), or open an issue with the shape.",
            1,
        )

    stem = reserve_stem(ctx, safe_stem(args.title or src.stem))
    canvas_path = dest_dir / f"{stem}.canvas"
    note_path = dest_dir / f"{stem}.md"
    for target in (canvas_path, note_path):
        if target.exists() and not ctx.force:
            return fail(f"{target} already exists (use --force to overwrite)", 1)

    canvas = build_canvas(root)
    if ctx.dry_run:
        print(f"[dry-run] would write {canvas_path} ({len(canvas['nodes'])} nodes)")
        ctx.written.append(str(canvas_path))
    else:
        dest_dir.mkdir(parents=True, exist_ok=True)
        canvas_path.write_text(json.dumps(canvas, ensure_ascii=False, indent=1), encoding="utf-8")
        ctx.written.append(str(canvas_path))

    text = (
        dump_frontmatter(note_frontmatter(ctx, "mind-map", args.artifact_id))
        + f"\n# {title}\n\n![[{stem}.canvas]]\n"
        + provenance(ctx, "mind-map", args.artifact_id)
    )
    return write_text(ctx, note_path, text)


# --------------------------------------------------------------------------
# mind map -> JSON Canvas
# --------------------------------------------------------------------------


@dataclass
class TreeNode:
    label: str
    children: list["TreeNode"] = field(default_factory=list)


LABEL_KEYS = ("label", "title", "text", "name", "topic", "content", "value")
CHILD_KEYS = ("children", "nodes", "subtopics", "sub_topics", "branches", "items", "topics")
ROOT_KEYS = ("root", "mindmap", "mind_map", "mindMap", "tree", "graph", "data", "result")


def _as_node(obj: Any) -> TreeNode | None:
    """One node of an unknown-but-conventional mind-map JSON.

    The CLI's mind-map payload is not a documented, stable schema (and its default `--kind`
    changes in v0.8.0), so the shape is detected rather than assumed: any of the usual label
    keys, any of the usual children keys.
    """
    if isinstance(obj, str):
        return TreeNode(obj.strip()) if obj.strip() else None
    if not isinstance(obj, dict):
        return None
    label = next(
        (str(obj[k]).strip() for k in LABEL_KEYS if isinstance(obj.get(k), (str, int, float))),
        "",
    )
    kids_raw: Any = next((obj[k] for k in CHILD_KEYS if isinstance(obj.get(k), list)), [])
    children = [n for n in (_as_node(k) for k in kids_raw) if n is not None]
    if not label and not children:
        return None
    return TreeNode(label or "…", children)


def _from_flat(items: list[dict], title: str) -> TreeNode | None:
    """Flat `[{id, parent_id, label}, …]` listings, the other common shape."""
    if not all(isinstance(i, dict) and "id" in i for i in items):
        return None
    parent_key = next(
        (k for k in ("parent", "parent_id", "parentId", "parentID") if any(k in i for i in items)),
        None,
    )
    if parent_key is None:
        return None
    nodes = {
        str(i["id"]): TreeNode(
            next(
                (str(i[k]).strip() for k in LABEL_KEYS if isinstance(i.get(k), (str, int, float))),
                "…",
            )
        )
        for i in items
    }
    roots: list[TreeNode] = []
    for item in items:
        node = nodes[str(item["id"])]
        parent = item.get(parent_key)
        if parent is None or str(parent) not in nodes or str(parent) == str(item["id"]):
            roots.append(node)
        else:
            nodes[str(parent)].children.append(node)
    if not roots:
        return None
    return roots[0] if len(roots) == 1 else TreeNode(title, roots)


def extract_tree(payload: Any, title: str) -> TreeNode | None:
    if isinstance(payload, list):
        flat = _from_flat([p for p in payload if isinstance(p, dict)], title)
        if flat is not None:
            return flat
        children = [n for n in (_as_node(p) for p in payload) if n is not None]
        return TreeNode(title, children) if children else None
    if isinstance(payload, dict):
        for key in ROOT_KEYS:
            if key in payload:
                found = extract_tree(payload[key], title)
                if found is not None:
                    return found
        node = _as_node(payload)
        if node is not None and (node.children or node.label != "…"):
            return node
    return None


NODE_W, H_GAP, V_GAP = 280, 140, 28


def _height(node: TreeNode) -> int:
    own = 44 + 22 * math.ceil(max(len(node.label), 1) / 34)
    if not node.children:
        return own
    return max(own, sum(_height(c) for c in node.children) + V_GAP * (len(node.children) - 1))


def build_canvas(root: TreeNode) -> dict:
    """Left-to-right layered tree, JSON Canvas 1.0."""
    nodes: list[dict] = []
    edges: list[dict] = []
    counter = {"n": 0}

    def place(node: TreeNode, depth: int, top: int) -> str:
        counter["n"] += 1
        node_id = f"n{counter['n']}"
        block = _height(node)
        own = 44 + 22 * math.ceil(max(len(node.label), 1) / 34)
        nodes.append(
            {
                "id": node_id,
                "type": "text",
                "text": node.label,
                "x": depth * (NODE_W + H_GAP),
                "y": top + (block - own) // 2,
                "width": NODE_W,
                "height": own,
            }
        )
        cursor = top
        for child in node.children:
            child_id = place(child, depth + 1, cursor)
            cursor += _height(child) + V_GAP
            edges.append(
                {
                    "id": f"e{len(edges) + 1}",
                    "fromNode": node_id,
                    "fromSide": "right",
                    "toNode": child_id,
                    "toSide": "left",
                }
            )
        return node_id

    place(root, 0, 0)
    return {"nodes": nodes, "edges": edges}


def cmd_import(args: argparse.Namespace) -> int:
    vault = args.vault.expanduser().resolve()
    if not (vault / "CLAUDE.md").is_file():
        return fail(f"{vault} has no CLAUDE.md — not a duel-vault project vault")

    try:
        extra = parse_kv(args.field)
    except ValueError as exc:
        return fail(str(exc))

    missing = [f for f in lint_required_fields() if f not in extra]
    if missing:
        return fail(
            "this vault mandates frontmatter field(s) "
            + ", ".join(f"`{m}`" for m in missing)
            + " (lint_required_fields in duel-vault.config.json) — pass them with --field k=v"
        )

    dest_dir = (vault / args.into).resolve()
    assets_dir = (vault / (args.assets or args.into)).resolve()
    for path in (dest_dir, assets_dir):
        if not inside(vault, path):
            return fail(f"{path} is outside the vault")

    backlink = args.backlink or resolve_backlink(vault, args.session)
    if backlink is None:
        print(
            f"nblm-import: no session note found for {args.session!r} — "
            "notes will carry no backlink (pass --backlink to force one)",
            file=sys.stderr,
        )

    ctx = Context(
        vault=vault,
        project=args.project or vault.name,
        session=args.session,
        notebook=args.notebook,
        date=args.date or _date.today().isoformat(),
        tags=list(args.tag),
        extra=extra,
        backlink=backlink,
        force=args.force,
        dry_run=args.dry_run,
    )

    status = 0
    for raw in args.files:
        src = raw.expanduser()
        if not src.is_file():
            status = max(status, fail(f"no such file: {src}"))
            continue
        suffix = src.suffix.lower()
        if args.canvas and suffix == ".json":
            rc = import_canvas(ctx, src, dest_dir, args)
        elif suffix in (".md", ".markdown"):
            rc = import_markdown(ctx, src, dest_dir, args)
        else:
            rc = import_asset(ctx, src, dest_dir, assets_dir, args)
        status = max(status, rc)

    if args.json:
        print(json.dumps({"written": ctx.written, "status": status}, indent=2, ensure_ascii=False))
    elif ctx.written:
        for path in ctx.written:
            print(f"wrote {path}")
    return status


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------


FLAG_ERROR_RE = re.compile(
    r"unrecognized|unrecognised|no such option|unknown (option|flag)|invalid choice", re.I
)


def run_cli(subcommand: list[str], notebook: str) -> tuple[dict | None, str]:
    """`notebooklm <sub> --json` for one notebook, addressed explicitly.

    The flag spelling differs across subcommands (`--notebook` vs `-n`). Only a failure that
    looks like an unrecognised flag justifies retrying with the other spelling — retrying on
    every failure would double the wall clock and the rate-limit exposure of a genuine error
    (stale auth, bad notebook id). Never falls back to the CLI's stored context: this lane
    runs concurrent agents and a shared context is exactly what they overwrite.
    """
    flags = ("--notebook", "-n")
    last = ""
    for i, flag in enumerate(flags):
        cmd = ["notebooklm", *subcommand, flag, notebook, "--json"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            return None, "the `notebooklm` CLI is not on PATH"
        except subprocess.TimeoutExpired:
            return None, f"`{' '.join(cmd)}` timed out after 120s"
        if proc.returncode == 0:
            try:
                parsed = json.loads(proc.stdout)
            except ValueError:
                return None, f"`{' '.join(cmd)}` returned non-JSON output: {proc.stdout[:200]}"
            if not isinstance(parsed, dict):
                return None, (
                    f"`{' '.join(cmd)}` returned {type(parsed).__name__}, expected a JSON object"
                )
            return parsed, ""
        last = (proc.stderr or proc.stdout).strip()[:400]
        looks_like_flag_error = proc.returncode == 2 or FLAG_ERROR_RE.search(last)
        if i < len(flags) - 1 and looks_like_flag_error:
            continue
        break
    return None, last or "unknown CLI failure"


def load_listing(explicit: Path | None, subcommand: list[str], notebook: str) -> tuple[dict, str]:
    if explicit is not None:
        try:
            parsed = json.loads(explicit.expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {}, f"cannot read {explicit}: {exc}"
        if not isinstance(parsed, dict):
            return {}, f"{explicit} contains {type(parsed).__name__}, expected a JSON object"
        return parsed, ""
    data, err = run_cli(subcommand, notebook)
    return (data or {}), err


def sources_table(sources: list[dict]) -> str:
    rows = ["| # | Title | Type | Status | Origin |", "|---|-------|------|--------|--------|"]
    for i, src in enumerate(sources, start=1):
        origin = src.get("url") or src.get("id") or "—"
        rows.append(
            f"| {src.get('index', i)} | {cell(src.get('title'))} | {pretty_type(src.get('type'))} "
            f"| {cell(src.get('status', 'unknown'))} | {cell(origin)} |"
        )
    if not sources:
        rows.append("| — | (no sources) | — | — | — |")
    return "\n".join(rows)


def artifacts_table(artifacts: list[dict]) -> str:
    rows = ["| # | Type | Title | Status | Artifact ID |", "|---|------|-------|--------|-------------|"]
    for i, art in enumerate(artifacts, start=1):
        rows.append(
            f"| {art.get('index', i)} | {pretty_type(art.get('type'))} | {cell(art.get('title'))} "
            f"| {cell(art.get('status', 'unknown'))} | {cell(art.get('id'))} |"
        )
    if not artifacts:
        rows.append("| — | (no artifacts) | — | — | — |")
    return "\n".join(rows)


def cmd_manifest(args: argparse.Namespace) -> int:
    vault = args.vault.expanduser().resolve()
    if not (vault / "CLAUDE.md").is_file():
        return fail(f"{vault} has no CLAUDE.md — not a duel-vault project vault")

    try:
        extra = parse_kv(args.field)
    except ValueError as exc:
        return fail(str(exc))
    if args.format == "note":
        # In block format there is no frontmatter to carry these fields into — the
        # Route A log that receives the pasted fragment carries them instead.
        missing = [f for f in lint_required_fields() if f not in extra]
        if missing:
            return fail(
                "this vault mandates frontmatter field(s) "
                + ", ".join(f"`{m}`" for m in missing)
                + " — pass them with --field k=v"
            )

    src_data, src_err = load_listing(args.sources_json, ["source", "list"], args.notebook)
    art_data, art_err = load_listing(args.artifacts_json, ["artifact", "list"], args.notebook)
    if src_err:
        # A manifest built from a failed query would be a fabricated corpus.
        return fail(f"could not read the notebook's sources: {src_err}", 1)
    if art_err:
        return fail(f"could not read the notebook's artifacts: {art_err}", 1)
    if "sources" not in src_data or not isinstance(src_data["sources"], list):
        return fail(
            "`source list --json` response has no `sources` list — shape: "
            f"{sorted(src_data)[:8]}",
            1,
        )
    if "artifacts" not in art_data or not isinstance(art_data["artifacts"], list):
        return fail(
            "`artifact list --json` response has no `artifacts` list — shape: "
            f"{sorted(art_data)[:8]}",
            1,
        )

    sources = [s for s in src_data["sources"] if isinstance(s, dict)]
    artifacts = [a for a in art_data["artifacts"] if isinstance(a, dict)]
    today = args.date or _date.today().isoformat()
    title = args.title or src_data.get("notebook_title") or args.session

    header = (
        f"> [!info] Notebook `{args.notebook}` · {len(sources)} sources · "
        f"{len(artifacts)} artifacts · captured {today}\n"
    )
    prompts = "\n".join(f"{i}. {p}" for i, p in enumerate(args.prompt, start=1)) or (
        "(no generation prompt recorded — record them, artifacts are not reproducible)"
    )

    body = (
        f"{header}\n## Sources\n{sources_table(sources)}\n\n"
        f"## Artifacts\n{artifacts_table(artifacts)}\n\n"
        f"## Prompts\n{prompts}\n"
    )

    if args.format == "block":
        text = body
        out = args.out.expanduser() if args.out else None
    else:
        data: dict[str, Any] = {
            "project": args.project or vault.name,
            "session": args.session,
            "phase": "sources",
            "date": today,
            "notebook_id": args.notebook,
            "sources": len(sources),
            "artifacts": len(artifacts),
        }
        data.update(extra)
        data["tags"] = ["duel-vault", "duel-vault/notebooklm", *args.tag]
        backlink = args.backlink or resolve_backlink(vault, args.session)
        trail = f"\n## Trail\n[[{backlink}]]\n" if backlink else ""
        text = dump_frontmatter(data) + f"\n# NotebookLM — {title}\n\n" + body + trail
        out = (
            args.out.expanduser()
            if args.out
            else vault / "Sessions" / args.session / "06-sources.md"
        )

    if out is None:
        print(text)
        return 0
    if out.exists() and not args.force and args.format != "block":
        return fail(f"{out} already exists (use --force to refresh it)", 1)
    if args.dry_run:
        print(f"[dry-run] would write {out} ({len(sources)} sources, {len(artifacts)} artifacts)")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} — {len(sources)} sources, {len(artifacts)} artifacts")

    # The MOC flag and this note are two halves of one fact; vault-lint checks the pair, so
    # say it here rather than editing someone else's note behind their back.
    session_dir = vault / "Sessions" / args.session
    if args.format != "block" and session_dir.is_dir():
        moc = next((p for p in sorted(session_dir.glob("*.md")) if MOC_RE.match(p.stem)), None)
        if moc is not None:
            fm, _err, _line = parse_frontmatter(moc.read_text(encoding="utf-8", errors="replace"))
            if (fm or {}).get("notebooklm") is not True:
                print(
                    f"nblm-import: add `notebooklm: true` to {moc.name} — vault-lint pairs the "
                    "MOC flag with this note",
                    file=sys.stderr,
                )
    return 0


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nblm-import",
        description="Materialize NotebookLM artifacts into an Obsidian vault, with provenance.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--vault", type=Path, required=True, help="vault root (must hold CLAUDE.md)")
        p.add_argument("--session", required=True, help="session slug")
        p.add_argument("--notebook", required=True, help="NotebookLM notebook id")
        p.add_argument("--project", help="project name for frontmatter (default: vault dir name)")
        p.add_argument("--date", help="YYYY-MM-DD (default: today)")
        p.add_argument("--title", help="title for the note")
        p.add_argument("--tag", action="append", default=[], metavar="TAG", help="extra tag")
        p.add_argument(
            "--field",
            action="append",
            default=[],
            metavar="K=V",
            help="extra frontmatter field this vault mandates (repeatable)",
        )
        p.add_argument("--backlink", help="note name to link back to (default: auto-detected)")
        p.add_argument("--force", action="store_true", help="overwrite existing files")
        p.add_argument("--dry-run", action="store_true", help="show what would be written")

    imp = sub.add_parser("import", help="materialize downloaded artifacts as vault notes")
    common(imp)
    imp.add_argument("--into", required=True, help="vault-relative folder for the notes")
    imp.add_argument("--assets", help="vault-relative folder for binaries (default: --into)")
    imp.add_argument("--artifact-type", help="report, audio, mind-map, … (default: inferred)")
    imp.add_argument("--artifact-id", help="NotebookLM artifact id, for provenance")
    imp.add_argument("--canvas", action="store_true", help="convert a mind-map .json to .canvas")
    imp.add_argument("--json", action="store_true", help="machine-readable summary")
    imp.add_argument("files", type=Path, nargs="+", help="downloaded artifact files")
    imp.set_defaults(func=cmd_import)

    man = sub.add_parser("manifest", help="write 06-sources.md from the live notebook")
    common(man)
    man.add_argument("--out", type=Path, help="output path (default: Sessions/<slug>/06-sources.md)")
    man.add_argument(
        "--format",
        choices=("note", "block"),
        default="note",
        help="note = full 06-sources.md; block = fragment to paste into a Route A log",
    )
    man.add_argument("--prompt", action="append", default=[], help="generation prompt (repeatable)")
    man.add_argument("--sources-json", type=Path, help="captured `source list --json` instead of a live call")
    man.add_argument("--artifacts-json", type=Path, help="captured `artifact list --json`")
    man.set_defaults(func=cmd_manifest)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except OSError as exc:
        return fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
