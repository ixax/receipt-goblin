"""Unit tests for server.py's read-only SQL validator - the sole
enforcement layer gating `query`/`profile_query` (see server.py's module
docstring: there is no separate read-only ClickHouse user).
No real ClickHouse connection or network access - every test exercises
`_validate_readonly_sql` and its helpers as pure functions.

Two groups of coverage:
- Regression coverage for existing behavior (none of this had any tests
  before this file).
- New coverage for the quote-awareness fix: `_strip_sql_comments` was
  already quote-aware, but every check downstream of it used to scan the
  raw (unmasked) text, so a string literal's *contents* (e.g. an HTML-
  entity-escaped `;`, or plain-text mentions of a forbidden keyword) could
  cause a false-positive rejection. `_mask_string_literals` fixes this by
  replacing string-literal content with a neutral placeholder before any
  check runs, without touching the real, unmasked query that actually
  gets executed."""

import pytest

from src import server


# ---------------------------------------------------------------------------
# Valid queries
# ---------------------------------------------------------------------------

def test_simple_select_passes():
    assert server._validate_readonly_sql("SELECT 1 FROM agent_events") == "SELECT 1 FROM agent_events"


def test_simple_with_passes():
    sql = "WITH x AS (SELECT 1) SELECT * FROM x, agent_events"
    assert server._validate_readonly_sql(sql) == sql


def test_leading_whitespace_select_passes():
    assert server._validate_readonly_sql("   SELECT 1 FROM agent_events") == "SELECT 1 FROM agent_events"


def test_case_insensitive_select_with_passes():
    server._validate_readonly_sql("select 1 from agent_events")
    server._validate_readonly_sql("With x As (Select 1) Select * From x, agent_events")


# ---------------------------------------------------------------------------
# Only SELECT/WITH allowed at the start
# ---------------------------------------------------------------------------

def test_non_select_with_start_rejected():
    with pytest.raises(ValueError, match="Only SELECT/WITH queries are allowed"):
        server._validate_readonly_sql("EXPLAIN SELECT 1 FROM agent_events")


def test_select_not_at_the_very_start_rejected():
    # "at the start" - a clean-looking SELECT preceded by other text is
    # still rejected, not scanned for anywhere in the string.
    with pytest.raises(ValueError, match="Only SELECT/WITH queries are allowed"):
        server._validate_readonly_sql("(SELECT 1 FROM agent_events)")


# ---------------------------------------------------------------------------
# Forbidden keywords (existing behavior - regression coverage)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [k for k in server._FORBIDDEN_KEYWORDS if k != "SYSTEM"])
def test_forbidden_keyword_rejected(kw):
    sql = f"SELECT 1 FROM agent_events WHERE {kw} = 1"
    with pytest.raises(ValueError, match=f"'{kw}' is not allowed"):
        server._validate_readonly_sql(sql)


def test_forbidden_keyword_matched_as_whole_word_only():
    # A real column/identifier that merely *contains* a forbidden keyword
    # as a substring (not a whole word) must not be rejected.
    server._validate_readonly_sql("SELECT insert_id FROM agent_events")


def test_system_command_rejected():
    with pytest.raises(ValueError, match="'SYSTEM' is not allowed"):
        server._validate_readonly_sql("SELECT 1 FROM agent_events WHERE 1 = 1 AND SYSTEM = 1")


# ---------------------------------------------------------------------------
# Forbidden table functions (existing behavior)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn", server._FORBIDDEN_TABLE_FUNCTIONS)
def test_forbidden_table_function_rejected(fn):
    sql = f"SELECT * FROM {fn}('a', 'b', 'c')"
    with pytest.raises(ValueError, match=f"'{fn}\\(...\\)' is not allowed"):
        server._validate_readonly_sql(sql)


# ---------------------------------------------------------------------------
# system.* / information_schema / mysql (existing behavior)
# ---------------------------------------------------------------------------

