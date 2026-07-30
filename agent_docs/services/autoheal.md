# Autoheal

Off-the-shelf `willfarrell/autoheal` image, no build context / own `services/` dir - added straight into `docker-compose.yml`.

## Why it exists

Docker's `restart: always` only reacts to the container process exiting.
It does nothing when a healthcheck starts failing but the process stays up.
Example: `webhook-2` on 2026-07-29.
The process never crashed, but its Python HTTP client's DNS resolver/connection pool got stuck mid-run, so every ClickHouse call failed with `NameResolutionError`.
`docker inspect` showed `Health: unhealthy` for 12+ hours with `RestartCount: 0`.
Recreating the container (fresh process) cleared it immediately.

Autoheal watches the Docker socket for containers whose healthcheck reports `unhealthy` and runs `docker restart` on them.
This covers exactly the "alive but stuck" gap that `restart: always` leaves open.

## Mechanism

- Mounts `/var/run/docker.sock` read-write (needs it to query container health and issue restarts).
- Polls every `AUTOHEAL_INTERVAL` seconds (10s here) for containers labeled `autoheal=true` whose `Health.Status` is `unhealthy`, then `docker restart`s them.
- A restart is a lighter operation than `make up` (recreate) - same container, same config, just a fresh process.
  It won't fix a genuinely broken image/config, only a stuck-but-otherwise-healthy one.

## Labeled services

Every core (non-profile, always-started) service with a `healthcheck:` carries `labels: [autoheal=true]`:

- `clickhouse`
- `grafana`
- `mcp-stats`
- `redis`
- `webhook-1`/`webhook-2` (via the shared `&webhook-service` anchor)
- `load-balancer`
- `worker`
- `litellm`
- `litellm-db`

Optional-profile services (`observability`, `langfuse`, `tools`) aren't labeled.
Add `autoheal=true` to a new core service's `labels:` if it gets its own healthcheck.

## Verifying it worked

```bash
docker inspect receipt-goblin-<service> --format 'Health:{{.State.Health.Status}} RestartCount:{{.RestartCount}}'
docker logs receipt-goblin-autoheal --since 10m
```

Autoheal only logs when it actually restarts something - silence means nothing's been unhealthy long enough to trigger it.
