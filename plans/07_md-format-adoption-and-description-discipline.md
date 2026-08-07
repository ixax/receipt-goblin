# План: md-format для пишущих агентов + дисциплина description у harness-expert

## Context

Две проблемы харнеса:

1. **md-format не применяется на практике.**
   Комментарии и md-тексты пишутся с нарушением «одно предложение — одна строка», и скилл md-format часто не подгружается пишущими агентами.
   Разведка нашла три причины:
   - Гейт-хук `hooks/harness_audit/md_format_skill_gate.py` засчитывает чтение скилла в **любом** транскрипте сессии (`already_read()` проверяет main + все `subagents/agent-*.jsonl`). Если оркестратор прочитал скилл, любой сабагент проходит гейт, не имея правил в своём контексте. Определить пишущего хук не может (получает transcript_path главной сессии даже для сабагентских вызовов — задокументировано в докстринге `_subagent_transcripts`).
   - Только 2 из 17 агентов (harness-expert, stale-ref-sweeper) имеют в теле инструкцию читать md-format. Остальные пишущие (dashboards-expert, dev-ops, script-ops, sql-expert) — нет.
   - Регекс `MULTI_SENTENCE_PATTERN = r"[a-z0-9`)\]]\. [A-Z]"` в `hooks/harness_audit/comment_format.py` пропускает предложения, заканчивающиеся аббревиатурой/заглавной («…OOM. Second»), кавычкой, и полностью слеп к кириллице. JSON-embedded текст (panel descriptions, `--` SQL-комменты в rawSql дашбордов) не покрыт вообще — ни скиллом, ни хуками.

2. **harness-expert льёт реализацию в description.**
   Спека «Description content spec» в `.claude/agents/harness-expert.md:61-69` существует (KEEP/DROP), но сформулирована как пассивный «glance … fix obvious bloat in passing». Бюджет слов проверяется хуком жёстко, а контент-качество — никем. Утечки уже в проде: dev-ops, loadtest-runner («Isolates traffic in a dedicated ClickHouse database», «known OOM regression», «NEED USER INPUT protocol»), script-ops, ast-index («Language-agnostic … by design»), harness-guardian.

Решения согласованы с пользователем: (а) строка-отсылка к md-format в телах пишущих агентов + усиление PostToolUse-проверок как страховка на исход; (б) покрыть все три зоны нарушений (py/yaml, .md, JSON-embedded); (в) ужесточить спеку description **и** сразу sweep всех существующих 33 description.

## Роутинг исполнения

- Правки харнес-файлов (`.claude/agents/*.md`, `.agents/skills/*/SKILL.md`) — только через агента **harness-expert** (он owner и sole editor; PostToolUse-хуки audit/sync проверят бюджеты и индекс автоматически).
- Правки кода хуков (`hooks/harness_audit/*.py`) и тестов — обычный Sonnet-сабагент (`claude`).
- Все Python-вызовы — через `uv run` (никогда bare python3).

## Part A — md-format доходит до пишущих агентов

### A1. Строка в теле пишущих агентов (через harness-expert)

Почему не AGENTS.md: Claude Code грузит только CLAUDE.md (его в репо нет), AGENTS.md — корневой док Codex-стороны и в контексты Claude Code не попадает; сабагенты видят только тело собственного `.claude/agents/*.md` (механизма инъекции AGENTS.md в сабагентов нет). Строка в теле — единственный надёжный per-agent канал, и это паттерн «rule at the cheapest layer» из harness-guardian.

Добавить в тела стандартную строку по образцу `stale-ref-sweeper.md` («Before editing `.md` prose, read `Skill(md-format)`»):

> Before any Edit/Write touching `.md` prose, a multi-sentence comment/docstring, or dashboard-JSON prose (panel `description`, `--` comments in `rawSql`), read `Skill(md-format)` first.

Агенты-получатели (имеют Write/Edit и реально пишут прозу/комменты):
- `.claude/agents/dashboards-expert.md` (+ упоминание JSON-embedded зоны)
- `.claude/agents/dev-ops.md` (комменты в Makefile/docker-compose.yml)
- `.claude/agents/script-ops.md`
- `.claude/agents/sql-expert.md` (документирует gotchas в SKILL.md скилла clickhouse-sql)

У каждого — patch-бамп версии в description.

### A2. Усиление регекса one-sentence-per-line

`hooks/harness_audit/comment_format.py` — расширить `MULTI_SENTENCE_PATTERN`:
- ловить конец предложения на заглавной/аббревиатуре: `…OOM. Second`, `…API. Next`;
- ловить конец на закрывающей кавычке/бэктике: `…"done". Next`;
- кириллица: `…конец. Следующее` (`[а-яё]\. [А-ЯЁ]` и смешанные случаи);
- при этом добавить исключения-аббревиатуры, чтобы не плодить false positives: `e.g.`, `i.e.`, `vs.`, `etc.`, версии вида `v1.2.` (сейчас `e.g. Foo` уже даёт false positive — заодно починить).

