# R&D: mem_limit для dev и prod профилей

## Context

Сейчас `mem_limit` у каждого сервиса один на все случаи — значение живёт только в prod-базовом `docker-compose.yml` и одинаково применяется что при разработке одним пользователем, что под серьёзной нагрузкой.
`docker-compose.dev.yml` пока переопределяет только bind-mounts/`--reload`, ни одного `mem_limit` там нет.

Цель этого R&D — эмпирически замерить пиковое потребление памяти в двух режимах и закрепить раздельные `mem_limit`:

- **dev** — один пользователь, ровно то, что нужно для разработки.
- **prod** — крупная нагрузка: 100 пользователей на скорости 5 (готовый профиль `loadtest-workflow`).

Существующие комментарии в `docker-compose.yml` (`clickhouse`: "3.6x observed peak", `worker`: "was 256m... silent OOM kills... now 512m", `webhook-1`: "~72-74MB peak, ~1.7x margin") — это ровно такой методологический прецедент: observed peak + margin, задокументированный прямо у `mem_limit:`.
Этот план продолжает ту же практику, а не изобретает новую.

## Scope (по итогам уточнений с пользователем)

**Отдельные dev/prod значения** (dev — новый override в `docker-compose.dev.yml`, prod — обновление существующей строки в `docker-compose.yml`):

- `webhook-1` / `webhook-2` (общий YAML-anchor `webhook-service`)
- `worker`
- `redis`
- `clickhouse`
- `litellm`
- `litellm-db`

**Только prod, без dev-override** — `grafana`: пользователь уже нагружает её по полной в личном использовании, значение проверяем/уточняем для прод, в dev не переопределяем.

**Не трогаем вообще** — `mcp-stats` (оставить как есть, 256m, в обоих профилях), и любые сервисы вне этого списка (`autoheal`, `load-balancer`, `clickhouse-migrate`, `metrics-reparse`, `loadtest-fixtures`, `loadtest`, `backup`, весь `langfuse`/`observability` профиль) — задача не про них.

Только `mem_limit`. `cpus` не трогаем — пользователь просил конкретно `mem_limit`.

## Методология измерения

Основной источник — `docker stats --no-stream`, сэмплируемый по ходу прогона (уже сложившийся паттерн из `.agents/skills/loadtest-workflow/SKILL.md` Phase 4).
Дополнительно, где есть более точный источник, чем cgroup-уровень `docker stats`:

- **ClickHouse** — `system.asynchronous_metrics` / `system.metrics` (`MemoryTracking`) через `mcp__dev__query` — точнее, чем OS RSS, отражает именно внутренний трекер памяти.
- **Redis** — `redis-cli info memory` (`used_memory`, `used_memory_rss`) — точнее, чем cgroup-view, различает фрагментацию.
- **LiteLLM** — `docker stats` на `litellm` и `litellm-db` одновременно (Postgres тоже пишет per-request spend-log строки, `store_prompts_in_spend_logs: true` значит туда попадает и текст запроса/ответа — стоит смотреть оба контейнера, не только `litellm`).

Grafana `dashboards-health/docker_containers.json` (cAdvisor/Prometheus) как опциональная перепроверка — **не обязательна**: `observability`-профиль by default выключен, а на Colima + containerd-snapshotter cAdvisor вообще не видит контейнеры (задокументированное ограничение в самом дашборде).
Не поднимаем `observability` профиль ради этого R&D — если он уже поднят и работает, можно свериться, иначе `docker stats` достаточно.

**Margin-конвенция** (по образцу существующих комментариев): целевой `mem_limit` = observed peak × ~1.5–2, округлённый вверх до "чистого" шага (64m/128m/256m/512m/1g/2g/4g).
Для ClickHouse и Redis запас шире — доказанная история всплесков при мержах/бёрстах команд, не опускать текущий prod-запас без явных оснований.
Если новый пик всё ещё укладывается в текущий лимит с адекватным запасом — значение не меняем, только обновляем комментарий с свежими цифрами.

## Phase A — Dev-профиль: один пользователь

