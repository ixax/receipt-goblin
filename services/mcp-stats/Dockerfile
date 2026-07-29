# Build context is the repo root (not this directory), so it can also bake
# in services/_common: `docker build -t mcp-stats -f services/mcp-stats/Dockerfile .`.
# mcp-stats is prod-facing (ships alongside litellm/grafana) - fixed
# statistics tools only, no arbitrary SQL. See services/mcp-dev for the
# dev-only sibling (query/profile_query, arbitrary read-only SQL, no auth).
FROM python:3.12-slim

WORKDIR /app

COPY services/mcp-stats/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/_common/src/ ./common/
COPY services/mcp-stats/src/ ./src/

EXPOSE 8002

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8002"]
