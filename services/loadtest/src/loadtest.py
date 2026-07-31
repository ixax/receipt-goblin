"""CLI-only load generator - replays real traffic from FIXTURES_DIR, filled
by the separate loadtest-fixtures service (services/loadtest-fixtures/,
which extracts it from ClickHouse - see AGENTS.md), against webhook's own
POST /api/v1/metrics, at a ramping concurrency profile. Run via `make
loadtest` or `python -m src.loadtest`; no HTTP API of its own.

Deliberately bypasses LiteLLM and the real Claude/Anthropic API entirely -
the only thing exercised is webhook -> redis -> webhook-worker -> clickhouse
(see AGENTS.md "Why a queue in front of ClickHouse"). /api/v1/metrics needs
no LiteLLM virtual key (unlike /api/v1/session-git-branch and
/api/v1/plan-proposal - see server.py), so this never touches LITELLM_*.

Traffic model: each session directory under FIXTURES_DIR is one real Claude
Code session's status="success" StandardLoggingPayload events (agentic
tool-call round trips, not standalone chat messages) - build_fixtures.py
already filters to status="success" at extraction time, so every file here
qualifies - with real inter-event gaps and real payload sizes/token counts.
One "virtual user" here is a loop
that repeatedly picks a random session, replays its events in order at the
real (or --speed-scaled) cadence, then immediately picks another session and
keeps going - modeling one person's continuous Claude usage, not a single
request. Each event file's bytes are sent to webhook completely unmodified -
no id/trace_id rewriting, no timestamp shifting. This is a load test, not a
data-integrity test: concurrent replays of the same captured session will
collide on ingest_raw' ReplacingMergeTree key in ClickHouse, so row counts
there under-report actual replayed volume - use Stats/the final report
(requests sent, status codes) as the real throughput signal, not ClickHouse
row growth.

Load profile is a ramp, not a flat concurrency level: starts at
--start-users, adds more every --ramp-step-minutes, up to --end-users, then
holds. See _resolve_schedule()'s docstring for the exact two ways total
length can be specified.

What to watch in another terminal/Grafana while this runs:
  - webhook-worker's own metrics on :9200/metrics - worker_stream_depth
    (Redis Stream backlog, the best "is the worker keeping up" signal),
    worker_pending_count, worker_flush_latency_seconds, worker_decode_failures_total.
  - webhook's FastAPI Instrumentator metrics on :8000/metrics (request
    latency/count - client-perceived POST latency here doubles as "is the
    queue backpressuring").
  - redis-exporter's stock Redis memory stats (config.yml's maxlen=1500 is
    sized around ~360KB average payload staying under redis's maxmemory).
  - ClickHouse's own :9363 Prometheus endpoint, or clickhouse-analyst for
    ingest_raw row growth.
"""
import argparse
import asyncio
import glob
import math
import os
import random
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from common.logging_config import create_logger

logger = create_logger("webhook.loadtest")

# Where loadtest.py reads its replay corpus from - a dedicated Docker
# volume in prod, written by the separate loadtest-fixtures service
# (services/loadtest-fixtures/, see AGENTS.md) and mounted ro here.
FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR", "/app/loadtest_fixtures"))

DEFAULT_TARGET_URL = "http://webhook:8000/api/v1/metrics"


@dataclass
class SessionTrace:
    """One real session: its event files' paths in chronological order, each
    paired with the real gap (seconds) since the previous file's timestamp.
    The first file's gap is always 0 - nothing to wait for before it.
    Deliberately holds only paths, not parsed payloads - a large fixture set
    can still run multi-GB, and loading every session's full event content
    into memory upfront doesn't scale with it. Each file's bytes are only
    ever read once, right before that event is sent, unmodified (see
    _read_bytes/_virtual_user)."""
    paths: list = field(default_factory=list)  # list[str]
    gaps: list = field(default_factory=list)  # list[float], same length as paths


