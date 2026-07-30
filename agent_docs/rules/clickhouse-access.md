# ClickHouse access rule

Full rule content for `AGENTS.md`'s "ClickHouse access" pointer.

Never run `docker exec .../clickhouse-client` against any ClickHouse container.
Use `mcp-dev`'s `query`/`profile_query` tools (dev) or `mcp-stats`'s `me` tool (prod) instead.
Both go through `_validate_readonly_sql` (`agent_docs/services/mcp-dev.md`), the only thing standing between an agent and an unrestricted read/write/DDL statement - a raw `clickhouse-client` session bypasses that entirely.
If the right tool call is rejected or unavailable, ask the user before falling back to any direct access.
