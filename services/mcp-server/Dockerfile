# Prod image - self-contained, buildable/runnable standalone with no
# docker-compose. docker-compose.yml reuses this same image for local dev,
# mounting ./src over /app/src and adding `--reload`, so deps stay baked in
# and only the source is live.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config.yml .

EXPOSE 8001

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8001"]
