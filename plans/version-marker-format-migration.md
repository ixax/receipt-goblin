# Миграция версионирования: `<version>X.Y.Z</version>` → `vX.Y.Z`

## Контекст

Каждый Subagent/Skill/Command несёт версию последней строкой frontmatter `description:` в виде XML-тега `<version>X.Y.Z</version>`.
Это не просто декоративная запись.
Этот же текст парсит продакшн-модуль `services/_common/src/ingest_parsing.py` (регэкспы `_COMMAND_VERSION_RE` и `_version_marker_for_name`), заполняя `agent_version`/`skill_version`/`command_version` в ClickHouse через листинги "Available agent types"/"available skills", которые LiteLLM логирует в `messages`.
Задача — сменить видимый формат на голый `vX.Y.Z`, строго последним токеном описания, и синхронно обновить парсер и его тесты, иначе атрибуция версий в `agent_events`/`agent_usage`/`agent_messages` молча сломается.

По итогам уточнений с пользователем в объём миграции также входит исправление устаревшего раздела README.md.
Он описывает древнюю трёх-тэговую конвенцию `<agent_version>/<skill_version>/<command_version>` в начале description и ссылается на отменённый путь `.claude/skills/`.

Command как тип сущности исключён из этой миграции целиком: пользователь подтвердил, что команды будут удалены полностью, поэтому `.claude/commands/*.md`, `command_version`-парсинг и связанная документация не трогаются — миграция формата на них не тратится.

## Затронутые файлы (23 источника с тегом)

- 15 × `.claude/agents/*.md`
- 8 × `.agents/skills/*/SKILL.md`

Плюс: `services/_common/src/ingest_parsing.py`, `services/_common/tests/test_ingest_parsing.py`, `services/_common/tests/test_ingest_db.py` (его единственный `<version>`-фикстур — про `skill_version`-backfill, не про команды, поэтому тест остаётся в скоупе), `README.md`, `agent_docs/harness-index.md` (генерируется, не редактируется руками).

Не трогаем: `.claude/commands/*.md`, `_COMMAND_VERSION_RE`/`_active_command_name_and_version` в `ingest_parsing.py`, `command_version`-тесты.

## Шаги

### 1. Механическая замена тега в 23 файлах — делегировать `harness-expert`

`harness-expert` — единственный владелец `Write`/`Edit` для `.claude/` и `.agents/skills/` (его собственный Scope это явно требует).
Задача одним вызовом: во всех 15 `.claude/agents/*.md` и 8 `.agents/skills/*/SKILL.md` заменить `<version>X.Y.Z</version>` на `vX.Y.Z` на том же месте (последний токен description).
`.claude/commands/*.md` в задачу не входят.
Это чисто форматная правка → по его же правилу бампа ("Pure cosmetic edit: no bump") версия у всех 23 не бампается.

Исключение — сам `harness-expert.md`: он также получает переписанный раздел "## Version marker" (см. шаг 2), это уже не косметика, а поведенческое изменение конвенции → минорный бамп по его же правилам.

### 2. Переписать `## Version marker` в `.claude/agents/harness-expert.md` (строки 44-56)

Текущий текст:

```
- One tag for all kinds: `<version>X.Y.Z</version>`.
- Placement: last line of `description:` (Subagent/Skill); last line of body (Command).
  ...
```

Новый текст должен:

- Заменить форму тега на `vX.Y.Z` (без XML), явно "strictly the last token of `description:`".
- Убрать Command из этого раздела вовсе (заменить на "Subagent/Skill only — Command entity type is being retired, not covered here") вместо попытки согласовать плейсмент, раз команды уходят.
- Сохранить остальные правила бампа как есть (new entity → `v1.0.0`, patch/minor/major, no bump for cosmetic).

### 3. Обновить парсер `services/_common/src/ingest_parsing.py`

Только `_version_marker_for_name` (line ~398, отвечает за `agent_version`/`skill_version` через листинги "Available agent types"/"available skills") переписывается на голый семвер, заякоренный на конец строки, а не на произвольную позицию в тексте: вместо `<{tag}>([^<]*)</{tag}>` — `rf"^- {re.escape(name)}: .*\bv(\d+\.\d+\.\d+)\s*$"` с `re.MULTILINE`.
`tag`-параметр (сейчас всегда `"version"`) можно убрать как более ненужный, раз формат один и без XML-имени тега.
Поведение "берём последнее совпадение" (`matches[-1]`) сохраняется.

