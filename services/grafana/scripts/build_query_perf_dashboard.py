#!/usr/bin/env python3
"""Generate/update the query-performance companion dashboard.

Reads panel metadata (id, title, tab/sub-tab path) for a given tab of
services/grafana/dashboards/agents_overview.json and emits, for each
panel already tagged by tag_panel_queries.py, a collapsible row in
services/grafana/dashboards-health/query_performance.json titled
"[<id>] <source title>", containing 4 panels:

  - Duration: query_duration_ms over time
  - Memory usage: memory_usage (bytes) over time
  - Rows / bytes read: read_rows / read_bytes over time
  - Recent executions: table of event_time, duration, memory, rows, bytes, query_id

All four are filtered by `log_comment = 'agents_overview:panel_<id>'`
against system.query_log - see tag_panel_queries.py for how that tag
gets onto the source panel's rawSql, and AGENTS.md for the known
CLICKHOUSE_USER grant fragility this depends on
(SELECT ON system.query_log).

The output dashboard's tab/sub-tab layout mirrors the source tab's
structure 1:1 (flat tab -> a RowsLayout of one collapsible row per
source panel; tab with sub-tabs -> nested TabsLayout, one level deep,
matching what agents_overview.json itself uses, with a RowsLayout at
each sub-tab leaf - see parse_dashboard.py's module docstring for the
v2beta1 TabsLayout/GridLayout shape, and dashboards-health/infra_overview.json
or docker_containers.json for the RowsLayout/RowsLayoutRow shape this
reuses).

Usage:
  build_query_perf_dashboard.py --tab "Top N" \\
      --source services/grafana/dashboards/agents_overview.json \\
      --out services/grafana/dashboards-health/query_performance.json

Re-running regenerates the given tab's panels from scratch (other tabs
already present in --out are left untouched) - this is the mechanism
dashboard-panels-builder uses to keep the mirror in sync after a panel
is added/edited/removed in agents_overview.json (see SKILL.md).
"""
import argparse
import json
import sys
import textwrap

TAG_PREFIX = "agents_overview:panel_"
PANEL_W, PANEL_H = 24, 8
BASE_ID = 100000  # new-dashboard panel ids: BASE_ID + source_panel_id * 10 + metric_index

QUERY_DETAIL_TAB_TITLE = "Query Detail"
QUERY_DETAIL_BASE_ID = 99000  # separate range from BASE_ID's per-source-panel ids


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def walk_source_layout(layout, path):
    """Yield (path_titles, [panel_refs]) for every leaf GridLayout in agents_overview.json."""
    if layout.get("kind") == "GridLayout":
        ids = []
        for item in layout["spec"]["items"]:
            el = item["spec"]["element"]
            if el.get("kind") == "ElementReference":
                ids.append(el["name"])
        yield path, ids
    elif layout.get("kind") == "TabsLayout":
        for tab in layout["spec"]["tabs"]:
            tspec = tab["spec"]
            yield from walk_source_layout(tspec["layout"], path + [tspec["title"]])


def refs_in_output_layout(layout):
    """All panel refs referenced anywhere under a query_performance.json layout
    (TabsLayout / RowsLayout / GridLayout, in any nesting)."""
    refs = set()
    kind = layout.get("kind")
    if kind == "GridLayout":
        for item in layout["spec"]["items"]:
            el = item["spec"]["element"]
            if el.get("kind") == "ElementReference":
                refs.add(el["name"])
    elif kind == "TabsLayout":
        for tab in layout["spec"]["tabs"]:
            refs |= refs_in_output_layout(tab["spec"]["layout"])
    elif kind == "RowsLayout":
        for row in layout["spec"]["rows"]:
            refs |= refs_in_output_layout(row["spec"]["layout"])
    return refs


def find_tab_layout(spec, tab_title):
    for tab in spec["layout"]["spec"]["tabs"]:
        if tab["spec"]["title"] == tab_title:
            return tab["spec"]["layout"]
    return None


def is_tagged(panel_spec, panel_id):
    marker = f"{TAG_PREFIX}{panel_id}'"
    for q in panel_spec["data"]["spec"]["queries"]:
        if marker in q["spec"]["query"]["spec"]["rawSql"]:
            return True
    return False


def query(ref_id, raw_sql, fmt):
    return {
        "kind": "PanelQuery",
        "spec": {
            "refId": ref_id,
            "hidden": False,
            "query": {
                "kind": "DataQuery",
                "group": "grafana-clickhouse-datasource",
                "version": "v0",
                "datasource": {"name": "clickhouse"},
                "spec": {"editorType": "sql", "format": fmt, "rawSql": raw_sql},
            },
        },
    }