def test_system_query_log_allowed():
    server._validate_readonly_sql("SELECT * FROM system.query_log")


@pytest.mark.parametrize("table", ["settings", "tables", "columns", "processes", "metrics"])
def test_other_system_tables_rejected(table):
    with pytest.raises(ValueError, match=f"'system.{table}' is not allowed"):
        server._validate_readonly_sql(f"SELECT * FROM system.{table}")


def test_information_schema_rejected():
    with pytest.raises(ValueError, match="information_schema/mysql"):
        server._validate_readonly_sql("SELECT * FROM information_schema.tables")


def test_mysql_schema_rejected():
    with pytest.raises(ValueError, match="information_schema/mysql"):
        server._validate_readonly_sql("SELECT * FROM mysql.user")


# ---------------------------------------------------------------------------
# Allowlist enforcement (existing behavior, incl. the join regression the
# module docstring calls out by name)
# ---------------------------------------------------------------------------

def test_allowed_table_passes():
    server._validate_readonly_sql("SELECT * FROM agent_events")


def test_disallowed_table_rejected():
    with pytest.raises(ValueError, match=r"not in the allowlist.*secret_table"):
        server._validate_readonly_sql("SELECT * FROM secret_table")


def test_join_of_allowed_and_disallowed_table_rejected():
    # The exact regression the module docstring calls out: joining an
    # allowlisted table together with an out-of-allowlist one used to pass
    # because the old check only required "some allowed name somewhere".
    sql = "SELECT * FROM agent_events JOIN secret_table ON agent_events.id = secret_table.id"
    with pytest.raises(ValueError, match=r"not in the allowlist.*secret_table"):
        server._validate_readonly_sql(sql)


def test_query_with_no_from_join_rejected():
    with pytest.raises(ValueError, match="must reference at least one"):
        server._validate_readonly_sql("SELECT 1")


# ---------------------------------------------------------------------------
# CTE names excluded from the allowlist requirement
# ---------------------------------------------------------------------------

def test_cte_name_not_required_to_be_allowlisted():
    sql = "WITH foo AS (SELECT 1 AS x FROM agent_events) SELECT * FROM foo"
    assert server._validate_readonly_sql(sql) == sql


def test_cte_names_helper_extracts_definitions_only():
    sql = "WITH foo AS (SELECT 1), bar AS (SELECT 2) SELECT * FROM foo JOIN bar ON 1 = 1"
    assert server._cte_names(sql) == {"foo", "bar"}


def test_referenced_tables_excludes_cte_names():
    sql = "WITH foo AS (SELECT 1 FROM agent_events) SELECT * FROM foo"
    assert server._referenced_tables(sql) == {"agent_events"}


def test_referenced_tables_handles_comma_join_and_schema_qualifier():
    sql = "SELECT * FROM agent_events, agent_usage JOIN default.agent_messages ON 1 = 1"
    assert server._referenced_tables(sql) == {"agent_events", "agent_usage", "agent_messages"}


def test_array_join_does_not_misparse_array_expression_as_table():
    # Regression for the real panel-76 ("Trace") failure.
    # `ARRAY JOIN arrayMap(...)` must not have `arrayMap` picked up as a
    # joined table.
    # `_TABLE_REFS_RE` matches `ARRAY JOIN` with group(1) = None precisely
    # so `_referenced_tables` skips it instead of treating it as a
    # FROM/JOIN target.
    sql = "SELECT * FROM agent_events ARRAY JOIN arrayMap(x -> x, arr) AS y"
    assert server._referenced_tables(sql) == {"agent_events"}
    server._validate_readonly_sql(sql)


# ---------------------------------------------------------------------------
# Comment stripping (existing behavior)
# ---------------------------------------------------------------------------

def test_line_comment_stripped():
    stripped = server._strip_sql_comments("SELECT 1 -- trailing comment\nFROM agent_events")
    assert "trailing comment" not in stripped


