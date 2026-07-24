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

from .matching import EventMatch
from .models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection
from .opportunity import MovementSource, Opportunity, OpportunitySnapshot, ResolutionReason
from .settlement import SettlementResult, settle

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
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

    Uses MAX(id) as a proxy for "latest" rather than MAX(captured_at)
    directly, for single-column subquery compatibility across SQLite
    versions — safe given captures are always inserted in real-time
    order, so id order matches capture order. Returns rows deleted.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=keep_hours)).isoformat()
    cursor = conn.execute(
        """
        DELETE FROM raw_market_snapshot
        WHERE captured_at < ?
          AND id NOT IN (
              SELECT MAX(id) FROM raw_market_snapshot
              GROUP BY site, event_id, market_type, line
          )
        """,
        (cutoff,),
    )
    conn.commit()
    return cursor.rowcount
