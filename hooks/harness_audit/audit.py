#!/usr/bin/env python3
"""Audit harness md-files for token budget violations. Stdlib only.

Usage: uv run python3 audit.py [repo_root]
Exit 0 = clean, 1 = violations found.

Dual-harness aware: this repo tracks both Claude Code (.claude/) and Codex
(.agents/, .codex/) equally.
Skills/rules are matched by filename/path pattern, not a single CLI's
directory.
Skill content lives under .agents/skills/<name>/SKILL.md, the one real
copy; .claude/skills is a directory-level symlink to it, so os.walk()
(followlinks=False) never descends into .claude/skills and each SKILL.md
is only ever reached via the .agents/skills/ path.
The realpath dedup below stays in place for other symlink cases (e.g.
.codex/ config), even though it's no longer load-bearing for skills
specifically.
Subagents stay .claude-only - Codex has no Task-tool/subagent
equivalent, so there's no .codex/agents to scan.
"""
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from budgets import BUDGETS  # noqa: E402

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

STRIP_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
VOLATILE_PATTERN = re.compile(
    r"\b(20\d\d-\d\d-\d\d|sprint\s*[#\w-]*\d|current (branch|sprint|version|status)|as of|updated on)\b",
    re.IGNORECASE,
)
REF_PATTERN = re.compile(r"(?:\]\(|`|\s)((?:agent_docs|thoughts|references|scripts|\.claude|\.agents)/[\w./-]+\.(?:md|py|sql|json))")
MULTI_SENTENCE_PATTERN = re.compile(r"[a-z0-9`)\]]\. [A-Z]")


def multi_sentence_lines(text: str):
    """Heuristic for md-format's one-sentence-per-line rule: a prose line
    containing '. X' mid-line is probably two sentences hard-wrapped
    together. Skips frontmatter, code fences, list/table/blockquote lines
    (blockquotes may be deliberate before/after examples of bad style)."""
    in_fm = False
    in_code = False
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip() == "---":
            in_fm = not in_fm
            continue
        if in_fm:
            continue
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        s = line.strip()
        if not s or s.startswith(("-", "*", "|", ">", "#")) or re.match(r"^\d+\.\s", s):
            continue
        if MULTI_SENTENCE_PATTERN.search(s):
            hits.append((i, s[:80]))
    return hits


def tokens(text: str) -> int:
    return len(STRIP_COMMENTS.sub("", text).encode("utf-8")) // 4


def description_words(text: str) -> int:
    m = re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return 0
    d = re.search(r"description:\s*(?:>\s*\n)?((?:.|\n)*?)(?=\n\w+:|\Z)", m.group(1))
    return len(d.group(1).split()) if d else 0


# --- Per-file rules --------------------------------------------------------
# Each check_* function takes only the data it needs and returns a list of
# formatted violation strings - no I/O, no shared state, so each is testable
# in isolation.

def check_token_budget(rel: str, kind: str, text: str) -> list:
    if kind not in ("root_md", "nested_md", "rule"):
        return []
    tok = tokens(text)
    if tok > BUDGETS[kind]:
        return [f"{rel}: {tok} tokens > budget {BUDGETS[kind]}"]
    return []


def check_skill_line_budget(rel: str, kind: str, text: str) -> list:
    if kind != "skill":
        return []
    nlines = text.count("\n") + 1
    if nlines > BUDGETS["skill_lines"]:
        return [f"{rel}: {nlines} lines > budget {BUDGETS['skill_lines']}"]
    return []


def check_description_word_budget(rel: str, kind: str, text: str) -> list:
    if kind not in ("skill", "agent"):
        return []
    dw = description_words(text)
    if dw > BUDGETS["description_words"]:
        return [f"{rel}: description {dw} words > {BUDGETS['description_words']}"]
    return []


def check_volatile_content(rel: str, kind: str, text: str, is_symlink: bool) -> list:
    if kind not in ("root_md", "nested_md", "rule") or is_symlink:
        return []
    violations = []
    for i, line in enumerate(text.splitlines(), 1):
        if VOLATILE_PATTERN.search(line):
            violations.append(f"{rel}:{i}: volatile content in cached prefix -> {line.strip()[:70]}")
    return violations


def check_one_sentence_per_line(rel: str, kind: str, text: str) -> list:
    if kind not in ("agent", "skill", "rule", "root_md", "nested_md", "deep_dive", "other_md"):
        return []
    return [f"{rel}:{i}: md-format one-sentence-per-line -> {s}" for i, s in multi_sentence_lines(text)]


def check_dead_references(rel: str, text: str, exists_fn) -> list:
    """exists_fn(ref) -> bool is injected so this stays a pure function of
    its inputs instead of reaching into the filesystem itself."""
    violations = []
    for ref in set(REF_PATTERN.findall(text)):
        if ref.startswith("thoughts/"):
            continue
        if not exists_fn(ref):
            violations.append(f"{rel}: dead reference -> {ref}")
    return violations


def rule_candidate_lines(kind: str, text: str) -> list:
    """List-item lines from this file eligible for the duplicate-rule check
    below, or [] for kinds that don't carry token-budgeted rules."""
    if kind == "other_md":
        return []
    return [
        line.strip().lower() for line in text.splitlines()
        if len(line.strip()) > 30 and line.strip().startswith(("- ", "* "))
    ]


