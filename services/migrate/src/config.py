import os
from pathlib import Path

MIGRATIONS_DIR = Path(os.environ.get("MIGRATIONS_DIR", "/app/migrations"))

# Bootstrap superuser - migrate.py's own DDL (schema_migrations,
# migrations/*.sql, dictionaries) needs rights no least-privilege role
# should hold.
CLICKHOUSE_BOOTSTRAP_USER = os.environ["CLICKHOUSE_BOOTSTRAP_USER"]
CLICKHOUSE_BOOTSTRAP_PASSWORD = os.environ["CLICKHOUSE_BOOTSTRAP_PASSWORD"]