def timeseries_panel(new_id, title, unit, raw_sql, single_series, series_colors=None):
    field_defaults = {
        "unit": unit,
        "custom": {
            "drawStyle": "line",
            "lineInterpolation": "smooth",
            "lineWidth": 1,
            "spanNulls": True,
            "showPoints": "auto",
            "pointSize": 4,
            "fillOpacity": 0,
            "stacking": {"mode": "none", "group": "A"},
            "thresholdsStyle": {"mode": "off"},
        },
    }
    overrides = []
    if single_series:
        field_defaults["color"] = {"mode": "fixed", "fixedColor": "blue"}
        legend = {"showLegend": False}
    else:
        for field_name, color in series_colors.items():
            overrides.append(
                {
                    "matcher": {"id": "byName", "options": field_name},
                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
                }
            )
        legend = {"showLegend": True, "displayMode": "list", "placement": "right"}

    return {
        "kind": "Panel",
        "spec": {
            "id": new_id,
            "title": title,
            "description": "",
            "links": [],
            "transparent": False,
            "data": {
                "kind": "QueryGroup",
                "spec": {
                    "queries": [query("A", raw_sql, 0)],
                    "transformations": [],
                    "queryOptions": {},
                },
            },
            "vizConfig": {
                "kind": "VizConfig",
                "group": "timeseries",
                "version": "",
                "spec": {
                    "options": {"legend": legend, "tooltip": {"mode": "multi"}},
                    "fieldConfig": {"defaults": field_defaults, "overrides": overrides},
                },
            },
        },
    }


def query_id_link_properties():
    """Same convention as session_id links (dashboard-panels/SKILL.md): a
    regex-all mapping shows a short "open ↗" label instead of the raw id,
    and the dataLink uses __value.raw (not .text, which now resolves to the
    mapped label) plus __url_time_range so the date range travels with the
    link even though the destination tab's own queries ignore it.

    targetBlank is False here (unlike session_id's Trace link) because this
    is a same-dashboard dtab= drill-down, confirmed live to always open in
    the same browser tab regardless of the flag - leaving it True would
    just misrepresent behavior the link doesn't actually have."""
    return [
        {
            "id": "mappings",
            "value": [{"type": "regex", "options": {"pattern": ".*", "result": {"text": "open ↗"}}}],
        },
        {
            "id": "links",
            "value": [
                {
                    "title": "Open query detail",
                    "url": f"/d/${{__dashboard.uid}}?${{__url_time_range}}&var-query_id=${{__value.raw}}&dtab={QUERY_DETAIL_TAB_TITLE.replace(' ', '-')}",
                    "targetBlank": False,
                }
            ],
        },
    ]


def long_text_override(col):
    """dashboard-panels/SKILL.md convention for a long free-text/JSON column:
    no fixed width (auto-fills), eye-icon inspect instead of truncating -
    the inspect drawer renders the raw value monospaced with real line
    breaks/whitespace preserved, unlike the table's own text-wrap feature
    (which as of Grafana's current release does not reliably honor
    embedded newlines - see grafana/grafana#91111/#89915)."""
    return {
        "matcher": {"id": "byName", "options": col},
        "properties": [
            {"id": "custom.cellOptions", "value": {"type": "auto"}},
            {"id": "custom.inspect", "value": True},
        ],
    }


def numeric_override(col, unit="locale", width=None):
    props = [{"id": "unit", "value": unit}]
    if width:
        props.append({"id": "custom.width", "value": width})
    return {"matcher": {"id": "byName", "options": col}, "properties": props}


def table_panel(new_id, title, raw_sql, sort_field="event_time", link_query_id=False, extra_overrides=None):
    col_units = {
        "query_duration_ms": ("ms", 130),
        "memory_usage": ("bytes", 130),
        "read_rows": ("locale", 130),
        "read_bytes": ("bytes", 130),
        "query_id": (None, 220),
    }
    overrides = []
    for col, (unit, width) in col_units.items():
        props = [{"id": "custom.width", "value": width}]
        if unit:
            props.insert(0, {"id": "unit", "value": unit})
        if col == "query_id" and link_query_id:
            props.extend(query_id_link_properties())
        overrides.append({"matcher": {"id": "byName", "options": col}, "properties": props})
    overrides.extend(extra_overrides or [])

    options = {"sortBy": [{"displayName": sort_field, "desc": True}]} if sort_field else {}

    return {
        "kind": "Panel",
        "spec": {
            "id": new_id,
            "title": title,
            "description": "",
            "links": [],
            "transparent": False,
            "data": {
                "kind": "QueryGroup",
                "spec": {
                    "queries": [query("A", raw_sql, 1)],
                    "transformations": [],
                    "queryOptions": {},
                },
            },
            "vizConfig": {
                "kind": "VizConfig",
                "group": "table",
                "version": "",
                "spec": {
                    "options": options,
                    "fieldConfig": {"defaults": {"custom": {}}, "overrides": overrides},
                },
            },
        },
    }


