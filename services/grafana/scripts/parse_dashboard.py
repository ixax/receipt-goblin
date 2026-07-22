#!/usr/bin/env python3
"""Parse a Grafana v2beta1 dashboard JSON file.

Schema recap: top-level apiVersion/kind/metadata/spec. spec.elements maps
"panel-<id>" -> {kind: "Panel", spec: {id, title, description, data: {
spec: {queries: [...]}}, vizConfig: {kind: <panel type>}}}. spec.layout is a
TabsLayout of tabs, each a GridLayout of items referencing elements by name.
"""
import argparse
import json
import sys


def load(path):
    with open(path) as f:
        return json.load(f)


def iter_tabs(spec):
    layout = spec.get("layout", {})
    if layout.get("kind") != "TabsLayout":
        return
    for tab in layout.get("spec", {}).get("tabs", []):
        tspec = tab.get("spec", {})
        yield tspec.get("title", "(untitled)"), tspec.get("layout", {})


def iter_panel_refs(tab_layout):
    if tab_layout.get("kind") != "GridLayout":
        return
    for item in tab_layout.get("spec", {}).get("items", []):
        ispec = item.get("spec", {})
        el = ispec.get("element", {})
        if el.get("kind") == "ElementReference":
            yield el.get("name")


def panel_by_ref(spec, ref):
    return spec.get("elements", {}).get(ref)


def cmd_list_tabs(args):
    spec = load(args.file)["spec"]
    for title, tab_layout in iter_tabs(spec):
        n = sum(1 for _ in iter_panel_refs(tab_layout))
        print(f"{title}\t{n} panels")


def cmd_list_panels(args):
    spec = load(args.file)["spec"]
    for title, tab_layout in iter_tabs(spec):
        if args.tab and args.tab != title:
            continue
        for ref in iter_panel_refs(tab_layout):
            panel = panel_by_ref(spec, ref)
            if not panel:
                print(f"{title}\t{ref}\t(missing element)")
                continue
            pspec = panel.get("spec", {})
            viz = pspec.get("vizConfig", {}).get("group", "?")
            print(f"{title}\t{ref}\tid={pspec.get('id')}\t{viz}\t{pspec.get('title')}")


def find_panel(spec, id_=None, title=None):
    for ref, panel in spec.get("elements", {}).items():
        pspec = panel.get("spec", {})
        if id_ is not None and pspec.get("id") == id_:
            return ref, panel
        if title is not None and pspec.get("title") == title:
            return ref, panel
    return None, None


def cmd_show_panel(args):
    spec = load(args.file)["spec"]
    ref, panel = find_panel(spec, id_=args.id, title=args.title)
    if not panel:
        print("no matching panel", file=sys.stderr)
        sys.exit(1)
    pspec = panel.get("spec", {})
    print(f"ref: {ref}")
    print(f"id: {pspec.get('id')}")
    print(f"title: {pspec.get('title')}")
    print(f"description: {pspec.get('description', '')}")
    print(f"panel type: {pspec.get('vizConfig', {}).get('group')}")
    queries = pspec.get("data", {}).get("spec", {}).get("queries", [])
    for i, q in enumerate(queries):
        qspec = q.get("spec", {}).get("query", {}).get("spec", {})
        print(f"query[{i}]: {json.dumps(qspec, indent=2)}")


def cmd_summary(args):
    d = load(args.file)
    spec = d["spec"]
    tabs = list(iter_tabs(spec))
    npanels = len(spec.get("elements", {}))
    variables = [v.get("spec", {}).get("name") for v in spec.get("variables", [])]
    datasources = set()
    for panel in spec.get("elements", {}).values():
        for q in panel.get("spec", {}).get("data", {}).get("spec", {}).get("queries", []):
            ds = q.get("spec", {}).get("query", {}).get("datasource", {}).get("name")
            if ds:
                datasources.add(ds)
    print(f"apiVersion: {d.get('apiVersion')}")
    print(f"title: {spec.get('title')}")
    print(f"tabs: {len(tabs)} ({', '.join(t for t, _ in tabs)})")
    print(f"panels: {npanels}")
    print(f"variables: {', '.join(v for v in variables if v)}")
    print(f"datasources: {', '.join(sorted(datasources))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list-tabs")
    p.add_argument("file")
    p.set_defaults(func=cmd_list_tabs)

    p = sub.add_parser("list-panels")
    p.add_argument("file")
    p.add_argument("--tab")
    p.set_defaults(func=cmd_list_panels)

    p = sub.add_parser("show-panel")
    p.add_argument("file")
    p.add_argument("--id", type=int)
    p.add_argument("--title")
    p.set_defaults(func=cmd_show_panel)

    p = sub.add_parser("summary")
    p.add_argument("file")
    p.set_defaults(func=cmd_summary)

    args = ap.parse_args()
    if args.cmd == "show-panel" and args.id is None and not args.title:
        ap.error("show-panel requires --id or --title")
    args.func(args)


if __name__ == "__main__":
    main()