def test_block_comment_stripped():
    stripped = server._strip_sql_comments("SELECT 1 /* a block comment */ FROM agent_events")
    assert "a block comment" not in stripped


def test_line_comment_marker_inside_string_not_stripped():
    stripped = server._strip_sql_comments("SELECT '--not a comment' AS x FROM agent_events")
    assert stripped == "SELECT '--not a comment' AS x FROM agent_events"


def test_block_comment_marker_inside_string_not_stripped():
    stripped = server._strip_sql_comments("SELECT '/* not a comment */' AS x FROM agent_events")
    assert stripped == "SELECT '/* not a comment */' AS x FROM agent_events"


def test_doubled_quote_does_not_end_string_early():
    # Without doubled-quote awareness, the "--" here would incorrectly be
    # treated as a real comment start.
    sql = "SELECT 'it''s fine -- not a comment' AS x FROM agent_events"
    stripped = server._strip_sql_comments(sql)
    assert stripped == sql


def test_adversarial_comment_payload_still_blocked_end_to_end():
    # The exact payload documented in _strip_sql_comments's own docstring:
    # a trailing `-- agent_events` comment used to supply a fake allowlist
    # token while the real (disallowed) FROM target went unchecked.
    sql = "SELECT 1) UNION ALL SELECT * FROM secret_table -- agent_events"
    with pytest.raises(ValueError, match=r"not in the allowlist.*secret_table"):
        server._validate_readonly_sql(sql)


# ---------------------------------------------------------------------------
# Semicolon handling (existing behavior)
# ---------------------------------------------------------------------------

def test_single_trailing_semicolon_accepted():
    assert server._validate_readonly_sql("SELECT 1 FROM agent_events;") == "SELECT 1 FROM agent_events"


def test_trailing_semicolon_with_whitespace_accepted():
    assert server._validate_readonly_sql("SELECT 1 FROM agent_events ;  \n") == "SELECT 1 FROM agent_events"


def test_second_real_statement_after_semicolon_rejected():
    sql = "SELECT 1 FROM agent_events; DROP TABLE agent_events"
    with pytest.raises(ValueError, match="Only a single statement is allowed"):
        server._validate_readonly_sql(sql)


# ---------------------------------------------------------------------------
# New coverage: quote-aware masking fixes false positives from string
# literal content
# ---------------------------------------------------------------------------

def test_semicolons_inside_string_literal_accepted():
    sql = "SELECT 'a;b;c' AS x FROM agent_events"
    assert server._validate_readonly_sql(sql) == sql


def test_html_entity_semicolons_inside_string_literal_accepted():
    # Shape of the real dashboard panels (agents_overview.json panels 76
    # and 99): Dynamic Text panels build HTML output whose string literals
    # contain entity-escaped output, every one of which ends in `;`.
    sql = (
        "SELECT '&amp;lt;span&amp;gt;text&amp;lt;/span&amp;gt;' AS html "
        "FROM agent_events"
    )
    assert server._validate_readonly_sql(sql) == sql


def test_forbidden_keyword_as_plain_text_in_string_accepted():
    sql = "SELECT 'please DELETE this row manually' AS x FROM agent_events"
    assert server._validate_readonly_sql(sql) == sql


def test_another_forbidden_keyword_as_plain_text_in_string_accepted():
    sql = "SELECT 'DROP the old habit' AS x FROM agent_events"
    assert server._validate_readonly_sql(sql) == sql


def test_forbidden_table_function_as_plain_text_in_string_accepted():
    sql = "SELECT 'call REMOTE(evil) if you dare' AS x FROM agent_events"
    assert server._validate_readonly_sql(sql) == sql


def test_system_dot_looking_text_in_string_accepted():
    sql = "SELECT 'see system.foo for details' AS x FROM agent_events"
    assert server._validate_readonly_sql(sql) == sql


def test_information_schema_looking_text_in_string_accepted():
    sql = "SELECT 'query information_schema.tables please' AS x FROM agent_events"
    assert server._validate_readonly_sql(sql) == sql


