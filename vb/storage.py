"""SQLite persistence: the raw capture layer (RawEvent/MarketSnapshot) and
the opportunity lifecycle (schema.sql).

Deliberately thin: the dataclasses in vb.models / vb.opportunity are the
model, this module only translates them to/from rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .identity import canonical_json
from .matching import EventMatch
from .models import (
    BetDecision,
    BetDecisionChoice,
    BetExecution,
    CanonicalEvent,
    CaptureRun,
    EpisodeEndReason,
    EventMatchV2,
    EventVersionV2,
    ExecutionStatus,
    MarketSnapshot,
    MarketSnapshotV2,
    MarketType,
    MatchOrientation,
    MatchRole,
    MatchTierV2,
    Outcome,
    RawEvent,
    RejectReason,
    RunStatus,
    Selection,
    SignalEpisode,
    SignalObservation,
    SourceRun,
    StrategyDefinition,
)
from .opportunity import MovementSource, Opportunity, OpportunitySnapshot, ResolutionReason
from .settlement import SettlementResult, settle

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# Bumped when vb/schema.sql gains new tables/columns that need a
# recorded migration event (schema_migration). v1 = the original 5-table
# schema (undocumented as a version at the time); v2 = the 2026-07-25
# audit-remediation append-only tables added in Phase 1.
CURRENT_SCHEMA_VERSION = 2


def init_db(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT OR IGNORE INTO schema_migration (version, applied_at, description) VALUES (?, ?, ?)",
        (CURRENT_SCHEMA_VERSION, _to_iso(datetime.now(timezone.utc)),
         "Schema v2: append-only provenance tables (2026-07-25 audit remediation, Phase 1)"),
    )
    conn.commit()
    return conn


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def save_raw_capture(conn: sqlite3.Connection, event: RawEvent, snapshots: Iterable[MarketSnapshot]) -> None:
    """Record one event and the market snapshots a scraper just read for it.

    Safe to call repeatedly across scrape cycles for the same event: the
    event row is upserted (its details may be refined, e.g. a kickoff time
    correction) and each call just appends new snapshot rows — this is a
    time series, not a point-in-time replace like save_opportunity.
    """
    conn.execute(
        """
        INSERT INTO raw_event (site, event_id, sport, competition, kickoff_utc, raw_home_team, raw_away_team)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(site, event_id) DO UPDATE SET
            competition = excluded.competition,
            kickoff_utc = excluded.kickoff_utc,
            raw_home_team = excluded.raw_home_team,
            raw_away_team = excluded.raw_away_team
        """,
        (
            event.site,
            event.event_id,
            event.sport,
            event.competition,
            _to_iso(event.kickoff_utc),
            event.raw_home_team,
            event.raw_away_team,
        ),
    )
    conn.executemany(
        """
        INSERT INTO raw_market_snapshot (site, event_id, market_type, line, outcomes_json, max_bet_size, captured_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                s.event.site,
                s.event.event_id,
                s.market_type.value,
                s.line,
                json.dumps([{"selection": o.selection.value, "odds": o.odds} for o in s.outcomes]),
                s.max_bet_size,
                _to_iso(s.captured_at),
            )
            for s in snapshots
        ],
    )
    conn.commit()


def save_review_candidate(conn: sqlite3.Connection, match: EventMatch) -> None:
    """Upsert a REVIEW-tier event match into the human review queue. Safe
    to call every pipeline cycle the candidate still appears in — updates
    score/reasons/last_seen_at but never touches `status` once a human
    has already approved or rejected it.
    """
    now = _to_iso(datetime.now(timezone.utc))
    conn.execute(
        """
        INSERT INTO event_match_review (
            benchmark_site, benchmark_event_id, comparison_site, comparison_event_id,
            score, reasons_json, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(benchmark_site, benchmark_event_id, comparison_site, comparison_event_id)
        DO UPDATE SET
            score = excluded.score,
            reasons_json = excluded.reasons_json,
            last_seen_at = excluded.last_seen_at
        """,
        (
            match.anchor.site,
            match.anchor.event_id,
            match.candidate.site,
            match.candidate.event_id,
            match.score,
            json.dumps(list(match.reasons)),
            now,
            now,
        ),
    )
    conn.commit()


