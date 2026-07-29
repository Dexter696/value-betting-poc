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
from typing import Callable, Optional
from sqlite3 import Connection

from .edge import devigged_edge, overround, raw_edge
from .episode import EpisodeIngestResult, EpisodeTracker, LegReadingV2
from .freshness import FreshnessLimits, check_freshness
from .market_mapping import match_events_v2, remap_handicap_line, remap_selection
from .matching import MatchTier, match_events, match_markets
from .models import MarketSnapshot, MarketType, MatchOrientation, Outcome, RawEvent, RejectReason, Selection, StrategyDefinition
from .opportunity import LegReading, Opportunity, OpportunityTracker
from .storage import (
    get_or_create_strategy_definition,
    load_approved_review_pairs,
    load_event_version,
    load_latest_market_snapshots,
    load_latest_market_snapshots_v2,
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


@dataclass(frozen=True)
class ParsedMarketIdentity:
    benchmark_site: str
    benchmark_event_id: str
    market_type: MarketType
    line: Optional[float]
    selection: Selection
    comparison_site: str


def parse_market_identity(market_identity_id: str) -> ParsedMarketIdentity:
    """Inverts market_key() — used by callers (vb.decision_runner) that
    only have a market_identity_id/SignalEpisode in hand and need the
    real site/event/market/selection back to look up live odds. Splits
    on the ":vs:" delimiter first to isolate comparison_site (chosen in
    market_key() specifically because it can't appear in a site name or
    enum value), then takes market_type/line/selection off the right
    end, since those are fixed enum/float values that never contain a
    colon — whatever's left in the middle is the event_id, rejoined
    with ":" in case it ever legitimately contains one.
    """
    left, comparison_site = market_identity_id.rsplit(":vs:", 1)
    parts = left.split(":")
    site = parts[0]
    selection = Selection(parts[-1])
    line = None if parts[-2] == "None" else float(parts[-2])
    market_type = MarketType(parts[-3])
    event_id = ":".join(parts[1:-3])
    return ParsedMarketIdentity(
        benchmark_site=site, benchmark_event_id=event_id, market_type=market_type,
        line=line, selection=selection, comparison_site=comparison_site,
    )


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


def classify_event_matches_v2(benchmark_snapshots: list[MarketSnapshot], comparison_snapshots: list[MarketSnapshot]):
    """v2 replacement for classify_event_matches() — uses
    vb.market_mapping.match_events_v2 (globally-optimal bipartite
    assignment within each competition+kickoff-window block, F-14)
    instead of vb.matching.match_events (greedy, order-dependent).
    Returns (trusted, needs_review) OrientedEventMatch lists, mirroring
    classify_event_matches()'s (AUTO, REVIEW) split — REJECT-tier
    "matches" never make it out of match_events_v2 in the first place.
    """
    benchmark_by_event = _group_by_event(benchmark_snapshots)
    comparison_by_event = _group_by_event(comparison_snapshots)
    benchmark_events = [snaps[0].event for snaps in benchmark_by_event.values()]
    comparison_events = [snaps[0].event for snaps in comparison_by_event.values()]

    matches = match_events_v2(benchmark_events, comparison_events)
    trusted = [m for m in matches if m.tier == MatchTier.AUTO]
    needs_review = [m for m in matches if m.tier == MatchTier.REVIEW]
    return trusted, needs_review


def _reorient_market(
    market: MarketSnapshot, orientation: MatchOrientation, snapshot_ids: Optional[dict] = None
) -> MarketSnapshot:
    """The other half of F-14's fix: a candidate market detected as
    SWAPPED orientation has its outcomes' selections, and (for a
    directional Asian Handicap) its line's sign, remapped into the
    ANCHOR's frame — so a "HOME" price that's really the anchor's away
    team, or a line signed from the wrong team's perspective, is never
    silently compared as if it were the anchor's own HOME/its own
    line. Returns `market` unchanged when orientation is SAME, so every
    downstream consumer (edge calculation, LegEdge's own odds-lookup
    properties, full_market_json) can treat every comparison_market the
    same way without needing to know orientation ever existed.

    `snapshot_ids` is run_cycle_v2's id()-keyed map from a translated
    MarketSnapshot back to its real v2 snapshot id
    (see _translate_v2_to_v1). Reorienting builds a NEW MarketSnapshot
    object (a frozen dataclass — its outcomes/line can't be mutated in
    place), which would silently break that id()-keyed lookup for every
    SWAPPED leg if left unregistered; this carries the original
    object's v2 id forward onto the new one's id() so it keeps
    resolving correctly downstream. A no-op when snapshot_ids is None
    (test callers that don't need v2 id resolution) or when orientation
    is SAME (nothing new was created to register).
    """
    if orientation is MatchOrientation.SAME:
        return market
    remapped_outcomes = tuple(Outcome(remap_selection(o.selection, orientation), o.odds) for o in market.outcomes)
    reoriented = MarketSnapshot(
        event=market.event, market_type=market.market_type, line=remap_handicap_line(market.line, orientation),
        outcomes=remapped_outcomes, captured_at=market.captured_at, max_bet_size=market.max_bet_size,
    )
    if snapshot_ids is not None and id(market) in snapshot_ids:
        snapshot_ids[id(reoriented)] = snapshot_ids[id(market)]
    return reoriented


def find_leg_edges_v2(
    benchmark_snapshots: list[MarketSnapshot],
    comparison_snapshots: list[MarketSnapshot],
    approved_pairs: frozenset = frozenset(),
    snapshot_ids: Optional[dict] = None,
) -> list[LegEdge]:
    """v2 replacement for find_leg_edges() — same overall shape and
    edge-computation logic, but matches events via match_events_v2
    (bipartite + orientation, F-14) instead of match_events (greedy),
    and reorients each matched candidate market into the anchor's frame
    (_reorient_market) BEFORE matching markets by (market_type, line) —
    a SWAPPED candidate's own raw handicap line is signed from ITS home
    team's perspective, which only lines up with the anchor's line
    after remapping. `snapshot_ids`, if given, is threaded through to
    _reorient_market so run_cycle_v2's id()-keyed v2-snapshot-id lookup
    keeps working for reoriented markets too.
    """
    benchmark_by_event = _group_by_event(benchmark_snapshots)
    comparison_by_event = _group_by_event(comparison_snapshots)

    trusted, needs_review = classify_event_matches_v2(benchmark_snapshots, comparison_snapshots)
    approved_review = [
        m for m in needs_review if (m.anchor.event_id, m.candidate.event_id) in approved_pairs
    ]
    event_matches = trusted + approved_review

    results: list[LegEdge] = []
    for em in event_matches:
        bm_markets = benchmark_by_event[em.anchor.event_id]
        cmp_markets = [
            _reorient_market(m, em.orientation, snapshot_ids) for m in comparison_by_event[em.candidate.event_id]
        ]
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


def _translate_v2_to_v1(conn: Connection, v2_snapshots: list) -> tuple[list[MarketSnapshot], dict[int, str]]:
    """Convert MarketSnapshotV2 rows (plus their EventVersionV2) back
    into v1's RawEvent/MarketSnapshot shapes so find_leg_edges can reuse
    vb.matching/vb.edge completely unchanged — the whole point of
    building run_cycle_v2 as a bridge on top of the existing matching
    engine rather than a parallel reimplementation of it.

    Returns the translated snapshots alongside a python id()-keyed map
    back to each snapshot's real v2 id, since v1's MarketSnapshot has no
    id field of its own and LegReadingV2 needs the real
    benchmark_snapshot_id/comparison_snapshot_id for provenance. Relies
    on vb.matching.match_markets/match_events returning references into
    the same objects passed in (never reconstructing them), which is
    what lets this id()-keyed map still resolve after matching.
    """
    event_version_cache: dict[str, RawEvent] = {}
    translated: list[MarketSnapshot] = []
    v2_id_by_object: dict[int, str] = {}
    for snap in v2_snapshots:
        raw_event = event_version_cache.get(snap.event_version_id)
        if raw_event is None:
            ev = load_event_version(conn, snap.event_version_id)
            raw_event = RawEvent(
                site=ev.site, sport=ev.sport, competition=ev.competition, kickoff_utc=ev.kickoff_utc,
                raw_home_team=ev.home_team, raw_away_team=ev.away_team, event_id=ev.event_id,
            )
            event_version_cache[snap.event_version_id] = raw_event
        v1_snap = MarketSnapshot(
            event=raw_event, market_type=snap.market_type, line=snap.line, outcomes=snap.outcomes,
            captured_at=snap.received_at, max_bet_size=snap.max_bet_size,
        )
        translated.append(v1_snap)
        v2_id_by_object[id(v1_snap)] = snap.id
    return translated, v2_id_by_object


def run_cycle_v2(
    conn: Connection,
    benchmark_site: str,
    comparison_site: str,
    strategy: StrategyDefinition,
    limits: FreshnessLimits,
    edge_selector: Callable[[LegEdge], float],
    sport: str = "soccer",
    now: Optional[datetime] = None,
) -> list[EpisodeIngestResult]:
    """Schema-v2 pipeline pass — reads from market_snapshot_v2
    (vb.capture_v2, SUCCESS source_runs only), matches events via
    find_leg_edges_v2 (globally-optimal bipartite assignment +
    orientation remapping — F-14, not the greedy vb.matching.match_events
    v1's run_cycle() still uses), gates every reading through F-01's
    check_freshness before it can open or extend an episode, and drives
    EpisodeTracker (UUID identity — F-02's fix) instead of
    OpportunityTracker (process-local counter identity).

    `strategy` and `edge_selector` together are F-04's fix: call this
    once with a Method-A StrategyDefinition and `lambda leg: leg.edge_a`,
    and again with a Method-B StrategyDefinition and
    `lambda leg: leg.edge_b` — each call drives its own independent
    EpisodeTracker keyed by its own strategy_version, so Method B scans
    for its OWN first crossing rather than inheriting Method A's
    already-open episode (the exact bug the audit found: Method B was
    only ever checked at Method A's entry snapshot, never on its own
    terms).

    Wired into scripts/scheduled_run.py's live scheduled capture as a
    shadow pipeline alongside the completely unchanged run_cycle() —
    see that script's own module docstring.
    """
    now = now or datetime.now(timezone.utc)
    strategy_id = get_or_create_strategy_definition(conn, strategy)

    bench_v1, bench_ids = _translate_v2_to_v1(conn, load_latest_market_snapshots_v2(conn, benchmark_site))
    comp_v1, comp_ids = _translate_v2_to_v1(conn, load_latest_market_snapshots_v2(conn, comparison_site))
    snapshot_ids = {**bench_ids, **comp_ids}

    _trusted, needs_review = classify_event_matches_v2(bench_v1, comp_v1)
    for candidate in needs_review:
        save_review_candidate(conn, candidate)

    approved_pairs = load_approved_review_pairs(conn, benchmark_site, comparison_site)
    legs = find_leg_edges_v2(bench_v1, comp_v1, approved_pairs, snapshot_ids)

    results: list[EpisodeIngestResult] = []
    for leg in legs:
        market_identity = market_key(leg.benchmark_event, leg.market_type, leg.line, leg.selection, comparison_site)
        freshness = check_freshness(
            benchmark_received_at=leg.benchmark_market.captured_at,
            comparison_received_at=leg.comparison_market.captured_at,
            kickoff_utc=leg.benchmark_event.kickoff_utc,
            now=now,
            limits=limits,
        )
        reading = LegReadingV2(
            received_at=leg.captured_at,
            edge=edge_selector(leg),
            benchmark_snapshot_id=snapshot_ids[id(leg.benchmark_market)],
            comparison_snapshot_id=snapshot_ids[id(leg.comparison_market)],
            edge_model=strategy.signal_model,
            eligible=freshness.eligible,
            reject_reason=RejectReason(freshness.reject_reason) if freshness.reject_reason else None,
            market_suspended=False,
            event_started=now >= leg.benchmark_event.kickoff_utc,
            now=now,
            kickoff_utc=leg.benchmark_event.kickoff_utc,
        )
        tracker = EpisodeTracker(conn, strategy_id, market_identity, threshold=strategy.threshold)
        results.append(tracker.ingest(reading))

    return results
