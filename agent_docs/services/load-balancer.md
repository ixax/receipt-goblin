# `load-balancer`

Single nginx service (`services/load-balancer/`) sits in front of every service that used to publish its own host port directly:

- `webhook` (via `webhook-1`/`webhook-2`)
- `litellm`
- `grafana`
- `mcp-dev` (dev-only)
- `mcp-stats`
- `clickhouse` (both its HTTP interface and native protocol)
- `prometheus` (opt-in `observability` profile)
- `langfuse-web` (opt-in `langfuse` profile)

## Ports stay unchanged

Each backend keeps its original `listen`/host port (`WEBHOOK_PORT`/`LITELLM_PORT`/`GRAFANA_PORT`/`MCP_DEV_PORT`/`MCP_STATS_PORT`/`CLICKHOUSE_HTTP_PORT`/`CLICKHOUSE_NATIVE_PORT`/`PROMETHEUS_PORT`/`LANGFUSE_PORT`).
`http://localhost:<port>` for each is unchanged, only the container actually terminating the connection changed.
`langfuse-web`'s own container-internal port (3000) collides with `grafana`'s, so nginx listens for it on its own internal `3001` instead - purely an nginx-internal socket choice, unrelated to `LANGFUSE_PORT`, which still controls the host side exactly as before.
`MCP_DEV_PORT` is the one exception, in two ways: it's only wired up in `docker-compose.dev.yml` (`mcp-dev` doesn't exist in `ENVIRONMENT=production`), and load-balancer publishes it with `host_ip: 127.0.0.1` specifically - reachable from the host only, never from other interfaces like every other port above.
`mcp-stats` is a normal prod service with no such restriction (auth is handled inside the service itself via a LiteLLM-virtual-key check, not by the gateway).

## Routing stays port-based, not path-prefixed or subdomain-based

This stack has no domain or local DNS infra anywhere: every consumer (README, `Makefile`, `.mcp.json`, `make setup-client`'s printed `ANTHROPIC_BASE_URL`/`AGENT_CLI_TRACKING_API_URL`) already addresses services as `http://localhost:<PORT>`, so per-port `server{}` blocks in `nginx.conf` reuse that with zero client-facing change.
Path prefixes would need every backend to tolerate a stripped/rewritten prefix (unverified for LiteLLM's UI/API and both MCP servers' `streamable-http` transport) and would ripple through every hardcoded URL above.
Subdomains would need `/etc/hosts`/DNS infra that doesn't exist here.
Revisit only if this stack ever runs behind a real external domain.

## Webhook load balancing is real `least_conn`, not a fixed split

This needs static backend addresses: nginx's `least_conn` only works inside a static `upstream{}` block, and open-source nginx (no nginx-plus) resolves a `server <hostname>` entry once, at startup/reload, never again.
So `webhook-1`/`webhook-2` get **static IPs** (`172.28.1.11`/`172.28.1.12`) in `docker-compose.yml`'s `networks:` block instead, letting `nginx.conf`'s `upstream webhook_backend { least_conn; ... }` address them directly - no DNS involved, so a replica recreate can't go stale.
The network's `ipam.ip_range` (`172.28.0.0/24`) deliberately excludes the `172.28.1.x` range the static IPs live in, so Docker's automatic allocator can never hand one of those addresses to some other container first (see `agent_docs/incidents.md` for the race this exclusion fixed).
`max_fails`/`fail_timeout` on each `server` line add nginx's built-in passive health check on top.

## Other backends use a resolver + variable-indirection pattern

`litellm`/`grafana`/`mcp-dev`/`mcp-stats`/`clickhouse`/`prometheus`/`langfuse-web` use a `resolver 127.0.0.11 valid=10s;` + `set $var host:port; proxy_pass http://$var;` pattern instead - no static IP needed since each is a single backend with nothing to `least_conn` between.
A literal `proxy_pass http://host:port;` still gets resolved once at parse time even with `resolver` set, so the variable indirection is what actually defers resolution to request time.
Without it, a routine `docker compose up -d --build <service>` recreate would 502 forever until nginx itself restarted.
ClickHouse's native protocol (port 9000, raw TCP) uses the same variable trick inside a `stream{}` block instead of `http{}`.
`prometheus`/`langfuse-web` not existing at all (profile not enabled) behaves the same way as any other backend not being up yet - per-request 502s, no crash.

## Access/error logs flow to Loki

nginx's `access_log` (stdout, logfmt, `backend=<name>` field per proxied service, set per `server{}` block) and `error_log` (stderr, nginx's native format) both flow through Alloy (`services/alloy/config.alloy`) into Loki - opt-in `observability` profile, see `agent_docs/services/observability.md`.
Query/filter in Grafana by the `container`, `stream` (`stdout`/`stderr`), and `backend` (access log only, ~8 known values) Loki labels.
The landing page (`listen 80`) and `stub_status` (`listen 8080`) stay excluded (`access_log off;`) - no debug value, and `stub_status` is a high-frequency scrape target.
`services/grafana/dashboards-health/infra_overview.json`'s "Load balancer" tab has "Access log"/"Error log" sub-tabs (full-height Logs panels) plus a `$backend` multi-select template variable for this - the go-to spot for diagnosing a routing/latency/error issue instead of `docker compose logs load-balancer` alone.

