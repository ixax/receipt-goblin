# Prod image - self-contained, no bind mounts assumed. Buildable and runnable
# standalone (`docker build -t mcp-server . && docker run -p 8001:8001
# mcp-server`), no docker-compose required.
# docker-compose.yml still builds this same image for local dev, but mounts
# ./src over /app/src and overrides `command:` to add `--reload` - deps stay
# baked into the image (no reinstall on every container start), only the
# source is live.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 8001

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8001"]
