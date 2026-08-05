#!/usr/bin/env python3
"""Flag comment/docstring lines in .py/.yml/.yaml files that violate
md-format's one-sentence-per-line rule (two sentences hard-wrapped onto
the same line). Stdlib only. Same heuristic as audit.py's markdown check,
applied to code comments instead of markdown prose.

Usage: uv run python3 comment_format.py <file> [<file> ...]
Exit 0 = clean, 1 = violations found.
"""
import re
import sys

MULTI_SENTENCE_PATTERN = re.compile(r"[a-z0-9`)\]]\. [A-Z]")
TRIPLE_QUOTE = re.compile(r'"""|\'\'\'')


def comment_lines_in_text(text: str, is_py: bool):
    """Yield (lineno, text) for whole-line '#' comments and, for Python,
    the inner lines of triple-quoted docstrings (single- or multi-line).
    lineno is 1-based within `text`, not necessarily the file's real line
    number - callers that pass a snippet (e.g. an Edit's new_string) get
    snippet-relative numbers, which is fine since this is just for the
    violation message excerpt."""
    raw_lines = text.splitlines()

    in_doc = False
    doc_quote = None
    for i, raw in enumerate(raw_lines, 1):
        s = raw.strip()
        if is_py:
            if in_doc:
                if doc_quote in s:
                    before = s.split(doc_quote, 1)[0].strip()
                    if before:
                        yield i, before
                    in_doc = False
                    continue
                if s:
                    yield i, s
                continue
            m = TRIPLE_QUOTE.search(s)
            if m:
                q = m.group(0)
                if s.count(q) >= 2:
                    start = s.index(q) + len(q)
                    end = s.index(q, start)
                    inner = s[start:end].strip()
                    if inner:
                        yield i, inner
                    continue
                doc_quote = q
                in_doc = True
                after = s.split(q, 1)[1].strip()
                if after:
                    yield i, after
                continue
        if s.startswith("#") and not s.startswith("#!"):
            yield i, s.lstrip("#").strip()


def check_file(path: str):
    is_py = path.endswith(".py")
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    return check_text(text, is_py)


def check_text(text: str, is_py: bool):
    return [
        (i, s[:80]) for i, s in comment_lines_in_text(text, is_py)
        if MULTI_SENTENCE_PATTERN.search(s)
    ]


def main() -> int:
    violations = []
    for path in sys.argv[1:]:
        if not (path.endswith(".py") or path.endswith(".yml") or path.endswith(".yaml")):
            continue
        for i, s in check_file(path):
            violations.append(f"{path}:{i}: md-format one-sentence-per-line (comment) -> {s}")
    if violations:
        print("\n".join(violations))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
