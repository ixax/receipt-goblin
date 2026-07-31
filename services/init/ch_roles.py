"""Loads the ClickHouse role/grant definitions from config.yml (same
directory) - the single source of truth for `make init`
(init_clickhouse_users.py, this directory) to provision every role from.
Nothing else reads this - services/migrate/src/migrate.py never touches
users/roles/grants at all (see its own module docstring).

The parser below is intentionally NOT a general YAML parser - it supports
exactly the one shape config.yml uses and nothing else:

    roles:
      - name: <scalar>
        <field>: <scalar>
        ...
        grants:
          - "<scalar>"
          - "<scalar>"
      - name: <scalar>
        ...

Concretely: a single top-level `roles:` key: a list of mappings, each
introduced by `  - name: ...` (2-space indent), with further scalar fields
at 4-space indent (`key: value`, quotes optional and stripped if present),
and exactly one list-valued field, `grants:`, whose items are 6-space-indent
`- "..."` lines. No flow style (`{...}`/`[...]`), no multi-line scalars, no
anchors/references, no second nested list. Comments (`#`) are only
recognized on their own line, not stripped mid-line, so a value string is
never accidentally truncated.

If config.yml ever needs more than this shape, extend this parser
deliberately (or accept a real YAML dependency at that point) - don't rely
on syntax this parser doesn't actually support; it will misparse silently
rather than reject unknown shapes.
"""
import pathlib
import re
from dataclasses import dataclass, field

CONFIG_PATH = pathlib.Path(__file__).resolve().parent / "config.yml"

_ROLE_START_RE = re.compile(r"^  - name:\s*(.*)$")
_FIELD_RE = re.compile(r"^    (\w+):\s*(.*)$")
_GRANT_ITEM_RE = re.compile(r"^      - (.*)$")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_roles_yaml(text: str) -> list[dict]:
    lines = [line for line in text.splitlines() if not line.strip().startswith("#")]

    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "roles:":
            start = i + 1
            break
    if start is None:
        raise ValueError(f"{CONFIG_PATH}: no top-level 'roles:' key found")

    raw_roles: list[dict] = []
    current: dict | None = None
    in_grants = False

    for line in lines[start:]:
        if not line.strip():
            continue

        role_match = _ROLE_START_RE.match(line)
        if role_match:
            if current is not None:
                raw_roles.append(current)
            current = {"name": _strip_quotes(role_match.group(1)), "grants": []}
            in_grants = False
            continue

        if current is None:
            raise ValueError(f"{CONFIG_PATH}: unexpected line before any role: {line!r}")

        if in_grants:
            grant_match = _GRANT_ITEM_RE.match(line)
            if grant_match:
                current["grants"].append(_strip_quotes(grant_match.group(1)))
                continue
            in_grants = False

        field_match = _FIELD_RE.match(line)
        if not field_match:
            raise ValueError(f"{CONFIG_PATH}: unrecognized line: {line!r}")
        key, value = field_match.group(1), field_match.group(2)
        if key == "grants":
            in_grants = True
            continue
        current[key] = _strip_quotes(value)

    if current is not None:
        raw_roles.append(current)
    return raw_roles


@dataclass(frozen=True)
class Role:
    name: str
    user_env: str
    password_env: str
    default_user: str
    grants: list[str] = field(default_factory=list)
    # Which env var supplies {database} for this role's grants + its
    # CREATE USER's DEFAULT DATABASE. Defaults to the app's main database.
    database_env: str = "CLICKHOUSE_DATABASE"
    # True if database_env's database doesn't already exist (unlike
    # CLICKHOUSE_DATABASE, which ClickHouse's own startup creates) and needs
    # CREATE DATABASE IF NOT EXISTS first.
    create_database: bool = False


def _build_roles(raw_roles: list[dict]) -> list[Role]:
    roles = []
    for raw in raw_roles:
        roles.append(
            Role(
                name=raw["name"],
                user_env=raw["user_env"],
                password_env=raw["password_env"],
                default_user=raw["default_user"],
                grants=raw.get("grants", []),
                database_env=raw.get("database_env", "CLICKHOUSE_DATABASE"),
                create_database=raw.get("create_database", "false").lower() == "true",
            )
        )
    return roles


ROLES: list[Role] = _build_roles(_parse_roles_yaml(CONFIG_PATH.read_text()))
