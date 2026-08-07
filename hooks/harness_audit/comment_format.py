#!/usr/bin/env python3
"""Flag comment/docstring lines in .py/.yml/.yaml files that violate
md-format's one-sentence-per-line rule (two sentences hard-wrapped onto
the same line).
Also covers JSON-embedded prose in Grafana dashboard files: panel
`description` values and `--` SQL comments inside `rawSql` values.
Stdlib only.
Same sentence-end heuristic as audit.py's markdown check, applied to
code comments and dashboard JSON instead of markdown prose.

Usage: uv run python3 comment_format.py <file> [<file> ...]
Exit 0 = clean, 1 = violations found.
"""
import json
import re
import sys

TRIPLE_QUOTE = re.compile(r'"""|\'\'\'')

# --- Sentence-end detection -------------------------------------------------
# Two alternatives instead of one monolithic regex: Latin and Cyrillic text
# use disjoint letter ranges, and keeping them separate is easier to read
# and extend than one pattern juggling both alphabets.
#
# The character class before the period covers any letter/digit/closing
# bracket/closing quote (straight or curly) - broad on purpose, since real
# false positives (abbreviations, version tokens) are filtered out
# afterward by _is_known_exception() rather than excluded from the regex
# itself.
LATIN_SENTENCE_END_PATTERN = re.compile(r'[A-Za-z0-9)\]"\'”’]\. [A-Z]')
CYRILLIC_SENTENCE_END_PATTERN = re.compile(r'[а-яё)\]"\'”’]\. [А-ЯЁ]')

# Known multi-period abbreviations, checked against the token immediately
# before a candidate match's period (case-insensitive, trailing period
# excluded since the regex already consumed it).
ABBREVIATIONS = {"e.g", "i.e", "vs", "etc"}
VERSION_TOKEN_PATTERN = re.compile(r"^v?\d+(\.\d+)*$", re.IGNORECASE)
OPENING_PUNCTUATION = "([{\"'“‘"


def _word_before_dot(s: str, dot_index: int) -> str:
    """The run of non-whitespace characters immediately preceding the
    period at `dot_index`. Used to check a candidate sentence-end match
    against known abbreviations and version tokens before flagging it."""
    start = dot_index
    while start > 0 and not s[start - 1].isspace():
        start -= 1
    return s[start:dot_index]


def _is_known_exception(word: str) -> bool:
    """True for a period-ending token that isn't really a sentence end:
    a known abbreviation (e.g., i.e., vs., etc.) or a version token like
    v1.2 or 2.0.

    Python's `re` has no variable-length lookbehind for multi-word
    abbreviations, so this runs as a post-filter on regex candidate
    matches instead of being folded into the sentence-end pattern
    itself."""
    w = word.strip(OPENING_PUNCTUATION)
    if w.lower() in ABBREVIATIONS:
        return True
    return bool(VERSION_TOKEN_PATTERN.match(w))


def _has_multi_sentence_violation(s: str) -> bool:
    for pattern in (LATIN_SENTENCE_END_PATTERN, CYRILLIC_SENTENCE_END_PATTERN):
        for m in pattern.finditer(s):
            dot_index = m.start() + 1
            if not _is_known_exception(_word_before_dot(s, dot_index)):
                return True
    return False


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
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if is_dashboard_json(path):
        return check_json_text(text)
    if is_agent_yaml(path):
        return check_agent_yaml_text(text)
    return check_text(text, path.endswith(".py"))


def check_text(text: str, is_py: bool):
    return [
        (i, s[:80]) for i, s in comment_lines_in_text(text, is_py)
        if _has_multi_sentence_violation(s)
    ]


# --- JSON-embedded prose (Grafana dashboard description/rawSql text) ------

