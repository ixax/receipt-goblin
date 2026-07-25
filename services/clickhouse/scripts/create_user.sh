#!/bin/sh
# Interactive ClickHouse user provisioning - creates a user and grants
# exactly the access answered for, nothing implicit. Run via:
#   docker compose exec clickhouse /scripts/create_user.sh
# (needs a TTY for the prompts - `exec`, not `run`, since this operates on
# the already-running clickhouse server, not a one-off container).
#
# Uses CLICKHOUSE_USER/CLICKHOUSE_PASSWORD, which for the `clickhouse`
# service's own container are the bootstrap superuser's credentials (see
# docker-compose.yml's `clickhouse` service - CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1
# makes that identity a full superuser with GRANT OPTION; only that account
# can CREATE USER/GRANT). Not the same CLICKHOUSE_USER the app services use.
#
# Every statement this would run is printed up front and requires an
# explicit y/N confirmation before anything touches the database - no flag
# skips that prompt. system.* access (Play's schema-browser sidebar, the
# built-in /dashboard page) is always read-only (SELECT), regardless of
# what's granted on the target database/table.
set -eu

CH="clickhouse-client --user ${CLICKHOUSE_USER} --password ${CLICKHOUSE_PASSWORD}"

echo "=== ClickHouse user provisioning ==="
printf 'Username: '
read -r USERNAME
if [ -z "$USERNAME" ]; then
  echo "Username can't be empty." >&2
  exit 1
fi

printf 'Password (leave blank to generate one): '
stty -echo 2>/dev/null || true
read -r PASSWORD
stty echo 2>/dev/null || true
echo
GENERATED=0
if [ -z "$PASSWORD" ]; then
  PASSWORD=$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
  GENERATED=1
fi
# Escape single quotes for safe interpolation into the SQL string literal below.
PASSWORD_ESCAPED=$(printf '%s' "$PASSWORD" | sed "s/'/''/g")

printf 'Grant Play/Dashboard UI access? (read-only system.* introspection - schema browser sidebar, built-in /dashboard page) [y/N]: '
read -r UI_ACCESS

printf 'Create a new database for this user? [y/N]: '
read -r CREATE_DB
NEWDB=""
if [ "$CREATE_DB" = "y" ] || [ "$CREATE_DB" = "Y" ]; then
  printf 'New database name: '
  read -r NEWDB
  if [ -z "$NEWDB" ]; then
    echo "Database name can't be empty." >&2
    exit 1
  fi
fi

if [ -n "$NEWDB" ]; then
  # A database was just created for this user - ask about access to that
  # one specifically instead of the generic prompt below (no reason to
  # default the question to 'default' when they just told us the database
  # they actually care about).
  printf 'Grant full access (not just SELECT) to %s? [y/N]: ' "$NEWDB"
  read -r WRITE_ACCESS
  TARGET="$NEWDB.*"
else
  printf "Database or database.table for full data access (e.g. 'default' or 'default.agent_events'; blank = no data access, just the UI grant above): "
  read -r TARGET

  WRITE_ACCESS=""
  if [ -n "$TARGET" ]; then
    printf 'Write access on %s (INSERT/ALTER/etc, not just SELECT)? [y/N]: ' "$TARGET"
    read -r WRITE_ACCESS
  fi
fi

STATEMENTS="CREATE USER OR REPLACE $USERNAME IDENTIFIED BY '<password>'"
if [ -n "$NEWDB" ]; then
  STATEMENTS="$STATEMENTS
CREATE DATABASE IF NOT EXISTS $NEWDB"
fi
if [ "$UI_ACCESS" = "y" ] || [ "$UI_ACCESS" = "Y" ]; then
  STATEMENTS="$STATEMENTS
GRANT SELECT ON system.* TO $USERNAME"
fi
if [ -n "$TARGET" ]; then
  if [ "$WRITE_ACCESS" = "y" ] || [ "$WRITE_ACCESS" = "Y" ]; then
    STATEMENTS="$STATEMENTS
GRANT ALL ON $TARGET TO $USERNAME"
  else
    STATEMENTS="$STATEMENTS
GRANT SELECT ON $TARGET TO $USERNAME"
  fi
fi

echo
echo "=== about to run (as bootstrap superuser) ==="
echo "$STATEMENTS"
if [ "$GENERATED" -eq 1 ]; then
  echo
  echo "(generated password: $PASSWORD)"
fi
echo "==============================================="

printf 'Execute these statements? [y/N]: '
read -r CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Aborted - nothing was changed."
  exit 0
fi

$CH -q "CREATE USER OR REPLACE $USERNAME IDENTIFIED BY '$PASSWORD_ESCAPED'"
if [ -n "$NEWDB" ]; then
  $CH -q "CREATE DATABASE IF NOT EXISTS $NEWDB"
fi
if [ "$UI_ACCESS" = "y" ] || [ "$UI_ACCESS" = "Y" ]; then
  $CH -q "GRANT SELECT ON system.* TO $USERNAME"
fi
if [ -n "$TARGET" ]; then
  if [ "$WRITE_ACCESS" = "y" ] || [ "$WRITE_ACCESS" = "Y" ]; then
    $CH -q "GRANT ALL ON $TARGET TO $USERNAME"
  else
    $CH -q "GRANT SELECT ON $TARGET TO $USERNAME"
  fi
fi

echo
echo "Done. User '$USERNAME' created."
if [ "$GENERATED" -eq 1 ]; then
  echo "Password: $PASSWORD  (shown once - save it now)"
fi
