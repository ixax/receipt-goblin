# Prod image, self-contained. Build context is the repo root (not this
# directory), so it can also bake in services/_common:
# `docker build -t webhook -f services/webhook/Dockerfile . && docker run -p 8000:8000 webhook`.
# docker-compose.dev.yml reuses this image for local dev, mounting ./src
# over /app/src and adding `--reload`.
# Split out of the old one-image/APP_ROLE design (worker/reparse/migrate/
# loadtest each have their own Dockerfile now) - see
# plans/webhook-worker-split.md.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

COPY services/webhook/requirements.lock .
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
COPY services/webhook/src/ ./src/

EXPOSE 8000

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