`_COMMAND_VERSION_RE` и `_active_command_name_and_version` не трогаем — `command_version`-парсинг вне скоупа (команды уходят).

Осознанное изменение семантики: сейчас парсер находит маркер в любом месте строки (тесты явно проверяют начало/середину строки).
Новая конвенция требует строго конец, поэтому эти два случая перестают матчиться.
Это ожидаемо и должно быть отражено в тестах (шаг 4), а не расценено как регрессия.

### 4. Обновить тесты

`services/_common/tests/test_ingest_parsing.py`:

- Построчно заменить фикстуры `<version>X.Y.Z</version>` → `vX.Y.Z` в тестах: `test_agent_invocations_from_messages_success_recovers_version_marker`, `test_active_skill_name_and_version_success_recovers_version_marker`, `test_version_marker_for_name_success_finds_marker_at_end_of_listing_line`.
- Тесты про `_active_command_name_and_version`/`_COMMAND_VERSION_RE` (включая `..._recovers_version_marker` и `..._at_end`) не трогаем — вне скоупа.
- Переписать (не просто заменить строку) семантику двух тестов, которые сейчас проверяют "не строго конец": `test_version_marker_for_name_success_finds_marker_at_start_of_listing_line` и `..._in_middle_of_listing_line`.
  Под новую конвенцию такие маркеры не в конце строки должны возвращать пустую версию.
  Тест либо переименовать и инвертировать ожидание, либо заменить на regression-тест "marker not at end → ignored".

`services/_common/tests/test_ingest_db.py`:

- `test_ingest_events_batch_success_backfills_skill_version_from_sibling_event` (строки ~146-181) — обновить `.replace()`-инъекцию, которая сейчас вставляет `<version>1.2.3</version>` в фикстуру skill-листинга, на вставку `v1.2.3` в ту же позицию (конец строки листинга).

### 5. Регенерировать `agent_docs/harness-index.md`

Файл генерируется `scripts/sync_harness.py` и не редактируется руками.
`sync_harness.py` не содержит специфичной для тега логики (просто схлопывает пробелы во всём description), поэтому после шага 1 достаточно прогнать `make harness-index`, затем `python3 scripts/sync_harness.py --check` для подтверждения отсутствия дрейфа.

### 6. Исправить README.md (раздел "### Frontmatter format", строки ~518-566)

Переписать под актуальную унифицированную конвенцию: один `vX.Y.Z` токен, последним в description, без XML-тегов, без разделения на `<agent_version>`/`<skill_version>`/`<command_version>`, без упоминания отменённого пути `.claude/skills/`.
Раздел про Command убрать целиком, а не переписывать под новый формат — команды уходят, документировать их конвенцию не требуется.

Это правит основной конвейер (main conversation), не `harness-expert` — README.md вне его Scope (`Write`/`Edit` ограничен `.claude/`, `.agents/skills/`, `AGENTS.md`, `agent_docs/*.md`).

### 7. `.claude/settings.local.json` (локальный, gitignored)

Строка 41 — захардкоженное awk-правило, ищущее буквально `<version>` в `clickhouse-sql/SKILL.md`.
После миграции формата это правило перестанет матчить и просто больше не будет использоваться (не ломает ничего, разрешение на уже неактуальную команду).
Поскольку файл в `.gitignore` и это персональный кэш разрешений — упомянуть пользователю, но не редактировать без явного запроса.

## Верификация

1. `pytest services/_common/tests/test_ingest_parsing.py services/_common/tests/test_ingest_db.py` — через делегата `webhook-test-runner` (как предписано для этой директории), либо напрямую если тесты быстрые локально.
2. `python3 scripts/sync_harness.py --check` — подтвердить, что `agent_docs/harness-index.md` пересобран и не расходится.
3. `python3 hooks/harness_audit/audit.py .` — убедиться, что бюджеты слов не нарушены после правки (замена тега не меняет число "слов", т.к. и старый, и новый маркер — один пробело-разделённый токен).
4. Точечно `grep -rn '<version>'` по `.claude/agents/`, `.agents/skills/`, `services/_common/src/ingest_parsing.py` — должно остаться пусто.
   `.claude/commands/` намеренно не проверяем (вне скоупа).