def build_panels_for(source_id):
    """Panel titles are just the metric name - the source panel's own id/title
    goes on the enclosing collapsible row instead (see build_row_for)."""
    tag = f"{TAG_PREFIX}{source_id}"
    where = f"log_comment = '{tag}' AND type = 'QueryFinish' AND $__timeFilter(event_time)"
    base = BASE_ID + source_id * 10

    duration_sql = textwrap.dedent(f"""\
        SELECT event_time AS time, query_duration_ms
        FROM system.query_log
        WHERE {where}
        ORDER BY time""")
    memory_sql = textwrap.dedent(f"""\
        SELECT event_time AS time, memory_usage
        FROM system.query_log
        WHERE {where}
        ORDER BY time""")
    rows_bytes_sql = textwrap.dedent(f"""\
        SELECT event_time AS time, read_rows, read_bytes
        FROM system.query_log
        WHERE {where}
        ORDER BY time""")
    table_sql = textwrap.dedent(f"""\
        SELECT event_time, query_duration_ms, memory_usage, read_rows, read_bytes, query_id
        FROM system.query_log
        WHERE {where}
        ORDER BY event_time DESC
        LIMIT 50""")

    panels = [
        timeseries_panel(base + 1, "Duration", "ms", duration_sql, True),
        timeseries_panel(base + 2, "Memory usage", "bytes", memory_sql, True),
        timeseries_panel(
            base + 3,
            "Rows / bytes read",
            "short",
            rows_bytes_sql,
            False,
            {"read_rows": "blue", "read_bytes": "white"},
        ),
        table_panel(base + 4, "Recent executions", table_sql, link_query_id=True),
    ]
    refs = [f"panel-{base + i}" for i in (1, 2, 3, 4)]
    return refs, panels


