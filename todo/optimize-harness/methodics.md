# Топ-10 техник настройки харнеса на максимальную экономию токенов

Источники: Anthropic — *Effective Context Engineering for AI Agents* (сент. 2025) и *Skill Authoring Best Practices*; Claude Code team — *Lessons from Building Claude Code: Prompt Caching Is Everything* (апр. 2026); Dex Horthy / HumanLayer — *Advanced Context Engineering for Coding Agents* (ACE-FCA); Lumer et al. — *Don't Break the Cache* (arXiv:2601.06007); Tomasz Tunguz — *The Hungry, Hungry AI Model*, *The Harness Is the New Battleground*; Endor Labs Agent Security League; Digital Applied — *Prompt Caching Economics* (июль 2026); материалы по CLAUDE.md-практикам 2026 (DataCamp, Firecrawl, dev.to). Ориентир — Claude Code, но всё переносимо на Codex CLI через AGENTS.md.

Ключевая рамка от Anthropic: контекст — конечный ресурс, и с ростом контекста качество деградирует (context rot). Цель — не «побольше информации», а минимальный набор высокосигнальных токенов на каждом шаге. HumanLayer добавляет эмпирику: рабочая зона утилизации контекста — 40–60%, выше начинается «Dumb Zone».

---

## 1. CLAUDE.md — карта, а не территория

**Суть.** Корневой CLAUDE.md загружается в каждую сессию и живёт в контексте каждое сообщение — это самый дорогой файл, который вы контролируете. Ориентиры: ~100–150 инструкций максимум (системный промпт Claude Code уже съедает ~50 «слотов» внимания), целевой размер 500–2000 токенов. Только универсальные истины, актуальные для *любой* сессии.

**Что внутри (в порядке приоритета):**
1. Команды: build / test / lint / run — точные инвокации, не «канонические». Самая высокая ROI-секция.
2. Стек: язык, версии, БД — факты, которые нельзя угадать.
3. Архитектура: 3–5 директорий, которые важны, одной строкой каждая.
4. Конвенции, которые не проверяет линтер.
5. Границы: что нельзя трогать (`legacy/`, generated, vendored).
6. Указатели на подробные доки (см. технику 2).

**Имплементация — скелет:**

```markdown
# Project: metrics-pipeline

## Commands
- test: `uv run pytest -x -q`
- lint: `uv run ruff check --fix`
- run:  `docker compose up ingest clickhouse grafana`

## Stack
Python 3.12 / FastAPI / ClickHouse 24.x / Grafana 11. SQL ends with `;`.

## Architecture
- `ingest/` — FastAPI hooks receiver
- `ch/` — schemas, migrations (ReplacingMergeTree, ASOF JOIN pricing)
- `dashboards/` — Grafana provisioning

## Boundaries
Never edit `ch/generated/`, `vendor/`.

## Deep docs (read on demand)
- ClickHouse query conventions → `agent_docs/clickhouse.md`
- Hook pipeline contract → `agent_docs/hooks.md`
```

**Анти-паттерны:** вставленные код-сниппеты, task-specific инструкции («как проектировать новую схему биллинга»), правила, дублирующие форматтер, история проекта.

---

## 2. Progressive disclosure: ссылки вместо содержимого + path-scoped rules

**Суть.** Детальные гайды живут в отдельных файлах и подтягиваются только когда релевантны. CLAUDE.md даёт путь и одну строку «когда читать». Дополнительно — нативные механизмы скоупинга: вложенные CLAUDE.md в поддиректориях (загружаются при работе с файлами этой директории) и `.claude/rules/*.md` с glob-скоупом.

**Имплементация:**

```markdown
<!-- в корневом CLAUDE.md -->
## Deep docs
- Grafana variable formats and `$__timeFilter` usage → `agent_docs/grafana.md` (read before touching dashboards/)
```

```markdown
<!-- .claude/rules/clickhouse.md -->
---
globs: ["ch/**/*.sql", "dashboards/**"]
---
- Use `PREWHERE $__timeFilter(ts)`; normalise time via `step`.
- Join hierarchy through `node_id` / `app_hierarchy`; never `GROUP BY resource_path`.
- Grafana variables: `*:csv` format.
```

```markdown
<!-- ingest/CLAUDE.md -->
Hooks are stdlib-only Python. No third-party imports. Comments in English.
```