def list_pending_reviews(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, benchmark_site, benchmark_event_id, comparison_site, comparison_event_id,
               score, reasons_json, first_seen_at, last_seen_at
        FROM event_match_review WHERE status = 'pending'
        ORDER BY score DESC
        """
    ).fetchall()
    return [
        {
            "id": r[0],
            "benchmark_site": r[1],
            "benchmark_event_id": r[2],
            "comparison_site": r[3],
            "comparison_event_id": r[4],
            "score": r[5],
            "reasons": json.loads(r[6]),
            "first_seen_at": r[7],
            "last_seen_at": r[8],
        }
        for r in rows
    ]


def set_review_status(conn: sqlite3.Connection, review_id: int, status: str) -> None:
    if status not in ("approved", "rejected"):
        raise ValueError("status must be 'approved' or 'rejected'")
    conn.execute(
        "UPDATE event_match_review SET status = ?, reviewed_at = ? WHERE id = ?",
        (status, _to_iso(datetime.now(timezone.utc)), review_id),
    )
    conn.commit()


def load_approved_review_pairs(conn: sqlite3.Connection, benchmark_site: str, comparison_site: str) -> set[tuple[str, str]]:
    """(benchmark_event_id, comparison_event_id) pairs a human has
    approved for this site pair — the pipeline treats these as trusted
    even though the fuzzy matcher alone wouldn't auto-accept them.
    """
    rows = conn.execute(
        """
        SELECT benchmark_event_id, comparison_event_id FROM event_match_review
        WHERE benchmark_site = ? AND comparison_site = ? AND status = 'approved'
        """,
        (benchmark_site, comparison_site),
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def load_latest_market_snapshots(conn: sqlite3.Connection, site: str) -> list[MarketSnapshot]:
    """The most recent snapshot per (event, market_type, line) captured
    for `site`, reconstructed as MarketSnapshot objects with their
    RawEvent attached. This is what the matching/edge pipeline reads —
    capture and processing are separate steps, so this always reflects
    whatever the last scrape cycle wrote, however long ago that was.
    """
    events: dict[str, RawEvent] = {}
    for event_id, sport, competition, kickoff_utc, home, away in conn.execute(
        "SELECT event_id, sport, competition, kickoff_utc, raw_home_team, raw_away_team "
        "FROM raw_event WHERE site = ?",
        (site,),
    ):
        events[event_id] = RawEvent(
            site=site, sport=sport, competition=competition,
            kickoff_utc=_from_iso(kickoff_utc), raw_home_team=home, raw_away_team=away, event_id=event_id,
        )

    latest: dict[tuple, tuple] = {}
    for row in conn.execute(
        "SELECT event_id, market_type, line, outcomes_json, max_bet_size, captured_at "
        "FROM raw_market_snapshot WHERE site = ?",
        (site,),
    ):
        key = (row[0], row[1], row[2])
        if key not in latest or row[5] > latest[key][5]:
            latest[key] = row

    snapshots = []
    for event_id, market_type, line, outcomes_json, max_bet_size, captured_at in latest.values():
        event = events.get(event_id)
        if event is None:
            continue
        outcomes = tuple(Outcome(Selection(o["selection"]), o["odds"]) for o in json.loads(outcomes_json))
        snapshots.append(
            MarketSnapshot(
                event=event, market_type=MarketType(market_type), line=line,
                outcomes=outcomes, captured_at=_from_iso(captured_at), max_bet_size=max_bet_size,
            )
        )
    return snapshots


def load_open_opportunity(conn: sqlite3.Connection, market_key: str) -> Optional[Opportunity]:
    """The currently-open (unresolved) opportunity instance for a
    market_key, if any — used to resume an OpportunityTracker's state
    across separate pipeline runs (see OpportunityTracker.resume).
    """
    row = conn.execute(
        "SELECT instance_id FROM opportunity WHERE market_key = ? AND resolved_at IS NULL",
        (market_key,),
    ).fetchone()
    if row is None:
        return None
    return load_opportunity(conn, row[0])


def save_opportunity(conn: sqlite3.Connection, opportunity: Opportunity) -> None:
    """Insert (or replace) one opportunity header and all of its snapshots.

    Safe to call once an opportunity has closed, or repeatedly while it's
    still accumulating snapshots — existing snapshot rows for this
    instance are cleared and rewritten each call rather than duplicated.
    """
    conn.execute(
        """
        INSERT INTO opportunity (
            instance_id, market_key, sport, benchmark_site, comparison_site,
            market_type, line, selection, first_cross_at, resolved_at, resolution_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(instance_id) DO UPDATE SET
            resolved_at = excluded.resolved_at,
            resolution_reason = excluded.resolution_reason
        """,
        (
            opportunity.instance_id,
            opportunity.market_key,
            opportunity.sport,
            opportunity.benchmark_site,
            opportunity.comparison_site,
            opportunity.market_type.value,
            opportunity.line,
            opportunity.selection.value,
            _to_iso(opportunity.first_cross_at),
            _to_iso(opportunity.resolved_at) if opportunity.resolved_at else None,
            opportunity.resolution_reason.value if opportunity.resolution_reason else None,
        ),
    )

    conn.execute(
        "DELETE FROM opportunity_snapshot WHERE opportunity_instance_id = ?",
        (opportunity.instance_id,),
    )
    conn.executemany(
        """
        INSERT INTO opportunity_snapshot (
            opportunity_instance_id, captured_at, edge_a, edge_b,
            benchmark_odds, comparison_odds, movement_source, max_bet_size, full_market_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                s.opportunity_instance_id,
                _to_iso(s.captured_at),
                s.edge_a,
                s.edge_b,
                s.benchmark_odds,
                s.comparison_odds,
                s.movement_source.value,
                s.max_bet_size,
                json.dumps(s.full_market),
            )
            for s in opportunity.snapshots
        ],
    )
    conn.commit()


