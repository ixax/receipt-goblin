#!/usr/bin/env python3
"""Deterministic before/after query-performance benchmarking toolkit for
services/grafana/dashboards/agents_overview.json.

Exists so that "how fast is panel X" / "did this rewrite actually help"
always gets answered the same way, by the same code, instead of being
re-derived by an LLM (Grafana macro/variable substitution, run bookkeeping,
diff math) on every ask - see the sql-expert agent
(.claude/agents/sql-expert.md) for the full workflow this supports, and the
query-perf-runner agent (.claude/agents/query-perf-runner.md) for who
actually executes it (this script has no ClickHouse access itself - the
`profile_query` MCP tool call in between `resolve` and `save-run` is the
one step only an agent can do).

Three subcommands, meant to be chained:

    resolve   dashboard.json [--panels 73,74 | --all] [--hours N] [--var name=value ...] --out resolved.json
              Deterministic. Extracts each selected panel's rawSql
              query/queries and substitutes Grafana macros/template
              variables with concrete literals (see SINGLEQUOTE_VAR_DEFAULTS/
              BARE_VAR_DEFAULTS below - one fixed table, not re-guessed per
              call). Writes resolved.json: one entry per (panel, query
              index) with both raw_sql and resolved_sql, plus any
              still-unresolved $variable found (which means this table
              needs a new entry, not that the caller should guess a value).

    save-run  --resolved resolved.json --stats stats.json --label before|after|<anything> --out run.json
              Deterministic. Merges resolved.json's panel/query identity
              with stats.json's per-query profile_query results (keyed
              "<panel_id>:<query_index>") into one timestamped run record.
              stats.json is written by whichever agent actually called
              `mcp__dev__profile_query` for each resolved_sql - this
              script never talks to ClickHouse itself.

    diff      run_a.json run_b.json
              Deterministic. Prints a per-query before/after table (memory,
              read_rows, read_bytes, duration - absolute and % change) plus
              a totals row. Exit code is non-zero if run_b.json is not
              strictly better on every metric summed across all queries -
              useful as a pass/fail signal, not just a report.

    report    run.json
              Deterministic. Prints one run's per-query stats with no
              comparison - for "what does the dashboard cost right now",
              not a before/after ask.

Run artifacts land in .claude/data/query_perf_runs/ (gitignored, and unlike
the rest of .claude/data/ - see AGENTS.md - not scratch: these are meant to
persist and be diffed against later, potentially across sessions) unless
--out points elsewhere.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_dashboard as pd  # noqa: E402 - reuse its JSON-walking helpers

# services/grafana/scripts/query_perf.py -> repo root is 4 parents up.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
RUNS_DIR = REPO_ROOT / ".claude" / "data" / "query_perf_runs"

# One fixed substitution table for every variable this dashboard actually
# has (see spec.variables in agents_overview.json) - not inferred per call.
# Values match the dashboard's own "no filter" / default state. Override
# any of these per invocation with --var name=value.
SINGLEQUOTE_VAR_DEFAULTS = {
    # ${name:singlequote} form - used as has([${name:singlequote}], col)
    "git_repo": "__all__",
    "git_branch": "__all__",
    "issue_id": "__all__",
    "model": "__all__",
    "agent_name": "__all__",
    "skill_name": "__all__",
    "command_name": "__all__",
    "mcp_tool": "__all__",
    "group_id": "__all__",
    "user_id": "__all__",
    # session_id's own no-filter sentinel is '' (has([${session_id:singlequote}], ''))
    # in this dashboard's queries, not '__all__' like every other multi-select -
    # confirmed against the actual rawSql, don't "fix" this to '__all__'.
    "session_id": "",
    # Client-attribution variables (migration 016_client_attribution, Clients tab).
    "client_product": "__all__",
    "client_surface": "__all__",
    "ingest_path": "__all__",
}
BARE_VAR_DEFAULTS = {
    # bare $name form, used inside existing quotes/expressions
    "provider": "all",
    "include_cache_tokens": "1",
    "window": "3600",
    "trace_width_budget": "120",
    "trace_ts": "",
    # Panel 99 (Fork tree) interpolates the bare $session_id form inside its own quotes, unlike the
    # ${session_id:singlequote} spelling every other session-scoped panel uses.
    # Same no-filter sentinel as its singlequote twin above: this panel is a drill-down and is meant to be
    # empty until a session is picked.
    "session_id": "",
}
DEFAULT_HOURS = 24

_TIME_FILTER_OPEN = "$__timeFilter("
_SINGLEQUOTE_RE = re.compile(r"\$\{(\w+):singlequote\}")
# Both spellings Grafana accepts for the same variable: $name and ${name}.
# The braced form is what every `toStartOfInterval(..., INTERVAL ${window} SECOND)` in this dashboard uses, and
# missing it left those queries with a literal "${window}" that is not valid SQL.
_BARE_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)\b")


def _substitute_time_filter(sql: str, hours: int) -> str:
    """Replaces every `$__timeFilter(<expr>)` with `<expr> >= now() - INTERVAL <hours> HOUR`.

    Scans for the matching close paren rather than using a regex, because `<expr>` is often itself a call -
    `$__timeFilter(toDateTime(s.day))` on the Subscription tab is the case that matters.
    A regex stopping at the first ")" swallowed the inner one and produced `toDateTime(s.day >= now() - ...)`,
    which ClickHouse rejects as "Invalid type for filter in WHERE: DateTime" - a resolver artifact that looks
    exactly like a broken panel.
    """
    out = []
    rest = sql
    while True:
        start = rest.find(_TIME_FILTER_OPEN)
        if start == -1:
            out.append(rest)
            return "".join(out)
        cursor = start + len(_TIME_FILTER_OPEN)
        depth = 1
        while cursor < len(rest) and depth:
            depth += {"(": 1, ")": -1}.get(rest[cursor], 0)
            cursor += 1
        if depth:  # unbalanced - leave the macro alone rather than mangle the query
            out.append(rest)
            return "".join(out)
        expression = rest[start + len(_TIME_FILTER_OPEN):cursor - 1]
        out.append(rest[:start])
        out.append(f"{expression} >= now() - INTERVAL {hours} HOUR")
        rest = rest[cursor:]


def resolve_sql(raw_sql: str, hours: int, overrides: dict) -> tuple:
    """Returns (resolved_sql, unresolved_names). unresolved_names is
    non-empty when the query references a $variable this table doesn't
    know about - the caller (save-run/the agent) must not silently run
    that query, since a leftover $name is not valid SQL."""
    sql = raw_sql
    sql = sql.replace("$__fromTime", f"(now() - INTERVAL {hours} HOUR)")
    sql = sql.replace("$__toTime", "now()")
    sql = _substitute_time_filter(sql, hours)

    def _sq(m):
        name = m.group(1)
        val = overrides.get(name, SINGLEQUOTE_VAR_DEFAULTS.get(name))
        if val is None:
            return m.group(0)
        return f"'{val}'"
    sql = _SINGLEQUOTE_RE.sub(_sq, sql)

    def _bare(m):
        name = m.group(1) or m.group(2)
        if name in overrides:
            return overrides[name]
        if name in BARE_VAR_DEFAULTS:
            return BARE_VAR_DEFAULTS[name]
        return m.group(0)
    sql = _BARE_VAR_RE.sub(_bare, sql)

    unresolved = sorted(
        {name for pair in _BARE_VAR_RE.findall(sql) for name in pair if name}
        | set(_SINGLEQUOTE_RE.findall(sql))
    )
    return sql, unresolved


def _iter_selected_panels(spec, panel_ids):
    for title, tab_layout in pd.iter_tabs(spec):
        for ref in pd.iter_panel_refs(tab_layout):
            panel = pd.panel_by_ref(spec, ref)
            if not panel:
                continue
            pspec = panel.get("spec", {})
            pid = pspec.get("id")
            if panel_ids is not None and pid not in panel_ids:
                continue
            if pid in (76, 77):
                continue  # Trace + companion detail table - not plain SQL panels, always excluded
            yield title, pid, pspec


def cmd_resolve(args):
    d = pd.load(args.file)
    spec = d["spec"]
    panel_ids = None
    if not args.all:
        if not args.panels:
            print("error: pass --panels 73,74,... or --all", file=sys.stderr)
            sys.exit(2)
        panel_ids = {int(x) for x in args.panels.split(",")}

    overrides = {}
    for kv in args.var or []:
        if "=" not in kv:
            print(f"error: --var must be name=value, got {kv!r}", file=sys.stderr)
            sys.exit(2)
        name, val = kv.split("=", 1)
        overrides[name] = val

    panels_out = []
    for tab_title, pid, pspec in _iter_selected_panels(spec, panel_ids):
        queries = pspec.get("data", {}).get("spec", {}).get("queries", [])
        q_out = []
        for i, q in enumerate(queries):
            qspec = q.get("spec", {}).get("query", {}).get("spec", {})
            raw_sql = qspec.get("rawSql")
            if not raw_sql:
                continue
            resolved, unresolved = resolve_sql(raw_sql, args.hours, overrides)
            q_out.append({
                "query_index": i,
                "raw_sql": raw_sql,
                "resolved_sql": resolved,
                "unresolved_vars": unresolved,
            })
        if q_out:
            panels_out.append({
                "tab": tab_title,
                "id": pid,
                "title": pspec.get("title"),
                "queries": q_out,
            })

    if panel_ids is not None:
        found = {p["id"] for p in panels_out}
        missing = panel_ids - found
        if missing:
            print(f"warning: panel id(s) not found or have no rawSql: {sorted(missing)}", file=sys.stderr)

    out = {
        "dashboard_file": args.file,
        "hours": args.hours,
        "overrides": overrides,
        "panels": panels_out,
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    n_queries = sum(len(p["queries"]) for p in panels_out)
    n_unresolved = sum(1 for p in panels_out for q in p["queries"] if q["unresolved_vars"])
    print(f"resolved {len(panels_out)} panel(s), {n_queries} query/queries -> {args.out}")
    if n_unresolved:
        print(f"warning: {n_unresolved} query/queries have unresolved $variables - see resolved_sql/unresolved_vars in the output file. Add a --var override or extend SINGLEQUOTE_VAR_DEFAULTS/BARE_VAR_DEFAULTS in this script, don't hand-patch the SQL.", file=sys.stderr)


def cmd_save_run(args):
    resolved = json.loads(Path(args.resolved).read_text())
    stats = json.loads(Path(args.stats).read_text())

    panels_out = []
    missing_stats = []
    for panel in resolved["panels"]:
        q_out = []
        for q in panel["queries"]:
            key = f"{panel['id']}:{q['query_index']}"
            if key not in stats:
                missing_stats.append(key)
                continue
            q_out.append({**q, "profile": stats[key]})
        panels_out.append({**panel, "queries": q_out})

    if missing_stats:
        print(f"warning: no profile_query result for {len(missing_stats)} query/queries: {missing_stats}", file=sys.stderr)

    run = {
        "label": args.label,
        "note": args.note or "",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dashboard_file": resolved["dashboard_file"],
        "hours": resolved["hours"],
        "overrides": resolved["overrides"],
        "panels": panels_out,
    }
    out_path = Path(args.out) if args.out else RUNS_DIR / f"run-{args.label}-{int(time.time())}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(run, indent=2))
    n_queries = sum(len(p["queries"]) for p in panels_out)
    print(f"saved run '{args.label}' ({n_queries} query/queries) -> {out_path}")


def _query_index(run):
    idx = {}
    for panel in run["panels"]:
        for q in panel["queries"]:
            idx[(panel["id"], q["query_index"])] = (panel["tab"], panel["title"], q)
    return idx


def _pct(before, after):
    if before in (None, 0):
        return None
    return round((after - before) / before * 100, 1)


def cmd_diff(args):
    run_a = json.loads(Path(args.run_a).read_text())
    run_b = json.loads(Path(args.run_b).read_text())
    idx_a = _query_index(run_a)
    idx_b = _query_index(run_b)

    keys = sorted(set(idx_a) & set(idx_b))
    only_a = sorted(set(idx_a) - set(idx_b))
    only_b = sorted(set(idx_b) - set(idx_a))

    print(f"# {run_a['label']!r} ({run_a['created_at']}) -> {run_b['label']!r} ({run_b['created_at']})")
    print(f"{'panel':<6} {'query':<50} {'mem before':>12} {'mem after':>12} {'mem %':>8} "
          f"{'rows before':>12} {'rows after':>12} {'rows %':>8} "
          f"{'dur before(ms)':>15} {'dur after(ms)':>14} {'dur %':>8}")

    totals = {"mem_before": 0, "mem_after": 0, "rows_before": 0, "rows_after": 0, "dur_before": 0.0, "dur_after": 0.0}
    worse = []
    for pid, qi in keys:
        tab, title, qa = idx_a[(pid, qi)]
        _, _, qb = idx_b[(pid, qi)]
        pa, pb = qa["profile"], qb["profile"]
        if pa.get("error") or pb.get("error"):
            print(f"{pid:<6} {title[:48]:<50} ERROR: before={pa.get('error')!r} after={pb.get('error')!r}")
            continue
        mem_a, mem_b = pa.get("memory_usage_bytes"), pb.get("memory_usage_bytes")
        rows_a, rows_b = pa["read_rows"], pb["read_rows"]
        dur_a, dur_b = pa["query_duration_ms"], pb["query_duration_ms"]
        label = f"{title} [{qi}]"
        print(f"{pid:<6} {label[:48]:<50} "
              f"{mem_a if mem_a is not None else 'n/a':>12} {mem_b if mem_b is not None else 'n/a':>12} "
              f"{(str(_pct(mem_a, mem_b)) + '%') if mem_a is not None and mem_b is not None else 'n/a':>8} "
              f"{rows_a:>12} {rows_b:>12} {str(_pct(rows_a, rows_b)) + '%':>8} "
              f"{dur_a:>15} {dur_b:>14} {str(_pct(dur_a, dur_b)) + '%':>8}")
        totals["rows_before"] += rows_a
        totals["rows_after"] += rows_b
        totals["dur_before"] += dur_a
        totals["dur_after"] += dur_b
        if mem_a is not None and mem_b is not None:
            totals["mem_before"] += mem_a
            totals["mem_after"] += mem_b
        if rows_b > rows_a or dur_b > dur_a:
            worse.append((pid, qi, title))

    print("-" * 60)
    print(f"TOTAL{'':<52} mem {_pct(totals['mem_before'], totals['mem_after'])}%   "
          f"rows {_pct(totals['rows_before'], totals['rows_after'])}%   "
          f"duration {_pct(totals['dur_before'], totals['dur_after'])}%")

    if only_a:
        print(f"\nonly in {run_a['label']!r} (missing from {run_b['label']!r}): {only_a}")
    if only_b:
        print(f"\nonly in {run_b['label']!r} (missing from {run_a['label']!r}): {only_b}")
    if worse:
        print(f"\n{len(worse)} query/queries got WORSE on rows and/or duration: {[(p, i, t) for p, i, t in worse]}")

    if worse or only_a:
        sys.exit(1)


def cmd_report(args):
    run = json.loads(Path(args.run).read_text())
    print(f"# {run['label']!r} ({run['created_at']}) - hours={run['hours']} overrides={run['overrides']}")
    print(f"{'panel':<6} {'query':<50} {'memory bytes':>14} {'read_rows':>12} {'read_bytes':>12} {'duration(ms)':>14}")
    totals = {"mem": 0, "rows": 0, "bytes": 0, "dur": 0.0}
    for panel in run["panels"]:
        for q in panel["queries"]:
            prof = q["profile"]
            if prof.get("error"):
                print(f"{panel['id']:<6} {panel['title'][:48]:<50} ERROR: {prof['error']}")
                continue
            label = f"{panel['title']} [{q['query_index']}]"
            mem = prof.get("memory_usage_bytes")
            print(f"{panel['id']:<6} {label[:48]:<50} {mem if mem is not None else 'n/a':>14} "
                  f"{prof['read_rows']:>12} {prof['read_bytes']:>12} {prof['query_duration_ms']:>14}")
            if mem is not None:
                totals["mem"] += mem
            totals["rows"] += prof["read_rows"]
            totals["bytes"] += prof["read_bytes"]
            totals["dur"] += prof["query_duration_ms"]
    print("-" * 60)
    print(f"TOTAL{'':<52} {totals['mem']:>14} {totals['rows']:>12} {totals['bytes']:>12} {round(totals['dur'], 1):>14}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("resolve")
    p.add_argument("file")
    p.add_argument("--panels", help="comma-separated panel ids, e.g. 73,74")
    p.add_argument("--all", action="store_true", help="resolve every panel (except 76/77)")
    p.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    p.add_argument("--var", action="append", help="name=value override, repeatable")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("save-run")
    p.add_argument("--resolved", required=True)
    p.add_argument("--stats", required=True, help='JSON: {"<panel_id>:<query_index>": <profile_query result>, ...}')
    p.add_argument("--label", required=True)
    p.add_argument("--note")
    p.add_argument("--out")
    p.set_defaults(func=cmd_save_run)

    p = sub.add_parser("diff")
    p.add_argument("run_a")
    p.add_argument("run_b")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("report")
    p.add_argument("run")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