DASHBOARD_JSON_PATH_PATTERN = re.compile(r"(^|/)services/grafana/dashboards(-health)?/")
DESCRIPTION_VALUE_PATTERN = re.compile(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"')
RAWSQL_VALUE_PATTERN = re.compile(r'"rawSql"\s*:\s*"((?:[^"\\]|\\.)*)"')


def is_dashboard_json(path: str) -> bool:
    return path.endswith(".json") and bool(DASHBOARD_JSON_PATH_PATTERN.search(path.replace("\\", "/")))


def _decode_json_string(raw: str) -> str:
    """Undo JSON string escaping (\\n, \\", etc.) on a regex-captured
    fragment.
    Wrapping it back in quotes and handing it to json.loads reuses the
    stdlib's own escape handling instead of reimplementing it.
    Falls back to a plain \\n/\\" replace if that fails, e.g. on a
    fragment truncated mid-escape by a partial Edit diff."""
    try:
        return json.loads(f'"{raw}"')
    except (json.JSONDecodeError, ValueError):
        return raw.replace("\\n", "\n").replace('\\"', '"')


def _rawsql_comment_lines(sql: str):
    for line in sql.split("\n"):
        s = line.strip()
        if s.startswith("--"):
            yield s.lstrip("-").strip()


def _json_embedded_lines_via_regex(text: str):
    """Fallback extraction for text that isn't valid standalone JSON, e.g.
    an Edit hook's new_string diff fragment: find "description" and
    "rawSql" string values by pattern instead of requiring a full parse."""
    for m in DESCRIPTION_VALUE_PATTERN.finditer(text):
        value = _decode_json_string(m.group(1)).strip()
        if value:
            yield text.count("\n", 0, m.start()) + 1, value
    for m in RAWSQL_VALUE_PATTERN.finditer(text):
        sql = _decode_json_string(m.group(1))
        lineno = text.count("\n", 0, m.start()) + 1
        for s in _rawsql_comment_lines(sql):
            yield lineno, s


def _walk_json_for_checkable_strings(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "description" and isinstance(value, str) and value.strip():
                yield value.strip()
            elif key == "rawSql" and isinstance(value, str):
                yield from _rawsql_comment_lines(value)
            else:
                yield from _walk_json_for_checkable_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json_for_checkable_strings(item)


def json_embedded_lines_in_text(text: str):
    """Yield (lineno, text) for checkable JSON-embedded prose in a Grafana
    dashboard file: panel "description" values, and "--" SQL-comment
    lines inside "rawSql" values (\\n-escaped in the JSON source, so
    un-escaped and split into lines before the "--" check).

    Tries a real json.loads() parse first - the normal case when checking
    a whole dashboard file - and falls back to regex extraction directly
    on the raw text for fragments that aren't valid standalone JSON, e.g.
    an Edit hook's new_string diff snippet.

    lineno is 1-based; for the parse-succeeded path it's just the
    extraction order (no real source offset survives a parse), same
    "snippet-relative, good enough for the excerpt" convention as
    comment_lines_in_text above."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        yield from _json_embedded_lines_via_regex(text)
        return
    for i, s in enumerate(_walk_json_for_checkable_strings(parsed), 1):
        yield i, s


def check_json_text(text: str):
    return [
        (i, s[:80]) for i, s in json_embedded_lines_in_text(text)
        if _has_multi_sentence_violation(s)
    ]


# --- Agent-YAML prose (.agents/agents/*.yaml description:/body: block scalars) ---

AGENT_YAML_PATH_PATTERN = re.compile(r"(^|/)\.agents/agents/[^/]+\.yaml$")


def is_agent_yaml(path: str) -> bool:
    return bool(AGENT_YAML_PATH_PATTERN.search(path.replace("\\", "/")))


def agent_yaml_prose_lines_in_text(text: str):
    """Yield (lineno, text) for prose-looking lines in a .agents/agents/*.yaml
    source.
    description:/body: block-scalar content is literal text, unlike JSON
    string values - no escaping to undo, so a plain line-by-line filter is enough.
    Same skip rules as audit.py's multi_sentence_lines (blank/list/quote/code-fence lines).
    Works the same on a whole file or a partial Edit fragment."""
    in_code = False
    for i, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not s:
            continue
        if s.startswith(("-", "*", "|", ">", "#")) or re.match(r"^\d+\.\s", s):
            continue
        yield i, s


def check_agent_yaml_text(text: str):
    return [
        (i, s[:80]) for i, s in agent_yaml_prose_lines_in_text(text)
        if _has_multi_sentence_violation(s)
    ]


def main() -> int:
    violations = []
    for path in sys.argv[1:]:
        if is_dashboard_json(path):
            for i, s in check_file(path):
                violations.append(f"{path}:{i}: md-format one-sentence-per-line (json) -> {s}")
            continue
        if is_agent_yaml(path):
            for i, s in check_file(path):
                violations.append(f"{path}:{i}: md-format one-sentence-per-line (agent-yaml) -> {s}")
            continue
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
