# Prod image - self-contained, standalone service with its own build
# context (this directory, not the repo root) and its own dependency set -
# deliberately not another APP_ROLE sharing services/webhook/Dockerfile's
# image, see AGENTS.md. Buildable/runnable standalone with no docker-compose:
# `docker build -t loadtest-fixtures . && docker run loadtest-fixtures`.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config.yml .

CMD ["python", "-m", "src.build_fixtures"]
