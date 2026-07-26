"""Schema-v2 provenance recording, run as a SHADOW alongside v1's
vb.storage.save_raw_capture - records the exact same already-fetched
data scrapers already produce, so it can be wired into
scripts/scheduled_run.py without touching a single scraper's fetch
code. Fixes F-06/F-16: "a Python process ran and fetched this" becomes
a real, statused, queryable capture_run/source_run row instead of an
untracked event with no record it ever happened.

Not yet the live capture path - see vb/pipeline.py's run_cycle_v2 for
the matching/edge consumer of what this writes, and
PROJECT_DOCUMENTATION.md's Phase 1/2 notes for why the actual cutover
of scripts/scheduled_run.py to depend on this is deferred to the
infrastructure migration (Phase 2) rather than done in place here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from .identity import new_id
from .models import CaptureRun, EventVersionV2, MarketSnapshot, MarketSnapshotV2, RawEvent, RunStatus, SourceRun
from .storage import (
    finish_capture_run,
    finish_source_run,
    get_or_create_event_version,
    save_capture_run,
    save_market_snapshot_v2,
    save_source_run,
)


def start_capture_run(
    conn, git_sha: str, schema_version: int, scheduled_for: Optional[str] = None, now: Optional[datetime] = None
) -> str:
    now = now or datetime.now(timezone.utc)
    run_id = new_id()
    save_capture_run(conn, CaptureRun(
        id=run_id, started_at=now, git_sha=git_sha, schema_version=schema_version, scheduled_for=scheduled_for,
    ))
    return run_id


def end_capture_run(conn, capture_run_id: str, status: RunStatus, now: Optional[datetime] = None) -> None:
    finish_capture_run(conn, capture_run_id, status, now or datetime.now(timezone.utc))


def record_source_capture(
    conn, capture_run_id: str, site: str, mode: str,
    captures: Iterable[tuple[RawEvent, list[MarketSnapshot]]], now: Optional[datetime] = None,
) -> str:
    """Record one site's already-successful capture as v2 provenance: a
    SourceRun plus one MarketSnapshotV2 per snapshot the scraper
    actually returned, each tied to a (possibly reused - see
    get_or_create_event_version) EventVersionV2.

    `captures` is `(RawEvent, list[MarketSnapshot])` pairs - the exact
    shape scrapers already hand to vb.storage.save_raw_capture. Always
    ends the source_run SUCCESS; call record_source_failure instead
    when the fetch itself raised, since this function assumes the data
    already exists.

    Each snapshot's received_at is its own MarketSnapshot.captured_at
    (the scraper's real per-snapshot receipt instant), not the batch
    `now` this function was called with - F-01's freshness gate needs
    the actual per-quote receipt time, not a save-time approximation
    that could lag behind a slow scrape.
    """
    now = now or datetime.now(timezone.utc)
    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=capture_run_id, site=site, mode=mode, started_at=now))

    event_count = 0
    snapshot_count = 0
    for event, snapshots in captures:
        event_count += 1
        event_version_id = get_or_create_event_version(conn, EventVersionV2(
            id=new_id(), site=event.site, event_id=event.event_id, valid_from=now, source_run_id=source_run_id,
            sport=event.sport, competition=event.competition, kickoff_utc=event.kickoff_utc,
            home_team=event.raw_home_team, away_team=event.raw_away_team,
        ))
        for s in snapshots:
            snapshot_count += 1
            save_market_snapshot_v2(conn, MarketSnapshotV2(
                id=new_id(), source_run_id=source_run_id, event_version_id=event_version_id,
                market_type=s.market_type, line=s.line, outcomes=s.outcomes,
                received_at=s.captured_at, max_bet_size=s.max_bet_size,
            ))

    finish_source_run(conn, source_run_id, RunStatus.SUCCESS, now, event_count=event_count, snapshot_count=snapshot_count)
    return source_run_id


def record_source_failure(
    conn, capture_run_id: str, site: str, mode: str, error_code: str, error_summary: str, now: Optional[datetime] = None,
) -> str:
    """The F-16 half of this module: a fetch that raised is recorded as
    a terminal FAILED source_run, not silently skipped - a signal must
    never be computed as if this site's capture succeeded when it
    didn't (vb.freshness/vb.storage.load_latest_market_snapshots_v2
    only reads status='success' source_runs)."""
    now = now or datetime.now(timezone.utc)
    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=capture_run_id, site=site, mode=mode, started_at=now))
    finish_source_run(conn, source_run_id, RunStatus.FAILED, now, error_code=error_code, error_summary=error_summary)
    return source_run_id
