"""Standalone extraction tool - pulls real, already-ingested traffic
straight out of the agent-tracking stack's ClickHouse (agent_events +
ingest_raw) into JSON fixture files under FIXTURES_DIR, sized by VOLUME
(small/medium/large), so `make loadtest` has a corpus to replay even on a
fresh clone. Run via `make loadtest-fixtures` or `python -m
src.build_fixtures`; no HTTP API, one-shot only. Deliberately its own
isolated service (own image, own ClickHouse client) rather than another role
sharing services/webhook's codebase - see AGENTS.md.

Two-phase query, mirroring services/reparse/src/reparse.py's keyset-
pagination precedent for reading ingest_raw in bounded chunks rather than
one unbounded query:

1. Row selection - agent_events has both a `status` column and an ORDER BY
   (timestamp, session_id, litellm_call_id) with timestamp leading, so
   `WHERE status = 'success' ... ORDER BY timestamp DESC LIMIT <n>` is cheap.
2. Payload fetch - ingest_raw has no status column and no time-ordered sort
   key (ORDER BY (litellm_call_id) only - schema.sql notes it's never read
   by time range scan), so the litellm_call_ids from step 1 are looked up
   FIXTURES_CHUNK_SIZE at a time via `WHERE litellm_call_id IN
   {ids:Array(String)}`, which still lets ClickHouse prune granules via the
   primary key instead of a full-table scan.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from common.logging_config import create_logger

from .clickhouse_client import get_client
from .config import FIXTURES_CHUNK_SIZE, FIXTURES_DIR

logger = create_logger("loadtest_fixtures.build_fixtures")

VOLUME_EVENT_COUNTS = {"small": 2000, "medium": 20000, "large": 100000}

_UNSAFE_SESSION_ID_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_session_dir_name(session_id: str) -> str:
    # session_id here traces back to real client-supplied traffic - strip to
    # a safe charset so a crafted value can't escape FIXTURES_DIR via path
    # separators or "..".
    cleaned = _UNSAFE_SESSION_ID_CHARS.sub("_", session_id).strip("._")
    return cleaned or "unknown"


def _select_rows(client, volume: str) -> list:
    count = VOLUME_EVENT_COUNTS[volume]
    result = client.query(
        "SELECT litellm_call_id, session_id, timestamp FROM agent_events "
        "WHERE status = 'success' AND litellm_call_id != '' "
        "ORDER BY timestamp DESC LIMIT {count:UInt32}",
        parameters={"count": count},
    )
    return result.result_rows


def _fetch_payloads(client, call_ids: list) -> dict:
    payloads: dict = {}
    total = len(call_ids)
    for i in range(0, total, FIXTURES_CHUNK_SIZE):
        chunk = call_ids[i:i + FIXTURES_CHUNK_SIZE]
        result = client.query(
            "SELECT litellm_call_id, raw_payload_full FROM ingest_raw "
            "WHERE litellm_call_id IN {ids:Array(String)}",
            parameters={"ids": chunk},
        )
        for call_id, raw_payload_full in result.result_rows:
            payloads[call_id] = raw_payload_full
        if sys.stdout.isatty():
            sys.stdout.write(f"\rfetched {len(payloads)}/{total} payloads")
            sys.stdout.flush()
        else:
            logger.info("fetched %d/%d payloads", len(payloads), total)
    if sys.stdout.isatty():
        sys.stdout.write("\n")
    return payloads


def _acquire_lock() -> Path:
    """One build_fixtures() run at a time - two runs racing on the same
    FIXTURES_DIR swap (see build_fixtures below) would corrupt each other's
    output. Lock file is named after the acquiring timestamp, not the PID -
    each `make loadtest-fixtures` run is a fresh container, so its process
    is always PID 1, which would collide with any earlier run's own PID 1
    and make a stale lock indistinguishable from the current one. The PID
    (still meaningful as *this container's* own top-level process) is kept
    as the lock file's content instead, and printed to the console."""
    existing = sorted(FIXTURES_DIR.glob("*.lock"))
    if existing:
        lock_file = existing[0]
        lock_pid = lock_file.read_text().strip() if lock_file.exists() else "unknown"
        message = (
            f"another loadtest-fixtures build is already running (pid {lock_pid}, "
            f"lock file {lock_file.name}) - refusing to start a second one"
        )
        logger.error(message)
        raise RuntimeError(message)

    pid = os.getpid()
    lock_path = FIXTURES_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.lock"
    lock_path.write_text(str(pid))
    print(f"PID: {pid} (lock: {lock_path.name})")
    logger.info("acquired lock %s (pid %d)", lock_path.name, pid)
    return lock_path


def build_fixtures(volume: str) -> dict:
    if volume not in VOLUME_EVENT_COUNTS:
        raise ValueError(f"unknown volume {volume!r} (expected one of {sorted(VOLUME_EVENT_COUNTS)})")

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _acquire_lock()

    try:
        client = get_client()
        rows = _select_rows(client, volume)
        if not rows:
            logger.warning("no successful agent_events rows found - writing an empty fixture set")

        call_ids = [row[0] for row in rows]
        session_by_call_id = {row[0]: row[1] for row in rows}
        timestamp_by_call_id = {row[0]: row[2] for row in rows}

        payloads = _fetch_payloads(client, call_ids)

        # Build the new fixture set fully in a randomly-named staging
        # subdirectory first, so a run that fails partway through never
        # leaves FIXTURES_DIR in a half-written state. Only once every file
        # (and the manifest) is written do we clear out the old contents and
        # swap the staged set in - FIXTURES_DIR is itself a Docker volume
        # mount point, so it can't be renamed over directly; its *contents*
        # are swapped instead.
        staging_dir = FIXTURES_DIR / f".staging-{uuid.uuid4().hex[:8]}"
        staging_dir.mkdir(parents=True)

        sessions: set = set()
        written = 0
        newest = None
        oldest = None
        for call_id in call_ids:
            raw_payload_full = payloads.get(call_id)
            if raw_payload_full is None:
                continue
            ts = timestamp_by_call_id[call_id]
            session_dir = staging_dir / _safe_session_dir_name(session_by_call_id[call_id])
            session_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{ts.strftime('%Y%m%dT%H%M%S%f')}-{hashlib.sha1(call_id.encode()).hexdigest()[:8]}.json"
            # raw_payload_full is already the original JSON string - write it
            # verbatim, no reserialization needed.
            (session_dir / filename).write_bytes(raw_payload_full.encode())
            sessions.add(session_by_call_id[call_id])
            written += 1
            if newest is None or ts > newest:
                newest = ts
            if oldest is None or ts < oldest:
                oldest = ts

        manifest = {
            "volume": volume,
            "event_count": written,
            "session_count": len(sessions),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "newest_event_timestamp": newest.isoformat() if newest else None,
            "oldest_event_timestamp": oldest.isoformat() if oldest else None,
        }
        (staging_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # lock_path itself lives directly under FIXTURES_DIR alongside the
        # session dirs/manifest.json being replaced - skip it here so it
        # survives to be removed by the `finally` below instead of vanishing
        # mid-run out from under this same process.
        for entry in FIXTURES_DIR.iterdir():
            if entry != staging_dir and entry != lock_path:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        for entry in staging_dir.iterdir():
            shutil.move(str(entry), str(FIXTURES_DIR / entry.name))
        staging_dir.rmdir()

        logger.info("wrote %d fixture files across %d sessions (volume=%s)", written, len(sessions), volume)
        return manifest
    finally:
        lock_path.unlink(missing_ok=True)
        logger.info("released lock %s", lock_path.name)


def print_status() -> None:
    """Reads and prints FIXTURES_DIR/manifest.json verbatim, without
    touching ClickHouse - what loadtest-runner calls to check freshness
    before deciding whether to regenerate."""
    manifest_path = FIXTURES_DIR / "manifest.json"
    if not manifest_path.exists():
        print(json.dumps({"status": "none", "detail": f"no manifest.json found under {FIXTURES_DIR}"}))
        return
    print(manifest_path.read_text())


def _prompt_volume() -> str:
    choices = sorted(VOLUME_EVENT_COUNTS)
    while True:
        raw = input(f"Fixture volume ({'/'.join(choices)}): ").strip().lower()
        if raw in VOLUME_EVENT_COUNTS:
            return raw
        print(f"  invalid choice {raw!r} - expected one of {choices}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--volume", choices=sorted(VOLUME_EVENT_COUNTS), default=None,
        help="How many successful agent_events rows to extract - small=2000, medium=20000, large=100000. "
             "Falls back to $LOADTEST_FIXTURES_VOLUME (validated, not silently defaulted), then prompts "
             "interactively if neither is set.",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print FIXTURES_DIR/manifest.json and exit, without touching ClickHouse.",
    )
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    volume = args.volume
    if volume is None:
        env_volume = os.environ.get("LOADTEST_FIXTURES_VOLUME")
        if env_volume is not None:
            if env_volume not in VOLUME_EVENT_COUNTS:
                print(
                    f"error: LOADTEST_FIXTURES_VOLUME={env_volume!r} is not valid "
                    f"(expected one of {sorted(VOLUME_EVENT_COUNTS)})",
                    file=sys.stderr,
                )
                sys.exit(1)
            volume = env_volume
        else:
            volume = _prompt_volume()

    build_fixtures(volume)


if __name__ == "__main__":
    main()
