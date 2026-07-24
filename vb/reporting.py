"""Finished-match report: for every match that has a settlement recorded,
assemble what each opportunity leg looked like — every book's odds at
entry, entry/peak edge, time to convergence, and the actual result.

Nothing is captured here; this only reads and assembles what's already in
storage (raw_market_snapshot, opportunity, settlement) into one view per
the methodology's "Results calculation" / evaluation intent.

Caveat this module surfaces rather than hides: an opportunity only closes
when the tracker sees a NEW reading showing the edge has dropped,
the market is suspended, or the event started. Once a match kicks off,
Pinnacle removes it from the live odds feed entirely — no more readings
ever arrive — so the tracker never gets a chance to close it out. A
settled match can therefore still show `is_open=True` with no
convergence time; that's a real gap (see PENDING notes), not a bug in
this report.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .edge import raw_edge
from .matching import MatchTier, score_event_pair
from .models import MarketType, RawEvent, Selection
from .opportunity import Opportunity
from .settlement import SettlementResult
from .storage import load_opportunity

ALL_SITES = ["pinnacle.com", "swisslos.ch", "loro.ch"]


@dataclass(frozen=True)
class BookOdds:
    site: str
    odds: Optional[float]  # None if that book never captured this leg


@dataclass(frozen=True)
class LegReport:
    market_type: MarketType
    line: Optional[float]
    selection: Selection
    entry_edge_a: float
    peak_edge_a: float
    is_open: bool
    convergence: Optional[timedelta]
    book_odds: list[BookOdds]
    outcome: Optional[SettlementResult]


@dataclass(frozen=True)
class MatchReport:
    benchmark_site: str
    benchmark_event_id: str
    home_team: str
    away_team: str
    competition: str
    home_goals: Optional[int]
    away_goals: Optional[int]
    legs: list[LegReport]


def _odds_for_selection(outcomes_json: str, selection: Selection) -> Optional[float]:
    for o in json.loads(outcomes_json):
        if o["selection"] == selection.value:
            return o["odds"]
    return None


def _latest_odds_for_leg(
    conn: sqlite3.Connection, site: str, event_id: str, market_type: MarketType, line: Optional[float], selection: Selection
) -> Optional[float]:
    row = conn.execute(
        """
        SELECT outcomes_json FROM raw_market_snapshot
        WHERE site = ? AND event_id = ? AND market_type = ? AND (line IS ? OR line = ?)
        ORDER BY captured_at DESC LIMIT 1
        """,
        (site, event_id, market_type.value, line, line),
    ).fetchone()
    return _odds_for_selection(row[0], selection) if row else None


def outcomes_at_or_before(
    conn: sqlite3.Connection, site: str, event_id: Optional[str], market_type: MarketType, line: Optional[float],
    at: datetime,
) -> Optional[list[dict]]:
    """Every outcome (not just one selection) for one book's market, as
    of the latest capture at-or-before `at`. Used to show a third site's
    full odds alongside the benchmark/comparison pair a given opportunity
    already tracks per-snapshot (those two carry their own full outcome
    set via OpportunitySnapshot.full_market - only the site that ISN'T
    either of those needs this separate per-timestamp lookup). Returns
    None if that site never captured this leg, or hasn't yet by `at`.
    """
    if event_id is None:
        return None
    row = conn.execute(
        """
        SELECT outcomes_json FROM raw_market_snapshot
        WHERE site = ? AND event_id = ? AND market_type = ? AND (line IS ? OR line = ?) AND captured_at <= ?
        ORDER BY captured_at DESC LIMIT 1
        """,
        (site, event_id, market_type.value, line, line, at.isoformat()),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _find_matching_event_id(conn: sqlite3.Connection, site: str, benchmark_event: RawEvent) -> Optional[str]:
    """Best-effort match of the benchmark event against `site`'s captured
    events, reusing the same fuzzy scorer the live pipeline uses — this
    is a reporting-time lookup, not a new matching mechanism.
    """
    best_event_id, best_score = None, 0.0
    for event_id, home, away, kickoff, competition in conn.execute(
        "SELECT event_id, raw_home_team, raw_away_team, kickoff_utc, competition FROM raw_event WHERE site = ? AND sport = ?",
        (site, benchmark_event.sport),
    ):
        candidate = RawEvent(
            site=site, sport=benchmark_event.sport, competition=competition,
            kickoff_utc=datetime.fromisoformat(kickoff), raw_home_team=home, raw_away_team=away, event_id=event_id,
        )
        m = score_event_pair(benchmark_event, candidate)
        if m is not None and m.tier == MatchTier.AUTO and m.score > best_score:
            best_event_id, best_score = event_id, m.score
    return best_event_id


def build_finished_matches_report(conn: sqlite3.Connection) -> list[MatchReport]:
    """One MatchReport per benchmark event that has at least one settled
    leg, each with every opportunity leg ever tracked for it (even legs
    whose settlement is still missing show up, with outcome=None).
    """
    reports: list[MatchReport] = []

    settled_events = conn.execute(
        "SELECT DISTINCT benchmark_site, benchmark_event_id FROM settlement"
    ).fetchall()

    for benchmark_site, benchmark_event_id in settled_events:
        event_row = conn.execute(
            "SELECT raw_home_team, raw_away_team, competition, sport, kickoff_utc FROM raw_event WHERE site = ? AND event_id = ?",
            (benchmark_site, benchmark_event_id),
        ).fetchone()
        if event_row is None:
            continue
        home_team, away_team, competition, sport, kickoff_utc = event_row
        # The real kickoff time matters here, not just team names: the
        # matching scorer hard-rejects candidates outside a time window
        # (see vb.matching.score_event_pair), so an arbitrary/"now"
        # timestamp would silently fail to match anything.
        benchmark_event = RawEvent(
            site=benchmark_site, sport=sport, competition=competition,
            kickoff_utc=datetime.fromisoformat(kickoff_utc),
            raw_home_team=home_team, raw_away_team=away_team, event_id=benchmark_event_id,
        )

        settlement_rows = {
            (mt, ln, sel): (outcome, hg, ag)
            for mt, ln, sel, outcome, hg, ag in conn.execute(
                "SELECT market_type, line, selection, outcome, home_goals, away_goals FROM settlement "
                "WHERE benchmark_site = ? AND benchmark_event_id = ?",
                (benchmark_site, benchmark_event_id),
            )
        }
        home_goals = away_goals = None
        if settlement_rows:
            _, home_goals, away_goals = next(iter(settlement_rows.values()))

        legs: list[LegReport] = []
        instance_rows = conn.execute(
            "SELECT instance_id, market_type, line, selection, comparison_site FROM opportunity "
            "WHERE benchmark_site = ? AND market_key LIKE ?",
            (benchmark_site, f"{benchmark_site}:{benchmark_event_id}:%"),
        ).fetchall()

        other_event_ids: dict[str, Optional[str]] = {}

        for instance_id, market_type_str, line, selection_str, comparison_site in instance_rows:
            market_type = MarketType(market_type_str)
            selection = Selection(selection_str)
            opp = load_opportunity(conn, instance_id)

            book_odds = [BookOdds(site=benchmark_site, odds=opp.snapshots[0].benchmark_odds)]
            for site in ALL_SITES:
                if site == benchmark_site:
                    continue
                if site == comparison_site:
                    book_odds.append(BookOdds(site=site, odds=opp.snapshots[0].comparison_odds))
                    continue
                if site not in other_event_ids:
                    other_event_ids[site] = _find_matching_event_id(conn, site, benchmark_event)
                other_event_id = other_event_ids[site]
                odds = (
                    _latest_odds_for_leg(conn, site, other_event_id, market_type, line, selection)
                    if other_event_id else None
                )
                book_odds.append(BookOdds(site=site, odds=odds))

            outcome_entry = settlement_rows.get((market_type_str, line, selection_str))
            outcome = SettlementResult(outcome_entry[0]) if outcome_entry else None

            legs.append(
                LegReport(
                    market_type=market_type, line=line, selection=selection,
                    entry_edge_a=opp.entry_edge_a, peak_edge_a=opp.peak_edge_a,
                    is_open=opp.is_open, convergence=opp.convergence_time,
                    book_odds=book_odds, outcome=outcome,
                )
            )

        reports.append(
            MatchReport(
                benchmark_site=benchmark_site, benchmark_event_id=benchmark_event_id,
                home_team=home_team, away_team=away_team, competition=competition,
                home_goals=home_goals, away_goals=away_goals, legs=legs,
            )
        )

    return reports


@dataclass(frozen=True)
class PreEntryReading:
    """A reconstructed pre-threshold reading: what the edge was on a
    capture cycle before it ever crossed 3% and became a tracked
    Opportunity. Not stored anywhere as such — raw_market_snapshot keeps
    every cycle's odds regardless of threshold, so this is recomputed
    from that raw history rather than read off a stored edge.
    """

    captured_at: datetime
    benchmark_odds: float
    comparison_odds: float
    edge_a: float
    # Full outcome sets (not just the tracked selection) for the two
    # sites this pairing already touches - parsed from the same
    # outcomes_json already fetched for bm_odds/cmp_odds above, so this
    # costs nothing extra to include. The third (untracked) site isn't
    # covered here - callers needing it use outcomes_at_or_before()
    # separately, the same way build_finished_matches_report() does.
    benchmark_outcomes: list = field(default_factory=list)
    comparison_outcomes: list = field(default_factory=list)


def pre_entry_history(
    conn: sqlite3.Connection,
    benchmark_site: str,
    benchmark_event_id: str,
    comparison_site: str,
    comparison_event_id: str,
    market_type: MarketType,
    line: Optional[float],
    selection: Selection,
    before: datetime,
    limit: int = 5,
) -> list[PreEntryReading]:
    """Up to `limit` readings for this leg from capture cycles strictly
    before `before` (typically an opportunity's first_cross_at). Cycles
    are anchored on the benchmark site's capture timestamps (its cadence
    is the more regular of the two); each is paired with the comparison
    site's latest reading at-or-before that same moment — the same
    pairing the live pipeline does every cycle via
    load_latest_market_snapshots, just replayed historically. Returned
    oldest-first.
    """
    bm_rows = conn.execute(
        """
        SELECT captured_at, outcomes_json FROM raw_market_snapshot
        WHERE site = ? AND event_id = ? AND market_type = ? AND (line IS ? OR line = ?) AND captured_at < ?
        ORDER BY captured_at DESC LIMIT ?
        """,
        (benchmark_site, benchmark_event_id, market_type.value, line, line, before.isoformat(), limit),
    ).fetchall()

    readings: list[PreEntryReading] = []
    for captured_at_str, outcomes_json in reversed(bm_rows):
        bm_odds = _odds_for_selection(outcomes_json, selection)
        if bm_odds is None:
            continue
        cmp_row = conn.execute(
            """
            SELECT outcomes_json FROM raw_market_snapshot
            WHERE site = ? AND event_id = ? AND market_type = ? AND (line IS ? OR line = ?) AND captured_at <= ?
            ORDER BY captured_at DESC LIMIT 1
            """,
            (comparison_site, comparison_event_id, market_type.value, line, line, captured_at_str),
        ).fetchone()
        if cmp_row is None:
            continue
        cmp_odds = _odds_for_selection(cmp_row[0], selection)
        if cmp_odds is None:
            continue
        readings.append(
            PreEntryReading(
                captured_at=datetime.fromisoformat(captured_at_str),
                benchmark_odds=bm_odds,
                comparison_odds=cmp_odds,
                edge_a=raw_edge(bm_odds, cmp_odds),
                benchmark_outcomes=json.loads(outcomes_json),
                comparison_outcomes=json.loads(cmp_row[0]),
            )
        )
    return readings


def pre_entry_history_for_opportunity(
    conn: sqlite3.Connection, opportunity: Opportunity, limit: int = 5
) -> list[PreEntryReading]:
    """Convenience wrapper: derive the benchmark event and re-find the
    comparison event (not stored on Opportunity itself - only the
    benchmark event id is embedded in market_key) before delegating to
    pre_entry_history().
    """
    benchmark_event_id = opportunity.market_key.split(":")[1]
    row = conn.execute(
        "SELECT raw_home_team, raw_away_team, competition, sport, kickoff_utc FROM raw_event WHERE site = ? AND event_id = ?",
        (opportunity.benchmark_site, benchmark_event_id),
    ).fetchone()
    if row is None:
        return []
    home_team, away_team, competition, sport, kickoff_utc = row
    benchmark_event = RawEvent(
        site=opportunity.benchmark_site, sport=sport, competition=competition,
        kickoff_utc=datetime.fromisoformat(kickoff_utc), raw_home_team=home_team, raw_away_team=away_team,
        event_id=benchmark_event_id,
    )
    comparison_event_id = _find_matching_event_id(conn, opportunity.comparison_site, benchmark_event)
    if comparison_event_id is None:
        return []
    return pre_entry_history(
        conn, opportunity.benchmark_site, benchmark_event_id, opportunity.comparison_site, comparison_event_id,
        opportunity.market_type, opportunity.line, opportunity.selection, opportunity.first_cross_at, limit,
    )