def load_opportunity(conn: sqlite3.Connection, instance_id: str) -> Optional[Opportunity]:
    row = conn.execute(
        """
        SELECT instance_id, market_key, sport, benchmark_site, comparison_site,
               market_type, line, selection, first_cross_at, resolved_at, resolution_reason
        FROM opportunity WHERE instance_id = ?
        """,
        (instance_id,),
    ).fetchone()
    if row is None:
        return None

    opportunity = Opportunity(
        instance_id=row[0],
        market_key=row[1],
        sport=row[2],
        benchmark_site=row[3],
        comparison_site=row[4],
        market_type=MarketType(row[5]),
        line=row[6],
        selection=Selection(row[7]),
        first_cross_at=_from_iso(row[8]),
        resolved_at=_from_iso(row[9]) if row[9] else None,
        resolution_reason=ResolutionReason(row[10]) if row[10] else None,
    )

    for srow in conn.execute(
        """
        SELECT opportunity_instance_id, captured_at, edge_a, edge_b, benchmark_odds,
               comparison_odds, movement_source, max_bet_size, full_market_json
        FROM opportunity_snapshot
        WHERE opportunity_instance_id = ?
        ORDER BY captured_at ASC
        """,
        (instance_id,),
    ).fetchall():
        opportunity.snapshots.append(
            OpportunitySnapshot(
                opportunity_instance_id=srow[0],
                captured_at=_from_iso(srow[1]),
                edge_a=srow[2],
                edge_b=srow[3],
                benchmark_odds=srow[4],
                comparison_odds=srow[5],
                movement_source=MovementSource(srow[6]),
                max_bet_size=srow[7],
                full_market=json.loads(srow[8]),
            )
        )

    return opportunity