## litellm-with-fallback proxy ports

Two extra ports (`ANTHROPIC_PROXY_PORT`/`4001`, `OPENAI_PROXY_PORT`/`4002`) proxy straight to litellm, same as `LITELLM_PORT`, but fail over to the real provider (`api.anthropic.com`/`api.openai.com`) if litellm doesn't respond, instead of failing the request.
`make setup-client` points `ANTHROPIC_BASE_URL`/`OPENAI_API_BASE`/Codex's `base_url` at these ports rather than plain `LITELLM_PORT`.

Mixing an HTTP backend (litellm) and an HTTPS backend (the fallback target) in one `upstream{}` doesn't work - `proxy_pass`'s scheme applies to every server in that upstream block, and litellm is plain HTTP while the fallback targets are HTTPS.
So instead of an `upstream{}` with a `backup` server, each port's `location /` proxies to litellm with `proxy_intercept_errors on;` and an `error_page 502 503 504 = @<name>_fallback;`, and the named `location` proxies to the real provider over HTTPS (`proxy_ssl_server_name on; proxy_ssl_name <host>;`).

`proxy_intercept_errors` only fires before any response bytes have reached the client, so this only ever triggers on "litellm didn't respond" (connect-refused/timeout), never on "litellm responded slowly to an already-streaming request" - matches "re-route if litellm doesn't respond" exactly, not "litellm is slow".
`proxy_connect_timeout` is cut to `3s` on these two ports (nginx's own `60s` default) so a genuinely dead litellm fails over quickly instead of making the client wait the better part of a minute first; `proxy_read_timeout` stays at the same `300s` as the main `litellm` block, since that only matters once a connection is already open and shouldn't punish a slow-but-alive stream by yanking it into a fallback mid-response.

The fallback `location` sets `Host` to the real provider's own hostname (`api.anthropic.com`/`api.openai.com`), not `$http_host` like every other block in this file - forwarding the client's own `Host` (`localhost:4001`) would send the wrong SNI/Host to a provider that actually cares which virtual host it's answering as.

`$backend_name` is set per-`location`, not once at the `server{}` level like every other block here - `anthropic-proxy`/`openai-proxy` for the litellm path, `anthropic-proxy-fallback`/`openai-proxy-fallback` for the provider path.
That split exists specifically so the access log - and therefore the Loki `backend` label the "Load balancer" health-dashboard tab filters on - can tell "served by litellm" and "fell back to the real provider" apart, both per-request and in aggregate (the dashboard's "Claude"/"Codex" fallback-count cards, see `services/grafana/dashboards-health/infra_overview.json`).

**Auth caveat, both ports**: whatever auth header the client sent for litellm (e.g. `x-litellm-api-key`, a virtual key) passes straight through unchanged to the fallback target, which won't recognize it.
Only a real `x-api-key`/`anthropic-version` (Anthropic) or `Authorization: Bearer <real key>` (OpenAI) header authenticates once fallen back - nothing here bridges or translates credentials between litellm's virtual-key scheme and the real provider's own auth.

Not extended: the existing blackbox-exporter `probe_success` panel that reports whether `load-balancer` itself is up.
That only reflects "is nginx reachable", not "is litellm behind it reachable" - the fallback-count cards cover that more directly, since they only light up when a fallback actually happened.

## No `depends_on`

`load-balancer` has no `depends_on` on anything, deliberately.
nginx tolerates any backend being down (502s a request until it's reachable, self-heals via the resolver/passive-check mechanisms above).
None of the backends it fronts should be forced to start (or be unstartable on their own) just because the gateway is also running.