def build_grid(refs):
    """3 timeseries side by side (x0/8/16, width 8 each), table below (width 24)."""
    items = []
    for i, ref in enumerate(refs[:3]):
        items.append(
            {
                "kind": "GridLayoutItem",
                "spec": {"x": i * (PANEL_W // 3), "y": 0, "width": PANEL_W // 3, "height": PANEL_H, "element": {"kind": "ElementReference", "name": ref}},
            }
        )
    if len(refs) == 4:
        items.append(
            {
                "kind": "GridLayoutItem",
                "spec": {"x": 0, "y": PANEL_H, "width": PANEL_W, "height": PANEL_H, "element": {"kind": "ElementReference", "name": refs[3]}},
            }
        )
    return {"kind": "GridLayout", "spec": {"items": items}}


def build_row_for(source_id, source_title, refs):
    return {
        "kind": "RowsLayoutRow",
        "spec": {
            "title": f"[{source_id}] {source_title}",
            "collapse": False,
            "layout": build_grid(refs),
        },
    }


def build_rows_layout(source_panels):
    """source_panels: list of (source_id, source_title, refs)."""
    return {
        "kind": "RowsLayout",
        "spec": {"rows": [build_row_for(pid, title, refs) for pid, title, refs in source_panels]},
    }


def build_tab_layout(groups):
    """groups: list of (subtab_title_or_None, [(source_id, source_title, refs)])."""
    if len(groups) == 1 and groups[0][0] is None:
        return build_rows_layout(groups[0][1])
    tabs = []
    for subtitle, source_panels in groups:
        tabs.append({"kind": "TabsLayoutTab", "spec": {"title": subtitle, "layout": build_rows_layout(source_panels)}})
    return {"kind": "TabsLayout", "spec": {"tabs": tabs}}


def query_id_variable():
    """Plain text variable, not a dropdown - populated by clicking a query_id
    link (var-query_id=...), not meant for manual browsing (unlike
    agents_overview.json's session_id QueryVariable, which lists recent
    sessions - there's no equivalent "recent query_ids" concept worth a
    dropdown here)."""
    return {
        "kind": "TextVariable",
        "spec": {
            "name": "query_id",
            "label": "Query ID",
            "description": "Set via a query_id column link elsewhere in this dashboard, not typed by hand.",
            "current": {"text": "", "value": ""},
            "hide": "dontHide",
            "skipUrlSync": False,
            "query": "",
        },
    }


def build_query_detail_panels():
    """Every panel here filters by query_id alone, deliberately ignoring
    $__timeFilter/the dashboard time range - a query_id already pins one
    exact execution, so there's nothing for a time bound to narrow down,
    and the link that gets you here already carried __url_time_range only
    so the *other* tabs' state survives the round trip, not to constrain
    these queries. Trade-off: with no time/partition bound at all, this is
    a full scan of system.query_log by query_id on every load - acceptable
    for an occasional drill-down, not a panel meant for a busy dashboard."""
    where = "query_id = '$query_id' AND type = 'QueryFinish'"

    info_sql = textwrap.dedent(f"""\
        SELECT
          event_time, log_comment, query_duration_ms, memory_usage,
          read_rows, read_bytes, exception, user, is_initial_query
        FROM system.query_log
        WHERE {where}""")
    profile_events_sql = textwrap.dedent(f"""\
        SELECT kv.1 AS event, kv.2 AS value
        FROM (
          SELECT arrayJoin(CAST(ProfileEvents, 'Array(Tuple(String, UInt64))')) AS kv
          FROM system.query_log
          WHERE {where}
        )
        ORDER BY value DESC
        LIMIT 50""")
    settings_sql = textwrap.dedent(f"""\
        SELECT kv.1 AS setting, kv.2 AS value
        FROM (
          SELECT arrayJoin(CAST(Settings, 'Array(Tuple(String, String))')) AS kv
          FROM system.query_log
          WHERE {where}
        )
        ORDER BY setting
        LIMIT 100""")

    # QUERY_DETAIL_BASE_ID + 2 ("Full query text") is deliberately skipped
    # here - it's a hand-built marcusolsson-dynamictext-panel (Dynamic Text),
    # owned by the dynamictext-panel-builder agent, not this generator. Its
    # ref is still reserved in the returned refs list below so the grid
    # layout keeps its slot; main() must not prune that element away just
    # because this function didn't (re)build it.
    panels_by_id = {
        QUERY_DETAIL_BASE_ID + 1: table_panel(QUERY_DETAIL_BASE_ID + 1, "Query info", info_sql, sort_field=None),
        QUERY_DETAIL_BASE_ID + 3: table_panel(
            QUERY_DETAIL_BASE_ID + 3,
            "Profile events",
            profile_events_sql,
            sort_field="value",
            extra_overrides=[numeric_override("value")],
        ),
        QUERY_DETAIL_BASE_ID + 4: table_panel(QUERY_DETAIL_BASE_ID + 4, "Settings", settings_sql, sort_field=None),
    }
    refs = [f"panel-{QUERY_DETAIL_BASE_ID + i}" for i in (1, 2, 3, 4)]
    return refs, panels_by_id


DYNAMICTEXT_QUERY_DETAIL_REF = f"panel-{QUERY_DETAIL_BASE_ID + 2}"


def build_query_detail_tab(elements):
    refs, panels_by_id = build_query_detail_panels()
    for r in refs:
        pid = int(r.split("-")[1])
        if pid in panels_by_id:
            elements[r] = panels_by_id[pid]
    # refs[0]=Query info, refs[1]=Full query text (Dynamic Text), refs[2]=Profile events, refs[3]=Settings
    # Layout: Query info full-width on top; below, left column stacks
    # Settings then Profile events (50% width each row), right column is
    # the Dynamic Text query panel spanning the full height of that stack.
    items = [
        {"kind": "GridLayoutItem", "spec": {"x": 0, "y": 0, "width": 24, "height": 4, "element": {"kind": "ElementReference", "name": refs[0]}}},
        {"kind": "GridLayoutItem", "spec": {"x": 0, "y": 4, "width": 12, "height": 10, "element": {"kind": "ElementReference", "name": refs[3]}}},
        {"kind": "GridLayoutItem", "spec": {"x": 0, "y": 14, "width": 12, "height": 10, "element": {"kind": "ElementReference", "name": refs[2]}}},
        {"kind": "GridLayoutItem", "spec": {"x": 12, "y": 4, "width": 12, "height": 20, "element": {"kind": "ElementReference", "name": refs[1]}}},
    ]
    tab = {
        "kind": "TabsLayoutTab",
        "spec": {
            "title": QUERY_DETAIL_TAB_TITLE,
            "layout": {"kind": "GridLayout", "spec": {"items": items}},
        },
    }
    return tab, refs


def new_dashboard_shell():
    return {
        "apiVersion": "dashboard.grafana.app/v2beta1",
        "kind": "Dashboard",
        "metadata": {"name": "query-performance"},
        "spec": {
            "title": "Query Performance (agents_overview)",
            "description": "Per-panel ClickHouse query cost for agents_overview.json, sourced from system.query_log via the log_comment tag set by tag_panel_queries.py. Mirrors agents_overview.json's tab structure; each source panel is a collapsible row titled '[id] title'.",
            "annotations": [],
            "cursorSync": "Tooltip",
            "editable": True,
            "links": [],
            "liveNow": False,
            "preload": False,
            "tags": ["performance", "query-perf"],
            "timeSettings": {
                "timezone": "browser",
                "from": "now-6h",
                "to": "now",
                "autoRefresh": "1m",
                "autoRefreshIntervals": ["30s", "1m", "5m", "15m", "30m", "1h"],
                "hideTimepicker": False,
                "fiscalYearStartMonth": 0,
            },
            "variables": [],
            "elements": {},
            "layout": {"kind": "TabsLayout", "spec": {"tabs": []}},
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="services/grafana/dashboards/agents_overview.json")
    ap.add_argument("--out", default="services/grafana/dashboards-health/query_performance.json")
    ap.add_argument("--tab", required=True, help="Source tab title to (re)generate, e.g. 'Top N'")
    args = ap.parse_args()

    src = load(args.source)
    src_spec = src["spec"]
    tab_layout = find_tab_layout(src_spec, args.tab)
    if tab_layout is None:
        print(f"error: tab '{args.tab}' not found in {args.source}", file=sys.stderr)
        sys.exit(1)

    try:
        out = load(args.out)
    except FileNotFoundError:
        out = new_dashboard_shell()

    out_spec = out["spec"]

    # drop any existing elements/layout entries for this tab before regenerating
    out_layout_tabs = out_spec["layout"]["spec"]["tabs"]
    out_layout_tabs[:] = [t for t in out_layout_tabs if t["spec"]["title"] != args.tab]

    groups = []
    all_new_refs = []
    untagged = []
    for path, panel_refs in walk_source_layout(tab_layout, [args.tab]):
        subtitle = path[1] if len(path) > 1 else None
        source_panels = []
        for ref in panel_refs:
            panel = src_spec["elements"].get(ref)
            if not panel:
                continue
            pspec = panel["spec"]
            pid = pspec["id"]
            if not is_tagged(pspec, pid):
                untagged.append((pid, pspec["title"]))
                continue
            refs, panels = build_panels_for(pid)
            for r, p in zip(refs, panels):
                out_spec["elements"][r] = p
            source_panels.append((pid, pspec["title"], refs))
            all_new_refs.extend(refs)
        groups.append((subtitle, source_panels))

    if untagged:
        names = ", ".join(f"{pid} ({t})" for pid, t in untagged)
        print(f"warning: skipped untagged panels (run tag_panel_queries.py first): {names}", file=sys.stderr)

    # drop stale elements from a previous generation of this tab that are no
    # longer referenced by any tab (covers panels removed from the source tab)
    referenced = set()
    for tab in out_layout_tabs:
        referenced |= refs_in_output_layout(tab["spec"]["layout"])
    referenced.update(all_new_refs)
    out_spec["elements"] = {k: v for k, v in out_spec["elements"].items() if k in referenced}

    new_tab = {"kind": "TabsLayoutTab", "spec": {"title": args.tab, "layout": build_tab_layout(groups)}}
    out_layout_tabs.append(new_tab)

    # (Re)generate the standalone Query Detail tab + its query_id variable
    # every run, regardless of which source --tab was requested - it isn't
    # tied to any one source tab, it's the shared drill-down destination
    # every "Recent executions" table's query_id column links to.
    out_layout_tabs[:] = [t for t in out_layout_tabs if t["spec"]["title"] != QUERY_DETAIL_TAB_TITLE]
    detail_tab, detail_refs = build_query_detail_tab(out_spec["elements"])
    out_layout_tabs.append(detail_tab)
    out_spec["elements"] = {
        k: v for k, v in out_spec["elements"].items() if k in referenced or k in detail_refs
    }
    out_spec["variables"] = [v for v in out_spec["variables"] if v.get("spec", {}).get("name") != "query_id"]
    out_spec["variables"].append(query_id_variable())

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=True)
        f.write("\n")

    print(f"wrote {len(all_new_refs)} panels for tab '{args.tab}' to {args.out}")


if __name__ == "__main__":
    main()