def _filename_epoch(path: str) -> float:
    # Filenames are "YYYYMMDDTHHMMSSffffff-hash.json" (build_fixtures.py's
    # naming convention) - chronological by sort, and the only timestamp
    # source used for gap calculation: reading it out of the filename means
    # indexing the whole corpus never has to open a single file's content
    # (see SessionTrace's docstring for why that matters).
    stem = os.path.basename(path).split("-", 1)[0]
    try:
        return datetime.strptime(stem, "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return time.time()


def load_session_corpus(fixtures_dir: str, max_gap_seconds: float) -> list[SessionTrace]:
    """Scans fixtures_dir for session subdirectories (one per real Claude
    Code session) and indexes each one's event file paths, in filename
    order, into a SessionTrace with real inter-event gaps (from filename
    timestamps, not file content) clamped to max_gap_seconds so a rare
    multi-minute "human stepped away" gap doesn't stall a virtual user for
    the whole test.

    No status filter here - build_fixtures.py already extracts only
    status="success" agent_events rows, so every file under fixtures_dir
    qualifies (unlike the old .capture/-based corpus, which mixed in failed
    LiteLLM calls that needed filtering out per-file).
    """
    traces: list[SessionTrace] = []
    for session_dir in sorted(glob.glob(os.path.join(fixtures_dir, "*"))):
        if not os.path.isdir(session_dir):
            continue
        files = sorted(glob.glob(os.path.join(session_dir, "*.json")))
        if not files:
            continue
        starts = [_filename_epoch(path) for path in files]
        gaps = [0.0]
        for i in range(1, len(starts)):
            gap = max(0.0, starts[i] - starts[i - 1])
            gaps.append(min(gap, max_gap_seconds))
        traces.append(SessionTrace(paths=files, gaps=gaps))
    return traces


def _read_bytes(path: str) -> Optional[bytes]:
    # Each file is read exactly once, never revisited - posix_fadvise(DONTNEED)
    # tells the kernel not to keep these pages cached, so the read-only
    # fixtures volume mount's page cache (charged to this container's memory
    # cgroup) doesn't accumulate across the run's full corpus (multi-GB) and
    # trip the OOM killer well before mem_limit would matter for actual heap
    # usage - confirmed via resource.getrusage staying flat (~50MB) while
    # docker stats climbed straight to mem_limit without this.
    try:
        with open(path, "rb") as fh:
            data = fh.read()
            try:
                os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
            return data
    except OSError:
        logger.warning("skipping unreadable fixture file %s", path)
        return None


@dataclass
class Stats:
    """Shared across all virtual users - plain counters/lists, no locking
    needed since asyncio tasks only yield at awaits, never mid-statement."""
    sent: int = 0
    status_counts: dict = field(default_factory=dict)
    errors: int = 0
    latencies: list = field(default_factory=list)
    bytes_sent: int = 0

    def record(self, status: Optional[int], latency: float, size: int) -> None:
        self.sent += 1
        self.bytes_sent += size
        self.latencies.append(latency)
        if status is None:
            self.errors += 1
        else:
            self.status_counts[status] = self.status_counts.get(status, 0) + 1

    def summary_line(self, elapsed: float) -> str:
        rate = self.sent / elapsed if elapsed > 0 else 0.0
        error_rate = self.errors / self.sent if self.sent else 0.0
        return f"sent={self.sent} rate={rate:.1f}/s errors={self.errors} ({error_rate:.1%})"


async def _virtual_user(session: aiohttp.ClientSession, target_url: str, corpus: list[SessionTrace],
                         speed: float, stats: Stats, deadline: float, rng: random.Random) -> None:
    while time.monotonic() < deadline:
        trace = rng.choice(corpus)
        for path, gap in zip(trace.paths, trace.gaps):
            if gap > 0:
                wait = gap / speed if speed > 0 else 0.0
                if wait > 0:
                    await asyncio.sleep(wait)
            if time.monotonic() >= deadline:
                return
            # Read this event's raw bytes from disk now, not at corpus-load
            # time (see SessionTrace's docstring) - to_thread so the blocking
            # file read doesn't stall every other virtual user's event loop
            # tasks. Sent completely unmodified - see module docstring.
            body = await asyncio.to_thread(_read_bytes, path)
            if body is None:
                continue
            start = time.monotonic()
            try:
                async with session.post(target_url, data=body,
                                         headers={"Content-Type": "application/json"}) as resp:
                    await resp.read()
                    stats.record(resp.status, time.monotonic() - start, len(body))
            except (aiohttp.ClientError, asyncio.TimeoutError):
                stats.record(None, time.monotonic() - start, len(body))


@dataclass
class RampStep:
    fires_at_minute: float
    target_users: int


@dataclass
class Schedule:
    start_users: int
    end_users: int
    ramp_step_users: int
    steps: list  # list[RampStep], first step is t=0 -> start_users
    ramp_minutes: float
    hold_minutes: float
    total_minutes: float


def _resolve_schedule(start_users: int, end_users: int, ramp_steps: int, ramp_step_minutes: float,
                       duration_minutes: float, hold_minutes: float) -> Schedule:
    """Two ways to specify total length, unified into one Schedule:

    - duration_minutes == 0 (default): total = ramp_minutes + hold_minutes.
      "Tell me start/end and how fast to step, figure out the rest."
    - duration_minutes > 0: that IS the total, hold_minutes is derived as
      duration_minutes - ramp_minutes (clamped at 0 - if the window is
      shorter than the ramp needs, the run just ends mid-ramp, never
      reaching end_users; no error). "I know max users and total test
      length, you figure out the ramp/hold split."

    ramp_step_users (how many users each step adds) is never a manual flag -
    it's derived from ramp_steps so the caller only has to pick how many
    increments to split the climb into, not hand-tune a step size.
    """
    end_users = max(end_users, start_users)
    ramp_steps = max(ramp_steps, 1)
    ramp_step_users = math.ceil((end_users - start_users) / ramp_steps) if end_users > start_users else 0
    ramp_minutes = ramp_steps * ramp_step_minutes if end_users > start_users else 0.0

    if duration_minutes and duration_minutes > 0:
        total_minutes = duration_minutes
        hold_minutes = max(0.0, total_minutes - ramp_minutes)
    else:
        total_minutes = ramp_minutes + hold_minutes

    steps = [RampStep(fires_at_minute=0.0, target_users=start_users)]
    current = start_users
    step_num = 0
    while current < end_users and (step_num + 1) * ramp_step_minutes <= total_minutes:
        step_num += 1
        current = min(end_users, start_users + step_num * ramp_step_users)
        steps.append(RampStep(fires_at_minute=step_num * ramp_step_minutes, target_users=current))

    return Schedule(
        start_users=start_users, end_users=end_users, ramp_step_users=ramp_step_users,
        steps=steps, ramp_minutes=ramp_minutes, hold_minutes=hold_minutes, total_minutes=total_minutes,
    )


def _print_schedule(schedule: Schedule) -> None:
    logger.info("=== resolved load schedule ===")
    logger.info("start_users=%d end_users=%d ramp_step_users=%d (derived)",
                schedule.start_users, schedule.end_users, schedule.ramp_step_users)
    for step in schedule.steps:
        logger.info("  t=%.2f min -> %d users", step.fires_at_minute, step.target_users)
    logger.info("ramp completes at t=%.2f min; hold %.2f min after that; total planned duration %.2f min",
                schedule.ramp_minutes, schedule.hold_minutes, schedule.total_minutes)
    logger.info("==============================")


async def _run_ramp(target_url: str, corpus: list, schedule: Schedule, speed: float, seed: Optional[int]) -> Stats:
    stats = Stats()
    rng = random.Random(seed)
    start = time.monotonic()
    deadline = start + schedule.total_minutes * 60

    tasks: list[asyncio.Task] = []
    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        current_users = 0
        for step in schedule.steps:
            wait = step.fires_at_minute * 60 - (time.monotonic() - start)
            if wait > 0:
                await asyncio.sleep(wait)
            to_add = step.target_users - current_users
            for _ in range(to_add):
                tasks.append(asyncio.create_task(
                    _virtual_user(session, target_url, corpus, speed, stats, deadline, rng)
                ))
            current_users = step.target_users
            logger.info("ramp: now at %d/%d users", current_users, schedule.end_users)

        remaining = deadline - time.monotonic()
        if remaining > 0:
            progress_task = asyncio.create_task(_log_progress(stats, start, deadline))
            await asyncio.sleep(remaining)
            progress_task.cancel()

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return stats


async def _log_progress(stats: Stats, start: float, deadline: float) -> None:
    last_sent = 0
    last_mark = start
    try:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            logger.info("progress: %s", stats.summary_line(now - start))
            if now - last_mark >= 60:
                window = now - last_mark
                sent_in_window = stats.sent - last_sent
                logger.info("last %.0fs: %d requests sent (%.1f/s)",
                            window, sent_in_window, sent_in_window / window)
                last_sent = stats.sent
                last_mark = now
    except asyncio.CancelledError:
        pass


def _print_final_report(stats: Stats, elapsed: float) -> None:
    logger.info("=== final report ===")
    logger.info("requests sent: %d over %.1fs (%.1f req/s)", stats.sent, elapsed,
                stats.sent / elapsed if elapsed > 0 else 0.0)
    logger.info("status breakdown: %s", stats.status_counts or "<none>")
    logger.info("client errors (no response): %d", stats.errors)
    logger.info("bytes sent: %d", stats.bytes_sent)
    if stats.latencies:
        sorted_lat = sorted(stats.latencies)
        def pct(p: float) -> float:
            idx = min(len(sorted_lat) - 1, int(len(sorted_lat) * p))
            return sorted_lat[idx]
        logger.info("latency p50=%.3fs p90=%.3fs p99=%.3fs max=%.3fs mean=%.3fs",
                    pct(0.50), pct(0.90), pct(0.99), sorted_lat[-1], statistics.mean(stats.latencies))
    logger.info("=====================")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target-url", default=os.environ.get("TARGET_URL", DEFAULT_TARGET_URL))
    parser.add_argument("--fixtures-dir", default=os.environ.get("FIXTURES_DIR_OVERRIDE") or str(FIXTURES_DIR))
    parser.add_argument("--start-users", type=int, default=int(os.environ.get("START_USERS", 10)))
    parser.add_argument("--end-users", type=int, default=int(os.environ.get("END_USERS", 100)))
    parser.add_argument("--ramp-steps", type=int, default=int(os.environ.get("RAMP_STEPS", 10)))
    parser.add_argument("--ramp-step-minutes", type=float, default=float(os.environ.get("RAMP_STEP_MINUTES", 1)))
    parser.add_argument("--hold-minutes", type=float, default=float(os.environ.get("HOLD_MINUTES", 5)))
    parser.add_argument("--duration-minutes", type=float, default=float(os.environ.get("DURATION_MINUTES", 0)))
    parser.add_argument("--speed", type=float, default=float(os.environ.get("SPEED", 1.0)),
                        help="Divides real inter-event gaps. 1.0=realistic, >1=faster, 0=no waiting (burst).")
    parser.add_argument("--max-gap-seconds", type=float, default=float(os.environ.get("MAX_GAP_SECONDS", 60)))
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    corpus = load_session_corpus(args.fixtures_dir, args.max_gap_seconds)
    if not corpus:
        logger.error("no session traces found under %s - nothing to replay", args.fixtures_dir)
        raise SystemExit(1)
    logger.info("loaded %d session traces (%d total events) from %s",
                len(corpus), sum(len(t.paths) for t in corpus), args.fixtures_dir)

    schedule = _resolve_schedule(
        args.start_users, args.end_users, args.ramp_steps, args.ramp_step_minutes,
        args.duration_minutes, args.hold_minutes,
    )
    _print_schedule(schedule)
    logger.info("target: %s (speed=%.2fx)", args.target_url, args.speed)

    start = time.monotonic()
    stats = asyncio.run(_run_ramp(args.target_url, corpus, schedule, args.speed, args.seed))
    _print_final_report(stats, time.monotonic() - start)


if __name__ == "__main__":
    main()
