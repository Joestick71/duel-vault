"""End-to-end checks for the session journal, state/recovery and progress-notebook scripts.

The real `codex` and `notebooklm` CLIs are replaced by small fakes on PATH, so the tests
run anywhere, cost nothing, and exercise the same subprocess boundary the scripts use.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import session_common as sc  # noqa: E402

FAKE_NOTEBOOKLM = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
db = Path(os.environ["FAKE_NBLM_DB"])
st = json.loads(db.read_text()) if db.exists() else {"notebooks": {}, "next": 1, "calls": []}
args = sys.argv[1:]
st["calls"].append(args)
def save():
    db.write_text(json.dumps(st))
def nb():
    for flag in ("--notebook", "-n"):
        if flag in args:
            return args[args.index(flag) + 1]
if Path(os.environ["FAKE_NBLM_DOWN"]).exists():
    save(); print("Error: authentication expired", file=sys.stderr); sys.exit(1)
if args[:1] == ["list"]:
    print(json.dumps({"notebooks": [{"id": k, "title": v["title"]} for k, v in st["notebooks"].items()]}))
elif args[:1] == ["create"]:
    nid = "nb%d" % st["next"]; st["next"] += 1
    st["notebooks"][nid] = {"title": args[1], "sources": {}}
    print(json.dumps({"notebook": {"id": nid, "title": args[1]}}))
elif args[:2] == ["source", "add"]:
    sid = "src%d" % st["next"]; st["next"] += 1
    p = Path(args[2])
    st["notebooks"][nb()]["sources"][sid] = {"title": p.name, "text": p.read_text()}
    print(json.dumps({"source": {"id": sid, "title": p.name}}))
elif args[:2] == ["source", "list"]:
    srcs = st["notebooks"][nb()]["sources"]
    print(json.dumps({"sources": [{"id": k, "title": v["title"]} for k, v in srcs.items()]}))
elif args[:2] == ["source", "delete"]:
    if sys.stdin.read().strip() != "y":
        print("aborted", file=sys.stderr); sys.exit(1)
    st["notebooks"][nb()]["sources"].pop(args[2], None); print("deleted")
elif args[:2] == ["source", "fulltext"]:
    src = st["notebooks"][nb()]["sources"][args[2]]
    print(json.dumps({"source": {"id": args[2], "content": src["text"]}}))
elif args[:2] == ["auth", "check"]:
    print(json.dumps({"status": "ok", "checks": {"token_fetch": True}}))
else:
    print("no such command", file=sys.stderr); sys.exit(2)
save()
'''

FAKE_CODEX = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
if Path(os.environ["FAKE_CODEX_LIMIT"]).exists():
    print("You've hit your usage limit. Upgrade to Pro or try again in 2 hours 5 minutes.", file=sys.stderr)
    sys.exit(1)
print("ok")
'''


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
with open(os.environ["FAKE_CLAUDE_CALLS"], "a") as fh:
    fh.write(os.environ.get("DUEL_VAULT_PROBE", "-") + "\n")
if Path(os.environ["FAKE_CLAUDE_LIMIT"]).exists():
    print("Claude usage limit reached.", file=sys.stderr)
    sys.exit(1)
print("ok")
'''


class Sandbox(unittest.TestCase):
    """A fresh HOME, vault and fake-CLI PATH per test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="duel-vault-test-"))
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        # Calls made in-process (sc.save_state, …) must not reach the real ~/.claude either.
        from unittest import mock
        for target, value in (
            ("REGISTRY_PATH", self.home / ".claude" / "duel-vault.sessions.json"),
            ("NOTIFY_LOG", self.home / ".claude" / "duel-vault-notifications.log"),
            ("CONFIG_PATH", self.home / ".claude" / "duel-vault.config.json"),
        ):
            patcher = mock.patch.object(sc, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env_patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.vault = self.tmp / "Vault"
        (self.vault / "Sessions").mkdir(parents=True)
        (self.vault / "CLAUDE.md").write_text("# Vault\n", encoding="utf-8")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("notebooklm", FAKE_NOTEBOOKLM), ("codex", FAKE_CODEX),
                           ("claude", FAKE_CLAUDE)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.nblm_db = self.tmp / "nblm.json"
        self.nblm_down = self.tmp / "nblm.down"
        self.codex_limit = self.tmp / "codex.limit"
        self.claude_limit = self.tmp / "claude.limit"
        self.claude_calls = self.tmp / "claude.calls"
        self.notified = self.tmp / "notified.txt"
        self.env = dict(
            os.environ,
            HOME=str(self.home),
            PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            FAKE_NBLM_DB=str(self.nblm_db),
            FAKE_NBLM_DOWN=str(self.nblm_down),
            FAKE_CODEX_LIMIT=str(self.codex_limit),
            FAKE_CLAUDE_LIMIT=str(self.claude_limit),
            FAKE_CLAUDE_CALLS=str(self.claude_calls),
            CLAUDE_CODE_SESSION_ID="claude-sess-1",
            PYTHONDONTWRITEBYTECODE="1",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, data: dict) -> None:
        (self.home / ".claude" / "duel-vault.config.json").write_text(json.dumps(data), encoding="utf-8")

    def run_script(self, script: str, *args: str, expect: int | None = 0) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        if expect is not None:
            self.assertEqual(
                proc.returncode, expect, f"{script} {' '.join(args)}\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def log(self, *args: str, expect: int | None = 0) -> subprocess.CompletedProcess:
        return self.run_script("session-log.py", *args, expect=expect)

    def state(self, *args: str, expect: int | None = 0) -> subprocess.CompletedProcess:
        return self.run_script("session-state.py", *args, expect=expect)

    def sync(self, *args: str, expect: int | None = 0) -> subprocess.CompletedProcess:
        return self.run_script("progress-sync.py", *args, expect=expect)

    def v(self, slug: str = "s1") -> list[str]:
        return ["--vault", str(self.vault), "--session", slug]

    def init_route_b(self, slug: str = "s1") -> Path:
        self.log("init", *self.v(slug), "--route", "B", "--task", "build the thing")
        folder = self.vault / "Sessions" / slug
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"00 - {slug} MOC.md").write_text(
            textwrap.dedent(
                f"""\
                ---
                project: Vault
                session: {slug}
                type: duel-vault-moc
                date: 2026-11-02
                status: in-progress
                tags: [duel-vault, duel-vault/moc]
                ---
                # MOC
                ## Phases
                - [[07-activity-log]]

                > [!decision] Use the cheap build model
                """
            ),
            encoding="utf-8",
        )
        return folder

    def journal(self, slug: str = "s1") -> list[dict]:
        return sc.load_events(self.vault, slug)


class TestParsing(unittest.TestCase):
    REF = datetime(2026, 11, 2, 10, 0, tzinfo=datetime.now().astimezone().tzinfo)

    def test_durations(self) -> None:
        self.assertEqual(sc.parse_when("in 2 days 3 hours", self.REF), self.REF + timedelta(days=2, hours=3))
        self.assertEqual(sc.parse_when("2h30m", self.REF), self.REF + timedelta(hours=2, minutes=30))
        self.assertEqual(sc.parse_when("tra 2 ore", self.REF), self.REF + timedelta(hours=2))

    def test_clock_times(self) -> None:
        self.assertEqual(sc.parse_when("3:47 PM", self.REF), self.REF.replace(hour=15, minute=47))
        self.assertEqual(sc.parse_when("17:30", self.REF), self.REF.replace(hour=17, minute=30))
        # A time already past today means tomorrow.
        self.assertEqual(sc.parse_when("9am", self.REF), self.REF.replace(hour=9) + timedelta(days=1))
        self.assertEqual(
            sc.parse_when("Nov 5th, 2026 9:12 AM", self.REF), self.REF.replace(day=5, hour=9, minute=12)
        )
        self.assertIsNone(sc.parse_when("soon", self.REF))

    def test_classify_limits(self) -> None:
        codex = sc.classify_limit(
            "You've hit your usage limit. Upgrade to Pro (https://openai.com/chatgpt/pricing) "
            "or try again in 4 days 2 hours 13 minutes.", self.REF)
        self.assertEqual(codex["kind"], "usage")
        self.assertEqual(sc.parse_iso(codex["resume_after"]),
                         self.REF + timedelta(days=4, hours=2, minutes=13))
        at = sc.classify_limit("You've hit your usage limit. Try again at 3:47 PM.", self.REF)
        self.assertEqual(sc.parse_iso(at["resume_after"]), self.REF.replace(hour=15, minute=47))
        claude = sc.classify_limit("Claude usage limit reached. Your limit will reset at 5pm (Europe/Rome).", self.REF)
        self.assertEqual(claude["kind"], "usage")
        self.assertEqual(sc.parse_iso(claude["resume_after"]).astimezone().hour,
                         datetime(2026, 11, 2, 17, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Europe/Rome")).astimezone().hour)
        rate = sc.classify_limit("stream error: exceeded retry limit, last status: 429 Too Many Requests", self.REF)
        self.assertEqual(rate["kind"], "rate")
        self.assertEqual(sc.parse_iso(rate["resume_after"]), self.REF + timedelta(minutes=2))
        unknown = sc.classify_limit("You've hit your usage limit.", self.REF)
        self.assertIsNone(unknown["resume_after"])
        self.assertIsNone(sc.classify_limit("error: file not found: plan.md", self.REF))


class TestJournal(Sandbox):
    def test_full_route_b_logging_cycle(self) -> None:
        folder = self.init_route_b()
        # init is idempotent
        out = self.log("init", *self.v(), "--route", "B").stdout
        self.assertIn("already initialised", out)

        self.state("set", *self.v(), "--phase", "build", "--step", "1", "--next", "run build step 1")
        self.log("event", *self.v(), "--kind", "decision", "--summary", "Use the cheap build model",
                 "--note", "00 - s1 MOC")
        self.log("event", *self.v(), "--kind", "activity", "--summary", "wrote build prompt",
                 "--field", "attempt=1")
        target = self.vault / "Notes" / "thing.md"
        target.parent.mkdir()
        target.write_text("v1", encoding="utf-8")
        self.log("change", *self.v(), "--file", "Notes/thing.md", "--actor", "codex", "--step", "1")
        out = self.log("change", *self.v(), "--file", str(target)).stdout
        self.assertIn("unchanged", out)
        self.log("invocation", *self.v(), "--resource", "codex", "--model", "mini", "--id", "build-1",
                 "--status", "ok", "--step", "1", "--input", "900", "--output", "300", "--total", "1200")
        self.log("invocation", *self.v(), "--resource", "claude_delegated", "--model", "haiku",
                 "--id", "verify-1", "--status", "ok", "--total", "800")
        self.log("tokens", "add", *self.v(), "--resource", "claude_main", "--total", "3000")

        kinds = [e["kind"] for e in self.journal()]
        self.assertEqual(kinds[0], "session_start")
        for kind in ("phase", "decision", "activity", "change", "invocation", "tokens"):
            self.assertIn(kind, kinds)
        self.assertEqual([e["seq"] for e in self.journal()], list(range(1, len(kinds) + 1)))
        change = next(e for e in self.journal() if e["kind"] == "change")
        self.assertEqual(change["path"], "Notes/thing.md")
        self.assertEqual(change["action"], "written")
        self.assertEqual(len(change["sha256"]), 64)

        summary = self.log("tokens", "summary", *self.v()).stdout
        self.assertIn("1,200", summary)
        self.assertIn("~3,000", summary)
        self.assertIn("`mini` 1,200", summary)

        verify = self.log("verify", *self.v()).stdout
        self.assertIn("0 error(s), 0 warning(s)", verify)

        self.log("render", *self.v())
        note = folder / "07-activity-log.md"
        self.assertTrue(note.is_file())
        text = note.read_text(encoding="utf-8")
        self.assertIn("phase: activity", text)
        self.assertIn("Use the cheap build model", text)
        lint = self.run_script("vault-lint.py", "--session", str(folder))
        self.assertIn("0 error(s)", lint.stdout)

    def test_verify_catches_logging_gaps(self) -> None:
        self.init_route_b()
        target = self.vault / "a.md"
        target.write_text("v1", encoding="utf-8")
        self.log("change", *self.v(), "--file", str(target))
        target.write_text("v2 — edited without logging", encoding="utf-8")
        self.log("event", *self.v(), "--kind", "decision", "--summary", "one", "--note", "00 - s1 MOC")
        self.log("event", *self.v(), "--kind", "decision", "--summary", "two (no callout)",
                 "--note", "00 - s1 MOC")
        self.log("invocation", *self.v(), "--resource", "codex", "--id", "b-2", "--status", "started")
        out = self.log("verify", *self.v(), "--json").stdout
        codes = {i["code"] for i in json.loads(out)["issues"]}
        self.assertTrue({"SL006", "SL008", "SL010", "SL011"} <= codes, codes)
        self.log("verify", *self.v(), "--strict", expect=1)

        # A torn journal line is an error, not a warning.
        with open(sc.journal_path(self.vault, "s1"), "a", encoding="utf-8") as fh:
            fh.write('{"seq": 99, "kind": "activ\n')
        self.log("verify", *self.v(), expect=1)

    def test_route_a_renders_a_block(self) -> None:
        self.log("init", *self.v("quick"), "--route", "A", "--task", "monthly analysis")
        self.log("render", *self.v("quick"), expect=2)
        block = self.log("render", *self.v("quick"), "--format", "block", "--heading", "Attività").stdout
        self.assertTrue(block.startswith("## Attività"))
        self.assertIn("### Timeline", block)


class TestBudgetsAndLimits(Sandbox):
    def test_budget_warn_and_over(self) -> None:
        self.config({"budgets": {"codex": 1000, "warn_ratio": 0.8}})
        self.init_route_b()
        self.log("invocation", *self.v(), "--resource", "codex", "--id", "b1", "--status", "ok",
                 "--total", "850", expect=3)
        out = self.log("invocation", *self.v(), "--resource", "codex", "--id", "b2", "--status", "ok",
                       "--total", "300", expect=4).stdout
        self.assertIn("OVER", out)
        self.state("guard", *self.v(), expect=4)

    def test_measured_figure_is_never_invented(self) -> None:
        self.init_route_b()
        proc = self.log("invocation", *self.v(), "--resource", "codex", "--id", "b1", "--status", "ok")
        self.assertIn("unavailable", proc.stderr)
        entry = sc.load_tokens(self.vault, "s1")["entries"][0]
        self.assertEqual(entry["total"], "unavailable")

    def test_limit_pause_wait_resume(self) -> None:
        self.init_route_b()
        self.state("set", *self.v(), "--phase", "build", "--step", "3", "--next", "re-run build step 3")
        # An ordinary error is not a limit.
        self.state("limit", *self.v(), "--resource", "codex", "--text", "error: no such file", expect=1)

        self.codex_limit.write_text("1")
        probe = subprocess.run(["codex", "exec", "x"], capture_output=True, text=True, env=self.env)
        err = self.tmp / "stderr.txt"
        err.write_text(probe.stderr, encoding="utf-8")
        out = self.state("limit", *self.v(), "--resource", "codex", "--stderr-file", str(err)).stdout
        self.assertIn("paused", out)
        self.assertIn("wait --vault", out)
        st = sc.load_state(self.vault, "s1")
        self.assertEqual(st["status"], "paused")
        self.assertEqual(st["blocked_on"]["kind"], "usage")
        self.assertIsNotNone(st["blocked_on"]["resume_after"])

        # Still blocked: resume refuses, and says how to wait.
        self.state("resume", *self.v(), expect=5)

        # The reset time passes but Codex is still limited: wait probes, then gives up.
        st["blocked_on"]["resume_after"] = sc.iso(sc.now() - timedelta(seconds=1))
        sc.save_state(self.vault, "s1", st)
        self.state("wait", *self.v(), "--poll", "1", "--grace", "0", "--max-wait", "2", expect=6)

        # Codex is back: the probe succeeds and the block clears.
        self.codex_limit.unlink()
        self.state("wait", *self.v(), "--poll", "1", "--grace", "0", "--max-wait", "5")
        st = sc.load_state(self.vault, "s1")
        self.assertIsNone(st["blocked_on"])
        self.assertEqual(st["blocked_history"][-1]["resource"], "codex")

        brief = self.state("resume", *self.v()).stdout
        self.assertIn("re-run build step 3", brief)
        self.assertEqual(sc.load_state(self.vault, "s1")["status"], "active")
        kinds = [e["kind"] for e in self.journal()]
        for kind in ("pause", "resource_available", "resume"):
            self.assertIn(kind, kinds)
        self.assertIn("0 error(s)", self.log("verify", *self.v()).stdout)

    def test_resume_without_slug_finds_the_open_session(self) -> None:
        self.init_route_b("old")
        self.state("set", *self.v("old"), "--status", "complete")
        self.init_route_b("new")
        self.state("set", *self.v("new"), "--next", "ask question 4")
        self.state("pause", *self.v("new"), "--resource", "claude", "--reason", "5h limit",
                   "--until", sc.iso(sc.now() - timedelta(minutes=1)))
        brief = self.state("resume", "--vault", str(self.vault)).stdout
        self.assertIn("ask question 4", brief)
        self.assertIn("`new`", brief)


class TestProgressNotebook(Sandbox):
    def notebook(self) -> dict:
        return json.loads(self.nblm_db.read_text())["notebooks"]

    def test_push_replace_queue_pull_restore(self) -> None:
        self.init_route_b()
        self.state("set", *self.v(), "--phase", "planning", "--next", "send round 2 to codex")

        self.sync("push", "--vault", str(self.vault), "--session", "s1")
        notebooks = self.notebook()
        self.assertEqual(len(notebooks), 1)
        (nb_id, nb), = notebooks.items()
        self.assertEqual(nb["title"], "Vault — avanzamento duel-vault")
        self.assertEqual(len(nb["sources"]), 1)
        self.assertIn("send round 2 to codex", next(iter(nb["sources"].values()))["text"])

        calls = len(json.loads(self.nblm_db.read_text())["calls"])
        out = self.sync("push", "--vault", str(self.vault)).stdout
        self.assertIn("unchanged", out)
        self.assertEqual(len(json.loads(self.nblm_db.read_text())["calls"]), calls)

        # The state source is replaced, never accumulated.
        self.state("set", *self.v(), "--next", "apply codex critique 3")
        self.sync("push", "--vault", str(self.vault), "--session", "s1")
        sources = self.notebook()[nb_id]["sources"]
        self.assertEqual(len(sources), 1)
        self.assertIn("apply codex critique 3", next(iter(sources.values()))["text"])

        # NotebookLM down: queued, never blocking, and verify notices.
        self.state("set", *self.v(), "--next", "checkpoint 2")
        self.nblm_down.write_text("1")
        self.sync("push", "--vault", str(self.vault), "--session", "s1", expect=3)
        meta = json.loads((sc.progress_dir(self.vault) / "notebook.json").read_text())
        self.assertTrue(meta["pending_push"])
        self.assertIn("SL012", self.log("verify", *self.v()).stdout)
        self.nblm_down.unlink()
        self.sync("push", "--vault", str(self.vault), "--session", "s1")
        meta = json.loads((sc.progress_dir(self.vault) / "notebook.json").read_text())
        self.assertFalse(meta["pending_push"])

        # Another machine: the working dir is gone; pull --restore rebuilds the open session.
        shutil.rmtree(sc.session_dir(self.vault, "s1"))
        (sc.progress_dir(self.vault) / "notebook.json").unlink()
        self.sync("pull", "--vault", str(self.vault), "--project", "Vault", "--restore")
        restored = sc.load_state(self.vault, "s1")
        self.assertEqual(restored["next_action"], "checkpoint 2")
        self.assertEqual(restored["phase"], "planning")
        self.assertTrue(self.journal()[0].get("restored"))
        self.assertIn("checkpoint 2", self.state("resume", *self.v(), "--check").stdout)

    def test_closed_session_adds_a_summary_source(self) -> None:
        self.init_route_b()
        self.sync("push", "--vault", str(self.vault), "--session", "s1")
        self.state("set", *self.v(), "--status", "complete")
        self.sync("push", "--vault", str(self.vault), "--session", "s1")
        (nb,) = self.notebook().values()
        titles = sorted(s["title"] for s in nb["sources"].values())
        self.assertEqual(titles, ["duel-vault-session-s1.md", "duel-vault-state-vault.md"])

    def test_disabled_writes_locally_only(self) -> None:
        self.config({"progress_notebook": {"enabled": False}})
        self.init_route_b()
        out = self.sync("push", "--vault", str(self.vault)).stdout
        self.assertIn("disabled", out)
        self.assertFalse(self.nblm_db.exists())


class TestLint(Sandbox):
    def test_paused_moc_and_activity_note_lint_clean(self) -> None:
        folder = self.init_route_b()
        moc = folder / "00 - s1 MOC.md"
        moc.write_text(moc.read_text().replace("status: in-progress", "status: paused"))
        self.log("event", *self.v(), "--kind", "decision", "--summary", "x", "--note", "00 - s1 MOC")
        self.log("render", *self.v())
        lint = self.run_script("vault-lint.py", "--session", str(folder), "--strict")
        self.assertIn("0 error(s), 0 warning(s)", lint.stdout)


class TestRegressions(Sandbox):
    """One test per defect found in review — each failed before its fix."""

    def test_pre_journal_session_is_not_open_until_upgraded(self) -> None:
        legacy = sc.session_dir(self.vault, "old")
        legacy.mkdir(parents=True)
        (legacy / "session.json").write_text(json.dumps({"token_capture": "stdout", "phase": "build"}))
        (legacy / "tokens.json").write_text(json.dumps({"build": [{"invocation": "b1", "total": 500}]}))
        self.assertIn("no sessions open", self.state("list", "--vault", str(self.vault), "--open").stdout)
        self.assertIn("no open session", self.state("resume", "--vault", str(self.vault)).stdout)
        proc = self.log("event", *self.v("old"), "--kind", "activity", "--summary", "x", expect=2)
        self.assertIn("pre-journal", proc.stderr)
        self.assertIn("SL006", self.log("verify", *self.v("old"), expect=1).stdout)

        out = self.log("init", *self.v("old"), "--route", "B", "--task", "finish it").stdout
        self.assertIn("upgraded", out)
        st = sc.load_state(self.vault, "old")
        self.assertEqual((st["status"], st["phase"], st["token_capture"]), ("active", "build", "stdout"))
        self.assertNotIn("legacy", st)
        self.assertEqual(sc.load_tokens(self.vault, "old")["entries"][0]["total"], 500)
        self.state("set", *self.v("old"), "--next", "re-run the build")
        self.assertIn("0 error(s)", self.log("verify", *self.v("old")).stdout)

    def test_reserved_field_names_are_refused_not_crashing(self) -> None:
        self.init_route_b()
        for field in ("phase=x", "actor=y", "step=3", "note=z", "seq=9"):
            proc = self.log("event", *self.v(), "--kind", "activity", "--summary", "s",
                            "--field", field, expect=2)
            self.assertNotIn("Traceback", proc.stderr)
        proc = self.state("set", *self.v(), "--field", "status=complete", expect=2)
        self.assertIn("managed", proc.stderr)
        self.state("set", *self.v(), "--field", "token_capture=json")
        self.assertEqual(sc.load_state(self.vault, "s1")["token_capture"], "json")

    def test_change_resolves_against_the_vault_and_never_infers_deletion(self) -> None:
        self.init_route_b()
        self.state("set", *self.v(), "--next", "n")
        (self.vault / "out").mkdir()
        (self.vault / "out" / "a.md").write_text("x")
        self.log("change", *self.v(), "--file", "out/a.md")  # relative to the vault, not cwd
        self.log("change", *self.v(), "--file", "out/missing.md", expect=2)
        kinds = [(e.get("path"), e.get("action")) for e in self.journal() if e["kind"] == "change"]
        self.assertEqual(kinds, [("out/a.md", "written")])
        (self.vault / "out" / "a.md").unlink()
        self.log("change", *self.v(), "--file", "out/a.md", "--action", "deleted")
        project = self.tmp / "proj"
        project.mkdir()
        (project / "main.py").write_text("print(1)")
        self.log("change", *self.v(), "--base", str(project), "--file", "main.py")
        paths = [e.get("path") for e in self.journal() if e["kind"] == "change"]
        self.assertEqual(paths[-1], str((project / "main.py").resolve()))
        self.assertIn("0 warning(s)", self.log("verify", *self.v()).stdout)

    def test_abandoned_and_reopened_sessions_verify_clean(self) -> None:
        self.init_route_b("gone")
        self.state("set", *self.v("gone"), "--status", "abandoned", "--reason", "superseded")
        self.assertIn("0 error(s)", self.log("verify", *self.v("gone")).stdout)

        self.init_route_b()
        self.state("set", *self.v(), "--status", "complete")
        self.assertIn("0 error(s)", self.log("verify", *self.v()).stdout)
        self.state("set", *self.v(), "--status", "active", "--next", "fix the finding")
        self.assertIn("0 error(s)", self.log("verify", *self.v()).stdout)
        self.state("set", *self.v(), "--status", "complete")
        self.assertIn("0 error(s)", self.log("verify", *self.v()).stdout)

    def test_project_output_is_not_mistaken_for_a_limit(self) -> None:
        self.init_route_b()
        for text in (
            'File "app.py", line 429, in handler\nKeyError: user',
            "implemented the rate limiter; tests: rate limit window ok",
            "RecursionError: maximum recursion limit reached",
            "error: overloaded function call is ambiguous",
        ):
            self.state("limit", *self.v(), "--resource", "codex", "--text", text, expect=1)
        self.assertEqual(sc.load_state(self.vault, "s1")["status"], "active")
        noisy = "\n".join(["test rate limit exceeded? no"] + ["log line"] * 60 +
                          ["You've hit your usage limit. Try again in 3 hours."])
        self.state("limit", *self.v(), "--resource", "codex", "--text", noisy)
        self.assertEqual(sc.load_state(self.vault, "s1")["blocked_on"]["kind"], "usage")

    def test_wait_stops_on_a_probe_error_instead_of_polling_for_days(self) -> None:
        self.init_route_b()
        self.state("set", *self.v(), "--next", "n")
        self.state("limit", *self.v(), "--resource", "codex", "--text", "You've hit your usage limit.")
        proc = self.state("wait", *self.v(), "--poll", "1", "--max-wait", "600",
                          "--probe-cmd", f"{sys.executable} -c \"import sys; sys.exit('unknown flag --x')\"",
                          expect=1)
        self.assertIn("not a limit", proc.stdout)
        self.assertEqual(sc.load_state(self.vault, "s1")["status"], "paused")

    def test_wait_leaves_a_newer_block_alone(self) -> None:
        self.init_route_b()
        self.state("set", *self.v(), "--next", "n")
        self.state("limit", *self.v(), "--resource", "codex", "--text", "You've hit your usage limit.")
        waiter = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "session-state.py"), "wait", *self.v(), "--poll", "1",
             "--probe-cmd", f"{sys.executable} -c \"import time; time.sleep(2)\""],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        self.state("resume", *self.v(), "--force")
        self.state("pause", *self.v(), "--resource", "user", "--reason", "lunch")
        out, _err = waiter.communicate(timeout=30)
        self.assertEqual(waiter.returncode, 0, out)
        self.assertIn("left as is", out)
        self.assertEqual(sc.load_state(self.vault, "s1")["blocked_on"]["resource"], "user")

    def test_resume_rules_per_resource(self) -> None:
        self.config({"budgets": {"codex": 100}})
        self.init_route_b()
        self.state("set", *self.v(), "--next", "n")
        self.state("pause", *self.v(), "--resource", "claude", "--reason", "5h limit")
        self.state("resume", *self.v())  # Claude is running this: it is back
        self.state("pause", *self.v(), "--resource", "user", "--reason", "later")
        self.state("resume", *self.v())
        self.log("invocation", *self.v(), "--resource", "codex", "--id", "b1", "--status", "ok",
                 "--total", "150", expect=4)
        self.state("pause", *self.v(), "--resource", "budget", "--reason", "over")
        self.state("resume", *self.v(), expect=5)
        self.state("set", *self.v(), "--budget", "codex=1000")
        self.state("resume", *self.v())
        self.state("limit", *self.v(), "--resource", "codex", "--text", "You've hit your usage limit.")
        self.state("resume", *self.v(), expect=5)  # unknown reset: not before a probe or --force

    def test_rewritten_notes_keep_their_permissions(self) -> None:
        folder = self.init_route_b()
        self.log("render", *self.v())
        note = folder / "07-activity-log.md"
        self.assertEqual(note.stat().st_mode & 0o777, 0o666 & ~self._umask())
        note.chmod(0o640)
        self.log("render", *self.v())
        self.assertEqual(note.stat().st_mode & 0o777, 0o640)

    @staticmethod
    def _umask() -> int:
        current = os.umask(0)
        os.umask(current)
        return current

    def test_tokens_add_accepts_the_natural_argument_order(self) -> None:
        self.init_route_b()
        self.log("tokens", "add", "--vault", str(self.vault), "--session", "s1",
                 "--resource", "claude_main", "--total", "10")

    def test_snapshot_keeps_stderr_local_and_survives_stripped_markup(self) -> None:
        self.init_route_b()
        self.state("set", *self.v(), "--next", "n")
        self.state("limit", *self.v(), "--resource", "codex",
                   "--text", "secret-token-123 You've hit your usage limit. Try again in 1 hour.")
        self.sync("render", "--vault", str(self.vault))
        snapshot = next(sc.progress_dir(self.vault).glob("duel-vault-state-*.md")).read_text()
        self.assertNotIn("secret-token-123", snapshot)
        self.assertIn("secret-token-123", json.dumps(sc.load_state(self.vault, "s1")))
        sys.path.insert(0, str(SCRIPTS))
        import importlib.util
        spec = importlib.util.spec_from_file_location("progress_sync", SCRIPTS / "progress-sync.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        stripped = snapshot.replace(mod.STATE_MARKER, "").replace("```json", "").replace("```", "")
        block = mod.extract_state_block(stripped)
        self.assertEqual(block["open_sessions"][0]["session"], "s1")

    def test_a_failed_summary_push_is_caught_up_later(self) -> None:
        self.init_route_b()
        self.state("set", *self.v(), "--status", "complete")
        self.nblm_down.write_text("1")
        self.sync("push", "--vault", str(self.vault), "--session", "s1", expect=3)
        self.nblm_down.unlink()
        self.init_route_b("s2")
        self.state("set", *self.v("s2"), "--next", "n")
        self.sync("push", "--vault", str(self.vault), "--session", "s2")
        (nb,) = json.loads(self.nblm_db.read_text())["notebooks"].values()
        titles = sorted(s["title"] for s in nb["sources"].values())
        self.assertIn("duel-vault-session-s1.md", titles)


class TestClaudeLimitNotification(Sandbox):
    """Decision: notify the user when Claude's limit resets, on any OS, via a hook."""

    def setUp(self) -> None:
        super().setUp()
        recorder = self.bin / "record-notification"
        recorder.write_text(
            f"#!{sys.executable}\nimport sys\n"
            f"open({str(self.notified)!r}, 'a').write(sys.argv[1] + ' | ' + sys.argv[2] + '\\n')\n"
        )
        recorder.chmod(recorder.stat().st_mode | stat.S_IEXEC)
        self.config({"notify": {"command": [str(recorder), "{title}", "{message}"], "poll_seconds": 1}})

    def hook(self, payload: dict, extra_env: dict | None = None) -> None:
        env = dict(self.env, **(extra_env or {}))
        proc = subprocess.run([sys.executable, str(SCRIPTS / "session-state.py"), "on-stop-failure"],
                              input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def wait_for(self, predicate, seconds: float = 20.0) -> bool:
        import time
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def start(self) -> None:
        self.init_route_b()
        self.state("set", *self.v(), "--phase", "build", "--next", "verify step 2")

    def test_hook_pauses_and_notifies_at_the_reset_time(self) -> None:
        self.start()
        reset = sc.iso(sc.now() + timedelta(seconds=2))
        self.hook({"session_id": "claude-sess-1", "error": "rate_limit",
                   "last_assistant_message": f"Claude usage limit reached. Your limit will reset at {reset}"})
        st = sc.load_state(self.vault, "s1")
        self.assertEqual((st["status"], st["blocked_on"]["resource"]), ("paused", "claude"))
        self.assertEqual(st["blocked_on"]["resume_after"], reset)
        self.assertFalse(self.notified.exists())  # not before the reset
        self.assertTrue(self.wait_for(self.notified.exists), "no notification")
        text = self.notified.read_text()
        self.assertIn("duel-vault · Vault", text)
        self.assertIn("verify step 2", text)
        kinds = [e["kind"] for e in self.journal()]
        self.assertIn("pause", kinds)
        self.assertTrue(self.wait_for(lambda: [e for e in self.journal() if e["kind"] == "notification"
                                               and "notified" in e["summary"]]))
        # Claude is back (it is running resume): no --force needed.
        self.assertIn("verify step 2", self.state("resume", *self.v()).stdout)

    def test_no_notification_if_the_user_already_resumed(self) -> None:
        self.start()
        reset = sc.iso(sc.now() + timedelta(seconds=2))
        self.hook({"session_id": "claude-sess-1", "error": "rate_limit",
                   "last_assistant_message": f"resets {reset}"})
        self.state("resume", *self.v())
        import time
        time.sleep(4)
        self.assertFalse(self.notified.exists())

    def test_unknown_reset_time_probes_claude_until_it_answers(self) -> None:
        self.start()
        self.claude_limit.write_text("1")
        self.hook({"session_id": "claude-sess-1", "error": "rate_limit",
                   "last_assistant_message": "API Error: Rate limit reached"})
        self.assertIsNone(sc.load_state(self.vault, "s1")["blocked_on"]["resume_after"])
        self.assertTrue(self.wait_for(lambda: self.claude_calls.exists()
                                      and len(self.claude_calls.read_text().splitlines()) >= 2))
        self.assertFalse(self.notified.exists())
        # every probe runs with the recursion guard set
        self.assertEqual(set(self.claude_calls.read_text().split()), {"1"})
        self.claude_limit.unlink()
        self.assertTrue(self.wait_for(self.notified.exists), "no notification after Claude came back")

    def test_hook_ignores_what_is_not_ours(self) -> None:
        self.start()
        self.hook({"session_id": "another-session", "error": "rate_limit", "last_assistant_message": "x"})
        self.hook({"session_id": "claude-sess-1", "error": "server_error", "last_assistant_message": "x"})
        self.hook({"session_id": "claude-sess-1", "error": "rate_limit", "last_assistant_message": "x"},
                  extra_env={"DUEL_VAULT_PROBE": "1"})
        self.hook({})
        self.assertEqual(sc.load_state(self.vault, "s1")["status"], "active")

    def test_wait_never_probes_claude_itself(self) -> None:
        self.start()
        self.state("pause", *self.v(), "--resource", "claude", "--reason", "limit",
                   "--until", sc.iso(sc.now() - timedelta(seconds=1)))
        self.state("wait", *self.v(), "--grace", "0")
        self.assertFalse(self.claude_calls.exists())

    def test_install_hook_is_idempotent_and_preserves_settings(self) -> None:
        settings = self.tmp / "settings.json"
        settings.write_text(json.dumps({"theme": "dark", "hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "echo stop"}]}]}}))
        self.state("install-hook", "--settings", str(settings))
        out = self.state("install-hook", "--settings", str(settings)).stdout
        self.assertIn("updated", out)
        data = json.loads(settings.read_text())
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "echo stop")
        (group,) = data["hooks"]["StopFailure"]
        self.assertEqual(group["matcher"], "rate_limit")
        self.assertIn("on-stop-failure", group["hooks"][0]["command"])
        self.state("install-hook", "--settings", str(settings), "--uninstall")
        data = json.loads(settings.read_text())
        self.assertNotIn("StopFailure", data["hooks"])
        settings.write_text("{not json")
        self.state("install-hook", "--settings", str(settings), expect=2)
        self.assertEqual(settings.read_text(), "{not json")


class TestProjectBudgets(Sandbox):
    """Decision: budgets are asked at a project's first Route B and saved per project."""

    def test_budgets_per_project(self) -> None:
        out = self.state("budgets", "--project", "Vault").stdout
        self.assertIn("NOT configured", out)
        self.state("budgets", "--project", "Vault", "--set", "codex=1000", "--set", "claude_main=500")
        self.assertIn("configured", self.state("budgets", "--project", "Vault").stdout)
        self.state("budgets", "--project", "Other", "--track-only")
        self.assertNotIn("NOT", self.state("budgets", "--project", "Other").stdout)
        cfg = json.loads((self.home / ".claude" / "duel-vault.config.json").read_text())
        self.assertEqual(cfg["budgets_by_project"]["Other"]["codex"], None)

        self.init_route_b()  # project defaults to the vault name: "Vault"
        self.assertEqual(sc.load_state(self.vault, "s1")["budgets"]["codex"], 1000)
        self.log("invocation", *self.v(), "--resource", "codex", "--id", "b", "--status", "ok",
                 "--total", "900", expect=3)


if __name__ == "__main__":
    unittest.main()
