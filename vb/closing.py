"""Closing consensus collection — Phase 5 step 8 of the audit's
remediation roadmap (2026-07-25).

Records a market's consensus closing price — every available site's
own final price for a leg, right before the market stops being
tradeable — independent of whether a bet was ever placed on it. This
is what closing-line value (CLV) analysis needs: how a bet's accepted
price compared to where the market ultimately settled is the standard
sports-betting proxy for "this is a real, skillful edge, not noise
that happened to pay off once" — a signal genuinely orthogonal to
whether any individual bet won or lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .episode import EpisodeIngestResult
from .identity import new_id
from .models import ClosingSnapshot, EpisodeEndReason, MarketType, Selection
from .pipeline import parse_market_identity
from .settlement_evidence import get_or_create_canonical_event
from .storage import (
    list_signal_observations,
    load_event_version,
    load_market_snapshot_v2,
    load_signal_episode,
    save_closing_snapshot,
)


@dataclass(frozen=True)
class SourceClosingPrice:
    site: str
    odds: float


def consensus_closing_odds(prices: list[SourceClosingPrice]) -> float:
    """The consensus is the MEDIAN of every available site's own final
    price for this leg, not the mean — one outlier or stale site (a
    scraper glitch right at kickoff is exactly when capture is least
    reliable) can't single-handedly move the CLV baseline every bet
    gets compared against.
    """
    if not prices:
        raise ValueError("consensus_closing_odds: need at least one source price")
    values = sorted(p.odds for p in prices)
    n = len(values)
    mid = n // 2
    if n % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def closing_line_value(accepted_odds: float, consensus_odds: float) -> float:
    """CLV as a fraction: how much better (positive) or worse
    (negative) the accepted price was than where the market ultimately
    closed. `accepted_odds` should come from BetExecution.accepted_odds
    (vb.execution) — the actually-executed price, not the originally-
    observed one, for the same reason headline P&L uses it."""
    return (accepted_odds / consensus_odds) - 1.0


def record_closing_snapshot(
    conn,
    canonical_event_id: str,
    market_type: MarketType,
    line,
    selection: Selection,
    captured_at: datetime,
    prices: list[SourceClosingPrice],
) -> str:
    """Compute the consensus from `prices` and persist it as a single
    insert-only ClosingSnapshot row, with every contributing source's
    own price kept in `source_json` for later audit (was the consensus
    actually representative, or dragged around by one bad source)."""
    consensus = consensus_closing_odds(prices)
    snapshot_id = new_id()
    save_closing_snapshot(conn, ClosingSnapshot(
        id=snapshot_id, canonical_event_id=canonical_event_id, market_type=market_type, line=line,
        selection=selection, captured_at=captured_at, consensus_odds=consensus,
        source_json={"sources": [{"site": p.site, "odds": p.odds} for p in prices]},
    ))
    return snapshot_id


def record_closing_for_episode(conn, episode_id: str, closing_observation, now: datetime) -> Optional[str]:
    """The real live-wiring entry point: when an episode closes because
    the event actually started (the market genuinely stopped being
    tradeable, as opposed to an edge merely dropping below threshold or
    a market being suspended), record its closing consensus from the
    two real prices already on hand — the benchmark and comparison
    snapshots that produced the CLOSING observation itself.

    Deliberately doesn't try to gather a THIRD site's price (full
    all-books consensus needs the cross-site canonical-event fusion
    vb.market_mapping will eventually provide once a real labeled
    negative dataset exists to calibrate it - still deferred, see that
    module's docstring): two real, known prices is still a real,
    honest CLV baseline, not a guess.

    Returns None (without raising) if the episode can't be resolved, if
    either snapshot is missing, or if the selection's own price isn't
    present in one of them - the same "auditable no-op, not an error"
    discipline every other v2 integration point in this project uses.
    """
    episode = load_signal_episode(conn, episode_id)
    if episode is None or episode.end_reason != EpisodeEndReason.EVENT_STARTED:
        return None

    benchmark_snapshot = load_market_snapshot_v2(conn, closing_observation.benchmark_snapshot_id)
    comparison_snapshot = load_market_snapshot_v2(conn, closing_observation.comparison_snapshot_id)
    if benchmark_snapshot is None or comparison_snapshot is None:
        return None

    parsed = parse_market_identity(episode.market_identity_id)
    prices = []
    bench_odds = next((o.odds for o in benchmark_snapshot.outcomes if o.selection == parsed.selection), None)
    if bench_odds is not None:
        prices.append(SourceClosingPrice(site=parsed.benchmark_site, odds=bench_odds))
    comp_odds = next((o.odds for o in comparison_snapshot.outcomes if o.selection == parsed.selection), None)
    if comp_odds is not None:
        prices.append(SourceClosingPrice(site=parsed.comparison_site, odds=comp_odds))
    if not prices:
        return None

    try:
        event_version = load_event_version(conn, benchmark_snapshot.event_version_id)
    except ValueError:
        return None
    canonical_id = get_or_create_canonical_event(conn, benchmark_snapshot.event_version_id, sport=event_version.sport, now=now)

    return record_closing_snapshot(conn, canonical_id, parsed.market_type, parsed.line, parsed.selection, now, prices)


def process_cycle_closings(conn, results: list[EpisodeIngestResult], now: datetime) -> int:
    """record_closing_for_episode() over every episode a run_cycle_v2()
    call touched this cycle - only actually records anything for
    results where the episode both closed AND the closing observation
    is the current one (result.observation_id set), so this doesn't
    redundantly re-record a closing snapshot for an episode that closed
    on some earlier cycle and is just being seen again. Returns the
    number of closing snapshots recorded.
    """
    recorded = 0
    for result in results:
        if not result.closed or result.episode_id is None or result.observation_id is None:
            continue
        observations = list_signal_observations(conn, result.episode_id)
        closing_observation = next((o for o in observations if o.id == result.observation_id), None)
        if closing_observation is None:
            continue
        if record_closing_for_episode(conn, result.episode_id, closing_observation, now) is not None:
            recorded += 1
    return recorded
