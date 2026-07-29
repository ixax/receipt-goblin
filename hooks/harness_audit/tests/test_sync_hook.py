#!/usr/bin/env python3
"""Unit tests for hooks/harness_audit/sync_hook.py. Stdlib unittest only.

Run: python3 -m unittest discover -s hooks/harness_audit/tests
"""
import importlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOK_DIR))
sync_hook = importlib.import_module("sync_hook")


class TestIsIndexSource(unittest.TestCase):
    def test_skill_md_matches(self):
        self.assertTrue(sync_hook.is_index_source(os.path.join(".claude", "skills", "x", "SKILL.md")))

    def test_codex_skill_md_matches(self):
        self.assertTrue(sync_hook.is_index_source(os.path.join(".codex", "skills", "x", "SKILL.md")))

    def test_agent_md_matches(self):
        self.assertTrue(sync_hook.is_index_source(os.path.join(".claude", "agents", "x.md")))

    def test_unrelated_claude_md_does_not_match(self):
        self.assertFalse(sync_hook.is_index_source(os.path.join(".claude", "rules", "x.md")))

    def test_agents_md_does_not_match(self):
        self.assertFalse(sync_hook.is_index_source("AGENTS.md"))

    def test_unrelated_source_file_does_not_match(self):
        self.assertFalse(sync_hook.is_index_source(os.path.join("services", "webhook", "src", "server.py")))


class TestMainEndToEnd(unittest.TestCase):
    """Patches SYNC_HARNESS_SCRIPT to a fake script - never invokes the real
    scripts/sync_harness.py, so these tests never touch this repo's own
    agent_docs/harness-index.md."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_script = sync_hook.SYNC_HARNESS_SCRIPT
        self._old_cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        sync_hook.SYNC_HARNESS_SCRIPT = self._old_script
        self._tmp.cleanup()

    def write_fake_script(self, exit_code: int, stderr: str = "") -> Path:
        script = self.root / "fake_sync.py"
        script.write_text(
            "import sys\n"
            f"sys.stderr.write({stderr!r})\n"
            f"sys.exit({exit_code})\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        sync_hook.SYNC_HARNESS_SCRIPT = script
        return script

    def run_hook(self, file_path: str):
        payload = json.dumps({"tool_input": {"file_path": file_path}})
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            return sync_hook.main()
        finally:
            sys.stdin = old_stdin

    def test_non_index_source_exits_zero_without_invoking_script(self):
        self.write_fake_script(exit_code=1)  # would fail if invoked
        code = self.run_hook(str(self.root / "README.md"))
        self.assertEqual(code, 0)

    def test_skill_edit_invokes_script_success(self):
        self.write_fake_script(exit_code=0)
        skill = self.root / ".claude" / "skills" / "x" / "SKILL.md"
        code = self.run_hook(str(skill))
        self.assertEqual(code, 0)

    def test_agent_edit_invokes_script_failure(self):
        self.write_fake_script(exit_code=1, stderr="boom")
        agent = self.root / ".claude" / "agents" / "x.md"
        code = self.run_hook(str(agent))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
