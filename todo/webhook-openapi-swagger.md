# Подключение полноценного OpenAPI/Swagger к webhook-сервису

## Контекст

В репозитории FastAPI используется только в одном сервисе — `services/webhook`
(`services/webhook/src/server.py`). Swagger/OpenAPI там уже технически включены
(`FastAPI()` без параметров), но бесполезны на практике:

- нет `title`/`version`/`description` — в `/docs` отображается дефолтная заглушка;
- все 4 роута объявлены прямо на `app` (`@app.get/post`), без `APIRouter`, без `tags`;
- тела запросов читаются как raw dict через `request.json()`/`request.body()` —
  Swagger не может показать реальную схему полей;
- ответы — голые `dict`, без `response_model`;
- auth-проверка (`_virtual_key_is_valid`) выполняется вручную внутри двух хендлеров,
  а не через `Depends(...)`, поэтому в OpenAPI-схеме не отражается, какие роуты
  защищены.

По решению пользователя: `/docs`, `/redoc`, `/openapi.json` остаются публично
доступными как есть (без ENV-гейтинга и без Basic Auth на nginx) — меняется
только *содержимое* документации. Также по решению пользователя — добавляем
Pydantic-модели для request/response, кроме `/api/v1/metrics`, который сознательно
работает с raw bytes ради производительности (см. комментарий в
`server.py:39-44` про CPU-стоимость парсинга на батчах 360KB–1.5MB) — это трогать
не нужно.

## Изменения в `services/webhook/src/server.py`

1. **Метаданные приложения** — задать `FastAPI(title=..., description=..., version=...)`.
   `version` — не хардкод, а `config.APP_VERSION`, читаемый из env var
   `APP_VERSION` (аналогично остальным настройкам в `config.py`, которые не
   задают дефолт в коде — см. `CLICKHOUSE_HOST` и др., `AGENTS.md` "Defaults ...
   live only in docker-compose.yml"). Так как это чисто косметическое поле для
   `/docs` и падать при его отсутствии (например при локальном запуске тестов
   без docker-compose) незачем, дать fallback:
   `os.environ.get("APP_VERSION", "0.0.0-dev")`.
   В `docker-compose.yml`, в `environment:` для `webhook-1`/`webhook-2`,
   прокинуть уже существующий `WEBHOOK_TAG` (из `VERSION.yml`, сейчас
   используется только как тег Docker-образа, `WEBHOOK_TAG: 0.1.0-{build}`) как
   `APP_VERSION: ${WEBHOOK_TAG}` — тот же источник версии, что уже используется
   для сборки образа, без дублирования номера версии в другом месте.
   Эту правку `docker-compose.yml` выполняет `dev-ops` агент (единственный
   владелец правок этого файла), не редактировать его напрямую.

2. **Pydantic-модели** для трёх роутов, у которых есть структурированное тело/ответ:
   - `HealthResponse` — `status: str`, `detail: str | None = None` для `/health`.
   - `MetricsAck` — `status: str` для ответа `/api/v1/metrics` (только response,
     без request-модели — тело остаётся raw bytes по указанной выше причине).
   - `GitBranchPayload` — `session_id: str`, `git_branch: str`, `git_repo: str = ""`
     для `/api/v1/session-git-branch`.
   - `PlanProposalPayload` — `session_id: str`, `plan_text: str` для
     `/api/v1/plan-proposal`.
   - Общий `AckResponse` (`status: str`) для ответов `session-git-branch` и
     `plan-proposal` (оба сейчас возвращают `{"status": "received"}`).
   Разместить модели прямо в `server.py` (файл небольшой, ещё нет отдельного
   `schemas.py`/`models.py` в сервисе — не создавать новый файл ради 5 моделей).

3. **Заменить `request: Request` + ручной `request.json()`** на типизированные
   параметры (`payload: GitBranchPayload`, `payload: PlanProposalPayload`) в
   `/api/v1/session-git-branch` и `/api/v1/plan-proposal` — FastAPI сам валидирует
   тело и генерирует схему. `/health` и `/api/v1/metrics` не трогать в этой части
   (у health нет входного тела, у metrics — сознательно raw `Request`).

4. **Auth через `Depends` + OpenAPI security scheme** — заменить ручной парсинг
   `Authorization`-заголовка (`auth_header.removeprefix("Bearer ").strip()`) на
   `fastapi.security.HTTPBearer`, чтобы FastAPI зарегистрировал `securityScheme`
   в OpenAPI-схеме. Это даёт кнопку **Authorize** в Swagger UI — токен вводится
   один раз и автоматически подставляется в "Try it out" для обоих защищённых
   роутов, без необходимости руками писать заголовок на каждый запрос.
   - `bearer_scheme = HTTPBearer()` на уровне модуля.
   - `require_virtual_key(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> None`
     — достаёт `credentials.credentials`, вызывает существующий
     `_virtual_key_is_valid()`, поднимает `HTTPException(401)` при неудаче.
   - Подключить через `Depends(require_virtual_key)` к `/api/v1/session-git-branch`
     и `/api/v1/plan-proposal`.
   - Указать `responses={401: {"description": "invalid or missing virtual key"}}`
     на этих роутах, чтобы 401 был виден в схеме явно.
   Поведение не меняется (тот же `_virtual_key_is_valid()`), только источник
   токена — через FastAPI security dependency вместо ручного `request.headers.get`.

5. **Тэги и описания** — сгруппировать роуты через `tags=`:
   - `tags=["health"]` — `/health`
   - `tags=["ingest"]` — `/api/v1/metrics`
   - `tags=["session-metadata"]` — `/api/v1/session-git-branch`, `/api/v1/plan-proposal`
   Добавить короткие `summary=`/`description=` на каждый роут (используя уже
   существующие комментарии в коде как основу текста, не выдумывая новые
   объяснения).

6. **`response_model=`** на каждом роуте, соответствующем его модели ответа.

## Что не меняется

- Порт/nginx/load-balancer конфигурация — не трогаем (пользователь подтвердил:
  публичный доступ остаётся как есть).
- `/api/v1/metrics` остаётся на raw `Request`/`bytes` — только добавляем
  `response_model=MetricsAck` и метаданные, тело не типизируем.
- `services/mcp-server` не затрагивается — это не FastAPI-сервис (Starlette через
  `FastMCP`), Swagger к нему неприменим.
- Никакого нового `import json` — модели используют Pydantic, сериализация
  ответов идёт через штатный FastAPI/`response_model` механизм, что не требует
  ручного `fastjson`/`json`.

## Проверка

1. Поднять/перезапустить `webhook-1`/`webhook-2` (через `dev-ops` агента, не
   вручную) после изменений.
2. Открыть `http://localhost:${WEBHOOK_PORT:-8010}/docs` — убедиться, что:
   - заголовок и версия приложения отображаются;
   - все 4 роута сгруппированы по тегам;
   - `session-git-branch` и `plan-proposal` показывают схему тела запроса и
     401-ответ в списке возможных responses.
3. Проверить `/openapi.json` — схема валидна (например `python -m json.tool` или
   просто открыть в браузере).
4. Прогнать существующий pytest-сьют (`services/webhook/tests`) через
   `webhook-test-runner` агента — убедиться, что переход с `request.json()` на
   Pydantic-модели не сломал существующие тесты (path/поля тела должны остаться
   теми же, только валидация становится строже).
