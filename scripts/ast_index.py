"""Build/query agent_docs/ast_index/, a committed structural digest of every tracked .py file.
Covers classes/methods/functions/signatures/docstrings, derived via stdlib `ast`.
Stdlib only - no third-party deps, but still runs via `uv run` for the pinned .python-version interpreter (not whatever bare `python3` resolves to).

A navigation aid, not a Read replacement: answers "what's in this file / where is X defined" without a full read.
Never substitutes for Read before an Edit (verbatim old_string needs real source).

Cache layout:
  agent_docs/ast_index/cache/manifest.json        - relpath -> {sha256, size, cache_path}
  agent_docs/ast_index/manifests/<relpath>.json   - one comprehensive-but-flat doc per file

sha256 (not mtime) is the staleness signal - content hash is authoritative.

Usage:
  uv run python3 scripts/ast_index.py build --file <relpath>   # single-file incremental regen
  uv run python3 scripts/ast_index.py build --all              # full rebuild, prunes stale entries
  uv run python3 scripts/ast_index.py build --check             # exit 1 if any tracked file is stale

  uv run python3 scripts/ast_index.py query <relpath> --view outline      # qualnames + line ranges
  uv run python3 scripts/ast_index.py query <relpath> --view signatures   # + signature + 1-line docstring
  uv run python3 scripts/ast_index.py query <relpath> --view full         # entire stored document
  uv run python3 scripts/ast_index.py query <relpath> --symbol Class.method
  uv run python3 scripts/ast_index.py query --grep <substring>            # cross-file qualname search
  (add --json to any query for structured output)
"""

import argparse
import ast
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "agent_docs" / "ast_index"
FILES_DIR = CACHE_DIR / "manifests"
MANIFEST = CACHE_DIR / "cache" / "manifest.json"

EXCLUDE_DIR_NAMES = {".venv", "__pycache__", "node_modules", ".git"}


# --- filesystem / hashing helpers ------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_relpath(raw: str) -> str:
    path = Path(raw)
    if path.is_absolute():
        path = path.resolve().relative_to(ROOT)
    return str(path).replace(os.sep, "/")


def iter_py_files():
    for p in sorted(ROOT.rglob("*.py")):
        parts = p.relative_to(ROOT).parts
        if any(part in EXCLUDE_DIR_NAMES for part in parts):
            continue
        yield str(p.relative_to(ROOT)).replace(os.sep, "/")


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {"schema_version": 1, "generated_at": "", "files": {}}


def save_manifest(manifest: dict) -> None:
    manifest["generated_at"] = now_iso()
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


# --- AST extraction ----------------------------------------------------------------

def get_docstring_first_line(doc):
    if not doc or not doc.strip():
        return None
    return doc.strip().splitlines()[0]


def arg_entry(a: ast.arg) -> dict:
    return {"name": a.arg, "annotation": ast.unparse(a.annotation) if a.annotation else None}


def extract_args(args_node: ast.arguments) -> list:
    out = [arg_entry(a) for a in args_node.posonlyargs]
    out.extend(arg_entry(a) for a in args_node.args)
    if args_node.vararg:
        ann = ast.unparse(args_node.vararg.annotation) if args_node.vararg.annotation else None
        out.append({"name": f"*{args_node.vararg.arg}", "annotation": ann})
    out.extend(arg_entry(a) for a in args_node.kwonlyargs)
    if args_node.kwarg:
        ann = ast.unparse(args_node.kwarg.annotation) if args_node.kwarg.annotation else None
        out.append({"name": f"**{args_node.kwarg.arg}", "annotation": ann})
    return out


def format_signature(node) -> str:
    sig = f"({ast.unparse(node.args)})"
    if node.returns is not None:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig


def function_entry(node, prefix: str = "") -> dict:
    return {
        "qualname": f"{prefix}{node.name}" if prefix else node.name,
        "signature": format_signature(node),
        "args": extract_args(node.args),
        "returns": ast.unparse(node.returns) if node.returns else None,
        "decorators": [ast.unparse(d) for d in node.decorator_list],
        "docstring": ast.get_docstring(node),
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "line_start": node.lineno,
        "line_end": node.end_lineno,
    }


