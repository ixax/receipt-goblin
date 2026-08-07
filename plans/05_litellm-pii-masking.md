# PII masking for LiteLLM (Presidio, per-model opt-in)

## Context

The user wants a minimal "gentleman's set" of PII masking on the LiteLLM proxy — hide emails, personal data (names/phones/etc.), and API keys/tokens from prompts before they reach an LLM provider.
It must be **scoped to specific models** the user will name later (not global), because some models proxy to internal/trusted destinations and others don't need the overhead.

Two decisions were made with the user before writing this plan:
- **Detection engine**: Presidio (analyzer + anonymizer, NER-based) for email/personal-data, plus regex ad-hoc recognizers for keys/tokens (Presidio has no built-in recognizer for those). This means two new containers, which the user explicitly wants wired into monitoring/alerting, documented, and fast to disable.
- **Scope**: input only (`pre_call`, before the request leaves the proxy) — not response/log scrubbing. The user also wants a Grafana panel showing how many of each PII class got masked, broken down over time/model.

**Key constraint discovered during research**: LiteLLM's native `guardrails:` config supports binding a guardrail to specific models via `litellm_params.guardrails` on a `model_list` entry — but this is an **Enterprise-only** feature, and this repo runs the open-source image (`ghcr.io/berriai/litellm:main-latest`, confirmed no license/enterprise config anywhere). So per-model scoping has to be done ourselves, not through LiteLLM's built-in guardrail-to-model binding. Rather than lean on undocumented hook-ordering to smuggle a request-level guardrail opt-in past that gate (fragile against a `main-latest`-tracked image), the plan calls Presidio directly from our own custom callback, which this repo already has an established pattern for (`services/litellm/custom_callbacks.py`).

## Design

### 1. Presidio as normal, always-on services in the main compose file

Presidio doesn't need Compose `profiles:` gating at all.
The actual on/off switch for the feature is the `PII_MASKING_MODELS` env var (§2), not whether the containers are running.
Running two lightweight containers unconditionally, even while masking is inert for every model, is a simpler operational story than a second start/stop lifecycle for infra people forget about.

