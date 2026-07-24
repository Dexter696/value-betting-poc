"""Wires the matching engine (vb.matching) and edge calculation (vb.edge)
together: match a benchmark site's events/markets against a comparison
site's, compute both Method A and B edges for every leg that has a
counterpart on both sides, and feed the results into the opportunity
tracker (vb.opportunity), resuming tracker state across runs via
vb.storage so repeated runs build real lifecycles instead of restarting
from scratch each time.

Deliberately reads already-captured data
(vb.storage.load_latest_market_snapshots) rather than scraping itself —
capture and processing are separate concerns, matching the methodology's
"entry policy is a post-processing choice" philosophy. Call run_cycle()
once per capture cycle (e.g. from a scheduled task after the scrapers run)
to get real opportunity lifecycles over time; a single run only ever
produces the first reading of anything it finds.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from sqlite3 import Connection

from .edge import devigged_edge, overround, raw_edge
from .matching import MatchTier, match_events, match_markets
from .models import MarketSnapshot, MarketType, RawEvent, Selection
from .opportunity import LegReading, Opportunity, OpportunityTracker
from .storage import (
    load_approved_review_pairs,
    load_latest_market_snapshots,
    load_open_opportunity,
    save_opportunity,
    save_review_candidate,
)


@dataclass(frozen=True)
class LegEdge:
    market_key: str
    selection: Selection
    benchmark_market: MarketSnapshot
    comparison_market: MarketSnapshot
    edge_a: float
    edge_b: float

    @property
    def benchmark_event(self) -> RawEvent:
        return self.benchmark_market.event

    @property
    def comparison_event(self) -> RawEvent:
        return self.comparison_market.event

    @property
    def market_type(self) -> MarketType:
        return self.comparison_market.market_type

    @property
    def line(self) -> Optional[float]:
        return self.comparison_market.line

    @property
    def benchmark_odds(self) -> float:
        return next(o.odds for o in self.benchmark_market.outcomes if o.selection == self.selection)

    @property
    def comparison_odds(self) -> float:
        return next(o.odds for o in self.comparison_market.outcomes if o.selection == self.selection)

    @property
    def max_bet_size(self) -> Optional[float]:
        return self.comparison_market.max_bet_size

    @property
    def captured_at(self) -> datetime:
        return self.comparison_market.captured_at


def market_key(
    benchmark_event: RawEvent, market_type: MarketType, line: Optional[float], selection: Selection, comparison_site: str
) -> str:
    """Stable id for one match+market+leg being compared against one
    comparison site — used to link re-occurring opportunities together
    across separate pipeline runs. Anchored on the BENCHMARK event id
    since the benchmark is the fixed reference point (methodology:
    "benchmark odds are the correct reference").
    """
    return f"{benchmark_event.site}:{benchmark_event.event_id}:{market_type.value}:{line}:{selection.value}:vs:{comparison_site}"


def full_market_json(leg: LegEdge) -> dict:
    """Both books' full market (every outcome + margin), per the
    methodology's "full market snapshot" requirement. Only the two sides
    of this particular match-up (not all 4 books at once — a 4-way join
    across every comparison site is a further increment, see project
    notes) but this is what makes a de-vig audit possible after the fact.
    """
    return {
        "benchmark": {
            "site": leg.benchmark_market.event.site,
            "outcomes": [{"selection": o.selection.value, "odds": o.odds} for o in leg.benchmark_market.outcomes],
            "overround": overround([o.odds for o in leg.benchmark_market.outcomes]),
        },
        "comparison": {
            "site": leg.comparison_market.event.site,
            "outcomes": [{"selection": o.selection.value, "odds": o.odds} for o in leg.comparison_market.outcomes],
            "overround": overround([o.odds for o in leg.comparison_market.outcomes]),
        },
    }


def _group_by_event(snapshots: list[MarketSnapshot]) -> dict[str, list[MarketSnapshot]]:
    grouped: dict[str, list[MarketSnapshot]] = defaultdict(list)
    for s in snapshots:
        grouped[s.event.event_id].append(s)
    return grouped


def classify_event_matches(
    benchmark_snapshots: list[MarketSnapshot], comparison_snapshots: list[MarketSnapshot]
):
    """Match benchmark vs comparison events and split the results into
    (trusted, needs_review): AUTO-tier matches vs. REVIEW-tier ones that
    a human hasn't weighed in on yet. REJECT-tier "matches" never make it
    out of vb.matching.match_events in the first place.
    """
    benchmark_by_event = _group_by_event(benchmark_snapshots)
    comparison_by_event = _group_by_event(comparison_snapshots)
    benchmark_events = [snaps[0].event for snaps in benchmark_by_event.values()]
    comparison_events = [snaps[0].event for snaps in comparison_by_event.values()]

    matches = match_events(benchmark_events, comparison_events)
    trusted = [m for m in matches if m.tier == MatchTier.AUTO]
    needs_review = [m for m in matches if m.tier == MatchTier.REVIEW]
    return trusted, needs_review


def find_leg_edges(
    benchmark_snapshots: list[MarketSnapshot],
    comparison_snapshots: list[MarketSnapshot],
    approved_pairs: frozenset = frozenset(),
) -> list[LegEdge]:
    """Match benchmark vs comparison events/markets and compute both edge
    methods for every leg with a counterpart on both sites. Returns one
    LegEdge per such leg regardless of whether it clears any threshold —
    filtering by threshold is the opportunity tracker's job (same
    "capture everything, decide later" philosophy as the rest of this
    project).

    Only AUTO-tier event matches are processed, plus any REVIEW-tier match
    whose (benchmark_event_id, comparison_event_id) pair appears in
    `approved_pairs` — a human has looked at it and confirmed it's the
    same fixture (see storage.load_approved_review_pairs). Everything
    else REVIEW-tier is silently excluded here; run_cycle() is what
    surfaces those into the review queue, this function only consumes the
    outcome of that process.
    """
    benchmark_by_event = _group_by_event(benchmark_snapshots)
    comparison_by_event = _group_by_event(comparison_snapshots)

    trusted, needs_review = classify_event_matches(benchmark_snapshots, comparison_snapshots)
    approved_review = [
        m for m in needs_review if (m.anchor.event_id, m.candidate.event_id) in approved_pairs
    ]
    event_matches = trusted + approved_review

    results: list[LegEdge] = []
    for em in event_matches:
        bm_markets = benchmark_by_event[em.anchor.event_id]
        cmp_markets = comparison_by_event[em.candidate.event_id]
        market_matches, _unmatched = match_markets(bm_markets, cmp_markets)

        for mm in market_matches:
            bm_selections = {o.selection for o in mm.anchor.outcomes}
            for cmp_outcome in mm.candidate.outcomes:
                if cmp_outcome.selection not in bm_selections:
                    continue
                results.append(
                    LegEdge(
                        market_key=market_key(
                            em.anchor, mm.anchor.market_type, mm.anchor.line, cmp_outcome.selection, em.candidate.site
                        ),
                        selection=cmp_outcome.selection,
                        benchmark_market=mm.anchor,
                        comparison_market=mm.candidate,
                        edge_a=raw_edge(
                            next(o.odds for o in mm.anchor.outcomes if o.selection == cmp_outcome.selection),
                            cmp_outcome.odds,
                        ),
                        edge_b=devigged_edge(mm.anchor, cmp_outcome.selection, cmp_outcome.odds),
                    )
                )
    return results


def run_cycle(
    conn: Connection, benchmark_site: str, comparison_site: str, sport: str = "soccer", now: Optional[datetime] = None
) -> list[Opportunity]:
    """One full pipeline pass for one benchmark/comparison site pair:
    load latest captures, match + compute edges, feed each leg's reading
    into its OpportunityTracker (resumed from any prior open instance),
    and persist every tracker touched this cycle — whether it's still
    open or just closed. Returns the Opportunity objects touched.

    Also surfaces any REVIEW-tier event matches into the human review
    queue (storage.save_review_candidate) and folds in matches a human
    has already approved there, so approving a candidate actually changes
    what future cycles process.

    `now` defaults to the real current time; pass it explicitly in tests
    that need to simulate "kickoff has already passed".
    """
    now = now or datetime.now(timezone.utc)
    benchmark_snapshots = load_latest_market_snapshots(conn, benchmark_site)
    comparison_snapshots = load_latest_market_snapshots(conn, comparison_site)

    _trusted, needs_review = classify_event_matches(benchmark_snapshots, comparison_snapshots)
    for candidate in needs_review:
        save_review_candidate(conn, candidate)

    approved_pairs = load_approved_review_pairs(conn, benchmark_site, comparison_site)
    legs = find_leg_edges(benchmark_snapshots, comparison_snapshots, approved_pairs)

    # load_latest_market_snapshots returns the last-known snapshot for
    # EVERY (event, market, line) ever captured, even ones the source
    # site has since dropped from its feed (Pinnacle removes a matchup
    # entirely once it kicks off) - so a leg keeps reappearing here with
    # frozen odds indefinitely unless something explicitly stops it. Pre-
    # match value analysis stops being meaningful once the match starts
    # anyway, so kickoff time is the right signal to close on, independent
    # of whether the frozen edge happens to still read above threshold.
    touched: list[Opportunity] = []
    for leg in legs:
        existing = load_open_opportunity(conn, leg.market_key)
        if existing is not None:
            tracker = OpportunityTracker.resume(existing)
        else:
            tracker = OpportunityTracker(
                market_key=leg.market_key,
                sport=sport,
                benchmark_site=benchmark_site,
                comparison_site=comparison_site,
                market_type=leg.market_type,
                line=leg.line,
                selection=leg.selection,
            )

        tracker.ingest(
            LegReading(
                captured_at=leg.captured_at,
                edge_a=leg.edge_a,
                edge_b=leg.edge_b,
                benchmark_odds=leg.benchmark_odds,
                comparison_odds=leg.comparison_odds,
                max_bet_size=leg.max_bet_size,
                full_market=full_market_json(leg),
                event_started=now >= leg.benchmark_event.kickoff_utc,
            )
        )

        opportunity = tracker.current
        if opportunity is not None:
            save_opportunity(conn, opportunity)
            touched.append(opportunity)

    return touched
