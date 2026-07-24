"""Evaluation: compare Method A (raw) vs Method B (de-vigged) on the same
settled dataset, bucketed by odds range — the methodology's core test.

"If the raw method's longshot bucket underperforms while its favorite
bucket holds up, that is the margin bias — not a failure of the
hypothesis — and the bucketed comparison against Method B is what
separates the two." Both methods are evaluated over the SAME set of bets
(every settled leg was captured because Method A crossed 3% — that's the
one fixed capture-time threshold), not two different bet sets: for each
bucket we report Method A's own edge/accuracy, and separately split out
the subset where Method B *also* would have flagged it (edge_b >= 3%),
so the divergence is visible on identical underlying bets rather than
inferred from separately-drawn samples.

Deliberately reads already-settled data (opportunity + settlement) rather
than computing anything new — same "post-processing, not capture-time"
philosophy as the rest of this project. Staking/P&L sizing (flat vs
variance-reduced) is a separate later step per the methodology; this
module reports flat-stake ROI only, as the simplest evaluation of
whether the edge signal was real.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .opportunity import THRESHOLD
from .settlement import SettlementResult

# Default bucket boundaries on the BENCHMARK's own odds at entry (the
# reference price, consistent with the methodology treating the benchmark
# as "correct"). Adjust here if a finer/coarser split turns out more
# useful once there's more data.
DEFAULT_BUCKETS = (
    ("favorite", 0.0, 2.0),
    ("mid", 2.0, 4.0),
    ("longshot", 4.0, float("inf")),
)


def odds_bucket(benchmark_odds: float, buckets=DEFAULT_BUCKETS) -> str:
    for label, low, high in buckets:
        if low <= benchmark_odds < high:
            return label
    return buckets[-1][0]


_PROFIT_MULTIPLIER = {
    SettlementResult.WON: 1.0,
    SettlementResult.LOST: -1.0,
    SettlementResult.PUSH: 0.0,
    SettlementResult.HALF_WON: 0.5,
    SettlementResult.HALF_LOST: -0.5,
}


def flat_stake_profit(outcome: SettlementResult, odds: float, stake: float = 1.0) -> float:
    """Profit on a flat `stake` bet at `odds`, given the settled outcome.
    A push always returns 0 regardless of odds; half-won/half-lost split
    the stake per the quarter-line handicap convention (see
    vb.settlement).
    """
    if outcome in (SettlementResult.WON, SettlementResult.HALF_WON):
        fraction = _PROFIT_MULTIPLIER[outcome]
        return fraction * stake * (odds - 1)
    return _PROFIT_MULTIPLIER[outcome] * stake


@dataclass
class SettledLeg:
    match_key: str
    bucket: str
    benchmark_odds: float
    comparison_odds: float
    entry_edge_a: float
    entry_edge_b: float
    outcome: SettlementResult

    @property
    def method_b_would_flag(self) -> bool:
        return self.entry_edge_b >= THRESHOLD

    @property
    def profit(self) -> float:
        return flat_stake_profit(self.outcome, self.comparison_odds)

    @property
    def is_win(self) -> Optional[float]:
        """Win credit for hit-rate purposes: 1.0 / 0.0 / 0.5 / None (push
        excluded from the hit-rate denominator, same convention as
        typical sports-betting ROI reporting)."""
        if self.outcome == SettlementResult.PUSH:
            return None
        return {
            SettlementResult.WON: 1.0,
            SettlementResult.LOST: 0.0,
            SettlementResult.HALF_WON: 0.75,
            SettlementResult.HALF_LOST: 0.25,
        }[self.outcome]


@dataclass
class BucketStats:
    bucket: str
    n: int = 0
    avg_edge_a: float = 0.0
    avg_edge_b: float = 0.0
    hit_rate: Optional[float] = None
    flat_roi: float = 0.0
    total_staked: float = 0.0
    total_profit: float = 0.0


def _summarize(legs: list[SettledLeg]) -> BucketStats:
    if not legs:
        return BucketStats(bucket="", n=0)
    hit_values = [leg.is_win for leg in legs if leg.is_win is not None]
    total_profit = sum(leg.profit for leg in legs)
    total_staked = float(len(legs))
    return BucketStats(
        bucket=legs[0].bucket,
        n=len(legs),
        avg_edge_a=sum(leg.entry_edge_a for leg in legs) / len(legs),
        avg_edge_b=sum(leg.entry_edge_b for leg in legs) / len(legs),
        hit_rate=(sum(hit_values) / len(hit_values)) if hit_values else None,
        flat_roi=total_profit / total_staked if total_staked else 0.0,
        total_staked=total_staked,
        total_profit=total_profit,
    )


@dataclass
class EvaluationReport:
    overall_a: BucketStats
    overall_b_agrees: BucketStats
    by_bucket_a: dict = field(default_factory=dict)
    by_bucket_b_agrees: dict = field(default_factory=dict)
    excluded_unsettled_or_unmatched: int = 0


def evaluate(conn: sqlite3.Connection, buckets=DEFAULT_BUCKETS) -> EvaluationReport:
    """Load every settled opportunity leg and compare Method A's own
    stats against the subset where Method B also would have flagged it —
    per bucket and overall.
    """
    from .storage import load_opportunity

    all_legs: list[SettledLeg] = []
    excluded = 0

    for benchmark_site, benchmark_event_id, market_type, line, selection, outcome_str in conn.execute(
        "SELECT benchmark_site, benchmark_event_id, market_type, line, selection, outcome FROM settlement"
    ):
        # market_key format (see vb.pipeline.market_key):
        # "{benchmark_site}:{event_id}:{market_type}:{line}:{selection}:vs:{comparison_site}"
        # - matching by prefix rather than parsing market_key apart, same
        # approach as storage.record_match_result.
        prefix = f"{benchmark_site}:{benchmark_event_id}:{market_type}:{line}:{selection}:vs:"
        instance_ids = [
            r[0]
            for r in conn.execute(
                "SELECT instance_id FROM opportunity WHERE benchmark_site = ? AND market_key LIKE ?",
                (benchmark_site, f"{prefix}%"),
            )
        ]
        for instance_id in instance_ids:
            opp = load_opportunity(conn, instance_id)
            if not opp.snapshots:
                excluded += 1
                continue
            entry = opp.snapshots[0]
            all_legs.append(
                SettledLeg(
                    match_key=opp.market_key,
                    bucket=odds_bucket(entry.benchmark_odds, buckets),
                    benchmark_odds=entry.benchmark_odds,
                    comparison_odds=entry.comparison_odds,
                    entry_edge_a=entry.edge_a,
                    entry_edge_b=entry.edge_b,
                    outcome=SettlementResult(outcome_str),
                )
            )

    b_agrees = [leg for leg in all_legs if leg.method_b_would_flag]

    by_bucket_a = {}
    by_bucket_b = {}
    for label, _low, _high in buckets:
        by_bucket_a[label] = _summarize([leg for leg in all_legs if leg.bucket == label])
        by_bucket_b[label] = _summarize([leg for leg in b_agrees if leg.bucket == label])

    return EvaluationReport(
        overall_a=_summarize(all_legs),
        overall_b_agrees=_summarize(b_agrees),
        by_bucket_a=by_bucket_a,
        by_bucket_b_agrees=by_bucket_b,
        excluded_unsettled_or_unmatched=excluded,
    )
