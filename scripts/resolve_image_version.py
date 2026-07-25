#!/usr/bin/env python3
"""Resolves VERSION.yml's per-service-image-group templates into concrete
image tags, substituting `{build}` with the current commit's short git hash
(templates without `{build}` pass through unchanged - see VERSION.yml's own
comment for why). No PyYAML dependency: VERSION.yml is a flat `key: value`
mapping by design, parsed line-by-line rather than pulling in a real YAML
parser for something this simple.

Two call shapes, both used by the Makefile (see `check-env`/`build`):
  resolve_image_version.py            -> prints "export FOO_TAG=..." for every
                                    key, one per line, for `$(eval $(shell ...))`
  resolve_image_version.py <key>      -> prints just that key's resolved tag, e.g.
                                    for a one-off lookup outside Make.
"""
import pathlib
import subprocess
import sys

VERSION_FILE = pathlib.Path(__file__).resolve().parent.parent / "VERSION.yml"


def git_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "nogit"


def load_versions(path: pathlib.Path) -> dict[str, str]:
    versions = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        versions[key.strip()] = value.strip()
    return versions


def resolve(template: str, build_hash: str) -> str:
    return template.replace("{build}", build_hash) if "{build}" in template else template


def make_var_name(key: str) -> str:
    return key.upper().replace("-", "_") + "_TAG"


def main() -> None:
    versions = load_versions(VERSION_FILE)
    build_hash = git_hash()

    if len(sys.argv) > 1:
        key = sys.argv[1]
        if key not in versions:
            print(f"resolve_image_version.py: unknown key '{key}' (see VERSION.yml)", file=sys.stderr)
            sys.exit(1)
        print(resolve(versions[key], build_hash))
        return

    for key, template in versions.items():
        print(f"export {make_var_name(key)}={resolve(template, build_hash)}")


if __name__ == "__main__":
    main()
