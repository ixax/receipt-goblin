"""Single source of truth for harness token/line/word budgets.

Imported by audit.py. Referenced (not restated) by harness-guardian/SKILL.md
and harness-expert.md - change numbers here only, never in prose.
"""

BUDGETS = {
    "root_md": 2000,           # tokens, root CLAUDE.md/AGENTS.md
    "nested_md": 500,          # tokens, nested CLAUDE.md/AGENTS.md
    "rule": 300,               # tokens, .claude/rules/*.md
    "skill_lines": 500,        # lines, SKILL.md body (target ~150)
    "description_words": 100,  # words, any frontmatter description
}
