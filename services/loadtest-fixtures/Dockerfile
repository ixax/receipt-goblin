# Build context is the repo root (not this directory), so it can also bake
# in services/_common: `docker build -t loadtest-fixtures -f services/loadtest-fixtures/Dockerfile . && docker run loadtest-fixtures`.
# Otherwise self-contained, standalone service with its own dependency set -
# deliberately not another APP_ROLE sharing services/webhook/Dockerfile's
# image, see AGENTS.md.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

COPY services/loadtest-fixtures/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/_common/src/ ./common/
COPY services/_common/queue.yml ./common/queue.yml
COPY services/loadtest-fixtures/src/ ./src/
COPY services/loadtest-fixtures/config.yml .

CMD ["python", "-m", "src.build_fixtures"]