**Правило:** ссылка `file.py:42` вместо вставленного 30-строчного сниппета — сниппет устаревает, ссылка нет. Бонус: block-level HTML-комментарии `<!-- ... -->` вырезаются до попадания в контекст — заметки для людей бесплатны.

---

## 3. Skills как «переполнение» CLAUDE.md

**Суть.** Всё task-specific, что вы вырезали из CLAUDE.md, живёт в скиллах. Экономика: на старте сессии в контексте только name + description каждого скилла (~30–100 токенов); тело SKILL.md грузится при срабатывании; файлы из `references/` — третий уровень, стоят ноль, пока не открыты. Можно держать десятки скиллов без влияния на сессии, где они не нужны.

**Имплементация — структура:**

```
.claude/skills/grafana-dashboard/
├── SKILL.md          # workflow, <500 lines
├── references/
│   ├── panels.md     # loaded only when needed
│   └── variables.md
└── scripts/
    └── provision.py  # executes without ever entering context
```

**Правило разнесения по уровням (тегирование правил, метод Karaca):**

| Тег | Критерий | Куда |
|---|---|---|
| UNIVERSAL | нужно в каждой сессии | CLAUDE.md |
| TASK-SPECIFIC | нужно в конкретном workflow | тело SKILL.md |
| DEEP-DIVE | справка внутри workflow | `references/` |
| OBSOLETE | починенные баги, старые workaround'ы | удалить |

Типичное распределение раздутого CLAUDE.md: ~15% universal, ~60% task-specific, ~15% deep-dive, ~10% obsolete — то есть ~85% файла можно вынести или удалить.

---

## 4. Точные trigger-descriptions

**Суть.** Description в frontmatter — единственный механизм срабатывания скилла. Расплывчатое описание = скилл не сработает (и его тело было написано зря) или сработает не там (лишние токены тела в чужой сессии). Claude склонен *недо*триггерить скиллы, поэтому описания стоит делать слегка «навязчивыми»: что делает + перечисление конкретных контекстов и ключевых слов, при которых применять.

**Имплементация:**

```yaml
# Bad
description: Helps with dashboards.

# Good
description: >
  Build and modify Grafana dashboards over ClickHouse.
  Use whenever the user mentions dashboards, panels, Grafana
  variables, $__timeFilter, metric visualisation, or edits
  anything under dashboards/ — even without the word "dashboard".
```

Всё «когда применять» — только в description, не в теле: тело Claude читает уже *после* решения о срабатывании.

---

## 5. Субагенты для шумных операций (context isolation)

**Суть.** Поиск по кодовой базе — Glob/Grep/Read — генерирует тысячи строк мусора. Субагент выполняет это в изолированном контекстном окне и возвращает родителю только компактное структурированное summary. Без этого исследовательские операции легко выталкивают утилизацию выше 80%. Важно (HumanLayer): субагенты — не «персоны», а именно механизм изоляции контекста; generic Task()-агент работает почти так же хорошо, как кастомные.

**Имплементация — `.claude/agents/locator.md`:**

```markdown
---
name: locator
description: Find files and symbols relevant to a task. Use for any codebase search instead of running Grep/Glob in the main thread.
tools: Glob, Grep, Read
model: haiku
---
Locate code relevant to the request. Return ONLY:
1. File paths with line ranges (`path:start-end`)
2. One sentence per file on why it matters
3. Open questions
Never return raw file contents or grep output.
```

Дешёвая модель (haiku) на шумную работу — двойная экономия: и контекст родителя чист, и токены поиска стоят меньше.

---

## 6. Research → Plan → Implement + frequent intentional compaction

**Суть.** Методология ACE-FCA: весь workflow проектируется вокруг управления контекстом. Три фазы, каждая начинается с чистого окна и потребляет только компактный артефакт-файл предыдущей: research-док → план → имплементация. При многошаговой имплементации статус после каждой верифицированной фазы компактится обратно в файл плана — это позволяет выполнять планы на 10+ шагов без переполнения. Практическое дополнение (DataCamp): не давать контексту превышать ~60% окна; паттерн Document & Clear — сбросить прогресс в md-файл, `/clear`, продолжить с чистого листа.

**Имплементация — `.claude/commands/`:**

