#!/usr/bin/env python3
"""Unit tests for hooks/harness_audit/comment_format.py and
comment_format_hook.py. Stdlib unittest only.

Run: python3 -m unittest discover -s hooks/harness_audit/tests
"""
import importlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
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

    def test_sentence_end_before_abbreviation_flagged(self):
        # Previously missed: the char before the period ("M" in "OOM") is
        # uppercase, so the old [a-z0-9`)\]] class never matched it.
        text = "# System ran out of memory (OOM. Second attempt failed too\nx = 1\n"
        self.assertTrue(comment_format.check_text(text, is_py=True))

    def test_eg_abbreviation_not_flagged(self):
        # Previously a false positive: "e.g. Foo" was flagged as a
        # sentence end.
        text = "# See e.g. Foo for context\nx = 1\n"
        self.assertEqual(comment_format.check_text(text, is_py=True), [])

    def test_ie_abbreviation_not_flagged(self):
        text = "# See i.e. Bar for context\nx = 1\n"
        self.assertEqual(comment_format.check_text(text, is_py=True), [])

    def test_vs_abbreviation_not_flagged(self):
        text = "# Compare vs. Baz here\nx = 1\n"
        self.assertEqual(comment_format.check_text(text, is_py=True), [])

    def test_etc_abbreviation_not_flagged(self):
        text = "# Handles etc. Qux cases\nx = 1\n"
        self.assertEqual(comment_format.check_text(text, is_py=True), [])

    def test_version_token_not_flagged(self):
        text = "# Bumped to v1.2. Next release adds foo\nx = 1\n"
        self.assertEqual(comment_format.check_text(text, is_py=True), [])

    def test_cyrillic_sentence_end_flagged(self):
        text = "# Это конец. Следующее предложение здесь\nx = 1\n"
        self.assertTrue(comment_format.check_text(text, is_py=True))

    def test_closing_quote_sentence_end_flagged(self):
        text = '# He said "done". Next steps follow\nx = 1\n'
        self.assertTrue(comment_format.check_text(text, is_py=True))


class TestJsonExtraction(unittest.TestCase):
    DASHBOARD_PATH = "services/grafana/dashboards/foo.json"

    def test_is_dashboard_json_true_for_dashboards_dir(self):
        self.assertTrue(comment_format.is_dashboard_json("services/grafana/dashboards/foo.json"))

    def test_is_dashboard_json_true_for_dashboards_health_dir(self):
        self.assertTrue(comment_format.is_dashboard_json("services/grafana/dashboards-health/foo.json"))

    def test_is_dashboard_json_false_for_unrelated_json(self):
        self.assertFalse(comment_format.is_dashboard_json("some/other/thing.json"))

    def test_multi_sentence_description_flagged_full_file(self):
        dashboard = {
            "panels": [
                {
                    "id": 1,
                    "description": "This is one sentence. This is a second sentence.",
                }
            ]
        }
        text = json.dumps(dashboard)
        self.assertTrue(comment_format.check_json_text(text))

    def test_single_sentence_description_not_flagged_full_file(self):
        dashboard = {"panels": [{"id": 1, "description": "Just one sentence here."}]}
        text = json.dumps(dashboard)
        self.assertEqual(comment_format.check_json_text(text), [])

    def test_multi_sentence_rawsql_comment_flagged_full_file(self):
        dashboard = {
            "panels": [
                {
                    "id": 1,
                    "targets": [
                        {"rawSql": "SELECT 1\n-- This is one comment. This is a second comment.\nFROM foo"}
                    ],
                }
            ]
        }
        text = json.dumps(dashboard)
        self.assertTrue(comment_format.check_json_text(text))

    def test_single_sentence_rawsql_comment_not_flagged_full_file(self):
        dashboard = {
            "panels": [{"id": 1, "targets": [{"rawSql": "SELECT 1\n-- Just one comment here.\nFROM foo"}]}]
        }
        text = json.dumps(dashboard)
        self.assertEqual(comment_format.check_json_text(text), [])

    def test_multi_sentence_description_flagged_edit_fragment(self):
        # An Edit hook only sees new_string, usually a partial diff that
        # isn't valid standalone JSON - exercises the regex fallback path.
        fragment = '  "description": "This is one sentence. This is a second sentence.",\n'
        self.assertTrue(comment_format.check_json_text(fragment))

    def test_single_sentence_description_not_flagged_edit_fragment(self):
        fragment = '  "description": "Just one sentence here.",\n'
        self.assertEqual(comment_format.check_json_text(fragment), [])

    def test_multi_sentence_rawsql_comment_flagged_edit_fragment(self):
        fragment = '  "rawSql": "SELECT 1\\n-- This is one comment. This is a second comment.\\nFROM foo",\n'
        self.assertTrue(comment_format.check_json_text(fragment))

    def test_single_sentence_rawsql_comment_not_flagged_edit_fragment(self):
        fragment = '  "rawSql": "SELECT 1\\n-- Just one comment here.\\nFROM foo",\n'
        self.assertEqual(comment_format.check_json_text(fragment), [])