### A1. webhook / worker / redis / clickhouse (путь `loadtest`)

Делегировать `loadtest-runner` прогон с одним пользователем на скорости 5, как подтвердил пользователь: `START_USERS=0 END_USERS=1 SPEED=5`, короткий ramp (`RAMP_STEPS=1 RAMP_STEP_MINUTES=1`) и достаточный `HOLD_MINUTES` (~10) чтобы увидеть устойчивое состояние, а не мгновенный всплеск.

Пример: `make loadtest START_USERS=0 END_USERS=1 RAMP_STEPS=1 RAMP_STEP_MINUTES=1 HOLD_MINUTES=10 SPEED=5`

Во время прогона (или в отчёте `loadtest-runner`) снять пиковые `docker stats` по `webhook-1`, `webhook-2`, `worker`, `redis`, `clickhouse`, а также `redis-cli info memory` и ClickHouse `system.asynchronous_metrics` (`MemoryTracking`) на пике.

### A2. litellm / litellm-db (нужна отдельная нагрузка — loadtest их не задевает)

`services/loadtest/src/loadtest.py` намеренно бьёт напрямую в webhook, минуя LiteLLM/Anthropic (`README.md:400` — осознанный выбор, чтобы не тратить реальный бюджет).
Для LiteLLM нужен отдельный, придуманный сценарий.

Использовать **локальные Ollama-модели** из `services/litellm/user_configs/rag.yaml` (`ollama/reasoning` → `gemma3:12b`, `ollama/embeddings` → `bge-m3`) — бесплатно, реальных Anthropic/OpenAI трат не будет.
Проверено: Ollama сейчас отвечает на `localhost:11434`, обе модели загружены.
`reranker`-запись в `rag.yaml` указывает на `huggingface/...` модель, которой в Ollama нет — не использовать её для нагрузки (не рабочая конфигурация, чинить не входит в scope).

Написать небольшой асинхронный скрипт (по образцу `services/loadtest/src/loadtest.py` — `asyncio`, ramping schedule; auth-заголовок `x-litellm-api-key: Bearer <LITELLM_MASTER_KEY>` как в `services/litellm/scripts/test-models.sh`), который эмулирует одного пользователя: несколько последовательных/слабо перекрывающихся вызовов `POST /v1/chat/completions` на `ollama/reasoning` с типичным по размеру промптом + один вызов `POST /v1/embeddings` на `ollama/embeddings`, в течение ~5-10 минут.
Достаточно временного скрипта в scratchpad — не обязательно коммитить в репозиторий, если не понадобится для повторных прогонов.

Снять `docker stats` по `litellm` и `litellm-db` на пике; учесть, что перед этим прогон нельзя перезапускать/пересоздавать `litellm` без явного подтверждения (правило из `agent_docs/rules/litellm-ops.md`) — просто запускать нагрузку на уже поднятый контейнер, не трогая его lifecycle.

## Phase B — Prod-профиль: 100 пользователей / скорость 5

### B1. webhook / worker / redis / clickhouse

Делегировать `loadtest-runner` штатный "крупный" профиль, уже задокументированный в `.agents/skills/loadtest-workflow/SKILL.md` как дефолт для сценария "100 users at speed 5":

`START_USERS=10 END_USERS=100 RAMP_STEPS=8 RAMP_STEP_MINUTES=0.25 HOLD_MINUTES=30 SPEED=5`

Дать `loadtest-runner` пройти полный жизненный цикл (изолированная БД `loadtest` в ClickHouse, мониторинг стоп-условий, отчёт).
Из отчёта/логов взять пиковые `docker stats` + ClickHouse/Redis native-метрики по тем же пяти контейнерам, что в Phase A1.

### B2. litellm / litellm-db

Тот же скрипт из A2, но с концентрацией ~100 "виртуальных пользователей" (ограничено реальной пропускной способностью локального Ollama — это нормально: цель — понять память LiteLLM/Postgres под давлением N параллельных in-flight запросов и их connection pool, а не скорость генерации у Ollama).
Снять `docker stats` на `litellm`/`litellm-db` на пике, сравнить с A2.