Реализация: скорее список regex-альтернатив + negative lookbehind на известные аббревиатуры, чем один монолитный паттерн. Обновить тесты `hooks/harness_audit/tests/` (кейсы: аббревиатура перед точкой, кириллица, кавычки, `e.g.` не флагается).

Этот же паттерн используется audit.py для .md-прозы — усиление автоматически покрывает и .md файлы.

### A3. Покрытие JSON-embedded текста (дашборды)

Зона: `services/grafana/dashboards/*.json`, `services/grafana/dashboards-health/*.json`.

1. **`comment_format.py`**: добавить извлечение проверяемого текста из дашбордного JSON — значения ключей `"description"` и строки, начинающиеся с `--`, внутри значений `"rawSql"` (с деэскейпом `\n`). Для Edit-фрагментов (невалидный JSON) — регекс-извлечение по тем же ключам. Прогонять через тот же multi-sentence чек.
2. **`comment_format_hook.py`**: расширить фильтр путей — `.json` под двумя дашбордными директориями.
3. **`md_format_skill_gate.py`**: в `qualifies()` добавить дашбордные `.json`, когда добавляемый текст содержит `"description"`-поле или `--`-коммент в rawSql (не гейтить каждую JSON-правку).
4. **`.agents/skills/md-format/SKILL.md`** (через harness-expert): добавить JSON-embedded зону в TRIGGER/Covers description и короткую секцию правил для panel descriptions / SQL-комментов. Minor-бамп → v1.10.0.

Также поправить устаревший докстринг-комментарий в `md_format_skill_gate.py` («Once read, the skill's content is already in context» — неверно для кросс-агентного случая; гейт остаётся session-wide backstop, гарантия контекста — строки в телах из A1).

## Part B — дисциплина description

### B1. Спека → обязательный гейт (`.claude/agents/harness-expert.md`, через harness-expert)

Переписать секцию «Description content spec» (сейчас строки 61–69) из пассивного «glance … in passing» в обязательную процедуру в стиле уже существующих Mandatory-гейтов этого файла (md-format gate, self-delegation gate):

- Каждое предложение description обязано принадлежать одному из классов:
  1. what-clause (ровно одно, первым);
  2. триггер (фразы, пути, формы задач; `MUST BE USED PROACTIVELY…` / `TRIGGER…` / «Called explicitly»);
  3. SKIP/исключение;
  4. дизамбигуатор против соседней сущности;
  5. version marker (последний токен).
- Предложение вне классов — переносится в body, не удаляется. Явный запретный список: механизмы («via…», «isolates…», «delegates X to Y»), внутренние протоколы, ссылки на известные баги, rationale («by design»), tool-gap workarounds.
- **Mandatory:** при любом касании description (новый, правка, бамп) — прогнать классификацию по каждому предложению и включить её результат (класс каждого предложения) в финальный отчёт агента, чтобы оркестратор мог проверить.
- Minor-бамп harness-expert → v1.24.0.

В `harness-guardian/SKILL.md` секцию 3 («write the description first…») дополнить одной строкой-указателем на классификацию из harness-expert.md (без пересказа). Patch-бамп.

### B2. Sweep всех существующих description (через harness-expert, с чтением harness-guardian)

Прогнать новую классификацию по всем 17 агентам + 16 скиллам:
- каждое неклассифицируемое предложение переносить в первую секцию body (relocate, не удалять информацию);
- триггерную семантику не ослаблять (proactive-роутинг должен срабатывать как раньше);
- patch-бамп каждой изменённой сущности.

Известные стартовые offenders: `dev-ops.md`, `loadtest-runner.md`, `script-ops.md`, `ast-index/SKILL.md`, `harness-guardian/SKILL.md`.
Слать sweep батчами (например, 4–6 файлов на вызов harness-expert), чтобы отчёт с классификацией оставался проверяемым.

## Verification

1. `uv run pytest hooks/harness_audit/tests/` — все тесты, включая новые кейсы (аббревиатуры, кириллица, кавычки, JSON-извлечение, negative-кейсы на `e.g.`).
2. `uv run python3 hooks/harness_audit/audit.py .` — exit 0 после всех правок харнеса (бюджеты description_words соблюдены после sweep).
3. Ручная проверка регекса: `uv run python3 hooks/harness_audit/comment_format.py` на заведомо нарушающем фикстур-файле (py с «…OOM. Second», кириллицей) и на дашбордном JSON с многопредложенческим panel description.
4. Смоук гейта: правка тестового дашбордного JSON с `"description"` в свежей сессии должна требовать чтения md-format.
5. После sweep: `- git diff` по `.claude/agents/` и `.agents/skills/` — в каждом изменённом description остались только what/trigger/SKIP/disambiguator/version; перенесённый материал виден в body.
6. `uv run python3 scripts/sync_harness.py --check` — индекс не устарел (PostToolUse sync_hook должен был перегенерировать сам).
