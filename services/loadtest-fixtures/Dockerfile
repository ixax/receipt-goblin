# Build context is the repo root (not this directory), so it can also bake
# in services/_common: `docker build -t loadtest-fixtures -f services/loadtest-fixtures/Dockerfile . && docker run loadtest-fixtures`.
# Otherwise self-contained, standalone service with its own dependency set -
# deliberately not another APP_ROLE sharing services/webhook/Dockerfile's
# image, see AGENTS.md.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

COPY services/loadtest-fixtures/requirements.lock .
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
COPY services/loadtest-fixtures/src/ ./src/
COPY services/loadtest-fixtures/config.yml .

CMD ["python", "-m", "src.build_fixtures"]
