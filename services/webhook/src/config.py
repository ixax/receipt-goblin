import os

# Cosmetic - only shown in Swagger UI (/docs) and /openapi.json.
# Falls back instead of raising (unlike required config, see AGENTS.md
# "Defaults ... live only in docker-compose.yml"), so tests can run
# locally without a running stack.
APP_VERSION = os.environ.get("APP_VERSION", "0.0.0-dev")