def save_settlement(
    conn: sqlite3.Connection,
    benchmark_site: str,
    benchmark_event_id: str,
    market_type: MarketType,
    line: Optional[float],
    selection: Selection,
    outcome: SettlementResult,
    home_goals: Optional[int] = None,
    away_goals: Optional[int] = None,
    source: Optional[str] = None,
) -> None:
    # Manual update-then-insert rather than INSERT ... ON CONFLICT: SQLite
    # (like standard SQL) treats NULL as never equal to NULL even under a
    # UNIQUE index, so two rows sharing the same NULL `line` (every
    # match_winner leg) would never be seen as conflicting and ON CONFLICT
    # would silently insert a duplicate instead of updating.
    now = _to_iso(datetime.now(timezone.utc))
    cursor = conn.execute(
        """
        UPDATE settlement SET outcome = ?, home_goals = ?, away_goals = ?, settled_at = ?, source = ?
        WHERE benchmark_site = ? AND benchmark_event_id = ? AND market_type = ?
          AND (line IS ? OR line = ?) AND selection = ?
        """,
        (
            outcome.value, home_goals, away_goals, now, source,
            benchmark_site, benchmark_event_id, market_type.value, line, line, selection.value,
        ),
    )
    if cursor.rowcount == 0:
        conn.execute(
            """
            INSERT INTO settlement (
                benchmark_site, benchmark_event_id, market_type, line, selection,
                outcome, home_goals, away_goals, settled_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                benchmark_site, benchmark_event_id, market_type.value, line, selection.value,
                outcome.value, home_goals, away_goals, now, source,
            ),
        )
    conn.commit()


def load_settlement(
    conn: sqlite3.Connection,
    benchmark_site: str,
    benchmark_event_id: str,
    market_type: MarketType,
    line: Optional[float],
    selection: Selection,
) -> Optional[SettlementResult]:
    row = conn.execute(
        """
        SELECT outcome FROM settlement
        WHERE benchmark_site = ? AND benchmark_event_id = ? AND market_type = ?
          AND (line IS ? OR line = ?) AND selection = ?
        """,
        (benchmark_site, benchmark_event_id, market_type.value, line, line, selection.value),
    ).fetchone()
    return SettlementResult(row[0]) if row else None


def list_unsettled_matches(conn: sqlite3.Connection, benchmark_site: str, kickoff_buffer_hours: float = 3.0) -> list[RawEvent]:
    """Every benchmark event that has at least one tracked opportunity,
    is old enough that the match should be over (kickoff + buffer in the
    past - soccer is ~2h with stoppage, the extra margin covers delayed
    kickoffs), and has no settlement row yet. This is the worklist for
    both automated (vb.sources.results) and manual (record_result.py)
    settlement.

    The opportunity table has no benchmark_event_id column of its own -
    only market_key (pipeline.market_key(): "{site}:{event_id}:{market_type}:
    {line}:{selection}:vs:{comparison_site}") - so event ids are pulled out
    of it in Python rather than via a fragile SQL LIKE/substring match.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=kickoff_buffer_hours)

    event_ids: set[str] = set()
    for (market_key,) in conn.execute(
        "SELECT DISTINCT market_key FROM opportunity WHERE benchmark_site = ?", (benchmark_site,)
    ):
        event_ids.add(market_key.split(":")[1])

    settled_ids = {
        event_id
        for (event_id,) in conn.execute(
            "SELECT DISTINCT benchmark_event_id FROM settlement WHERE benchmark_site = ?", (benchmark_site,)
        )
    }

    results: list[RawEvent] = []
    for event_id in event_ids - settled_ids:
        row = conn.execute(
            "SELECT sport, competition, kickoff_utc, raw_home_team, raw_away_team "
            "FROM raw_event WHERE site = ? AND event_id = ?",
            (benchmark_site, event_id),
        ).fetchone()
        if row is None:
            continue  # shouldn't happen (raw_event rows are never deleted) - defensive only
        sport, competition, kickoff_utc, home, away = row
        kickoff = datetime.fromisoformat(kickoff_utc)
        if kickoff >= cutoff:
            continue  # match hasn't finished yet
        results.append(RawEvent(
            site=benchmark_site, sport=sport, competition=competition,
            kickoff_utc=kickoff, raw_home_team=home, raw_away_team=away, event_id=event_id,
        ))
    return results


def force_resolve_stale_opportunities(conn: sqlite3.Connection, kickoff_buffer_hours: float = 4.0) -> int:
    """Force-close any still-open opportunity whose benchmark event kicked
    off more than `kickoff_buffer_hours` ago.

    Normally an opportunity closes itself the moment run_cycle() ingests
    a reading with event_started=True (vb.pipeline, computed from
    wall-clock time vs. kickoff, independent of whether the source sites
    sent fresh odds - see LegReading/OpportunityTracker.ingest). That
    requires find_leg_edges() to still be producing a LegEdge for the
    market_key on some later cycle, which depends on both sides' events
    still being found by match_events()/match_markets() - if that ever
    stops happening for any reason (found live: several opportunities
    stuck open days past kickoff with no clear single cause), the normal
    close path never fires and `is_open` stays True forever, with no
    settlement possible (list_unsettled_matches doesn't care about
    is_open, but downstream reporting/dashboard treatment of "open" vs.
    "closed, unsettled" differs).

    This is a periodic safety net, not a replacement for the normal
    close path - only touches opportunities well past their own kickoff,
    never anything that might still be a legitimately live tracking
    window. resolved_at is set to the kickoff time itself (not "now"),
    since that's the actual moment pre-match value analysis stopped
    being meaningful for this leg - the same convention run_cycle() uses
    for a normal EVENT_STARTED close, so a force-resolved opportunity is
    indistinguishable in the data from one that closed the normal way.
    Only touches the opportunity header row, never opportunity_snapshot -
    the trajectory already captured is left exactly as it was.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=kickoff_buffer_hours)

    rows = conn.execute(
        "SELECT instance_id, market_key, benchmark_site FROM opportunity WHERE resolved_at IS NULL"
    ).fetchall()

    resolved_count = 0
    for instance_id, market_key, benchmark_site in rows:
        event_id = market_key.split(":")[1]
        row = conn.execute(
            "SELECT kickoff_utc FROM raw_event WHERE site = ? AND event_id = ?",
            (benchmark_site, event_id),
        ).fetchone()
        if row is None:
            continue  # shouldn't happen (raw_event rows are never deleted) - defensive only
        kickoff = datetime.fromisoformat(row[0])
        if kickoff >= cutoff:
            continue  # not stale yet - still within a plausible legitimate tracking window

        conn.execute(
            "UPDATE opportunity SET resolved_at = ?, resolution_reason = ? WHERE instance_id = ?",
            (_to_iso(kickoff), ResolutionReason.EVENT_STARTED.value, instance_id),
        )
        resolved_count += 1

    conn.commit()
    return resolved_count


def record_match_result(
    conn: sqlite3.Connection,
    benchmark_site: str,
    benchmark_event_id: str,
    home_goals: int,
    away_goals: int,
    source: Optional[str] = None,
) -> int:
    """Settle every leg that has ever been tracked as an opportunity for
    this benchmark event, given the match's final score. A human (or,
    eventually, a results feed) only needs to supply the score once per
    match — every distinct (market_type, line, selection) already seen in
    the `opportunity` table for this event gets settled from it. Returns
    the number of legs settled.
    """
    legs = conn.execute(
        "SELECT DISTINCT market_type, line, selection FROM opportunity "
        "WHERE benchmark_site = ? AND market_key LIKE ?",
        (benchmark_site, f"{benchmark_site}:{benchmark_event_id}:%"),
    ).fetchall()

    for market_type_str, line, selection_str in legs:
        market_type = MarketType(market_type_str)
        selection = Selection(selection_str)
        outcome = settle(market_type, line, selection, home_goals, away_goals)
        save_settlement(
            conn, benchmark_site, benchmark_event_id, market_type, line, selection,
            outcome, home_goals, away_goals, source,
        )
    return len(legs)


def prune_raw_snapshots(conn: sqlite3.Connection, keep_hours: int = 24) -> int:
    """Delete raw_market_snapshot rows older than `keep_hours`, except
    each (site, event_id, market_type, line)'s single latest row, which
    is always kept regardless of age — the live pipeline
    (load_latest_market_snapshots) only ever reads the latest row per
    key, never the history, so nothing operational depends on the rest.

    This exists because raw snapshots are captured every cycle
    indefinitely and dominate the database's size (580k+ rows / 150+ MB
    after < 24h of 5-minute-cadence capture) — unbounded growth isn't
    survivable for long-running unattended capture on constrained storage
    (e.g. GitHub Actions' cache limits). pre_entry_history_for_opportunity
    only looks back a handful of samples before an opportunity's entry,
    so a same-day retention window comfortably covers it; opportunity/
    opportunity_snapshot/settlement rows (the actually meaningful data)
    are never touched by this function.

    F-10 fix (2026-07-25 external audit): this used to keep MAX(id) as a
    proxy for "latest", on the assumption that a higher id always means
    a later captured_at. That assumption breaks under merge_databases.py
    (a historical row inserted later can get a higher id than a
    genuinely more recent row already present) - the audit found 3,292
    market keys in the live release DB where MAX(id)'s captured_at was
    actually OLDER than the true MAX(captured_at) for that key, meaning
    pruning was keeping a stale row and deleting the real latest one.
    Now ranks explicitly by captured_at (id only as a tiebreaker for two
    rows sharing a timestamp) via ROW_NUMBER(), so "latest" always means
    latest by the field that actually defines recency.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=keep_hours)).isoformat()
    cursor = conn.execute(
        """
        DELETE FROM raw_market_snapshot
        WHERE captured_at < ?
          AND id IN (
              SELECT id FROM (
                  SELECT id, ROW_NUMBER() OVER (
                      PARTITION BY site, event_id, market_type, line
                      ORDER BY captured_at DESC, id DESC
                  ) AS rn
                  FROM raw_market_snapshot
              )
              WHERE rn > 1
          )
        """,
        (cutoff,),
    )
    conn.commit()
    return cursor.rowcount


