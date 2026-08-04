# Build context is the repo root (not this directory), so it can also bake
# in services/_common:
# `docker build -t worker -f services/worker/Dockerfile . && docker run worker`.
# Split out of services/webhook/Dockerfile's one-image/APP_ROLE design -
# see plans/webhook-worker-split.md.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

COPY services/worker/requirements.lock .
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
COPY services/worker/src/ ./src/

EXPOSE 9200

CMD ["python", "-m", "src.worker"]
