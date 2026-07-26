#!/usr/bin/env python3
"""One-time (idempotent) tagging of agents_overview.json panel queries.

Appends `SETTINGS log_comment = 'agents_overview:panel_<id>'` to every
rawSql query of the given panel ids, so system.query_log rows can be
attributed back to the panel that issued them (used by the
query_performance.json companion dashboard - see
build_query_perf_dashboard.py).

Does surgical raw-text substring replacement rather than json.load +
json.dump of the whole file: this dashboard's existing JSON is not
consistently ensure_ascii - some string fields already contain literal
non-ASCII characters (arrows in chart labels) while others use \\uXXXX
escapes - so re-serializing the entire document would rewrite unrelated
lines and produce a huge, noisy diff. Only the targeted rawSql values are
touched; everything else in the file is left byte-for-byte identical.

Safe to re-run: replaces an existing log_comment setting for a panel
instead of appending a second one.
"""
import argparse
import json
import re
import sys

COMMENT_RE = re.compile(r"\n?SETTINGS log_comment = '[^']*'\s*$")


def tag(raw_sql: str, panel_id: int) -> str:
    base = COMMENT_RE.sub("", raw_sql)
    return f"{base}\nSETTINGS log_comment = 'agents_overview:panel_{panel_id}'"


def encode_variants(s: str):
    """The two ways this string could appear as a JSON string literal body."""
    return {
        json.dumps(s, ensure_ascii=True)[1:-1],
        json.dumps(s, ensure_ascii=False)[1:-1],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file")
    ap.add_argument("--id", type=int, action="append", required=True, dest="ids")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as f:
        text = f.read()
    doc = json.loads(text)

    elements = doc["spec"]["elements"]
    changed = []
    for panel_id in args.ids:
        ref = f"panel-{panel_id}"
        panel = elements.get(ref)
        if not panel:
            print(f"warning: {ref} not found", file=sys.stderr)
            continue
        queries = panel["spec"]["data"]["spec"]["queries"]
        for q in queries:
            old_sql = q["spec"]["query"]["spec"]["rawSql"]
            new_sql = tag(old_sql, panel_id)
            if old_sql == new_sql:
                continue

            old_variants = [v for v in encode_variants(old_sql) if text.count(v) == 1]
            if not old_variants:
                print(f"error: could not uniquely locate rawSql for {ref} in raw text", file=sys.stderr)
                sys.exit(1)
            old_encoded = old_variants[0]
            was_ascii = old_encoded == json.dumps(old_sql, ensure_ascii=True)[1:-1]
            new_encoded = json.dumps(new_sql, ensure_ascii=was_ascii)[1:-1]

            text = text.replace(old_encoded, new_encoded, 1)
        changed.append(panel_id)

    with open(args.file, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"tagged {len(changed)} panel(s): {changed}")


if __name__ == "__main__":
    main()