# ============================================================
# SCHEMA V2 persistence (2026-07-25 external-audit remediation, Phase 1)
# ============================================================
# Every function below is INSERT-only (plus a small number of narrow,
# explicit UPDATEs of specific late-bound columns, documented per
# function) - never a DELETE, never a blanket rewrite of a row's
# children. This is the actual fix for F-02/F-09/F-11: v1's
# save_opportunity() deleted and rewrote ALL of an opportunity's
# snapshots on every call, which is exactly the mechanism that let a
# process-restart identity collision silently destroy an earlier
# opportunity's real trajectory. Nothing here can do that, because
# nothing here ever deletes.


def save_capture_run(conn: sqlite3.Connection, run: CaptureRun) -> None:
    conn.execute(
        """
        INSERT INTO capture_run (id, scheduled_for, started_at, finished_at, git_sha, schema_version, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run.id, run.scheduled_for, _to_iso(run.started_at),
         _to_iso(run.finished_at) if run.finished_at else None,
         run.git_sha, run.schema_version, run.status.value),
    )
    conn.commit()


def finish_capture_run(conn: sqlite3.Connection, run_id: str, status: RunStatus, finished_at: datetime) -> None:
    """The one narrow, explicit state transition capture_run allows -
    setting finished_at/status once, when the run genuinely ends.
    Never touches any other column."""
    conn.execute(
        "UPDATE capture_run SET status = ?, finished_at = ? WHERE id = ?",
        (status.value, _to_iso(finished_at), run_id),
    )
    conn.commit()


def save_source_run(conn: sqlite3.Connection, run: SourceRun) -> None:
    conn.execute(
        """
        INSERT INTO source_run (
            id, capture_run_id, site, mode, started_at, finished_at, status,
            event_count, snapshot_count, http_error_count, error_code, error_summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run.id, run.capture_run_id, run.site, run.mode, _to_iso(run.started_at),
         _to_iso(run.finished_at) if run.finished_at else None, run.status.value,
         run.event_count, run.snapshot_count, run.http_error_count, run.error_code, run.error_summary),
    )
    conn.commit()


