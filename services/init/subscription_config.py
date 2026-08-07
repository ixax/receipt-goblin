"""Loads and validates the subscription/person definitions from subscriptions.yml (same directory).

The single source of truth for `make subscriptions` (load_subscriptions.py, this directory) to populate person_identities and subscriptions from.
Nothing else reads this file - ingest never writes those two tables, and no dashboard query reaches past the subscription_cost_daily view.

The parser below is intentionally NOT a general YAML parser, for the same reason ch_roles.py's isn't: this script runs on the host, stdlib-only, with no venv or pip install.
It supports exactly the one shape subscriptions.yml uses and nothing else:

    <section>:
      - <field>: <scalar>
        <field>: <scalar>
        <list_field>:
          - "<scalar>"
          - "<scalar>"
      - <field>: <scalar>

Concretely: top-level `<section>:` keys, each a list of mappings introduced by `  - key: value` (2-space indent), with further scalar fields at 4-space indent (quotes optional and stripped if present), and at most one list-valued field per mapping whose items are 6-space-indent `- "..."` lines.
No flow style (`{...}`/`[...]`), no multi-line scalars, no anchors/references, no mapping nested inside a mapping.
Comments (`#`) are only recognized on their own line, not stripped mid-line, so a value string is never accidentally truncated.

If subscriptions.yml ever needs more than this shape, extend this parser deliberately (or accept a real YAML dependency at that point).
It will misparse silently rather than reject unknown shapes.

Validation is the substantive half of this module, not a formality.
Both tables it feeds are write-once-per-run projections of this file, and the errors that matter here - a user_id claimed by two people, two overlapping subscriptions for the same person and provider - produce a plausible-looking wrong number rather than a failure.
Nothing downstream can detect either one after the fact, so every check runs before a single row is written.
"""
import pathlib
import re
from dataclasses import dataclass, field
from datetime import date

CONFIG_PATH = pathlib.Path(__file__).resolve().parent / "subscriptions.yml"

# Open-ended subscriptions carry this rather than an empty valid_to.
# Keeping the column non-nullable means every query is a plain BETWEEN, and subscription_cost_daily clamps this to today() anyway.
OPEN_ENDED = date(2099, 12, 31)

# Matches agent_usage.provider's domain (services/_common/src/ingest_parsing.py's _provider_for_model).
# A provider outside this set would silently never join to any usage row.
VALID_PROVIDERS = ("claude", "openai", "other")

