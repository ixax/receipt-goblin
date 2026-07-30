#!/usr/bin/env python3
"""Unit tests for hooks/harness_audit/audit_hook.py. Stdlib unittest only.

Run: python3 -m unittest discover -s hooks/harness_audit/tests
"""
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOK_DIR))
audit_hook = importlib.import_module("audit_hook")


class TestIsHarnessPath(unittest.TestCase):
    def test_claude_path_matches(self):
        self.assertTrue(audit_hook.is_harness_path(".claude/agents/x.md"))

    def test_codex_path_matches(self):
        self.assertTrue(audit_hook.is_harness_path(".codex/skills/x/SKILL.md"))

    def test_root_agents_md_matches(self):
        self.assertTrue(audit_hook.is_harness_path("AGENTS.md"))

    def test_root_claude_md_matches(self):
        self.assertTrue(audit_hook.is_harness_path("CLAUDE.md"))

    def test_nested_agents_md_matches(self):
        self.assertTrue(audit_hook.is_harness_path(os.path.join("services", "webhook", "AGENTS.md")))

    def test_agent_docs_path_matches(self):
        self.assertTrue(audit_hook.is_harness_path(os.path.join("agent_docs", "architecture.md")))

    def test_unrelated_path_does_not_match(self):
        self.assertFalse(audit_hook.is_harness_path(os.path.join("services", "webhook", "src", "server.py")))

    def test_readme_does_not_match(self):
        self.assertFalse(audit_hook.is_harness_path("README.md"))


class TestMainEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def run_hook(self, file_path: str):
        payload = json.dumps({"tool_input": {"file_path": file_path}})
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            return audit_hook.main()
        finally:
            sys.stdin = old_stdin

    def test_non_harness_edit_exits_zero_without_running_audit(self):
        code = self.run_hook(str(self.root / "README.md"))
        self.assertEqual(code, 0)

    def test_harness_edit_within_budget_exits_zero(self):
        agents_md = self.root / "AGENTS.md"
        agents_md.write_text("# Project\n\nShort.\n", encoding="utf-8")
        code = self.run_hook(str(agents_md))
        self.assertEqual(code, 0)

    def test_harness_edit_over_budget_exits_two(self):
        agents_md = self.root / "AGENTS.md"
        agents_md.write_text("x " * 20000, encoding="utf-8")
        code = self.run_hook(str(agents_md))
        self.assertEqual(code, 2)

    def test_codex_skill_edit_over_budget_exits_two(self):
        skill = self.root / ".codex" / "skills" / "x" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"line {i}" for i in range(600))
        skill.write_text(f"---\nname: x\ndescription: d\n---\n{body}\n", encoding="utf-8")
        code = self.run_hook(str(skill))
        self.assertEqual(code, 2)

    def test_preexisting_violation_in_other_file_does_not_block(self):
        """A pre-existing md-format violation in an unrelated file must not
        block an edit to a different, clean harness file - only violations
        in the file just edited (or genuinely cross-file kinds) should."""
        dirty = self.root / ".claude" / "agents" / "dirty.md"
        dirty.parent.mkdir(parents=True, exist_ok=True)
        dirty.write_text(
            "---\nname: dirty\ndescription: d\n---\n"
            "One sentence here. Two sentences crammed together.\n",
            encoding="utf-8",
        )
        clean = self.root / ".claude" / "agents" / "clean.md"
        clean.write_text("---\nname: clean\ndescription: d\n---\nOne sentence.\n", encoding="utf-8")
        code = self.run_hook(str(clean))
        self.assertEqual(code, 0)

    def test_own_violation_still_blocks(self):
        skill = self.root / ".claude" / "skills" / "x" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(
            "---\nname: x\ndescription: d\n---\n"
            "One sentence here. Two sentences crammed together.\n",
            encoding="utf-8",
        )
        code = self.run_hook(str(skill))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
