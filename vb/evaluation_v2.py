"""Schema-v2 evaluation report — Phase 6 steps 4/5/6/8 of the audit's
remediation roadmap (2026-07-25).

vb.evaluation (v1) reads opportunity/settlement directly and is kept
unchanged — it's what scripts/evaluation_report.py still uses against
the frozen legacy dataset (see PROJECT_DOCUMENTATION.md's Phase 0
notice). This module is its schema-v2 replacement, built against
ExecutedBet — one fully-resolved paper bet already joined across
bet_decision -> bet_execution -> settlement_version -> canonical_event
by the caller. Keeping that join out of this module is deliberate: it
lets the metric functions here be built and fully tested against
synthetic data now, before any real join exists to run them against
(nothing is wired into the live capture path yet — see
vb/capture_v2.py's module docstring).

Implements the audit's own Phase 6 acceptance criteria:
- "row-level P&L sums to exactly the headline" — build_report()'s
  total_profit is a literal sum of every bet's own profit, not a
  separately-computed aggregate that could drift from it.
- "CI clusters by canonical event" — clustered_roi_confidence_interval
  uses a cluster bootstrap over canonical_event_id, not a naive
  per-bet standard error, so correlated bets on the same match don't
  understate uncertainty.
- "evaluation_run records code SHA, config hash, DB snapshot hash, and
  cutoff" — build_report() takes all four and embeds them in the
  returned artifact, and persists them via vb.storage.save_evaluation_run.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .closing import closing_line_value
from .evaluation import flat_stake_profit
from .identity import content_hash, new_id
from .models import EvaluationRun, ExecutionStatus
from .settlement import SettlementResult
from .storage import save_evaluation_run


@dataclass(frozen=True)
class ExecutedBet:
    """One fully-resolved paper bet, assembled by the caller from a
    join over bet_decision/bet_execution/settlement_version/
    canonical_event. `outcome` and `consensus_closing_odds` are None
    when not yet known (bet placed but match not yet settled / no
    closing-consensus collected) — every metric function here treats
    those as "not included," never as a default value."""

    strategy_version: str
    canonical_event_id: str
    decided_at: datetime
    execution_status: ExecutionStatus
    requested_odds: float
    accepted_odds: Optional[float]
    accepted_stake: Optional[float]
    outcome: Optional[SettlementResult]
    consensus_closing_odds: Optional[float]


def _settled(bets: list[ExecutedBet]) -> list[ExecutedBet]:
    return [b for b in bets if b.outcome is not None and b.accepted_odds is not None]


def event_level_counts(bets: list[ExecutedBet]) -> dict:
    return {
        "total_decisions": len(bets),
        "unique_events": len({b.canonical_event_id for b in bets}),
        "settled_bets": len(_settled(bets)),
    }


def rejection_rate(bets: list[ExecutedBet]) -> float:
    if not bets:
        return float("nan")
    return sum(1 for b in bets if b.execution_status == ExecutionStatus.REJECTED) / len(bets)


def average_clv(bets: list[ExecutedBet]) -> Optional[float]:
    values = [
        closing_line_value(b.accepted_odds, b.consensus_closing_odds)
        for b in bets
        if b.accepted_odds is not None and b.consensus_closing_odds is not None
    ]
    if not values:
        return None
    return sum(values) / len(values)


def average_slippage(bets: list[ExecutedBet]) -> Optional[float]:
    """Mean (accepted_odds / requested_odds - 1) over every bet that
    actually got a live-odds check (excludes REJECTED, which has no
    accepted_odds at all)."""
    values = [
        (b.accepted_odds / b.requested_odds) - 1.0
        for b in bets
        if b.accepted_odds is not None and b.requested_odds
    ]
    if not values:
        return None
    return sum(values) / len(values)


def max_drawdown(bets: list[ExecutedBet]) -> float:
    """Largest peak-to-trough decline in cumulative flat-stake P&L,
    over settled bets in decided_at order. 0.0 if profit never dips
    below a prior running peak (including the empty-bets case)."""
    settled = sorted(_settled(bets), key=lambda b: b.decided_at)
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for b in settled:
        cumulative += flat_stake_profit(b.outcome, b.accepted_odds, b.accepted_stake or 0.0)
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst  # <= 0.0; 0.0 means no drawdown ever occurred


def exposure_by_site_or_event(bets: list[ExecutedBet], key_fn) -> dict[str, float]:
    """Total accepted stake grouped by whatever `key_fn(bet)` returns -
    reused for both per-event and per-site exposure summaries by the
    caller passing a different key_fn (e.g. lambda b: b.canonical_event_id)."""
    totals: dict[str, float] = {}
    for b in bets:
        if b.accepted_stake is None:
            continue
        key = key_fn(b)
        totals[key] = totals.get(key, 0.0) + b.accepted_stake
    return totals


def clustered_roi_confidence_interval(
    bets: list[ExecutedBet], n_bootstrap: int = 2000, confidence: float = 0.95, seed: int = 42,
) -> tuple[float, float, float]:
    """Cluster bootstrap CI for flat-stake ROI, clustered by
    canonical_event_id — resampling individual bets would treat
    correlated bets on the same match as independent draws and
    understate uncertainty (the audit's own "CI clusters by canonical
    event" acceptance criterion). Returns (point_estimate, lower,
    upper); all three are NaN if there's no settled data.
    """
    settled = _settled(bets)
    if not settled:
        return (float("nan"), float("nan"), float("nan"))

    clusters: dict[str, list[ExecutedBet]] = {}
    for b in settled:
        clusters.setdefault(b.canonical_event_id, []).append(b)
    cluster_keys = list(clusters.keys())

    def roi_of(sample: list[ExecutedBet]) -> float:
        total_stake = sum(b.accepted_stake or 0.0 for b in sample)
        if total_stake == 0:
            return float("nan")
        total_profit = sum(flat_stake_profit(b.outcome, b.accepted_odds, b.accepted_stake or 0.0) for b in sample)
        return total_profit / total_stake

    point = roi_of(settled)

    rng = random.Random(seed)
    bootstrapped = []
    for _ in range(n_bootstrap):
        sampled_keys = [rng.choice(cluster_keys) for _ in cluster_keys]
        sampled_bets = [b for k in sampled_keys for b in clusters[k]]
        value = roi_of(sampled_bets)
        if value == value:  # exclude NaN (an all-zero-stake resample)
            bootstrapped.append(value)

    if not bootstrapped:
        return (point, float("nan"), float("nan"))

    bootstrapped.sort()
    alpha = (1 - confidence) / 2
    lower = bootstrapped[max(0, int(alpha * len(bootstrapped)))]
    upper = bootstrapped[min(len(bootstrapped) - 1, int((1 - alpha) * len(bootstrapped)) - 1)]
    return (point, lower, upper)


def build_report(
    conn,
    bets: list[ExecutedBet],
    strategy_version: str,
    code_sha: str,
    config: dict,
    db_snapshot_hash: str,
    data_cutoff: datetime,
    created_at: datetime,
) -> dict:
    """Assemble every metric above into ONE JSON-serializable dict —
    the single artifact both Python and the dashboard's JavaScript
    should read from (steps 6/7: "JS only renders JSON, never
    recomputes its own cohort logic"). Persists an EvaluationRun row
    via vb.storage.save_evaluation_run with the metrics embedded, so
    a report can always be traced back to the exact
    code/config/data it was computed from (step 8).
    """
    settled = _settled(bets)
    total_stake = sum(b.accepted_stake or 0.0 for b in settled)
    total_profit = sum(flat_stake_profit(b.outcome, b.accepted_odds, b.accepted_stake or 0.0) for b in settled)
    point, lower, upper = clustered_roi_confidence_interval(bets)

    metrics = {
        "strategy_version": strategy_version,
        **event_level_counts(bets),
        "total_stake": total_stake,
        "total_profit": total_profit,
        "roi": point,
        "roi_confidence_interval": {"lower": lower, "upper": upper, "confidence": 0.95},
        "rejection_rate": rejection_rate(bets),
        "average_clv": average_clv(bets),
        "average_slippage": average_slippage(bets),
        "max_drawdown": max_drawdown(bets),
        "exposure_by_event": exposure_by_site_or_event(settled, lambda b: b.canonical_event_id),
    }

    run = EvaluationRun(
        id=new_id(), code_sha=code_sha, config_hash=content_hash(config), db_snapshot_hash=db_snapshot_hash,
        data_cutoff=data_cutoff, created_at=created_at, metrics=metrics,
    )
    save_evaluation_run(conn, run)

    return {
        "evaluation_run_id": run.id, "code_sha": code_sha, "config_hash": run.config_hash,
        "db_snapshot_hash": db_snapshot_hash, "data_cutoff": data_cutoff.isoformat(),
        "created_at": created_at.isoformat(), "metrics": metrics,
    }