def class_entry(node: ast.ClassDef) -> dict:
    methods = [
        function_entry(child, prefix=f"{node.name}.")
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return {
        "qualname": node.name,
        "line_start": node.lineno,
        "line_end": node.end_lineno,
        "decorators": [ast.unparse(d) for d in node.decorator_list],
        "bases": [ast.unparse(b) for b in node.bases],
        "docstring": ast.get_docstring(node),
        "methods": methods,
    }


def import_entries(node) -> list:
    if isinstance(node, ast.Import):
        return [{"kind": "import", "module": a.name, "names": [], "line": node.lineno} for a in node.names]
    module = ("." * node.level) + (node.module or "")
    return [{"kind": "from", "module": module, "names": [a.name for a in node.names], "line": node.lineno}]


def module_constant_entries(node) -> list:
    if isinstance(node, ast.Assign):
        return [{"name": t.id, "line": node.lineno} for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [{"name": node.target.id, "line": node.lineno}]
    return []


def extract_module(source: str, relpath: str) -> dict:
    """One level deep only: class bodies walk direct methods, not nested defs."""
    tree = ast.parse(source, filename=relpath)
    imports, constants, classes, functions = [], [], [], []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.extend(import_entries(node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            constants.extend(module_constant_entries(node))
        elif isinstance(node, ast.ClassDef):
            classes.append(class_entry(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(function_entry(node))
    return {
        "path": relpath,
        "line_count": len(source.splitlines()),
        "module_docstring": ast.get_docstring(tree),
        "imports": imports,
        "module_constants": constants,
        "classes": classes,
        "functions": functions,
    }


# --- build -----------------------------------------------------------------------

def build_one(relpath: str, manifest: dict) -> bool:
    """Update manifest in place for one file.
    Returns True if the cache entry changed.
    """
    src_path = ROOT / relpath
    data = src_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    existing = manifest["files"].get(relpath)
    if existing and existing.get("sha256") == sha:
        return False
    try:
        entry = extract_module(data.decode("utf-8"), relpath)
        entry["sha256"] = sha
    except SyntaxError as e:
        entry = {"path": relpath, "sha256": sha, "error": f"SyntaxError: {e}"}
    cache_path = FILES_DIR / f"{relpath}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
    manifest["files"][relpath] = {
        "sha256": sha,
        "size": len(data),
        "cache_path": str(cache_path.relative_to(CACHE_DIR)).replace(os.sep, "/"),
    }
    return True


def prune_orphans(tracked: set) -> None:
    if not FILES_DIR.exists():
        return
    for p in FILES_DIR.rglob("*.json"):
        rel = str(p.relative_to(FILES_DIR)).replace(os.sep, "/")[: -len(".json")]
        if rel not in tracked:
            p.unlink()
    for d in sorted((d for d in FILES_DIR.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass


def cmd_build_file(relpath: str) -> int:
    relpath = normalize_relpath(relpath)
    if not (ROOT / relpath).exists():
        print(f"error: {relpath} does not exist", file=sys.stderr)
        return 1
    manifest = load_manifest()
    if build_one(relpath, manifest):
        save_manifest(manifest)
        print(f"ast-index: wrote {relpath}")
    else:
        print(f"ast-index: unchanged {relpath}")
    return 0


def cmd_build_all() -> int:
    manifest = {"schema_version": 1, "generated_at": "", "files": {}}
    errors = []
    count = 0
    for relpath in iter_py_files():
        build_one(relpath, manifest)
        count += 1
        doc_path = FILES_DIR / f"{relpath}.json"
        if doc_path.exists():
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
            if "error" in doc:
                errors.append(relpath)
    prune_orphans(set(manifest["files"].keys()))
    save_manifest(manifest)
    msg = f"ast-index: built {count} files, {len(manifest['files'])} tracked"
    if errors:
        msg += f", {len(errors)} parse errors: {', '.join(errors)}"
    print(msg)
    return 0


def build_check() -> int:
    manifest = load_manifest()
    stale = []
    for relpath, info in manifest.get("files", {}).items():
        p = ROOT / relpath
        if not p.exists() or file_sha256(p) != info.get("sha256"):
            stale.append(relpath)
    if stale:
        print("STALE ast-index entries:", file=sys.stderr)
        for s in stale:
            print(f"  {s}", file=sys.stderr)
        print("Run: uv run python3 scripts/ast_index.py build --file <path>  (or --all)", file=sys.stderr)
        return 1
    print("ast-index: up to date")
    return 0


def cmd_build(args) -> int:
    if args.check:
        return build_check()
    if args.all:
        return cmd_build_all()
    return cmd_build_file(args.file)


# --- query -----------------------------------------------------------------------

def load_entry(relpath: str):
    manifest = load_manifest()
    info = manifest.get("files", {}).get(relpath)
    if info is None:
        return None, None
    cache_path = CACHE_DIR / info["cache_path"]
    if not cache_path.exists():
        return None, info
    return json.loads(cache_path.read_text(encoding="utf-8")), info


def is_stale(relpath: str, info: dict) -> bool:
    p = ROOT / relpath
    return (not p.exists()) or file_sha256(p) != info.get("sha256")


def iter_entries(doc: dict):
    for c in doc.get("classes", []):
        yield c
        yield from c.get("methods", [])
    yield from doc.get("functions", [])


def project_view(doc: dict, view: str) -> dict:
    items = []
    for entry in iter_entries(doc):
        if view == "outline":
            items.append({"qualname": entry["qualname"], "line_start": entry["line_start"], "line_end": entry["line_end"]})
        else:  # signatures
            items.append({
                "qualname": entry["qualname"],
                "signature": entry.get("signature"),
                "docstring_first_line": get_docstring_first_line(entry.get("docstring")),
                "line_start": entry["line_start"],
                "line_end": entry["line_end"],
            })
    return {"path": doc["path"], "items": items}


def print_full(doc: dict) -> None:
    print(f"# {doc['path']}  ({doc.get('line_count')} lines)")
    if doc.get("module_docstring"):
        print(doc["module_docstring"].strip())
    if doc.get("imports"):
        print("\n## Imports")
        for imp in doc["imports"]:
            if imp["kind"] == "from":
                print(f"from {imp['module']} import {', '.join(imp['names'])}  L{imp['line']}")
            else:
                print(f"import {imp['module']}  L{imp['line']}")
    if doc.get("module_constants"):
        print("\n## Module constants")
        for c in doc["module_constants"]:
            print(f"{c['name']}  L{c['line']}")
    if doc.get("classes"):
        print("\n## Classes")
        for c in doc["classes"]:
            bases = f"({', '.join(c['bases'])})" if c["bases"] else ""
            print(f"{c['qualname']}{bases}  L{c['line_start']}-{c['line_end']}")
            first = get_docstring_first_line(c.get("docstring"))
            if first:
                print(f"  {first}")
            for m in c["methods"]:
                print(f"  {m['qualname']}{m['signature']}  L{m['line_start']}-{m['line_end']}")
                mfirst = get_docstring_first_line(m.get("docstring"))
                if mfirst:
                    print(f"    {mfirst}")
    if doc.get("functions"):
        print("\n## Functions")
        for f in doc["functions"]:
            print(f"{f['qualname']}{f['signature']}  L{f['line_start']}-{f['line_end']}")
            first = get_docstring_first_line(f.get("docstring"))
            if first:
                print(f"  {first}")


def query_view(doc: dict, view: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps(doc if view == "full" else project_view(doc, view), indent=2))
        return 0
    if view == "full":
        print_full(doc)
        return 0
    print(f"# {doc['path']}")
    for entry in iter_entries(doc):
        if view == "outline":
            print(f"{entry['qualname']}  L{entry['line_start']}-{entry['line_end']}")
        else:  # signatures
            print(f"{entry['qualname']}{entry.get('signature', '')}  L{entry['line_start']}-{entry['line_end']}")
            first = get_docstring_first_line(entry.get("docstring"))
            if first:
                print(f"    {first}")
    return 0


def query_symbol(doc: dict, symbol: str, as_json: bool) -> int:
    for entry in iter_entries(doc):
        if entry["qualname"] == symbol:
            if as_json:
                print(json.dumps(entry, indent=2))
            else:
                print(f"{entry['qualname']}{entry.get('signature', '')}  L{entry['line_start']}-{entry['line_end']}")
                if entry.get("decorators"):
                    print(f"  decorators: {', '.join(entry['decorators'])}")
                if entry.get("docstring"):
                    print(f"  {entry['docstring'].strip()}")
            return 0
    print(f"error: symbol {symbol!r} not found in {doc['path']}", file=sys.stderr)
    return 1


def query_grep(substring: str, as_json: bool) -> int:
    manifest = load_manifest()
    results = []
    for relpath, info in sorted(manifest.get("files", {}).items()):
        cache_path = CACHE_DIR / info["cache_path"]
        if not cache_path.exists():
            continue
        doc = json.loads(cache_path.read_text(encoding="utf-8"))
        if "error" in doc:
            continue
        for entry in iter_entries(doc):
            if substring in entry["qualname"]:
                results.append((relpath, entry["qualname"], entry["line_start"]))
    if as_json:
        print(json.dumps([{"path": p, "qualname": q, "line": line_no} for p, q, line_no in results], indent=2))
    else:
        for p, q, line_no in results:
            print(f"{p}:{line_no}: {q}")
    return 0


def cmd_query(args) -> int:
    if args.grep:
        return query_grep(args.grep, args.json)
    if not args.relpath:
        print("error: relpath is required unless --grep is given", file=sys.stderr)
        return 2
    relpath = normalize_relpath(args.relpath)
    doc, info = load_entry(relpath)
    if doc is None:
        print(f"error: no ast-index entry for {relpath} (run build --file {relpath} or build --all)", file=sys.stderr)
        return 1
    if is_stale(relpath, info):
        print(f"STALE: run build --file {relpath}", file=sys.stderr)
    if "error" in doc:
        print(f"error: {relpath} failed to parse: {doc['error']}", file=sys.stderr)
        return 1
    if args.symbol:
        return query_symbol(doc, args.symbol, args.json)
    return query_view(doc, args.view or "outline", args.json)


# --- CLI -----------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", help="single-file incremental regen")
    g.add_argument("--all", action="store_true", help="full rebuild, prunes stale entries")
    g.add_argument("--check", action="store_true", help="exit 1 if any tracked file is stale")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("query")
    p.add_argument("relpath", nargs="?", help="required unless --grep is given")
    p.add_argument("--view", choices=["outline", "signatures", "full"])
    p.add_argument("--symbol", help="exact qualname, e.g. Class.method")
    p.add_argument("--grep", help="cross-file qualname substring search")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_query)

    return ap


def main() -> int:
    args = build_arg_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
