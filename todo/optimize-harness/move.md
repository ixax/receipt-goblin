Да, получится — и даже проще, чем год назад: экосистемы заметно сошлись. Проверил текущее состояние Codex CLI и обновил файлы под dual-harness. Расклад такой:

**Переносится как есть (~85% системы):**
- **Корневой файл** — канон делаем `AGENTS.md`, а `CLAUDE.md` — симлинк на него. Codex поддерживает вложенные AGENTS.md с мержем от корня вниз (ближние файлы переопределяют дальние), но с лимитом 32 KiB на суммарную цепочку — при превышении инструкции молча обрезаются — ещё один аргумент за наши бюджеты.
- **Skills** — это теперь общий стандарт. Codex CLI поддерживает SKILL.md-скиллы через `.codex/skills/` — те же скиллы, что работают в Claude Code, работают и там. Механика та же: в контекст грузится только метаданные, Codex выбирает скиллы имплицитно по description. Значит одна директория `skills/` в репо + симлинки в `.claude/skills` и `.codex/skills`. Наш harness-guardian — тоже скилл этого стандарта, встанет в оба.
- **Progressive disclosure, RPI-workflow, `thoughts/`, `agent_docs/`, scripts/** — файловые, tool-agnostic полностью.
- **Cache-гигиена** — принципы идентичны: после июльского перехода GPT-5.6 на explicit breakpoints с 1.25x write-премией модель кэширования OpenAI фактически сошлась с Anthropic. Правила «никакой динамики в always-loaded, стабильный тулсет, одна модель на сессию» работают для обоих.

**Что требует тонких адаптеров (per-tool):**
- **Path-scoped rules**: `.claude/rules` с glob — фича только Claude Code. Общий знаменатель — вложенные AGENTS.md/CLAUDE.md по директориям (работает в обоих). Glob-rules оставляем как Claude-only оптимизацию.
- **Hooks**: lifecycle-события у Codex беднее (ты это уже исследовал для observability). Портируемый enforcement-слой — pre-commit/CI + линтеры; харнес-хуки остаются тонкими адаптерами в `.claude/` и `.codex/`.
- **Субагенты**: у Claude — `agents/*.md`, у Codex своя конфигурация. Но поскольку изоляция контекста у нас идёт через артефакты (`thoughts/`), а не через персоны, суть переносится, различаются только дефиниции.

Итоговая структура:

```
AGENTS.md              # canonical map, CLAUDE.md -> symlink
<dir>/AGENTS.md        # nested, both harnesses
skills/*/SKILL.md      # shared standard, symlinked to .claude/ and .codex/
agent_docs/ thoughts/ scripts/   # tool-agnostic
.claude/  .codex/      # thin adapters: hooks, settings, glob-rules
```

Обновляю skill и скрипт под это (проверка 32 KiB-цепочки Codex + секция dual-harness):Готово. Итог по dual-harness:

- В SKILL.md добавлена секция **Dual-harness layout**: AGENTS.md — канон, CLAUDE.md — симлинк; общая `skills/` с симлинками в `.claude/skills` и `.codex/skills`; уникальные правила не живут в Claude-only glob-rules; source of truth для enforcement — pre-commit/CI, а хуки обоих тулов — тонкие адаптеры.
- Скрипт теперь дополнительно ловит три dual-harness проблемы: **суммарная цепочка AGENTS.md > 32 KiB** (Codex молча обрежет), **разошедшиеся CLAUDE.md/AGENTS.md** как отдельные файлы (требует симлинк), и симлинки исключены из подсчёта тотала. Оба кейса проверил — симлинк чистый, дубль ловится.

Единственное место, где полной симметрии не будет, — богатство hook-событий (у Claude Code их больше) и субагентские дефиниции. Но поскольку архитектура у нас файловая (артефакты `thoughts/`, скрипты, CI-enforcement), это различие остаётся на уровне тонких адаптеров и на экономику токенов не влияет: бюджеты, progressive disclosure, скиллы и cache-гигиена работают одинаково в обоих.
