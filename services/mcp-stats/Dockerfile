# Self-contained, buildable/runnable standalone with no docker-compose.
# mcp-stats is prod-facing (ships alongside litellm/grafana) - fixed
# statistics tools only, no arbitrary SQL. See services/mcp-dev for the
# dev-only sibling (query/profile_query, arbitrary read-only SQL, no auth).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 8002

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8002"]
