"""Durable, privacy-minimal usage collector for direct Claude transcripts."""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

DEFAULT_API_URL = "http://localhost:8010"
DEFAULT_HOOK_TIMEOUT_SECONDS = 1.5
DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_EVENTS_PER_SECOND = 100.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 3.0
DIRECT_TRACKING_MODE = "direct"

logger = logging.getLogger("claude_usage_collector")


def _first_tool_name(content) -> str:
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            return name if isinstance(name, str) else ""
    return ""


def _source_for(entrypoint: str, tracking_mode: str | None) -> str | None:
    if entrypoint == "claude-desktop":
        return "claude_desktop"
    if entrypoint == "cli" and tracking_mode == DIRECT_TRACKING_MODE:
        return "claude_remote_control"
    return None


def _envelope_from_row(row: dict, tracking_mode: str | None) -> dict | None:
    if row.get("type") != "assistant" or not row.get("requestId"):
        return None
    message = row.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    source = _source_for(row.get("entrypoint", ""), tracking_mode)
    if source is None:
        return None
    cache_creation = usage.get("cache_creation") or {}
    return {
        "schema_version": 1,
        "source": source,
        "event_id": row["requestId"],
        "session_id": row.get("sessionId") or "",
        "timestamp": row.get("timestamp") or "",
        "model": message.get("model") or "",
        "client_version": row.get("version") or "unknown",
        "entrypoint": row.get("entrypoint") or "",
        "stop_reason": message.get("stop_reason") or "",
        "tool_name": _first_tool_name(message.get("content")),
        "agent_id": row.get("agentId") or "",
        "usage": {
            "input_tokens": usage.get("input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
            "cache_creation_tokens": usage.get("cache_creation_input_tokens") or 0,
            "cache_read_tokens": usage.get("cache_read_input_tokens") or 0,
            "cache_creation_1h_tokens": cache_creation.get("ephemeral_1h_input_tokens") or 0,
            "cache_creation_5m_tokens": cache_creation.get("ephemeral_5m_input_tokens") or 0,
        },
    }


def _merge_envelope(existing: dict | None, new: dict) -> dict:
    if existing is None or (not existing.get("tool_name") and new.get("tool_name")):
        return new
    return existing


def _read_envelopes(
    transcript_path: Path,
    *,
    tracking_mode: str | None,
    start_offset: int = 0,
) -> tuple[list[dict], int]:
    path = Path(transcript_path)
    if not path.is_file():
        return [], start_offset
    size = path.stat().st_size
    if start_offset < 0 or start_offset > size:
        start_offset = 0

    envelopes: dict[str, dict] = {}
    offset = start_offset
    with path.open("rb") as transcript:
        transcript.seek(start_offset)
        while True:
            line_start = transcript.tell()
            line = transcript.readline()
            if not line:
                offset = transcript.tell()
                break
            if not line.endswith(b"\n"):
                offset = line_start
                break
            offset = transcript.tell()
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            envelope = _envelope_from_row(row, tracking_mode)
            if envelope is not None:
                event_id = envelope["event_id"]
                envelopes[event_id] = _merge_envelope(envelopes.get(event_id), envelope)
    return list(envelopes.values()), offset


def extract_usage_envelopes(
    transcript_path: Path,
    tracking_mode: str | None = None,
) -> Iterable[dict]:
    envelopes, _ = _read_envelopes(
        Path(transcript_path),
        tracking_mode=tracking_mode,
    )
    return iter(envelopes)


class Outbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=2)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=2000")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_events (
                event_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS transcript_cursors (
                transcript_path TEXT PRIMARY KEY,
                byte_offset INTEGER NOT NULL
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def put(self, envelope: dict) -> None:
        self.put_batch([envelope])

    def put_batch(
        self,
        envelopes: Iterable[dict],
        *,
        transcript_path: Path | None = None,
        byte_offset: int | None = None,
    ) -> None:
        with self._db:
            for envelope in envelopes:
                self._db.execute(
                    """
                    INSERT INTO pending_events(event_id, payload, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET payload = excluded.payload
                    """,
                    (envelope["event_id"], json.dumps(envelope, separators=(",", ":")), time.time()),
                )
            if transcript_path is not None and byte_offset is not None:
                self._db.execute(
                    """
                    INSERT INTO transcript_cursors(transcript_path, byte_offset)
                    VALUES (?, ?)
                    ON CONFLICT(transcript_path) DO UPDATE SET byte_offset = excluded.byte_offset
                    """,
                    (str(Path(transcript_path).resolve()), byte_offset),
                )

    def cursor_for(self, transcript_path: Path) -> int:
        row = self._db.execute(
            "SELECT byte_offset FROM transcript_cursors WHERE transcript_path = ?",
            (str(Path(transcript_path).resolve()),),
        ).fetchone()
        return int(row[0]) if row else 0

    def pending(self, limit: int = 50) -> list[tuple[str, str]]:
        return self._db.execute(
            "SELECT event_id, payload FROM pending_events ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()

    def pending_count(self) -> int:
        return int(self._db.execute("SELECT count() FROM pending_events").fetchone()[0])

    def delete(self, event_id: str) -> None:
        self.delete_many([event_id])

    def delete_many(self, event_ids: Iterable[str]) -> None:
        ids = [(event_id,) for event_id in event_ids]
        if not ids:
            return
        with self._db:
            self._db.executemany("DELETE FROM pending_events WHERE event_id = ?", ids)

    def mark_failed(self, event_id: str, error: str) -> None:
        self.mark_failed_many([event_id], error)

    def mark_failed_many(self, event_ids: Iterable[str], error: str) -> None:
        rows = [(error[:500], event_id) for event_id in event_ids]
        if not rows:
            return
        with self._db:
            self._db.executemany(
                """
                UPDATE pending_events
                SET attempts = attempts + 1, last_error = ?
                WHERE event_id = ?
                """,
                rows,
            )


def collect_transcript(transcript_path: Path, outbox: Outbox, tracking_mode: str | None = None) -> int:
    start_offset = outbox.cursor_for(transcript_path)
    envelopes, byte_offset = _read_envelopes(
        Path(transcript_path),
        tracking_mode=tracking_mode,
        start_offset=start_offset,
    )
    outbox.put_batch(
        envelopes,
        transcript_path=transcript_path,
        byte_offset=byte_offset,
    )
    return len(envelopes)


def _windows_user_environment(name: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except (FileNotFoundError, OSError):
        return ""


def _environment(name: str, default: str = "") -> str:
    return os.environ.get(name) or _windows_user_environment(name) or default


def _default_state_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    return Path(root) / "receipt-goblin" if root else Path.home() / ".cache" / "receipt-goblin"


def _default_outbox_path() -> Path:
    configured = _environment("CLAUDE_USAGE_OUTBOX_PATH")
    return Path(configured) if configured else _default_state_dir() / "claude-usage-outbox.sqlite3"


def flush_outbox(
    outbox: Outbox,
    *,
    api_url: str,
    virtual_key: str,
    time_budget_seconds: float,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_events_per_second: float = DEFAULT_MAX_EVENTS_PER_SECOND,
) -> int:
    if not api_url or not virtual_key:
        pending = outbox.pending_count()
        if pending:
            logger.warning(
                "flush skipped: AGENT_CLI_TRACKING_API_URL/LITELLM_VIRTUAL_KEY unset, %d event(s) stay queued",
                pending,
            )
        return 0
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    endpoint = f"{api_url.rstrip('/')}/api/v1/usage-events"
    deadline = time.monotonic() + max(time_budget_seconds, 0)
    sent = 0
    current_batch_size = batch_size
    next_request_at = 0.0
    while True:
        rows = outbox.pending(limit=current_batch_size)
        if not rows:
            break

        now = time.monotonic()
        if now < next_request_at:
            delay = min(next_request_at - now, max(deadline - now, 0))
            if delay <= 0:
                break
            time.sleep(delay)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        event_ids = [event_id for event_id, _ in rows]
        payload = "[" + ",".join(raw_payload for _, raw_payload in rows) + "]"
        request = urllib.request.Request(
            endpoint,
            data=payload.encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {virtual_key}",
                "Content-Type": "application/json",
            },
        )
        request_started_at = time.monotonic()
        try:
            with urllib.request.urlopen(
                request,
                timeout=min(remaining, DEFAULT_REQUEST_TIMEOUT_SECONDS),
            ) as response:
                if not 200 <= response.status < 300:
                    raise OSError(f"HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            if exc.code == 422:
                if len(rows) > 1:
                    current_batch_size = 1
                    continue
                outbox.mark_failed_many(event_ids, f"HTTP {exc.code}")
                logger.error("usage event rejected by server", extra={"event_id": event_ids[0]})
                outbox.delete_many(event_ids)
                current_batch_size = batch_size
                # The rejected single still consumed a request - keep the per-event pacing honest instead of bursting the retries.
                if max_events_per_second > 0:
                    next_request_at = request_started_at + 1 / max_events_per_second
                continue
            outbox.mark_failed_many(event_ids, f"HTTP {exc.code}")
            break
        except (OSError, urllib.error.URLError) as exc:
            outbox.mark_failed_many(event_ids, type(exc).__name__)
            break
        outbox.delete_many(event_ids)
        sent += len(event_ids)
        current_batch_size = batch_size
        if max_events_per_second > 0:
            next_request_at = request_started_at + len(event_ids) / max_events_per_second
    return sent


def _configure_logging() -> None:
    state_dir = _default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=state_dir / "claude-usage-collector.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _run_hook() -> int:
    try:
        hook_input = json.load(sys.stdin)
        transcript_path = Path(hook_input.get("transcript_path") or "")
        if not transcript_path.is_file():
            return 0
        outbox = Outbox(_default_outbox_path())
        try:
            collect_transcript(
                transcript_path,
                outbox,
                tracking_mode=os.environ.get("CLAUDE_TRANSCRIPT_TRACKING_MODE"),
            )
            flush_outbox(
                outbox,
                api_url=_environment("AGENT_CLI_TRACKING_API_URL", DEFAULT_API_URL),
                virtual_key=_environment("LITELLM_VIRTUAL_KEY"),
                time_budget_seconds=DEFAULT_HOOK_TIMEOUT_SECONDS,
            )
        finally:
            outbox.close()
    except Exception:
        logger.exception("hook collection failed")
    return 0


def _run_backfill(roots: list[Path]) -> int:
    outbox = Outbox(_default_outbox_path())
    try:
        for root in roots:
            paths = [root] if root.is_file() else root.rglob("*.jsonl")
            for path in paths:
                try:
                    # tracking_mode is forced to None: with the ambient CLAUDE_TRANSCRIPT_TRACKING_MODE=direct a launcher exports, historical entrypoint=cli transcripts (including sessions already counted through the proxy) would be reclassified as claude_remote_control and double-counted.
                    # Backfill only ever trusts entrypoint=claude-desktop rows.
                    collect_transcript(path, outbox, tracking_mode=None)
                except Exception:
                    # One locked or corrupt transcript must not abort the rest of the run; committed cursors keep completed files done.
                    logger.exception("backfill failed for %s - skipping", path)
        flush_outbox(
            outbox,
            api_url=_environment("AGENT_CLI_TRACKING_API_URL", DEFAULT_API_URL),
            virtual_key=_environment("LITELLM_VIRTUAL_KEY"),
            time_budget_seconds=120,
        )
        logger.info("backfill complete", extra={"pending_events": outbox.pending_count()})
    finally:
        outbox.close()
    return 0


def _install() -> int:
    hooks_dir = Path.home() / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / "receipt-goblin-usage.py"
    shutil.copy2(Path(__file__).resolve(), target)

    settings_path = Path.home() / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    hooks = settings.setdefault("hooks", {})
    command = f'"{sys.executable}" "{target}" hook'
    for event_name in ("Stop", "SubagentStop"):
        entries = hooks.setdefault(event_name, [])
        already_installed = False
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks", []):
                if isinstance(hook, dict) and hook.get("command") == command:
                    already_installed = True
                    break
            if already_installed:
                break
        if not already_installed:
            entries.append(
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": 5,
                            "statusMessage": "Recording usage...",
                        }
                    ]
                }
            )
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hook")
    backfill = subparsers.add_parser("backfill")
    backfill.add_argument("roots", nargs="*", type=Path)
    subparsers.add_parser("flush")
    subparsers.add_parser("install")
    args = parser.parse_args(argv)

    _configure_logging()
    if args.command == "hook":
        return _run_hook()
    if args.command == "install":
        return _install()

    outbox = Outbox(_default_outbox_path())
    if args.command == "flush":
        try:
            flush_outbox(
                outbox,
                api_url=_environment("AGENT_CLI_TRACKING_API_URL", DEFAULT_API_URL),
                virtual_key=_environment("LITELLM_VIRTUAL_KEY"),
                time_budget_seconds=120,
            )
        finally:
            outbox.close()
        return 0
    outbox.close()
    roots = args.roots or [Path.home() / ".claude" / "projects"]
    return _run_backfill(roots)


if __name__ == "__main__":
    raise SystemExit(main())
