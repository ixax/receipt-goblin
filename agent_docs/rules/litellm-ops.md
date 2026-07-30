# `litellm` restart/recreate rule

Full rule content for `AGENTS.md`'s "`litellm` restart/recreate" pointer.

Never `restart`/recreate the `litellm` container without asking first.
`docker compose restart` reuses the container's existing environment snapshot; it never picks up a new/changed `environment:` entry in `docker-compose.yml`.
Only `up -d` (recreate) does.
See `agent_docs/incidents.md`'s "`litellm` restart vs. recreate" entry for the incident this rule came from.