# --- Cross-file rules --------------------------------------------------------
# These need the full file set, so they run once after the per-file loop.

def check_duplicate_rules(line_index: dict) -> list:
    violations = []
    for line, where in line_index.items():
        if len(set(where)) > 1:
            violations.append(f"duplicate rule in {sorted(set(where))}: {line[:60]}...")
    return violations


def check_agents_chain_budget(files: dict, is_symlink_fn) -> list:
    agents_chain = sum(
        len(t.encode("utf-8")) for rel, (k, t) in files.items()
        if os.path.basename(rel) == "AGENTS.md" and not is_symlink_fn(rel)
    )
    if agents_chain > 32 * 1024:
        return [f"AGENTS.md chain {agents_chain} bytes > Codex 32 KiB cap (silent truncation)"]
    return []


def check_claude_agents_pairing(files: dict, is_symlink_fn) -> list:
    violations = []
    for rel in files:
        if os.path.basename(rel) != "CLAUDE.md":
            continue
        d = os.path.dirname(rel)
        other = f"{d}/AGENTS.md" if d else "AGENTS.md"
        if other in files and not (is_symlink_fn(rel) or is_symlink_fn(other)):
            violations.append(f"{d or '.'}: CLAUDE.md and AGENTS.md are separate files — symlink one to the other")
    return violations


def collect():
    files = {}
    seen_real = set()
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "vendor", ".venv"}]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            # Posix-style rel keys on every OS - violation lines built from
            # these are matched by audit_hook.py against its own normalized
            # rel, so both sides must agree on the separator.
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if fn in ("CLAUDE.md", "AGENTS.md"):
                kind = "root_md" if os.path.dirname(rel) in ("", ".") else "nested_md"
            elif rel.startswith(".claude/rules") and fn.endswith(".md"):
                kind = "rule"
            elif fn == "SKILL.md":
                # Matched by filename, not a single CLI's directory.
                # The real copy lives under .agents/skills/x/SKILL.md;
                # .claude/skills is a directory-level symlink to it, so
                # followlinks=False keeps os.walk() from ever descending
                # into .claude/skills.
                kind = "skill"
            elif fn.endswith(".md") and rel.startswith(".claude/agents"):
                kind = "agent"
            elif fn.endswith(".md") and rel.startswith("agent_docs/") and fn != "harness-index.md":
                # Hand-authored deep-dive docs (architecture.md, incidents.md) -
                # no token budget (not always-loaded), but still checked for
                # dead references and duplicate rules. harness-index.md is
                # excluded: it's GENERATED by scripts/sync_harness.py, never
                # hand-edited, and would churn on every frontmatter change.
                kind = "deep_dive"
            elif fn == "harness-index.md":
                # GENERATED by scripts/sync_harness.py, never hand-edited -
                # excluded from every check, same as its agent_docs/ case above.
                continue
            elif fn.endswith(".md"):
                # Any other markdown file in the repo: README.md, todo/*.md,
                # service-level docs, etc.
                # No token budget.
                # Still checked for md-format one-sentence-per-line and dead
                # references.
                kind = "other_md"
            else:
                continue
            real = os.path.realpath(path)
            if real in seen_real:
                continue
            seen_real.add(real)
            with open(path, encoding="utf-8", errors="replace") as f:
                files[rel] = (kind, f.read())
    return files


def main():
    files = collect()
    if not files:
        print("no harness files found under", ROOT)
        return 0

    violations = []
    line_index = defaultdict(list)

    def is_symlink(rel):
        return os.path.islink(os.path.join(ROOT, rel))

    print(f"{'file':<55} {'kind':<10} {'tokens':>7} {'lines':>6}")
    for rel, (kind, text) in sorted(files.items()):
        tok = tokens(text)
        nlines = text.count("\n") + 1
        print(f"{rel:<55} {kind:<10} {tok:>7} {nlines:>6}")

        violations.extend(check_token_budget(rel, kind, text))
        violations.extend(check_skill_line_budget(rel, kind, text))
        violations.extend(check_description_word_budget(rel, kind, text))

        for line in rule_candidate_lines(kind, text):
            line_index[line].append(rel)

        violations.extend(check_volatile_content(rel, kind, text, is_symlink(rel)))
        violations.extend(check_one_sentence_per_line(rel, kind, text))

        base = os.path.dirname(os.path.join(ROOT, rel))

        def exists_fn(ref, base=base):
            return os.path.exists(os.path.join(ROOT, ref)) or os.path.exists(os.path.join(base, ref))

        violations.extend(check_dead_references(rel, text, exists_fn))

    violations.extend(check_duplicate_rules(line_index))

    total = sum(
        tokens(t) for rel, (k, t) in files.items()
        if k in ("root_md", "nested_md", "rule") and not is_symlink(rel)
    )
    print(f"\nalways-loaded layer total: ~{total} tokens")

    violations.extend(check_agents_chain_budget(files, is_symlink))
    violations.extend(check_claude_agents_pairing(files, is_symlink))

    if violations:
        print("\nVIOLATIONS:")
        for v in violations:
            print(" -", v)
        return 1
    print("\nclean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