def test_masking_does_not_blind_real_semicolon_outside_strings():
    # Multiple string literals carry false-positive-shaped content, but a
    # genuine second statement after a real (out-of-string) semicolon must
    # still be rejected - masking only tolerates in-string content, not
    # actual SQL injection.
    sql = "SELECT 'a;b' AS x, 'please DELETE' AS y FROM agent_events; DROP TABLE agent_events"
    with pytest.raises(ValueError, match="Only a single statement is allowed"):
        server._validate_readonly_sql(sql)


def test_doubled_quote_escape_then_semicolon_content_accepted():
    sql = "SELECT 'it''s a;b;c' AS x FROM agent_events"
    assert server._validate_readonly_sql(sql) == sql


def test_trailing_semicolon_strip_is_quote_aware():
    # A real trailing terminator is stripped...
    assert server._validate_readonly_sql("SELECT 'a' AS x FROM agent_events;") == \
        "SELECT 'a' AS x FROM agent_events"
    # ...but a string literal that happens to end in a `;`-like character
    # right at the very end of the query is NOT a real terminator and must
    # not be stripped or otherwise misparsed.
    sql = "SELECT 'ends with semicolon;' AS x FROM agent_events"
    assert server._validate_readonly_sql(sql) == sql


def test_panel_76_99_shaped_dynamic_text_query_accepted():
    # A minimal repro of the real panel-76/99 rawSql shape (Dynamic Text
    # panels building HTML rows with entity-escaped output) - before the
    # fix this was rejected purely because of the literal `;` characters
    # inside these string literals.
    sql = (
        "SELECT concat("
        "'<div class=\"t-row\"&gt;', "
        "'&amp;lt;span class=&amp;quot;t-label&amp;quot;&amp;gt;', "
        "toString(session_id), "
        "'&amp;lt;/span&amp;gt;&amp;lt;/div&amp;gt;'"
        ") AS html_row "
        "FROM agent_events "
        "WHERE session_id != ''"
    )
    assert server._validate_readonly_sql(sql) == sql


# ---------------------------------------------------------------------------
# _mask_string_literals - direct unit coverage
# ---------------------------------------------------------------------------

def test_mask_string_literals_preserves_length_and_positions():
    sql = "SELECT 'abc;def' FROM t"
    masked = server._mask_string_literals(sql)
    assert len(masked) == len(sql)
    assert masked == "SELECT 'xxxxxxx' FROM t"


def test_mask_string_literals_keeps_quote_characters():
    sql = "SELECT 'hi' FROM t"
    assert server._mask_string_literals(sql) == "SELECT 'xx' FROM t"


def test_mask_string_literals_leaves_non_string_text_untouched():
    sql = "SELECT DELETE_ATTEMPTS FROM agent_events WHERE x = 'DELETE'"
    masked = server._mask_string_literals(sql)
    assert masked.startswith("SELECT DELETE_ATTEMPTS FROM agent_events WHERE x = ")
    assert "DELETE" not in masked.split("=", 1)[1]


def test_mask_string_literals_handles_doubled_quote_escape():
    sql = "SELECT 'a''b' FROM t"
    assert server._mask_string_literals(sql) == "SELECT 'x''x' FROM t"


def test_mask_string_literals_handles_unterminated_string_without_crashing():
    # Pathological input (unterminated string literal at end of text) -
    # must not raise or infinite-loop; the trailing content is masked as
    # if the string ran to the end.
    sql = "SELECT 'abc;"
    masked = server._mask_string_literals(sql)
    assert len(masked) == len(sql)
    assert masked == "SELECT 'xxxx"


def test_mask_string_literals_multiple_literals_independently_masked():
    sql = "SELECT 'first;one', 'second;two' FROM t"
    masked = server._mask_string_literals(sql)
    assert ";" not in masked
    assert len(masked) == len(sql)
