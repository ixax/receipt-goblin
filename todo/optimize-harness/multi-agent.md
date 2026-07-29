Правильная рамка: **не «два харнеса», а один канонический слой + тонкие пер-CLI адаптеры**. Вы этот паттерн уже используете с хуками — логика в общих `hooks/*.py`, а `.claude/settings.json` и `.codex/hooks.json` лишь регистрируют их. Осталось распространить его на скиллы и агентов.

```
shared (single source)          thin adapters
──────────────────────          ─────────────────────────
AGENTS.md                       CLAUDE.md -> symlink
agent_docs/*.md                 .codex/hooks.json (регистр.)
.claude/skills/*/SKILL.md       .claude/settings.json (регистр.)
.claude/agents/*.md             .mcp.json  (endpoints)
hooks/*.py                      ~/.codex/config.toml (endpoints)
agent_docs/harness-index.md [G] 
```
`[G]` — генерируется, руками не правится.

**Что шарится как есть, без копипаста**

1. **AGENTS.md** — Codex читает нативно. Для Claude Code, если он у вас ещё не подхватывает AGENTS.md напрямую, стандартное решение — симлинк `CLAUDE.md → AGENTS.md` (git хранит симлинки нормально). Один файл, ноль дублей.
2. **Тела скиллов и агентов** — это просто markdown в `.claude/`. Codex спокойно читает файлы по пути; ничего не мешает правилу «read `.claude/skills/clickhouse-sql/SKILL.md` before writing SQL» работать в обеих CLI. Путь `.claude/` в имени не нарушает вашу CLI-agnostic-норму — это имя, которое определяет сам Claude Code, а такие переименовывать запрещено вашим же правилом.

**Где механики расходятся — и как не дублировать контент**

3. **Триггеринг скиллов.** У Claude Code — нативный, по description из frontmatter. Для Codex (если ваша версия не подхватывает SKILL.md сама — это стоит проверить, поддержка формата расползается по инструментам) fallback — сгенерированный индекс: скрипт собирает `name`+`description` из всех frontmatter'ов в один `agent_docs/harness-index.md`, а в AGENTS.md добавляется одна строка: «Before any task, check `agent_docs/harness-index.md` for an owning skill/agent; read its file before proceeding». Description по-прежнему живёт ровно в одном месте — frontmatter'е; индекс — производная, помечен `GENERATED`, пересобирается make-таргетом. Дубля нет, потому что нет второго рукописного экземпляра.
4. **Субагенты.** У Codex нет Task-tool, поэтому карта делегирования для него означает другое исполнение при том же контенте:
   - дешёвый вариант — «inline-режим»: прочитать `.claude/agents/x.md` и следовать ему в основном контексте. Работает, но теряется изоляция контекста — pytest-выхлоп и docker logs осядут в главном окне, ровно то, от чего вы уходили;
   - вариант с изоляцией — сабпроцесс: `codex exec "$(cat .claude/agents/webhook-test-runner.md) ... task"` из основной сессии. Это ваш же паттерн «субагент = механизм изоляции», просто через процесс. `model: haiku` из frontmatter Codex не поймёт, но раз обе CLI ходят через ваш LiteLLM, маппинг «дешёвая модель для шумной работы» можно повесить на virtual key / model alias в `services/litellm/config.yaml` — тоже без дублирования, конфиг один.
   
   В AGENTS.md тогда добавляется короткий блок «CLI adapter notes» (3–4 строки), а не вторая копия карты.
5. **MCP-регистрация.** `.mcp.json` и `[mcp_servers]` в `~/.codex/config.toml` придётся держать оба, но это ~5 строк endpoint'ов на файл, не контент. Можно генерить тем же скриптом, если захочется нуля ручной синхронизации.

**Enforcement, чтобы копипаст не вернулся**

- Заголовок `<!-- GENERATED from .claude/**; edit sources, run make harness-index -->` в индексе.
- Проверка в harness-guardian: индекс устарел относительно frontmatter'ов → fail (сравнение по содержимому, детерминированный вывод — заодно cache-friendly: файл не меняется, если не менялись источники).
- `stale-ref-sweeper` после внедрения — появились новые пути.

Написал генератор — stdlib-only, в стиле ваших хуков: парсит frontmatter (включая `description: >`-блоки), собирает скиллы+агентов в один индекс с детерминированной сортировкой, exit code 1 в режиме `--check`, если индекс устарел:Скрипт кладите в `scripts/sync_harness.py`. К нему три маленьких ручных шага:

**Makefile** (через `dev-ops`, по вашему правилу):
```make
harness-index: check-env
	python3 scripts/sync_harness.py
```

**AGENTS.md** — добавка ~6 строк (через `harness-expert`):
```markdown
## CLI adapter notes (Codex)
- Skill/agent discovery: check `agent_docs/harness-index.md` (generated;
  edit frontmatter sources, then `make harness-index`).
- Delegation map above: Codex has no Task tool — read the agent's file and
  follow it, or isolate via `codex exec` for noisy work (tests, logs).
- Cheap-model routing for noisy agents: LiteLLM model alias, not frontmatter.
```

**harness-guardian** — в чеклист аудита одна строка: запускать `python3 scripts/sync_harness.py --check`, fail при устаревшем индексе.

По токенам это нейтрально для Claude Code (индекс он не грузит — триггерится нативно) и дёшево для Codex: индекс подтягивается по ссылке on-demand, always-loaded слой вырос на ~40 токенов адаптер-блока. Единственная реальная плата — Codex-сессии без `codex exec`-изоляции будут жирнее на делегированных workflow'ах; если Codex у вас в активной ротации, изоляцию через сабпроцесс стоит сделать нормой, а не опцией.

И проверьте один факт до внедрения fallback'а: не читает ли ваша версия Codex SKILL.md нативно — формат сейчас активно перенимают другие инструменты, и если да, индекс для скиллов становится не нужен (останется только агентская половина).
