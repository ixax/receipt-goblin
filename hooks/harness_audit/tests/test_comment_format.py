#!/usr/bin/env python3
"""Unit tests for hooks/harness_audit/comment_format.py and
comment_format_hook.py. Stdlib unittest only.

Run: python3 -m unittest discover -s hooks/harness_audit/tests
"""
import importlib
import io
import json
import sys
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOK_DIR))
comment_format = importlib.import_module("comment_format")
comment_format_hook = importlib.import_module("comment_format_hook")


class TestCheckText(unittest.TestCase):
    def test_multi_sentence_hash_comment_flagged(self):
        text = "# This is one sentence. This is a second sentence.\nx = 1\n"
        self.assertTrue(comment_format.check_text(text, is_py=True))

    def test_single_sentence_comments_clean(self):
        text = "# One sentence.\n# Another sentence.\nx = 1\n"
        self.assertEqual(comment_format.check_text(text, is_py=True), [])

    def test_shebang_not_flagged(self):
        text = "#!/usr/bin/env python3. Not a real sentence break here\n"
        self.assertEqual(comment_format.check_text(text, is_py=True), [])

    def test_multiline_docstring_flagged(self):
        text = '"""One sentence here.\nTwo sentences crammed together. Like this one.\n"""\n'
        self.assertTrue(comment_format.check_text(text, is_py=True))

    def test_single_line_docstring_flagged(self):
        text = '"""One sentence. Two sentence.\n"""\n'
        self.assertTrue(comment_format.check_text(text, is_py=True))

    def test_yaml_comment_flagged(self):
        text = "# One sentence here. Two sentence here.\nkey: value\n"
        self.assertTrue(comment_format.check_text(text, is_py=False))

    def test_code_not_flagged(self):
        text = 'x = "a. B"\ny = re.compile(r"foo. Bar")\n'
        self.assertEqual(comment_format.check_text(text, is_py=True), [])


class TestHook(unittest.TestCase):
    def run_hook(self, payload):
        buf_in = io.StringIO(json.dumps(payload))
        old_stdin = sys.stdin
        sys.stdin = buf_in
        try:
            return comment_format_hook.main()
        finally:
            sys.stdin = old_stdin

    def test_edit_violation_blocks(self):
        code = self.run_hook({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "x.py",
                "new_string": "# One sentence. Two sentence.\n",
            },
        })
        self.assertEqual(code, 2)

    def test_edit_clean_passes(self):
        code = self.run_hook({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "x.py",
                "new_string": "# One sentence.\n",
            },
        })
        self.assertEqual(code, 0)

    def test_non_target_extension_skipped(self):
        code = self.run_hook({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "README.md",
                "new_string": "# One sentence. Two sentence.\n",
            },
        })
        self.assertEqual(code, 0)

    def test_write_checks_full_content(self):
        code = self.run_hook({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "x.yml",
                "content": "# One sentence. Two sentence.\nkey: value\n",
            },
        })
        self.assertEqual(code, 2)

    def test_other_tool_ignored(self):
        code = self.run_hook({
            "tool_name": "Read",
            "tool_input": {"file_path": "x.py"},
        })
        self.assertEqual(code, 0)

    def test_plans_dir_violation_skipped(self):
        code = self.run_hook({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "plans/scratch.py",
                "new_string": "# One sentence. Two sentence.\n",
            },
        })
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
