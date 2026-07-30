# Load-balancer access logs → Loki, split by backend

## Context

Currently `load-balancer`'s nginx has `access_log off;` — no request logs are captured anywhere, so debugging routing/latency/error issues on the gateway (502s, timeouts, which webhook replica served a request, etc.) is only possible live.
Loki+Alloy already exist in the stack (opt-in `observability` profile) with 7-day retention already configured, but Alloy labels logs only by container name — since all 8 proxied services share the single `load-balancer` container, their access-log lines would land in Loki as one undifferentiated stream.
There's also no Grafana panel to view logs at all yet (only the Loki datasource is provisioned).

This plan: (1) turn on access logging to stdout only (no file, no extra disk footprint in the container), (2) tag each line with which backend it's for, (3) have Alloy promote that tag to a real Loki label so lines are filterable per backend, (4) add a Logs panel + `$backend` template variable to `dashboards-health/infra_overview.json`, next to the existing "Load balancer" health panel.

## 1. `services/load-balancer/nginx.conf`

- Replace `access_log off;` (line 61, in the `http {}` block) with a custom `log_format` in logfmt style (so Alloy's `stage.logfmt` can parse it directly), writing to `/dev/stdout`:
  ```
  log_format backend_logfmt 'backend=$backend_name method=$request_method status=$status '
                             'bytes=$body_bytes_sent request_time=$request_time '
                             'upstream_time=$upstream_response_time remote_addr=$remote_addr '
                             'uri="$request_uri" host="$http_host"';
  access_log /dev/stdout backend_logfmt;
  ```
- Each backend already lives in its own `server{}` block (by `listen` port) — matching the file's existing `set $xxx_backend ...;` style, add `set $backend_name "webhook";` (etc.) as the first line of each of the 8 proxied `server{}` blocks: webhook (8000), litellm (4000), grafana (3000), langfuse-web (3001), mcp-dev (8001), mcp-stats (8002), prometheus (9090), clickhouse (8123).
- Leave the landing page (`listen 80`) and `stub_status` (`listen 8080`, internal metrics scrape, high-frequency, no debug value) with no `backend_name` set / excluded from logging — override with `access_log off;` in those two `server{}` blocks specifically, since they'd otherwise inherit the new default from `http{}`.
- ClickHouse native TCP (`stream{}` block, port 9000) is out of scope — separate `stream` logging module, not requested.

## 2. `services/alloy/config.alloy`

Insert a `loki.process` stage between the docker source and the write stage, so only `load-balancer` lines get parsed (everything else passes through untouched):

```
loki.source.docker "default" {
	host             = "unix:///var/run/docker.sock"
	targets          = discovery.relabel.containers.output
	forward_to       = [loki.process.default.receiver]
	relabel_rules    = discovery.relabel.containers.rules
}

loki.process "default" {
	forward_to = [loki.write.default.receiver]

	stage.match {
		selector = `{container="load-balancer"}`

		stage.logfmt {
			mapping = { backend = "" }
		}

		stage.labels {
			values = { backend = "" }
		}
	}
}

loki.write "default" {
	endpoint {
		url = "http://loki:3100/loki/api/v1/push"
	}
}
```

(Only the middle `loki.process` block is new; `loki.source.docker`'s `forward_to` changes from `loki.write.default.receiver` to `loki.process.default.receiver`.) `backend` becomes a real indexed Loki label with ~8 known values — safe cardinality.

## 3. `services/grafana/dashboards-health/infra_overview.json`

- Add a dashboard template variable `backend` (query-type, sourced from the Loki datasource label `backend`, multi-select with an "All" option) — this is dashboard-level (`spec.variables`), done directly, not delegated.
- Delegate the actual panel creation to the `dashboard-panels-builder` agent: a Logs panel, Loki datasource (`DS_LOKI`), query `{container="load-balancer", backend=~"$backend"}`, placed directly after the existing "Load balancer" panel (`panel-13`) / near "Request rate" (`panel-15`) and "Latency (p50/p90/p95)" (`panel-16`) panels in the same file, following that file's existing panel id numbering and layout conventions.

## Verification

- `docker compose exec load-balancer nginx -t` after the config change to confirm syntax is valid before recreating.
- `dev-ops` agent rebuilds/recreates `load-balancer` and `alloy` (config-only changes need a recreate to pick up).
- Hit a couple of proxied endpoints (e.g. `curl localhost:$GRAFANA_PORT`, `curl localhost:$WEBHOOK_PORT/...`), then check via `docker compose logs load-balancer` that lines show the expected `backend=...` field, and confirm the same lines appear in Loki (Grafana Explore, `{container="load-balancer"}`) once `observability` profile is up, with `backend` selectable as a label/filter.
- Open the updated `infra_overview.json` dashboard, confirm the new Logs panel renders and the `$backend` variable filters it.