def finish_source_run(
    conn: sqlite3.Connection, run_id: str, status: RunStatus, finished_at: datetime,
    event_count: int = 0, snapshot_count: int = 0, error_code: Optional[str] = None, error_summary: Optional[str] = None,
) -> None:
    """The one narrow state transition source_run allows. F-16's fix
    depends on this always being called, including on failure - a
    source_run with no terminal status is what makes a partial-failure
    cycle detectable instead of silently looking like it never ran."""
    conn.execute(
        """
        UPDATE source_run SET status = ?, finished_at = ?, event_count = ?, snapshot_count = ?,
            error_code = ?, error_summary = ? WHERE id = ?
        """,
        (status.value, _to_iso(finished_at), event_count, snapshot_count, error_code, error_summary, run_id),
    )
    conn.commit()


def save_event_version(conn: sqlite3.Connection, ev: EventVersionV2) -> str:
    conn.execute(
        """
        INSERT INTO event_version (id, site, event_id, valid_from, source_run_id, sport, competition, kickoff_utc, home_team, away_team)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ev.id, ev.site, ev.event_id, _to_iso(ev.valid_from), ev.source_run_id,
         ev.sport, ev.competition, _to_iso(ev.kickoff_utc), ev.home_team, ev.away_team),
    )
    conn.commit()
    return ev.id


def save_canonical_event(conn: sqlite3.Connection, ce: CanonicalEvent) -> str:
    conn.execute(
        "INSERT INTO canonical_event (id, sport, created_at) VALUES (?, ?, ?)",
        (ce.id, ce.sport, _to_iso(ce.created_at)),
    )
    conn.commit()
    return ce.id


def save_event_match_v2(conn: sqlite3.Connection, m: EventMatchV2) -> str:
    conn.execute(
        """
        INSERT INTO event_match (
            id, canonical_event_id, event_version_id, role, orientation, score,
            score_components_json, model_version, tier, review_status, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (m.id, m.canonical_event_id, m.event_version_id, m.role.value, m.orientation.value, m.score,
         canonical_json(m.score_components), m.model_version, m.tier.value, m.review_status, _to_iso(m.decided_at)),
    )
    conn.commit()
    return m.id


def save_market_snapshot_v2(conn: sqlite3.Connection, s: MarketSnapshotV2) -> str:
    conn.execute(
        """
        INSERT INTO market_snapshot_v2 (
            id, source_run_id, event_version_id, market_type, line, outcomes_json,
            max_bet_size, observed_at, received_at, request_started_at, request_finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (s.id, s.source_run_id, s.event_version_id, s.market_type.value, s.line,
         json.dumps([{"selection": o.selection.value, "odds": o.odds} for o in s.outcomes]),
         s.max_bet_size, _to_iso(s.observed_at) if s.observed_at else None, _to_iso(s.received_at),
         _to_iso(s.request_started_at) if s.request_started_at else None,
         _to_iso(s.request_finished_at) if s.request_finished_at else None),
    )
    conn.commit()
    return s.id


def get_or_create_event_version(conn: sqlite3.Connection, ev: EventVersionV2) -> str:
    """Content-addressed like get_or_create_strategy_definition: if the
    most recent event_version for (site, event_id) already has these
    exact (sport, competition, kickoff_utc, home_team, away_team)
    values, reuse its id instead of inserting an identical row every
    single capture cycle. Only a genuine correction (e.g. a kickoff-time
    change) creates a new version - that's what F-03 requires: a later
    correction must never silently rewrite what an earlier decision saw,
    which is exactly why this is get-or-create rather than update.
    """
    row = conn.execute(
        """
        SELECT id, sport, competition, kickoff_utc, home_team, away_team FROM event_version
        WHERE site = ? AND event_id = ? ORDER BY valid_from DESC LIMIT 1
        """,
        (ev.site, ev.event_id),
    ).fetchone()
    if row is not None:
        _id, sport, competition, kickoff_utc, home_team, away_team = row
        if (sport, competition, kickoff_utc, home_team, away_team) == (
            ev.sport, ev.competition, _to_iso(ev.kickoff_utc), ev.home_team, ev.away_team,
        ):
            return _id
    return save_event_version(conn, ev)


def load_latest_market_snapshots_v2(conn: sqlite3.Connection, site: str) -> list[MarketSnapshotV2]:
    """The most recent MarketSnapshotV2 per (event_id, market_type, line)
    for `site`, restricted to source_runs that finished SUCCESS - never
    read a signal off a partial/failed run (F-16). Reads through
    event_version's `event_id`/`site` rather than joining on
    event_version.id directly, matching v1's load_latest_market_snapshots
    identity (an event is identified by site+event_id even across
    versions).
    """
    rows = conn.execute(
        """
        SELECT m.id, m.source_run_id, m.event_version_id, m.market_type, m.line, m.outcomes_json,
               m.max_bet_size, m.observed_at, m.received_at, m.request_started_at, m.request_finished_at,
               ev.event_id
        FROM market_snapshot_v2 m
        JOIN event_version ev ON ev.id = m.event_version_id
        JOIN source_run sr ON sr.id = m.source_run_id
        WHERE ev.site = ? AND sr.status = 'success'
        """,
        (site,),
    ).fetchall()

    latest: dict[tuple, tuple] = {}
    for row in rows:
        key = (row[11], row[3], row[4])  # event_id, market_type, line
        if key not in latest or row[8] > latest[key][8]:  # received_at
            latest[key] = row

    snapshots = []
    for row in latest.values():
        (snap_id, source_run_id, event_version_id, market_type, line, outcomes_json,
         max_bet_size, observed_at, received_at, request_started_at, request_finished_at, _event_id) = row
        outcomes = tuple(Outcome(Selection(o["selection"]), o["odds"]) for o in json.loads(outcomes_json))
        snapshots.append(
            MarketSnapshotV2(
                id=snap_id, source_run_id=source_run_id, event_version_id=event_version_id,
                market_type=MarketType(market_type), line=line, outcomes=outcomes,
                max_bet_size=max_bet_size, observed_at=_from_iso(observed_at) if observed_at else None,
                received_at=_from_iso(received_at),
                request_started_at=_from_iso(request_started_at) if request_started_at else None,
                request_finished_at=_from_iso(request_finished_at) if request_finished_at else None,
            )
        )
    return snapshots


def load_event_version(conn: sqlite3.Connection, event_version_id: str) -> EventVersionV2:
    row = conn.execute(
        "SELECT id, site, event_id, valid_from, source_run_id, sport, competition, kickoff_utc, home_team, away_team "
        "FROM event_version WHERE id = ?",
        (event_version_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"event_version {event_version_id} not found")
    return EventVersionV2(
        id=row[0], site=row[1], event_id=row[2], valid_from=_from_iso(row[3]), source_run_id=row[4],
        sport=row[5], competition=row[6], kickoff_utc=_from_iso(row[7]), home_team=row[8], away_team=row[9],
    )


def get_or_create_strategy_definition(conn: sqlite3.Connection, strategy: StrategyDefinition) -> str:
    """StrategyDefinition is content-addressed - `strategy.id` should
    already be a deterministic id/config_hash the caller computed, but
    this checks by config_hash regardless so an identical config always
    resolves to the SAME row rather than creating a duplicate. Never
    updates an existing row - a strategy definition is immutable by
    construction (F-06)."""
    existing = conn.execute(
        "SELECT id FROM strategy_definition WHERE config_hash = ?", (strategy.config_hash,)
    ).fetchone()
    if existing is not None:
        return existing[0]
    conn.execute(
        """
        INSERT INTO strategy_definition (id, signal_model, threshold, max_age_s, max_skew_s, min_lead_time_s, config_json, config_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (strategy.id, strategy.signal_model, strategy.threshold, strategy.max_age_s, strategy.max_skew_s,
         strategy.min_lead_time_s, canonical_json(strategy.config), strategy.config_hash, _to_iso(strategy.created_at)),
    )
    conn.commit()
    return strategy.id


def find_open_signal_episode(conn: sqlite3.Connection, strategy_version: str, market_identity_id: str) -> Optional[SignalEpisode]:
    """The schema-v2 replacement for v1's load_open_opportunity - but
    unlike v1, finding NOTHING open here is never ambiguous with "a
    fresh process forgot": episode identity is a UUID minted once at
    creation (vb.identity.new_id()), never a process-local counter, so
    there is nothing for a fresh process to "forget" in the first
    place. This function exists for convenience (avoid reopening an
    already-open episode), not as the mechanism that prevents F-02 -
    the UUID identity itself is what prevents it.
    """
    row = conn.execute(
        "SELECT id, strategy_version, market_identity_id, started_at, ended_at, end_reason "
        "FROM signal_episode WHERE strategy_version = ? AND market_identity_id = ? AND ended_at IS NULL",
        (strategy_version, market_identity_id),
    ).fetchone()
    if row is None:
        return None
    return SignalEpisode(
        id=row[0], strategy_version=row[1], market_identity_id=row[2],
        started_at=_from_iso(row[3]), ended_at=_from_iso(row[4]) if row[4] else None,
        end_reason=EpisodeEndReason(row[5]) if row[5] else None,
    )


def open_signal_episode(conn: sqlite3.Connection, episode: SignalEpisode) -> str:
    """Insert a brand-new episode. `episode.id` must already be a fresh
    vb.identity.new_id() - this function does not mint one, to keep
    "when is an id created" visible at the call site rather than
    hidden inside storage."""
    conn.execute(
        """
        INSERT INTO signal_episode (id, strategy_version, market_identity_id, started_at, ended_at, end_reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (episode.id, episode.strategy_version, episode.market_identity_id, _to_iso(episode.started_at), None, None),
    )
    conn.commit()
    return episode.id


def close_signal_episode(conn: sqlite3.Connection, episode_id: str, ended_at: datetime, end_reason: EpisodeEndReason) -> None:
    """The one narrow state transition signal_episode allows - setting
    ended_at/end_reason exactly once. Guarded so a second close attempt
    (e.g. a re-run racing with itself) can never silently overwrite the
    first close's real timestamp/reason."""
    cursor = conn.execute(
        "UPDATE signal_episode SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL",
        (_to_iso(ended_at), end_reason.value, episode_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise ValueError(f"signal_episode {episode_id} is already closed or does not exist - refusing to overwrite")


def save_signal_observation(conn: sqlite3.Connection, obs: SignalObservation) -> str:
    """Always insert-only, including for REJECTED observations
    (eligible=False) - F-01's freshness/skew gate produces an auditable
    row either way, never a silent no-op."""
    conn.execute(
        """
        INSERT INTO signal_observation (
            id, episode_id, decision_time, benchmark_snapshot_id, comparison_snapshot_id,
            edge_model, edge, eligible, reject_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (obs.id, obs.episode_id, _to_iso(obs.decision_time), obs.benchmark_snapshot_id, obs.comparison_snapshot_id,
         obs.edge_model, obs.edge, 1 if obs.eligible else 0, obs.reject_reason.value if obs.reject_reason else None),
    )
    conn.commit()
    return obs.id


def save_bet_decision(conn: sqlite3.Connection, decision: BetDecision) -> str:
    """UNIQUE(idempotency_key) at the schema level means a second
    attempt to record the same logical decision raises IntegrityError
    rather than silently duplicating it - callers should catch that and
    treat it as "already decided", not retry-insert."""
    conn.execute(
        """
        INSERT INTO bet_decision (id, strategy_version, signal_observation_id, decided_at, decision, reason, intended_odds, intended_stake, idempotency_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (decision.id, decision.strategy_version, decision.signal_observation_id, _to_iso(decision.decided_at),
         decision.decision.value, decision.reason, decision.intended_odds, decision.intended_stake, decision.idempotency_key),
    )
    conn.commit()
    return decision.id


def save_bet_execution(conn: sqlite3.Connection, execution: BetExecution) -> str:
    conn.execute(
        """
        INSERT INTO bet_execution (
            id, decision_id, requested_at, responded_at, status, requested_odds,
            accepted_odds, requested_stake, accepted_stake, external_bet_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (execution.id, execution.decision_id, _to_iso(execution.requested_at),
         _to_iso(execution.responded_at) if execution.responded_at else None, execution.status.value,
         execution.requested_odds, execution.accepted_odds, execution.requested_stake,
         execution.accepted_stake, execution.external_bet_id),
    )
    conn.commit()
    return execution.id
