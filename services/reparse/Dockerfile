# Build context is the repo root (not this directory), so it can also bake
# in services/_common:
# `docker build -t reparse -f services/reparse/Dockerfile . && docker run reparse`.
# Split out of services/webhook/Dockerfile's one-image/APP_ROLE design -
# see plans/webhook-worker-split.md. CLI-only, no HTTP API, not started by
# a plain `docker compose up` (profiles: tools on the compose side).
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

COPY services/reparse/requirements.lock .
# requirements.lock pins the full transitive tree (regenerate with
# `make lock`), so pip resolves nothing here - that resolver pass, not the
# downloads, was most of a cold build's time. --mount then keeps pip's
# wheel cache across builds, so a lock change re-downloads only what
# actually moved. Both need BuildKit (docker-buildx) - see README
# "Prerequisites: Docker via Colima".
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.lock

COPY services/_common/src/ ./common/
COPY services/_common/queue.yml ./common/queue.yml
COPY services/reparse/src/ ./src/
COPY services/reparse/config.yml .

CMD ["python", "-m", "src.reparse"]
