#!/usr/bin/env python3
"""Unit tests for hooks/harness_audit/md_format_bash_gate.py.
Stdlib unittest only.

Run: python3 -m unittest discover -s hooks/harness_audit/tests
"""
import importlib
import json
import sys
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOK_DIR))
bash_gate = importlib.import_module("md_format_bash_gate")

SKILL_READ_ENTRY = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "md-format"}}]},
}


class TestHeredocBody(unittest.TestCase):
    def test_extracts_body_up_to_terminator(self):
        command = "cat > x.md <<EOF\nline one\nline two\nEOF\n"
        self.assertEqual(bash_gate.heredoc_body(command), "\nline one\nline two")

    def test_no_heredoc_returns_empty(self):
        self.assertEqual(bash_gate.heredoc_body("echo hi > x.md"), "")


class TestQualifies(unittest.TestCase):
    def test_md_redirect_qualifies(self):
        self.assertEqual(bash_gate.qualifies("echo hi > README.md"), "README.md")

    def test_tee_target_qualifies(self):
        self.assertEqual(bash_gate.qualifies("echo hi | tee README.md"), "README.md")

    def test_py_heredoc_with_multiline_comment_qualifies(self):
        command = 'cat > x.py <<EOF\n"""First sentence.\nSecond sentence.\n"""\nEOF\n'
        self.assertEqual(bash_gate.qualifies(command), "x.py")

    def test_py_heredoc_without_comment_block_does_not_qualify(self):
        command = "cat > x.py <<EOF\nx = 1\nEOF\n"
        self.assertEqual(bash_gate.qualifies(command), "")

    def test_no_redirect_target_does_not_qualify(self):
        self.assertEqual(bash_gate.qualifies("ls -la"), "")

    def test_plans_dir_md_redirect_does_not_qualify(self):
        self.assertEqual(bash_gate.qualifies("echo hi > plans/my-plan.md"), "")

    def test_plans_dir_tee_does_not_qualify(self):
        self.assertEqual(bash_gate.qualifies("echo hi | tee plans/features/sub-plan.md"), "")


class TestMain(unittest.TestCase):
    def _run(self, command, transcript_lines):
        import io
        import tempfile
        from contextlib import redirect_stdout

        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        for entry in transcript_lines:
            f.write(json.dumps(entry) + "\n")
        f.close()

        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "transcript_path": f.name,
        }
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                bash_gate.main()
        finally:
            sys.stdin = old_stdin
        return out.getvalue()

    def test_denies_when_unread(self):
        out = self._run("echo hi > README.md", [])
        self.assertIn('"permissionDecision": "deny"', out)

    def test_allows_when_already_read(self):
        out = self._run("echo hi > README.md", [SKILL_READ_ENTRY])
        self.assertEqual(out, "")

    def test_allows_plans_dir_write_even_when_unread(self):
        out = self._run("echo hi > plans/my-plan.md", [])
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
