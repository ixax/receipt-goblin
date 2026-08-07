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

    def test_agent_yaml_description_words_block_scalar(self):
        text = "name: foo\ndescription: |\n  one two three four five\n  six seven\ntools:\n  - Bash\n"
        self.assertEqual(audit.agent_yaml_description_words(text), 7)

    def test_agent_yaml_description_words_no_description(self):
        self.assertEqual(audit.agent_yaml_description_words("name: foo\ntools:\n  - Bash\n"), 0)


class TestRuleFunctions(unittest.TestCase):
    """Each check_* rule in audit.py is a pure function of its arguments -
    no filesystem, no shared state - so it's tested directly here instead
    of only indirectly through main()."""

    def test_check_token_budget_over(self):
        text = "x " * (audit.BUDGETS["root_md"] * 5)
        v = audit.check_token_budget("AGENTS.md", "root_md", text)
        self.assertEqual(len(v), 1)
        self.assertIn("tokens > budget", v[0])

    def test_check_token_budget_under(self):
        self.assertEqual(audit.check_token_budget("AGENTS.md", "root_md", "short"), [])

    def test_check_token_budget_ignores_other_kinds(self):
        text = "x " * (audit.BUDGETS["root_md"] * 5)
        self.assertEqual(audit.check_token_budget("README.md", "other_md", text), [])

    def test_check_skill_line_budget_over(self):
        body = "\n".join(f"line {i}" for i in range(audit.BUDGETS["skill_lines"] + 10))
        v = audit.check_skill_line_budget("x/SKILL.md", "skill", body)
        self.assertEqual(len(v), 1)
        self.assertIn("lines > budget", v[0])

    def test_check_skill_line_budget_ignores_non_skill(self):
        body = "\n".join(f"line {i}" for i in range(audit.BUDGETS["skill_lines"] + 10))
        self.assertEqual(audit.check_skill_line_budget("x.md", "other_md", body), [])

    def test_check_description_word_budget_over(self):
        long_desc = " ".join(["word"] * (audit.BUDGETS["description_words"] + 10))
        text = f"---\nname: x\ndescription: {long_desc}\n---\nbody\n"
        v = audit.check_description_word_budget("x.md", "agent", text)
        self.assertEqual(len(v), 1)
        self.assertIn("words >", v[0])

    def test_check_description_word_budget_ignores_other_kinds(self):
        long_desc = " ".join(["word"] * (audit.BUDGETS["description_words"] + 10))
        text = f"---\nname: x\ndescription: {long_desc}\n---\nbody\n"
        self.assertEqual(audit.check_description_word_budget("x.md", "root_md", text), [])

    def test_check_description_word_budget_agent_yaml_over(self):
        long_desc = " ".join(["word"] * (audit.BUDGETS["description_words"] + 10))
        text = f"name: x\ndescription: |\n  {long_desc}\ntools:\n  - Bash\n"
        v = audit.check_description_word_budget("x.yaml", "agent_yaml", text)
        self.assertEqual(len(v), 1)
        self.assertIn("words >", v[0])

    def test_check_description_word_budget_agent_yaml_under(self):
        text = "name: x\ndescription: |\n  short description here\ntools:\n  - Bash\n"
        self.assertEqual(audit.check_description_word_budget("x.yaml", "agent_yaml", text), [])

    def test_check_volatile_content_flagged(self):
        v = audit.check_volatile_content("AGENTS.md", "root_md", "Current sprint: 2026-07-29.\n", is_symlink=False)
        self.assertEqual(len(v), 1)
        self.assertIn("volatile content", v[0])

    def test_check_volatile_content_skips_symlink(self):
        v = audit.check_volatile_content("AGENTS.md", "root_md", "Current sprint: 2026-07-29.\n", is_symlink=True)
        self.assertEqual(v, [])

    def test_check_one_sentence_per_line_flagged(self):
        text = "This is one sentence. This is a second sentence on the same line.\n"
        v = audit.check_one_sentence_per_line("x.md", "other_md", text)
        self.assertEqual(len(v), 1)
        self.assertIn("one-sentence-per-line", v[0])

    def test_check_one_sentence_per_line_ignores_unlisted_kind(self):
        text = "This is one sentence. This is a second sentence on the same line.\n"
        self.assertEqual(audit.check_one_sentence_per_line("x.md", "harness_index", text), [])

    def test_check_one_sentence_per_line_agent_yaml_flagged(self):
        text = "description: |\n  This is one sentence. This is a second sentence.\n"
        v = audit.check_one_sentence_per_line("x.yaml", "agent_yaml", text)
        self.assertEqual(len(v), 1)
        self.assertIn("one-sentence-per-line", v[0])

    def test_check_dead_references_flags_missing(self):
        text = "See `agent_docs/does-not-exist.md` for details.\n"
        v = audit.check_dead_references("AGENTS.md", text, exists_fn=lambda ref: False)
        self.assertEqual(len(v), 1)
        self.assertIn("dead reference", v[0])

    def test_check_dead_references_skips_existing(self):
        text = "See `agent_docs/architecture.md` for details.\n"
        v = audit.check_dead_references("AGENTS.md", text, exists_fn=lambda ref: True)
        self.assertEqual(v, [])

    def test_check_dead_references_skips_thoughts_prefix(self):
        text = "See `thoughts/scratch.md` for details.\n"
        v = audit.check_dead_references("AGENTS.md", text, exists_fn=lambda ref: False)
        self.assertEqual(v, [])

    def test_rule_candidate_lines_skips_other_md(self):
        text = "- This exact bullet line is long enough to count as a rule\n"
        self.assertEqual(audit.rule_candidate_lines("other_md", text), [])

    def test_rule_candidate_lines_collects_bullets(self):
        text = "- This exact bullet line is long enough to count as a rule\nshort\n"
        self.assertEqual(
            audit.rule_candidate_lines("agent_yaml", text),
            ["- this exact bullet line is long enough to count as a rule"],
        )

    def test_rule_candidate_lines_skips_compiled_agent_md(self):
        # kind "agent" (.claude/agents/*.md) is compiled from "agent_yaml"
        # (.agents/agents/*.yaml) - it legitimately duplicates its source,
        # so it's excluded from the duplicate-rule scan same as "other_md".
        text = "- This exact bullet line is long enough to count as a rule\n"
        self.assertEqual(audit.rule_candidate_lines("agent", text), [])

    def test_check_duplicate_rules_flags_shared_line(self):
        line_index = {"- shared line": ["a.md", "b.md"], "- unique line": ["a.md"]}
        v = audit.check_duplicate_rules(line_index)
        self.assertEqual(len(v), 1)
        self.assertIn("duplicate rule", v[0])

    def test_check_agents_chain_budget_over(self):
        files = {"AGENTS.md": ("root_md", "x" * (33 * 1024))}
        v = audit.check_agents_chain_budget(files, is_symlink_fn=lambda rel: False)
        self.assertEqual(len(v), 1)
        self.assertIn("Codex 32 KiB cap", v[0])

    def test_check_agents_chain_budget_symlink_excluded(self):
        files = {"AGENTS.md": ("root_md", "x" * (33 * 1024))}
        v = audit.check_agents_chain_budget(files, is_symlink_fn=lambda rel: True)
        self.assertEqual(v, [])

    def test_check_claude_agents_pairing_flags_both_present(self):
        files = {"CLAUDE.md": ("root_md", "a"), "AGENTS.md": ("root_md", "b")}
        v = audit.check_claude_agents_pairing(files, is_symlink_fn=lambda rel: False)
        self.assertEqual(len(v), 1)
        self.assertIn("separate files", v[0])

    def test_check_claude_agents_pairing_symlink_not_flagged(self):
        files = {"CLAUDE.md": ("root_md", "a"), "AGENTS.md": ("root_md", "b")}
        v = audit.check_claude_agents_pairing(files, is_symlink_fn=lambda rel: True)
        self.assertEqual(v, [])


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
        write(self.root / ".agents" / "skills" / "a" / "SKILL.md", f"---\nname: a\ndescription: d\n---\n{dup_line}\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("duplicate rule", out)

    def test_duplicate_rule_not_flagged_between_agent_yaml_and_its_compiled_md(self):
        dup_line = "- This exact bullet line is the source of truth, compiled verbatim"
        write(self.root / ".agents" / "agents" / "a.yaml", f"name: a\ndescription: |\n  d\n{dup_line}\n")
        write(self.root / ".claude" / "agents" / "a.md", f"---\nname: a\ndescription: d\n---\n{dup_line}\n")
        code, out = self.run_main()
        self.assertEqual(code, 0)

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

    def test_agents_and_claude_skill_symlink_deduped(self):
        real = self.root / ".agents" / "skills" / "shared" / "SKILL.md"
        write(real, "---\nname: shared\ndescription: d\n---\nbody\n")
        (self.root / ".claude").mkdir(parents=True, exist_ok=True)
        os.symlink(self.root / ".agents" / "skills", self.root / ".claude" / "skills")

        files = audit.collect()
        skill_entries = [rel for rel, (kind, _) in files.items() if kind == "skill"]
        self.assertEqual(len(skill_entries), 1, f"expected exactly one deduped skill entry, got {skill_entries}")

    def test_agents_only_skill_still_picked_up(self):
        write(self.root / ".agents" / "skills" / "agentsonly" / "SKILL.md", "---\nname: c\ndescription: d\n---\nbody\n")
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

    def test_agent_yaml_classified(self):
        write(self.root / ".agents" / "agents" / "script-ops.yaml", "name: script-ops\ndescription: |\n  short\n")
        files = audit.collect()
        self.assertEqual(files[".agents/agents/script-ops.yaml"][0], "agent_yaml")

    def test_agent_yaml_multi_sentence_flagged(self):
        write(
            self.root / ".agents" / "agents" / "script-ops.yaml",
            "name: script-ops\ndescription: |\n  This is one sentence. This is a second sentence.\n",
        )
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("md-format one-sentence-per-line", out)

    def test_root_readme_classified_as_other_md(self):
        write(self.root / "README.md", "# Repo\n\nSome content.\n")
        files = audit.collect()
        self.assertEqual(files["README.md"][0], "other_md")

    def test_other_md_has_no_token_budget(self):
        write(self.root / "README.md", "x " * (audit.BUDGETS["root_md"] * 5))
        code, out = self.run_main()
        self.assertEqual(code, 0)

    def test_other_md_multi_sentence_line_flagged(self):
        write(
            self.root / "todo" / "x.md",
            "This is one sentence. This is a second sentence on the same line.\n",
        )
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("md-format one-sentence-per-line", out)

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