```markdown
<!-- .claude/commands/research.md -->
Research the codebase for: $ARGUMENTS
Use the locator subagent for all searches — keep this context clean.
Write findings to `thoughts/research-{slug}.md`: relevant files
(path:lines), current behaviour, constraints. No code dumps.

<!-- .claude/commands/plan.md -->
Read `thoughts/research-{slug}.md`. Do NOT re-search the codebase.
Produce `thoughts/plan-{slug}.md`: exact phases, files to edit,
verification command per phase.

<!-- .claude/commands/implement.md -->
Read `thoughts/plan-{slug}.md`. Execute phase by phase.
After each verified phase: compact status back into the plan file.
At 60% context: stop, update the plan, tell the user to /clear.
```

Побочный эффект: артефакты между фазами — точки ревью человеком. Ошибка в research стоит тысяч строк неверного кода, ошибка в diff — десятков; ревьюить дешевле всего наверху.

---

## 7. Не дублировать линтер: детерминизм → hooks, не промпт

**Суть.** LLM — медленный и дорогой способ делать то, что линтер делает мгновенно и детерминированно. Каждое правило стиля в CLAUDE.md, которое может проверить машина, — это токены в каждой сессии плюс ненадёжное исполнение. Переносим в конфиг форматтера и в hooks: hook стоит ноль токенов контекста и срабатывает всегда.

**Имплементация — `.claude/settings.json`:**

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{ "type": "command", "command": "python3 .claude/hooks/post_edit.py" }]
    }]
  }
}
```

```python
# .claude/hooks/post_edit.py (stdlib-only)
import json, subprocess, sys

payload = json.load(sys.stdin)
path = payload.get("tool_input", {}).get("file_path", "")
if path.endswith(".sql"):
    text = open(path, encoding="utf-8").read().rstrip()
    if not text.endswith(";"):
        print("SQL file must end with a semicolon", file=sys.stderr)
        sys.exit(2)  # blocks and feeds the message back to the agent
if path.endswith(".py"):
    subprocess.run(["ruff", "check", "--fix", path], check=False)
```

После этого строки «SQL всегда заканчивается `;`», «комментарии на английском» и т.п. из CLAUDE.md удаляются — их гарантирует машина.

---

## 8. Диета инструментов: MCP-минимализм и programmatic tool calling

**Суть.** Определения инструментов каждого подключённого MCP-сервера сидят в контексте всей сессии — крупный сервер легко стоит тысячи токенов, даже если ни разу не вызван. Вторая утечка — цепочки tool-вызовов, чьи промежуточные выводы оседают в контексте. Ответы: (a) подключать MCP per-project, а не глобально, и отключать неиспользуемые; (b) programmatic tool calling / скрипты — код потребляет промежуточные результаты, в контекст возвращается только финальный обработанный ответ.

**Имплементация:**

```bash
claude mcp list          # audit what's loaded
claude mcp remove <name> # anything not used weekly
```

Вместо «агент вызывает clickhouse-MCP, получает 400 строк, фильтрует в голове»:

```markdown
<!-- в SKILL.md -->
To inspect metric data run `scripts/query.py "<sql>"` —
it returns at most 20 rows and a row count. Never paste full result sets.
```

```python
# scripts/query.py — executes SQL, prints only head + count
import subprocess, sys
sql = sys.argv[1]
out = subprocess.run(
    ["clickhouse-client", "--format", "TSVWithNames", "-q", sql],
    capture_output=True, text=True, check=True
).stdout.splitlines()
print("\n".join(out[:21]))
print(f"... {max(0, len(out) - 21)} more rows")
```

---

## 9. Компактные форматы самих md-файлов

**Суть.** Anthropic: правильная «высота» инструкций — минимальный набор слов, полностью задающий поведение; структурные секции лучше прозы; ни жёстко закодированной казуистики, ни размытых лозунгов. Практические приёмы для харнеса:

- Императивные буллеты вместо абзацев: «Use X. Never Y.» — не «обычно мы предпочитаем...».
- Таблицы для справочных данных (колонки дешевле повторяющихся предложений).
- Один канонический пример вместо трёх похожих; anti-example только там, где агент реально ошибается.
- Никаких повторов между уровнями: правило живёт ровно в одном файле (CLAUDE.md ⊕ rule ⊕ skill), иначе платите за него дважды и получаете конфликт версий.
- Пути и команды — в backticks, без пояснений, *что такое* команда.

**Имплементация — рефактор:**

```markdown
# Before (41 words)
When you are writing queries for ClickHouse it is generally
preferable to use PREWHERE with the time filter macro because
it is more efficient, and please try to remember that we use
the step variable for time normalisation.