_SECTION_RE = re.compile(r"^(\w+):\s*$")
_ITEM_START_RE = re.compile(r"^  - (\w+):\s*(.*)$")
_FIELD_RE = re.compile(r"^    (\w+):\s*(.*)$")
_LIST_ITEM_RE = re.compile(r"^      - (.*)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PRICE_RE = re.compile(r"^\d+(\.\d{1,2})?$")


class ConfigError(ValueError):
    """Raised for any malformed or self-contradictory subscriptions.yml.

    Always names the offending entry, since the file is hand-edited.
    """


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_sections(text: str) -> dict[str, list[dict]]:
    """Parses the restricted grammar documented in this module's docstring into {section: [mapping, ...]}.

    A list-valued field becomes a list of strings; every other value stays a string.
    """
    sections: dict[str, list[dict]] = {}
    current_section: list[dict] | None = None
    current_item: dict | None = None
    current_list: list[str] | None = None

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        section_match = _SECTION_RE.match(line)
        if section_match:
            current_section = []
            sections[section_match.group(1)] = current_section
            current_item = None
            current_list = None
            continue

        if current_section is None:
            raise ConfigError(f"{CONFIG_PATH.name}:{lineno}: content before any top-level section key")

        item_match = _ITEM_START_RE.match(line)
        if item_match:
            current_item = {item_match.group(1): _strip_quotes(item_match.group(2))}
            current_section.append(current_item)
            current_list = None
            continue

        if current_item is None:
            raise ConfigError(f"{CONFIG_PATH.name}:{lineno}: field outside any list item")

        field_match = _FIELD_RE.match(line)
        if field_match:
            key, value = field_match.group(1), field_match.group(2)
            if value.strip():
                current_item[key] = _strip_quotes(value)
                current_list = None
            else:
                # A key with no value opens this item's one nested list.
                current_list = []
                current_item[key] = current_list
            continue

        list_match = _LIST_ITEM_RE.match(line)
        if list_match:
            if current_list is None:
                raise ConfigError(f"{CONFIG_PATH.name}:{lineno}: list item with no list field above it")
            current_list.append(_strip_quotes(list_match.group(1)))
            continue

        raise ConfigError(f"{CONFIG_PATH.name}:{lineno}: unsupported syntax for this parser: {line!r}")

    return sections


@dataclass(frozen=True)
class Person:
    person_id: str
    user_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Subscription:
    person_id: str
    provider: str
    plan: str
    monthly_price: str
    currency: str
    seats: int
    valid_from: date
    valid_to: date


def _require(item: dict, key: str, where: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: missing required field {key!r}")
    return value.strip()


def _parse_date(value: str, where: str, key: str) -> date:
    if not _DATE_RE.match(value):
        raise ConfigError(f"{where}: {key} must be YYYY-MM-DD, got {value!r}")
    return date.fromisoformat(value)


def _build_people(raw_items: list[dict]) -> list[Person]:
    people = []
    for index, item in enumerate(raw_items):
        where = f"people[{index}]"
        person_id = _require(item, "person_id", where)
        user_ids = item.get("user_ids")
        if not isinstance(user_ids, list) or not user_ids:
            raise ConfigError(f"{where} ({person_id}): needs a non-empty user_ids list")
        people.append(Person(person_id=person_id, user_ids=[u.strip() for u in user_ids if u.strip()]))
    return people


def _build_subscriptions(raw_items: list[dict]) -> list[Subscription]:
    subscriptions = []
    for index, item in enumerate(raw_items):
        where = f"subscriptions[{index}]"
        person_id = _require(item, "person_id", where)
        where = f"{where} ({person_id})"

        provider = _require(item, "provider", where)
        if provider not in VALID_PROVIDERS:
            raise ConfigError(f"{where}: provider must be one of {'/'.join(VALID_PROVIDERS)}, got {provider!r}")

        price = _require(item, "monthly_price", where)
        if not _PRICE_RE.match(price):
            raise ConfigError(f"{where}: monthly_price must be a non-negative number with at most 2 decimals, got {price!r}")

        seats_raw = item.get("seats", "1")
        if not isinstance(seats_raw, str) or not seats_raw.strip().isdigit() or int(seats_raw) < 1:
            raise ConfigError(f"{where}: seats must be a positive integer, got {seats_raw!r}")

        valid_from = _parse_date(_require(item, "valid_from", where), where, "valid_from")
        valid_to_raw = item.get("valid_to")
        valid_to = _parse_date(valid_to_raw.strip(), where, "valid_to") if isinstance(valid_to_raw, str) and valid_to_raw.strip() else OPEN_ENDED
        if valid_to < valid_from:
            raise ConfigError(f"{where}: valid_to {valid_to} is before valid_from {valid_from}")

        subscriptions.append(Subscription(
            person_id=person_id,
            provider=provider,
            plan=_require(item, "plan", where),
            monthly_price=price,
            currency=item.get("currency", "USD").strip() or "USD",
            seats=int(seats_raw),
            valid_from=valid_from,
            valid_to=valid_to,
        ))
    return subscriptions


def _validate(people: list[Person], subscriptions: list[Subscription]) -> None:
    """Rejects any config that would produce a wrong number rather than an error downstream.

    See this module's docstring for why these run before writing rather than as a query-time check.
    """
    seen_person_ids: set[str] = set()
    for person in people:
        if person.person_id in seen_person_ids:
            raise ConfigError(f"people: duplicate person_id {person.person_id!r}")
        seen_person_ids.add(person.person_id)

    # One user_id under two people would attribute the same usage to two payers.
    owner_of_user_id: dict[str, str] = {}
    for person in people:
        for user_id in person.user_ids:
            previous = owner_of_user_id.get(user_id)
            if previous is not None:
                raise ConfigError(f"people: user_id {user_id!r} is claimed by both {previous!r} and {person.person_id!r}")
            owner_of_user_id[user_id] = person.person_id

    currencies = {sub.currency for sub in subscriptions}
    if len(currencies) > 1:
        raise ConfigError(f"subscriptions: mixed currencies {sorted(currencies)} - nothing in this stack converts between them, so totals would be meaningless")

    for sub in subscriptions:
        if sub.person_id not in seen_person_ids:
            raise ConfigError(f"subscriptions: person_id {sub.person_id!r} is not declared under people")

    # Overlapping intervals for one (person, provider) double-count the fee for every day they share.
    by_key: dict[tuple[str, str], list[Subscription]] = {}
    for sub in subscriptions:
        by_key.setdefault((sub.person_id, sub.provider), []).append(sub)
    for (person_id, provider), subs in by_key.items():
        ordered = sorted(subs, key=lambda s: s.valid_from)
        for earlier, later in zip(ordered, ordered[1:]):
            if later.valid_from <= earlier.valid_to:
                raise ConfigError(
                    f"subscriptions: {person_id}/{provider} has overlapping intervals - "
                    f"{earlier.plan} runs to {earlier.valid_to} but {later.plan} starts {later.valid_from}"
                )


def load() -> tuple[list[Person], list[Subscription]]:
    """Parses and validates subscriptions.yml, or raises ConfigError naming the offending entry."""
    sections = _parse_sections(CONFIG_PATH.read_text(encoding="utf-8"))
    unknown = set(sections) - {"people", "subscriptions"}
    if unknown:
        raise ConfigError(f"{CONFIG_PATH.name}: unknown top-level section(s) {sorted(unknown)}")

    people = _build_people(sections.get("people", []))
    subscriptions = _build_subscriptions(sections.get("subscriptions", []))
    _validate(people, subscriptions)
    return people, subscriptions
