#!/usr/bin/env python3
"""Unit tests for hooks/harness_audit/audit.py. Stdlib unittest only - no pytest
dependency, matching audit.py's own "stdlib only" design so this hook never
needs a venv.

Run: python3 -m unittest discover -s hooks/harness_audit/tests
"""
import importlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOK_DIR))
audit = importlib.import_module("audit")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestTokensAndWords(unittest.TestCase):
    def test_tokens_strips_comments(self):
        text = "hello world <!-- this is a comment, not counted --> more text"
        with_comment = audit.tokens(text)
        without = audit.tokens("hello world  more text")
        self.assertEqual(with_comment, without)

    def test_tokens_roughly_bytes_over_four(self):
        text = "a" * 400
        self.assertEqual(audit.tokens(text), 400 // 4)

    def test_description_words_block_scalar(self):
        text = (
            "---\n"
            "name: foo\n"
            "description: >\n"
            "  one two three four five\n"
            "  six seven\n"
            "version: 1.0.0\n"
            "---\n"
            "body\n"
        )
        self.assertEqual(audit.description_words(text), 7)

    def test_description_words_no_frontmatter(self):
        self.assertEqual(audit.description_words("just a body, no frontmatter"), 0)


class TestCollectAndMain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_root = audit.ROOT
        audit.ROOT = str(self.root)

    def tearDown(self):
        audit.ROOT = self._old_root
        self._tmp.cleanup()

    def run_main(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = audit.main()
        return code, buf.getvalue()

    def test_clean_repo_exits_zero(self):
        write(self.root / "AGENTS.md", "# Project\n\nShort and under budget.\n")
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("clean", out)

    def test_root_md_over_budget_flagged(self):
        write(self.root / "AGENTS.md", "x " * (audit.BUDGETS["root_md"] * 5))
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("tokens > budget", out)

    def test_nested_md_uses_nested_budget(self):
        write(self.root / "services" / "webhook" / "AGENTS.md", "x " * (audit.BUDGETS["nested_md"] * 5))
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("nested_md", out)

    def test_skill_over_line_budget_flagged(self):
        body = "\n".join(f"line {i}" for i in range(audit.BUDGETS["skill_lines"] + 10))
        write(self.root / ".claude" / "skills" / "x" / "SKILL.md", "---\nname: x\ndescription: short\n---\n" + body)
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("lines > budget", out)

    def test_description_over_word_budget_flagged(self):
        long_desc = " ".join(["word"] * (audit.BUDGETS["description_words"] + 10))
        write(self.root / ".claude" / "agents" / "x.md", f"---\nname: x\ndescription: {long_desc}\n---\nbody\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("description", out)
        self.assertIn("words >", out)

    def test_volatile_content_flagged(self):
        write(self.root / "AGENTS.md", "Current sprint: 2026-07-29. Do the thing.\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("volatile content", out)

    def test_duplicate_rule_flagged_across_files(self):
        dup_line = "- This exact bullet line appears in two different harness files verbatim"
        write(self.root / "AGENTS.md", f"# Project\n\n{dup_line}\n")
        write(self.root / ".claude" / "agents" / "a.md", f"---\nname: a\ndescription: d\n---\n{dup_line}\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("duplicate rule", out)

    def test_dead_reference_flagged(self):
        write(self.root / "AGENTS.md", "See `agent_docs/does-not-exist.md` for details.\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("dead reference", out)

    def test_live_reference_not_flagged(self):
        write(self.root / "agent_docs" / "architecture.md", "content")
        write(self.root / "AGENTS.md", "See `agent_docs/architecture.md` for details.\n")
        code, out = self.run_main()
        self.assertEqual(code, 0)

    def test_claude_and_codex_skill_symlink_deduped(self):
        real = self.root / ".claude" / "skills" / "shared" / "SKILL.md"
        write(real, "---\nname: shared\ndescription: d\n---\nbody\n")
        codex_dir = self.root / ".codex" / "skills" / "shared"
        codex_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(real, codex_dir / "SKILL.md")

        files = audit.collect()
        skill_entries = [rel for rel, (kind, _) in files.items() if kind == "skill"]
        self.assertEqual(len(skill_entries), 1, f"expected exactly one deduped skill entry, got {skill_entries}")

    def test_codex_only_skill_still_picked_up(self):
        write(self.root / ".codex" / "skills" / "codexonly" / "SKILL.md", "---\nname: c\ndescription: d\n---\nbody\n")
        files = audit.collect()
        skill_entries = [rel for rel, (kind, _) in files.items() if kind == "skill"]
        self.assertEqual(len(skill_entries), 1)

    def test_claude_md_agents_md_separate_files_flagged(self):
        write(self.root / "AGENTS.md", "canonical content\n")
        write(self.root / "CLAUDE.md", "diverged content\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("separate files", out)

    def test_claude_md_symlink_to_agents_md_not_flagged(self):
        write(self.root / "AGENTS.md", "canonical content\n")
        os.symlink(self.root / "AGENTS.md", self.root / "CLAUDE.md")
        code, out = self.run_main()
        self.assertEqual(code, 0)

    def test_agent_docs_md_classified_as_deep_dive(self):
        write(self.root / "agent_docs" / "architecture.md", "# Architecture\n\nSome content.\n")
        files = audit.collect()
        self.assertEqual(files["agent_docs/architecture.md"][0], "deep_dive")

    def test_harness_index_excluded_from_classification(self):
        write(self.root / "agent_docs" / "harness-index.md", "<!-- GENERATED -->\n")
        files = audit.collect()
        self.assertNotIn("agent_docs/harness-index.md", files)

    def test_deep_dive_dead_reference_flagged(self):
        write(self.root / "agent_docs" / "architecture.md", "See `agent_docs/does-not-exist.md` for details.\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("dead reference", out)

    def test_deep_dive_has_no_token_budget(self):
        write(self.root / "agent_docs" / "architecture.md", "x " * (audit.BUDGETS["root_md"] * 5))
        code, out = self.run_main()
        self.assertEqual(code, 0)

    def test_multi_sentence_line_flagged(self):
        write(
            self.root / ".claude" / "agents" / "x.md",
            "---\nname: x\ndescription: d\n---\n"
            "This is one sentence. This is a second sentence on the same line.\n",
        )
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("md-format one-sentence-per-line", out)

    def test_multi_sentence_skips_frontmatter_and_code_and_lists(self):
        write(
            self.root / ".claude" / "agents" / "x.md",
            "---\nname: x\ndescription: One sentence. Another sentence, still fine here.\n---\n"
            "```\ncode. Still code.\n```\n"
            "- A bullet. With two sentences.\n"
            "> Quoted before-example. Two sentences.\n"
            "One clean sentence per line here.\n",
        )
        code, out = self.run_main()
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