# After (12 words)
- ClickHouse: `PREWHERE $__timeFilter(ts)`; normalise time via `step`.
```

Та же семантика, ~70% экономии, выше вероятность исполнения (меньше «шума внимания»).

---

## 10. Измеряемый бюджет и регулярный аудит

**Суть.** Экономия без измерения деградирует: CLAUDE.md растёт «ещё одной строчкой» после каждого инцидента. Нужны: (a) явные бюджеты на файл; (b) инструмент измерения (`/context` показывает разбивку: system prompt, tools, MCP, memory-файлы); (c) регулярный аудит с тегированием UNIVERSAL/TASK-SPECIFIC/DEEP-DIVE/OBSOLETE и выносом/удалением; (d) телеметрия — раз у вас уже стоит пайплайн hooks → FastAPI → ClickHouse → Grafana, стоимость контекста на сессию становится метрикой на дашборде, а рост харнеса виден как тренд.

**Бюджеты-ориентиры:**

| Файл | Бюджет |
|---|---|
| корневой CLAUDE.md | ≤ 2 000 токенов (~8 КБ) |
| вложенный CLAUDE.md | ≤ 500 токенов |
| rule-файл | ≤ 300 токенов |
| SKILL.md body | ≤ 500 строк, целиться в ~150 |
| description скилла | ≤ 100 слов |
| summary субагента | ≤ 400 токенов |

Аудит автоматизируется — это и есть задача harness-guardian (см. `harness-guardian/SKILL.md` рядом с этим файлом): скилл + hook, которые не дают харнесу снова начать жрать токены.

---

## Сводная модель системы

```
context, always loaded          on demand                    zero-context
─────────────────────           ─────────────────            ────────────
CLAUDE.md (map, ≤2k tok)   →    .claude/rules/* (globs)      hooks (enforcement)
skill descriptions (~50t)  →    SKILL.md bodies         →    scripts/ (execution)
agent descriptions         →    references/*                 linters/formatters
                                thoughts/* (RPI artifacts)
```

Принцип один: каждый токен в always-loaded слое должен быть нужен в каждой сессии; всё остальное — вниз по лестнице, вплоть до кода, который вообще не заходит в контекст.

---

# Дополнение: cache-first слой (техники 11–14)

Первые десять техник сокращают *количество* токенов. Второй, независимый рычаг — цена оставшихся токенов. Экономика жёсткая: у агентных нагрузок input превышает output в среднем в ~300 раз (до 4000x), и даже при 4-кратной дороговизне output-токена input даёт ~98% счёта. Значит, весь бюджет решается на входе — а вход почти целиком повторяется от запроса к запросу, то есть кэшируется. Команда Claude Code строит весь харнес вокруг prompt caching и алертит на падение cache hit rate как на инцидент. Замеры (arXiv:2601.06007, DeepResearch Bench, 500+ сессий): кэширование снижает стоимость API на 41–80% и TTFT на 13–31%, причём стратегическое управление breakpoint'ами стабильно выигрывает у наивного «закэшировать всё».

Ключевой факт для md-файлов харнеса: кэш — это prefix matching. Порядок в запросе Claude Code: статический системный промпт + tools → CLAUDE.md (кэш на проект) → session context → сообщения. **Ваши md-файлы — часть кэшируемого префикса.** Любое изменение в начале инвалидирует всё после него.

## 11. Никакой динамики в always-loaded файлах

**Суть.** Один timestamp, git-статус, «текущий спринт» или счётчик в CLAUDE.md ломает кэш префикса при каждом изменении — команда Claude Code сама ловила такие регрессии (in-depth timestamp в системном промпте, недетерминированный порядок tool-определений). Изменчивая информация передаётся не правкой промпта, а сообщениями: Claude Code вставляет `<system-reminder>` в следующий user message / tool result — кэш цел.

**Имплементация:**

```markdown
# Bad — in CLAUDE.md (breaks the project cache on every change)
Current sprint: 2026-W31. Active branch: feature/pricing.

# Good — CLAUDE.md stays static; dynamic context comes from a hook
```

```python
# .claude/hooks/session_start.py — inject volatile context as a message
import json, subprocess
branch = subprocess.run(["git", "branch", "--show-current"],
                        capture_output=True, text=True).stdout.strip()
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": f"Active branch: {branch}"}}))
```

Следствие для guardian: правки CLAUDE.md/rules — между сессиями, не посреди активной сессии (каждая правка = пересборка кэша всего диалога по полной цене).

## 12. Стабильный набор тулзов и одна модель на сессию

**Суть.** Добавление/удаление тула посреди диалога — один из самых частых способов сломать кэш: tools — часть префикса. Приёмы Claude Code: состояния моделируются *тулзами*, а не сменой тулсета (Plan Mode = тулзы EnterPlanMode/ExitPlanMode при неизменном наборе); вместо удаления MCP-тулзов — `defer_loading`-заглушки с tool search (стабильный префикс + полные схемы грузятся при выборе). Это уточняет технику 8: MCP-диету проводите на уровне конфигурации проекта, до старта сессии — не переключайте mid-session.

То же с моделями: кэш уникален для модели. На 100k токенах диалога переключиться на Haiku ради простого вопроса *дороже*, чем ответить Opus'ом — кэш пересобирается с нуля. Экономика «swap tax» (Digital Applied, на тарифах Fable 5): чередование семейств моделей на каждом вызове даёт +25% к цене *без кэша вообще*, тогда как cache-first на одной модели даёт −86%. Правильная смена модели — через субагента с hand-off сообщением (так Explore-агенты Claude Code работают на Haiku) и model-homogeneous стадии пайплайна.

## 13. Компакция без слома кэша (уточнение техники 6)

**Суть.** Наивная суммаризация отдельным вызовом («summarize this», без тулзов) — ценовая ловушка: префикс расходится с первого токена, весь длинный диалог оплачивается по полной uncached-ставке, и тем дороже, чем длиннее диалог. Решение Claude Code — cache-safe forking: компакция использует *тот же* системный промпт, контекст и tool-определения, что и родительский диалог, а промпт компакции добавляется последним user message — новые токены = только сам промпт компакции. Для API-пайплайнов это уже встроено в compaction API. Для FIC-workflow из техники 6 вывод: артефакты (`thoughts/*.md`) и `/clear` не конфликтуют с кэшем — новая сессия строит новый префикс из тех же статических слоёв, которые кэшированы на уровне проекта.

## 14. Cache hit rate и input:output ratio как SLO

**Суть.** «Monitor your cache hit rate like you monitor uptime» — несколько процентных пунктов cache miss драматически меняют цену и латентность; устойчивое падение hit rate — это деплой-регрессия (кто-то переставил секции промпта, поменял tool-определение, добавил timestamp). Экономика write-премий (1.25x у Anthropic 5-min tier): кэш окупается уже при ~0.28 повторного чтения, но при write-on-every-miss паттерне hit rate ниже ~22% — убыток; sub-60% на стабильном промпте — структурный запах дизайна.

**Имплементация под ваш стек (hooks → FastAPI → ClickHouse → Grafana):** API уже возвращает `cache_read_input_tokens` / `cache_creation_input_tokens` — пишете их в ClickHouse рядом с ASOF-pricing и строите панели:

```sql
SELECT
    toStartOfInterval(ts, INTERVAL {step:UInt32} SECOND) AS t,
    sum(cache_read_input_tokens) / sum(input_tokens + cache_read_input_tokens + cache_creation_input_tokens) AS cache_hit_rate,
    sum(input_tokens + cache_read_input_tokens + cache_creation_input_tokens) / sum(output_tokens) AS io_ratio
FROM agent_usage
PREWHERE $__timeFilter(ts)
GROUP BY t
ORDER BY t;
```

Алерт: `cache_hit_rate < 0.6` на стабильной нагрузке. Вторая метрика — io_ratio: если он растёт без роста сложности задач, харнес начал «жрать» — сигнал запускать harness-guardian.

## Мета-замечание: харнес — рычаг качества, не только цены

Бенчмарк Endor Labs (Agent Security League): одна и та же модель через разные харнесы даёт разрыв ~26 п.п. по functional correctness — «the agent harness matters as much as model capability». Практический вывод для guardian: любая экономия токенов обязана проходить verify-шаг (тесты/эвалы до и после урезания), иначе можно оптимизировать цену ценой качества. И governance-угол (Tunguz): харнес решает, какие данные утекают в контекст и логи — hooks с redaction/deny-list для секретов и проприетарного кода относятся к тому же слою zero-token enforcement, что и техника 7.
