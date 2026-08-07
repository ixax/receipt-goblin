#!/usr/bin/env python3
"""Parse agents_overview.json into a panel tree model for build_query_perf_dashboard.py.

Walks every top-level tab of services/grafana/dashboards/agents_overview.json
(and one level of nested sub-tabs, same shape parse_dashboard.py's module
docstring describes) and records, for each panel referenced anywhere in that
layout, its id/title and whether it's already tagged with
`SETTINGS log_comment = 'agents_overview:panel_<id>'` (see tag_panel_queries.py
for how that tag gets onto a panel's rawSql).

This is the "parse" half of a two-script pipeline - the other half,
build_query_perf_dashboard.py, takes this tree's --out JSON and stamps it onto
the query_performance.json template. Splitting these means a full rebuild of
every tab is one build_query_perf_dashboard.py invocation over a tree that
already covers the whole dashboard, instead of one build invocation per tab
each re-parsing agents_overview.json from scratch.

Output tree shape:
  {"tabs": [
    {"title": "Top N", "groups": [
      {"subtitle": "Issues", "panels": [{"id": 95, "title": "...", "tagged": true}, ...]},
      {"subtitle": "Models", "panels": [...]}
    ]},
    {"title": "Overview", "groups": [{"subtitle": null, "panels": [...]}]}
  ]}

A tab with no sub-tabs gets a single group with subtitle: null (mirrors
build_query_perf_dashboard.py's build_tab_layout, which collapses that case to
a flat RowsLayout instead of a nested TabsLayout).

The tree is a regenerated intermediate artifact (services/grafana/scripts/
panel_tree.json is gitignored) - re-run this any time agents_overview.json's
panels/tags/tabs change before rendering, since build_query_perf_dashboard.py
only ever reads the tree, never the source dashboard directly.

Usage:
  extract_panel_tree.py --source services/grafana/dashboards/agents_overview.json \\
      --out services/grafana/scripts/panel_tree.json
"""
import argparse
import json

TAG_PREFIX = "agents_overview:panel_"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def is_tagged(panel_spec, panel_id):
    marker = f"{TAG_PREFIX}{panel_id}'"
    for q in panel_spec["data"]["spec"]["queries"]:
        if marker in q["spec"]["query"]["spec"]["rawSql"]:
            return True
    return False


def walk_layout(layout, path):
    """Yield (path_titles, [panel_refs]) for every leaf GridLayout under layout."""
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
            yield from walk_layout(tspec["layout"], path + [tspec["title"]])


def build_tree(src_spec):
    tabs = []
    top_layout = src_spec["layout"]
    if top_layout.get("kind") != "TabsLayout":
        return {"tabs": tabs}

    for top_tab in top_layout["spec"]["tabs"]:
        top_title = top_tab["spec"]["title"]
        groups_by_subtitle = {}
        order = []
        for path, panel_refs in walk_layout(top_tab["spec"]["layout"], [top_title]):
            subtitle = path[1] if len(path) > 1 else None
            panels = []
            for ref in panel_refs:
                panel = src_spec["elements"].get(ref)
                if not panel:
                    continue
                pspec = panel["spec"]
                pid = pspec["id"]
                panels.append({"id": pid, "title": pspec["title"], "tagged": is_tagged(pspec, pid)})
            if subtitle not in groups_by_subtitle:
                groups_by_subtitle[subtitle] = []
                order.append(subtitle)
            groups_by_subtitle[subtitle].extend(panels)

        groups = [{"subtitle": s, "panels": groups_by_subtitle[s]} for s in order]
        tabs.append({"title": top_title, "groups": groups})

    return {"tabs": tabs}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="services/grafana/dashboards/agents_overview.json")
    ap.add_argument("--out", default="services/grafana/scripts/panel_tree.json")
    args = ap.parse_args()

    src = load(args.source)
    tree = build_tree(src["spec"])

    # newline="": these files are LF-only, and Windows would otherwise translate
    # every line ending to CRLF and rewrite the whole file.
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        json.dump(tree, f, indent=2, ensure_ascii=True)
        f.write("\n")

    total_panels = sum(len(g["panels"]) for t in tree["tabs"] for g in t["groups"])
    print(f"wrote {len(tree['tabs'])} tabs, {total_panels} panels to {args.out}")


if __name__ == "__main__":
    main()