## Phase C — Grafana: подтверждение prod-значения без синтетической нагрузки

Пользователь уже использует Grafana в полную силу в личной работе — не запускаем отдельный нагрузочный сценарий.
Снять несколько `docker stats`-снэпшотов `grafana` в течение обычной рабочей сессии (открытые дашборды, обычные переходы), взять наблюдённый пик.
Если `observability`-профиль когда-либо поднимался и данные в Prometheus ещё живы (retention ~14 дней) — свериться через `docker exec receipt-goblin-prometheus wget -qO- 'http://localhost:9090/api/v1/query_range?query=container_memory_usage_bytes{name="receipt-goblin-grafana"}&...'`, иначе достаточно точечных `docker stats`.
Сравнить с текущим `mem_limit: 1g` (`docker-compose.yml:218`) — задокументированная история OOM при старом `512m` (README.md:449) уже объясняет, почему было поднято; подтвердить, что `1g` соответствует текущему наблюдаемому пику с разумным запасом, либо скорректировать с той же margin-логикой.

## Phase D — Применить значения

Делегировать compose-правки в `dev-ops` (единственный владелец `Makefile`/`docker-compose*.yml`):

1. **`docker-compose.dev.yml`** — добавить `mem_limit:` оверрайды для `webhook-1`, `webhook-2`, `worker`, `redis`, `litellm` (в их существующие блоки) и новые блоки для `clickhouse` и `litellm-db` (сейчас там вообще нет записи для этих двух сервисов).
   Каждое значение — с комментарием в стиле существующих (`docker-compose.yml:135`, `:320-323`, `:478-479`): observed peak + методология + дата.
   Учесть существующий комментарий в шапке файла ("`redis` is NOT overridden... no dev override for this one") — он про bind-mount конфига, не про `mem_limit`; пояснить это в новом комментарии рядом с redis-оверрайдом, чтобы не создавалось впечатление противоречия.
2. **`docker-compose.yml`** (prod) — обновить `mem_limit:` и inline-комментарии для `webhook-1`/`webhook-2` (общий anchor, строка ~324), `worker` (~480), `redis` (~284), `clickhouse` (~137), `litellm` (~683), `litellm-db` (~720), `grafana` (~218, если по Phase C потребовалась правка) — только если новые данные показывают, что текущее значение занижено/сильно завышено; иначе оставить число, обновить комментарий свежими цифрами.
3. **`README.md:601`** — эта строка уже устарела независимо от этого плана (называет `clickhouse` 2g при факте 4g, `grafana` 512m при факте 1g, `redis` 768m при факте 1200m, ссылается на несуществующий `webhook-worker` вместо `worker`).
   Обновить её значениями, которые получатся после Phase D.1-2 — заодно чинит расхождение, которое иначе осталось бы.

## Phase E — Verification

1. `dev-ops` пересоздаёт (`up -d --force-recreate`, не голый `restart` — `mem_limit` требует пересоздания контейнера) затронутые сервисы в prod-конфигурации.
2. Короткий повторный прогон каждого сценария (A1/A2 для dev-конфигурации, B1/B2 для prod) — подтвердить отсутствие OOM (`docker inspect` на `RestartCount`/`OOMKilled`, логи без обрыва mid-batch, как в существующем worker-инциденте) и что новый `mem_limit` не проседает по margin.
3. `runner-test`/`make test-services` — новых кодовых изменений эта работа не вносит (только compose YAML + временный скрипт нагрузки в scratchpad), но прогнать по факту любых правок в `services/litellm/` если скрипт решат закоммитить.

## Файлы

- `docker-compose.dev.yml` — новые/дополненные `mem_limit` оверрайды.
- `docker-compose.yml` — обновлённые prod `mem_limit` + комментарии.
- `README.md` (строка ~601) — синхронизация со свежими значениями.
- Временный нагрузочный скрипт для LiteLLM — в scratchpad, не в репозитории (если не понадобится для повторных прогонов в будущем — тогда обсудить с пользователем, класть ли его в `services/litellm/scripts/`).