class TestAgentYamlExtraction(unittest.TestCase):
    PATH = ".agents/agents/script-ops.yaml"

    def test_is_agent_yaml_true_for_agents_dir(self):
        self.assertTrue(comment_format.is_agent_yaml(".agents/agents/script-ops.yaml"))

    def test_is_agent_yaml_false_for_unrelated_yaml(self):
        self.assertFalse(comment_format.is_agent_yaml("some/other/thing.yaml"))

    def test_is_agent_yaml_false_for_skills_dir(self):
        self.assertFalse(comment_format.is_agent_yaml(".agents/skills/md-format/SKILL.md"))

    def test_multi_sentence_description_flagged(self):
        text = "description: |\n  This is one sentence. This is a second sentence.\n"
        self.assertTrue(comment_format.check_agent_yaml_text(text))

    def test_single_sentence_description_not_flagged(self):
        text = "description: |\n  Just one sentence here.\n"
        self.assertEqual(comment_format.check_agent_yaml_text(text), [])

    def test_list_and_heading_lines_skipped(self):
        text = "tools:\n  - Bash\n  - Read\n# comment line here. Second sentence here.\n"
        self.assertEqual(comment_format.check_agent_yaml_text(text), [])

    def test_check_file_routes_agent_yaml_through_prose_checker(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / ".agents" / "agents"
            agents_dir.mkdir(parents=True)
            path = agents_dir / "script-ops.yaml"
            content = "description: |\n  One sentence. Two sentence.\n"
            path.write_text(content)
            self.assertEqual(comment_format.check_file(str(path)), comment_format.check_agent_yaml_text(content))


class TestHook(unittest.TestCase):
    def run_hook(self, payload):
        # comment_format_hook.main() prints its violation list to stderr on
        # the blocking path - intended for the live hook, not a passing test.
        buf_in = io.StringIO(json.dumps(payload))
        old_stdin = sys.stdin
        sys.stdin = buf_in
        try:
            with redirect_stderr(io.StringIO()):
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

    def test_dashboard_json_violation_blocks(self):
        code = self.run_hook({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "services/grafana/dashboards/foo.json",
                "new_string": '"description": "This is one sentence. This is a second sentence.",',
            },
        })
        self.assertEqual(code, 2)

    def test_dashboard_json_clean_passes(self):
        code = self.run_hook({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "services/grafana/dashboards/foo.json",
                "new_string": '"description": "Just one sentence here.",',
            },
        })
        self.assertEqual(code, 0)

    def test_non_dashboard_json_skipped(self):
        code = self.run_hook({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "some/other/thing.json",
                "new_string": '"description": "This is one sentence. This is a second sentence.",',
            },
        })
        self.assertEqual(code, 0)

    def test_agent_yaml_violation_blocks(self):
        code = self.run_hook({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": ".agents/agents/script-ops.yaml",
                "new_string": "description: |\n  One sentence. Two sentence.\n",
            },
        })
        self.assertEqual(code, 2)

    def test_agent_yaml_clean_passes(self):
        code = self.run_hook({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": ".agents/agents/script-ops.yaml",
                "new_string": "description: |\n  Just one sentence here.\n",
            },
        })
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
