# Self-contained, buildable/runnable standalone with no docker-compose.
# mcp-dev is dev-only (query/profile_query, arbitrary read-only SQL) -
# docker-compose.dev.yml is the only compose file that builds/runs this
# image, mounting ./src over /app/src and adding `--reload`, so deps stay
# baked in and only the source is live. See services/mcp-stats for the
# prod-facing sibling (fixed statistics tools, real auth).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config.yml .

EXPOSE 8001

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8001"]
