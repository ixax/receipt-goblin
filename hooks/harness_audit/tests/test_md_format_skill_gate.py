#!/usr/bin/env python3
"""Unit tests for hooks/harness_audit/md_format_skill_gate.py.
Stdlib unittest only.

Run: python3 -m unittest discover -s hooks/harness_audit/tests
"""
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOK_DIR))
gate = importlib.import_module("md_format_skill_gate")


def write_transcript(lines) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    for entry in lines:
        f.write(json.dumps(entry) + "\n")
    f.close()
    return f.name


SKILL_READ_ENTRY = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "md-format"}}]},
}
SKILL_FILE_READ_ENTRY = {
    "type": "assistant",
    "message": {
        "content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": ".claude/skills/md-format/SKILL.md"}}
        ]
    },
}
OTHER_TOOL_ENTRY = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]},
}


class TestAlreadyRead(unittest.TestCase):
    def test_true_when_skill_invoked(self):
        path = write_transcript([OTHER_TOOL_ENTRY, SKILL_READ_ENTRY])
        self.assertTrue(gate.already_read(path))

    def test_true_when_skill_file_read_directly(self):
        # Subagents without the Skill tool (most of .claude/agents/*.md)
        # satisfy the gate by Reading SKILL.md directly instead.
        path = write_transcript([OTHER_TOOL_ENTRY, SKILL_FILE_READ_ENTRY])
        self.assertTrue(gate.already_read(path))

    def test_true_for_absolute_skill_file_path(self):
        entry = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/repo/.claude/skills/md-format/SKILL.md"},
                    }
                ]
            },
        }
        path = write_transcript([entry])
        self.assertTrue(gate.already_read(path))

    def test_false_when_skill_not_invoked(self):
        path = write_transcript([OTHER_TOOL_ENTRY])
        self.assertFalse(gate.already_read(path))

    def test_false_for_missing_file(self):
        self.assertFalse(gate.already_read("/nonexistent/path.jsonl"))

    def test_false_for_different_skill(self):
        entry = {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "dataviz"}}]},
        }
        path = write_transcript([entry])
        self.assertFalse(gate.already_read(path))

    def test_false_for_reading_unrelated_md_file(self):
        entry = {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]},
        }
        path = write_transcript([entry])
        self.assertFalse(gate.already_read(path))

    def test_true_when_only_subagent_transcript_has_the_read(self):
        # Reproduces the bug where a Task-spawned subagent's own Skill/Read
        # call landed only in its own <session>/subagents/agent-<id>.jsonl
        # file, never in the main <session-id>.jsonl the hook was given as
        # transcript_path - so a subagent could read the skill any number
        # of times and still get denied forever.
        tmp_dir = Path(tempfile.mkdtemp())
        main_path = tmp_dir / "session123.jsonl"
        main_path.write_text(json.dumps(OTHER_TOOL_ENTRY) + "\n")
        subagents_dir = tmp_dir / "session123" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "agent-abc123.jsonl").write_text(json.dumps(SKILL_READ_ENTRY) + "\n")
        self.assertTrue(gate.already_read(str(main_path)))

    def test_false_when_no_subagent_dir_exists(self):
        path = write_transcript([OTHER_TOOL_ENTRY])
        self.assertFalse(gate.already_read(path))


class TestQualifies(unittest.TestCase):
    def test_md_file_always_qualifies(self):
        self.assertTrue(gate.qualifies("README.md", "trivial"))

    def test_py_file_single_line_comment_does_not_qualify(self):
        text = "# one short comment\nx = 1\n"
        self.assertFalse(gate.qualifies("x.py", text))

    def test_py_file_multiline_docstring_qualifies(self):
        text = '"""First sentence.\nSecond sentence.\n"""\n'
        self.assertTrue(gate.qualifies("x.py", text))

    def test_unrelated_extension_does_not_qualify(self):
        self.assertFalse(gate.qualifies("x.json", "{}"))


class TestMain(unittest.TestCase):
    def _run(self, tool_name, file_path, text, transcript_lines):
        payload = {
            "tool_name": tool_name,
            "tool_input": (
                {"file_path": file_path, "new_string": text}
                if tool_name == "Edit"
                else {"file_path": file_path, "content": text}
            ),
            "transcript_path": write_transcript(transcript_lines),
        }
        import io
        from contextlib import redirect_stdout

        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                gate.main()
        finally:
            sys.stdin = old_stdin
        return out.getvalue()

    def test_denies_when_unread(self):
        out = self._run("Write", "README.md", "content", [])
        self.assertIn('"permissionDecision": "deny"', out)

    def test_allows_when_already_read(self):
        out = self._run("Write", "README.md", "content", [SKILL_READ_ENTRY])
        self.assertEqual(out, "")

    def test_allows_non_qualifying_file(self):
        out = self._run("Write", "x.py", "# one short comment\n", [])
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