- Add `presidio-analyzer` (recommend `ghcr.io/data-privacy-stack/presidio-analyzer:<pinned-tag>` — `mcr.microsoft.com/presidio-analyzer:latest` is stale per upstream's own migration notice) and `presidio-anonymizer` (`ghcr.io/data-privacy-stack/presidio-anonymizer:<pinned-tag>`) directly to `docker-compose.yml`, no `profiles:` key — started and stopped by `make up`/`make down`/`make start`/`make stop` like every other core service, no host port published (internal Docker network only, reached as `http://presidio-analyzer:3000` / `http://presidio-anonymizer:3000` from the `litellm` container — same pattern as `litellm-db`).
- `litellm` still has no `depends_on` on Presidio — masking is a best-effort per-request call per §3.6's fail-closed choice, not a startup-ordering requirement.
- `mem_limit`, matching the pattern every service in `docker-compose.yml` already follows (e.g. `clickhouse: mem_limit: 4g`, `mcp-stats: mem_limit: 256m`): `presidio-analyzer: mem_limit: 1g` (the spaCy NER model lives in memory here — the bigger consumer of the two), `presidio-anonymizer: mem_limit: 256m` (no model, just template substitution). Treat both as a starting estimate to confirm against observed peak once running, same caveat the existing `clickhouse` comment carries.
- Healthcheck + `labels: [autoheal=true]` on both, matching `litellm`'s/`worker`'s healthcheck shape (`docker-compose.yml:661-704`) — `agent_docs/services/autoheal.md:39` says any core service with a healthcheck should carry the label, and these are load-bearing for the whitelisted models whenever `PII_MASKING_MODELS` is non-empty.
- `Makefile` targets `pii-down` / `pii-up` / `pii-logs` / `pii-status`, running plain `docker compose stop/start/logs/ps presidio-analyzer presidio-anonymizer` against the existing compose files — no `--profile` flag needed, since there's no profile. These are a resource-saving convenience (stop the two containers once you know masking is off for good), not required for disabling masking itself — see §6.
- `services/prometheus/prometheus.yml`: add both containers to the blackbox HTTP probe list and wire the standard down-alert, the same as every other core service — not the old "optional stack, no alert" treatment, since these now start by default and a silent Presidio outage fail-closes every whitelisted model per §3.6 without anyone noticing otherwise.
- README: new `## PII Masking` top-level section — what it does, that both containers start with the stack automatically, the fast-disable knob (§2), and `make pii-down` as the optional "also free the resources" step.
- A new factual reference doc, `pii-masking.md`, under `agent_docs/services/` (style of `agent_docs/services/autoheal.md`) describing the pipeline end-to-end for future agents.

### 2. Fast disable: one env var, no rebuild

`PII_MASKING_MODELS` — comma-separated list of `model_name` values from `services/litellm/config.yaml`'s `model_list` (e.g. `claude-haiku-4-5`).
Empty/unset = feature inert everywhere, which is the shipped default (user will fill this in once ready).
Read from `os.environ` per-request in the callback, so toggling it is just an env change + `docker compose up -d litellm` (per the existing `agent_docs/rules/litellm-ops.md` rule: `up -d`, never `restart`, to pick up new env).
This is the "quickly disable" switch the user asked for — no code change, no rebuild.
`make pii-down` is the heavier stop-the-containers-entirely option, documented alongside it.

New env vars (added to `.env.example` under a new `# === PII Masking (optional, presidio) ===` section, and to the `litellm` service's `environment:` block in `docker-compose.yml`, same way `LANGFUSE_*` vars are threaded from the core file into a service that talks to an optional stack):
- `PII_MASKING_MODELS` (default empty)
- `PRESIDIO_ANALYZER_API_BASE` (default `http://presidio-analyzer:3000`)
- `PRESIDIO_ANONYMIZER_API_BASE` (default `http://presidio-anonymizer:3000`)

### 3. The masking callback itself

New `PiiMaskingHandler(CustomLogger)` in `services/litellm/custom_callbacks.py`, registered in `litellm_settings.callbacks` (`config.yaml:103-117`) alongside the existing handlers — same file, same pattern as `SessionIdHandler`/`ChatGPTAuthForwardHandler`.

`async_pre_call_hook`:
1. If `data.get("model")` isn't in the `PII_MASKING_MODELS` set, return `data` unchanged immediately (near-zero overhead for every other model).
2. Walk `data["messages"]`, extracting text (handle both plain-string `content` and list-of-content-block `content` — mask only `type: "text"` blocks, skip images/tool blocks).
3. For each message's text, call Presidio Analyzer's `POST /analyze` with `language: "en"` plus `ad_hoc_recognizers` loaded from a new `services/litellm/pii_recognizers.json` (regex recognizers for the "keys and tokens" half of the set: AWS `AKIA...`, GitHub `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`, Slack `xox[baprs]-`, generic `Bearer <token>`, PEM private-key blocks, Anthropic/OpenAI-style `sk-`/`sk-ant-` prefixes) — run the per-message analyze calls concurrently via `asyncio.gather`.
4. Call Presidio Anonymizer's `POST /anonymize` with the analyzer results to get masked text back; substitute it into the message in place.
5. Tally masked counts per `entity_type` (from Presidio's response, e.g. `EMAIL_ADDRESS`, `PERSON`, `PHONE_NUMBER`, plus the ad-hoc key/token types) and fire-and-forget POST them to the webhook (see §4) — wrapped in try/except so a metrics-emission failure never blocks the actual LLM call.
6. **Presidio unreachable/erroring**: fail closed — raise, so the request is blocked rather than silently forwarding unmasked PII to the provider. This only affects the explicitly-whitelisted models, and an outage is visible on the Infra dashboard per §1. Flagging this as the one behavior worth double-checking against your expectations before implementation — the alternative (fail open + log) trades a hard outage for a silent leak.

Timeouts: short client timeout (e.g. 3s) on both Presidio calls, no retry — this is a synchronous hop in the pre-call path, and a whitelisted model should fail fast rather than stall the caller.

### 4. Making the masked-counts queryable in Grafana

This repo's Grafana dashboards are exclusively ClickHouse-backed (no Prometheus datasource wired to Grafana — worker's Prometheus counters are the only precedent and nothing scrapes them into a dashboard), and the existing `metrics_webhook`/`generic_api` pipeline is hard-restricted to LiteLLM's own `llm_api_success`/`llm_api_failure` event types — it can't carry an arbitrary custom event.
So this reuses the **side-stream** pattern already used for `litellm_alert`/`git_branch`/`plan_proposal`:

- `services/webhook/src/server.py`: new `POST /api/v1/pii-masking` endpoint, no auth (internal-network-only, same posture as `/api/v1/litellm-alert`, `server.py:151-168`), calls `enqueue_side("pii_masked", body)`.
- `services/worker/src/worker.py`: add a `"pii_masked"` case to `_decode_side_into`'s routing.
- `services/_common/src/side_ingest.py`: new `insert_pii_masking_batch` row-builder, mirroring `insert_litellm_alert_batch` (`side_ingest.py:90-92`).
- New ClickHouse table `pii_masking_events`, added to both `services/clickhouse/schema.sql` and a new numbered migration file (`clickhouse-migration` skill conventions), following the `litellm_alerts` shape (`schema.sql:545-559`): plain `MergeTree`, half-year `PARTITION BY` on the row's own timestamp, `ORDER BY (received_at)`. Columns: `received_at DateTime64(3)`, `model LowCardinality(String)`, `entity_type LowCardinality(String)`, `masked_count UInt32`.
- New Grafana dashboard `services/grafana/dashboards/pii_masking.json` — hand to the `dashboards-expert` agent at implementation time with: a time series of masked count by `entity_type`, a table broken down by `model` × `entity_type`, and total counters. Exact panel JSON is that agent's job, not this plan's.

### 5. Files touched, summary

| File | Change |
|---|---|
| `docker-compose.yml` | add `presidio-analyzer`/`presidio-anonymizer` services (always-on, no `profiles:`, `mem_limit` set); add `PII_MASKING_MODELS`/`PRESIDIO_ANALYZER_API_BASE`/`PRESIDIO_ANONYMIZER_API_BASE` to `litellm` service env |
| `.env.example` | new PII Masking section |
| `Makefile` | `pii-down`/`pii-up`/`pii-logs`/`pii-status` targets — plain stop/start/logs/ps on the two containers, no profile flag; `init`/`_init_provision` step banners renumbered 1/6–6/6 to fit the new PII MASKING step (§7) |
| `services/init/init_pii_masking.py` | new — interactive `make init` step, writes `PII_MASKING_MODELS` (§7) |
| `services/prometheus/prometheus.yml` | blackbox probe entries for both containers, plus the standard down-alert |
| `services/litellm/custom_callbacks.py` | new `PiiMaskingHandler` |
| `services/litellm/pii_recognizers.json` | new — regex ad-hoc recognizers for keys/tokens |
| `services/litellm/config.yaml` | register the new callback |
| `services/webhook/src/server.py` | new `POST /api/v1/pii-masking` endpoint |
| `services/worker/src/worker.py` | route `pii_masked` side-events |
| `services/_common/src/side_ingest.py` | `insert_pii_masking_batch` |
| `services/clickhouse/schema.sql` + new migration | `pii_masking_events` table |
| `services/grafana/dashboards/pii_masking.json` | new dashboard (via dashboards-expert) |
| `README.md` | new `## PII Masking` section |
| `agent_docs/services/` (new file) | `pii-masking.md` reference doc |

### 6. Day-2 operations: three scenarios

**Start everything together (litellm + pii)**:
Just `make up` — Presidio is part of the default stack now, no second command needed.

**Start only litellm, pii not running**:
`make up` (starts everything, including Presidio), then `make pii-down` if you specifically don't want the two containers running.
There's no "start without Presidio" flag on `make up` itself — Presidio is a core service now, the same way `clickhouse`/`grafana` are, so opting it out fully means stopping it after the fact.

**Already running everything, now want to disable pii**:
This is the case that matters day-to-day, since `PII_MASKING_MODELS` is the actual feature switch:
1. Clear `PII_MASKING_MODELS` in `.env` and run `docker compose up -d litellm` (the fast-disable knob from §2) — masking stops immediately, Presidio containers keep running idle.
2. Optionally, `make pii-down` on top of that if you also want to free the containers' memory — not required for masking itself to stop, since the callback already short-circuits per §3.6 step 1 once the env var is empty.

Reversing the two steps (stopping Presidio first, `PII_MASKING_MODELS` still set) still leaves the whitelisted model **fail-closed** per §3.6, same as before — Presidio no longer being "optional" doesn't change that ordering hazard.

### 7. `make init` step: enable/disable interactively

`make init`'s existing steps are all "ask once, write to `.env`" (ENVIRONMENT) or "provision now" (CLICKHOUSE, LITELLM) — PII masking's env-var-only toggle (§2) fits the "ask once, write to `.env`" shape exactly, so it becomes a new interactive step instead of something the user has to know to set by hand.

- New `services/init/init_pii_masking.py`, same pattern as `init_environment.py` (stdlib-only, loads `init_common.py` via `importlib.util.spec_from_file_location`, always asks — re-confirming is cheap and the existing `.env` value is offered as the default, so a repeat run is safe):
  1. Ask `Enable PII masking for specific models now? [y/N]` — default reflects whether `PII_MASKING_MODELS` is already non-empty in `.env`, so re-running `make init` doesn't silently clear a prior choice.
  2. If yes: prompt for a comma-separated `model_name` list (free text — these are `services/litellm/config.yaml`'s `model_list` entries; not validated against that file here, since it isn't guaranteed to be in its final form at init time), write it to `PII_MASKING_MODELS`.
  3. If no: write `PII_MASKING_MODELS=` (empty) — matches §2's shipped-inert default.
  `PRESIDIO_ANALYZER_API_BASE`/`PRESIDIO_ANONYMIZER_API_BASE` aren't touched here — they're internal service DNS names nobody needs to type, already shipped correct in `.env.example` (§2), so `init_common.write_env`'s copy-from-example-if-missing behavior covers them for free like every other unprompted var.
- `Makefile`: insert as **Step 2/6 PII MASKING**, right after ENVIRONMENT and before GIT HOOKS — both are cheap prompt-and-write-`.env` steps with no Docker involved, unlike CLICKHOUSE/LITELLM further down. Existing `Step 2/5 GIT HOOKS` through `Step 5/5 CLIENT CONFIG` banners renumber to `3/6`–`6/6`.
- No behavior change to `_init_provision`/CLICKHOUSE/LITELLM — they don't read `PII_*` vars, so this is purely about grouping the "cheap prompt" steps together before the docker-touching ones, not a functional dependency.

## Verification

1. `make up`, confirm `presidio-analyzer`/`presidio-anonymizer` come up healthy alongside everything else (`make pii-status`, `docker compose ps`).
2. Set `PII_MASKING_MODELS=<a test model>` in `.env`, `docker compose up -d litellm` to pick up the env change.
3. Send a completion request through that model with an email address, a fake AWS-style key, and a name in the prompt — confirm via LiteLLM debug logs (or a direct curl to `presidio-analyzer:3000/analyze`) that entities are detected and the outgoing request to the provider carries masked text, not the original.
4. Send the same request through a model **not** in `PII_MASKING_MODELS` — confirm it passes through unmasked (feature is opt-in, not global).
5. Query `pii_masking_events` in ClickHouse directly to confirm rows landed with the right `entity_type`/`masked_count`.
6. Load the new Grafana dashboard, confirm the panels render against real data from step 3.
7. Clear `PII_MASKING_MODELS`, recreate `litellm`, re-run step 3's request — confirm it now passes through unmasked, with Presidio containers left running (fast-disable path works without touching them).
8. `make pii-down`, confirm both containers stop — since Presidio now alerts like any other core service (§1), expect the down-alert to fire here; this step confirms the stop mechanism itself, not silent-off behavior.
9. `make init`, confirm the new **Step 2/6 PII MASKING** prompt writes `PII_MASKING_MODELS` correctly for both a "yes, here's a model" answer and a "no" answer, and that re-running offers the just-written value back as the default (idempotent, matches every other `make init` step's re-run safety).
