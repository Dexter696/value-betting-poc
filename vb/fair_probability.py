"""Fair-probability estimation and calibration metrics — Phase 4 of the
audit's remediation roadmap (2026-07-25).

Method A's "the benchmark's own published price is fair" and Method
B's plain proportional de-vig both have a known blind spot: a
bookmaker's margin usually isn't spread evenly across outcomes, it's
loaded more heavily onto longshots (favorite-longshot bias). The audit
flagged this directly — F-12 ("Method A doesn't measure fair expected
value") and F-13 ("proportional de-vig doesn't address favorite-
longshot bias"). This module adds two more de-vig methods and treats
"which fair-probability model to trust" as an explicit, calibratable
choice instead of one hardcoded assumption.

Three de-vig methods, all normalizing the same raw implied
probabilities (1/odds) down from their overround-inflated sum to
exactly 1.0, differing in HOW the margin is assumed to be distributed
across outcomes:

- proportional (vb.edge.devig_proportional): margin removed uniformly.
  Does not correct favorite-longshot bias.
- power (devig_power): margin removed via a power-law compression that
  shrinks every outcome's raw probability by `r ** e` for a shared
  exponent e - since raw probabilities are all < 1, this shrinks
  longshots (small r) proportionally MORE than favorites (large r) for
  the same e, which is exactly the correction favorite-longshot bias
  calls for.
- odds_ratio (devig_odds_ratio, Cheung 2015): a different, independent
  non-linear correction with the same favorite-longshot-aware
  direction, useful as a cross-check against the power method rather
  than relying on one alone.

Every method is checked (in tests, not just asserted in prose — a
load-bearing Phase 4 acceptance criterion) to sum to 1.0, and to give
the favorite a HIGHER fair probability than plain proportional gives
it, with the longshot correspondingly lower — the direction favorite-
longshot-bias correction is supposed to move things.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .edge import devig_proportional, implied_probability
from .models import MarketSnapshot, Selection

_BISECTION_ITERATIONS = 200
_BISECTION_TOLERANCE = 1e-12


def devig_power(outcome_odds: list[float]) -> list[float]:
    """Power method: find exponent e >= 1 such that
    sum((1/odds_i) ** e) == 1, then return (1/odds_i) ** e per outcome.
    total(e) is strictly decreasing in e (every raw implied probability
    is < 1, so raising it to a higher power shrinks it further), so a
    plain bisection search finds the unique root without needing an
    external numerical-solver dependency.
    """
    raw = [implied_probability(o) for o in outcome_odds]

    def total(e: float) -> float:
        return sum(r ** e for r in raw)

    lo, hi = 1.0, 2.0
    while total(hi) > 1.0:
        hi *= 2
        if hi > 1e6:
            raise ValueError("devig_power: no root found - check input odds for a real overround")

    for _ in range(_BISECTION_ITERATIONS):
        mid = (lo + hi) / 2
        if total(mid) > 1.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < _BISECTION_TOLERANCE:
            break

    e = (lo + hi) / 2
    return [r ** e for r in raw]


def devig_odds_ratio(outcome_odds: list[float]) -> list[float]:
    """Odds-ratio method (Cheung 2015): find a single scalar OR such
    that applying `OR*r / (1 - r + OR*r)` to every outcome's raw
    implied probability r sums to 1. Derived directly from the
    odds-ratio definition `OR = p*(1-r) / (r*(1-p))` solved for p given
    r and OR. total(OR) is strictly increasing in OR (p -> 0 as
    OR -> 0, p -> 1 as OR -> inf), so bisection over OR in (0, 1) finds
    the unique root for any book with positive overround.
    """
    raw = [implied_probability(o) for o in outcome_odds]

    def apply(odds_ratio: float) -> list[float]:
        return [odds_ratio * r / (1 - r + odds_ratio * r) for r in raw]

    def total(odds_ratio: float) -> float:
        return sum(apply(odds_ratio))

    lo, hi = 1e-9, 1.0
    for _ in range(_BISECTION_ITERATIONS):
        mid = (lo + hi) / 2
        if total(mid) < 1.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < _BISECTION_TOLERANCE:
            break

    return apply((lo + hi) / 2)


DEVIG_METHODS: dict[str, Callable[[list[float]], list[float]]] = {
    "proportional": devig_proportional,
    "power": devig_power,
    "odds_ratio": devig_odds_ratio,
}


def fair_edge(benchmark_market: MarketSnapshot, selection: Selection, comparison_odds: float, method: str) -> float:
    """The Phase 4 step 8/9 "general strategy runner" entry point:
    compute edge using any registered de-vig method by name, instead of
    Method B's previously-hardcoded proportional de-vig
    (vb.edge.devigged_edge is unchanged and still exists — this is a
    generalization of it, not a replacement). Method A (the raw,
    non-devigged benchmark price - vb.edge.raw_edge) remains available
    unchanged as a diagnostic feature, per step 9.
    """
    devig_fn = DEVIG_METHODS[method]
    odds_by_selection = {o.selection: o.odds for o in benchmark_market.outcomes}
    order = list(odds_by_selection.keys())
    fair_probs = devig_fn([odds_by_selection[s] for s in order])
    fair_prob = fair_probs[order.index(selection)]
    return comparison_odds * fair_prob - 1.0


# ---- Calibration metrics (Phase 4 step 5) ----


def log_loss(probability: float, outcome_occurred: bool) -> float:
    """Cross-entropy loss for one binary prediction. Lower is better;
    heavily penalizes confident-and-wrong predictions (a predicted 0.99
    that doesn't occur costs far more than a predicted 0.6 that
    doesn't)."""
    p = min(max(probability, 1e-15), 1 - 1e-15)
    return -math.log(p) if outcome_occurred else -math.log(1 - p)


def brier_score(probability: float, outcome_occurred: bool) -> float:
    """Mean squared error between a predicted probability and the
    realized 0/1 outcome. Lower is better; 0 is perfect."""
    actual = 1.0 if outcome_occurred else 0.0
    return (probability - actual) ** 2


@dataclass(frozen=True)
class CalibrationBucket:
    label: str
    predicted_mean: float
    observed_rate: float
    n: int


def calibration_report(
    predictions: list[tuple[float, bool]], bucket_edges: list[float]
) -> list[CalibrationBucket]:
    """Buckets (predicted_probability, outcome_occurred) pairs by
    predicted probability and reports predicted-mean vs observed-rate
    per bucket. A well-calibrated model has predicted_mean roughly
    equal to observed_rate in every bucket with enough samples - this
    is the audit's acceptance criterion ("stable out-of-sample
    calibration according to pre-declared buckets"), made checkable
    instead of asserted.

    `bucket_edges` are the upper bound of every bucket except the last,
    e.g. [0.2, 0.4, 0.6, 0.8] makes 5 buckets covering
    [0, 0.2), [0.2, 0.4), ..., [0.8, 1.0].
    """
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(len(bucket_edges) + 1)]
    for p, outcome in predictions:
        idx = 0
        while idx < len(bucket_edges) and p >= bucket_edges[idx]:
            idx += 1
        buckets[idx].append((p, outcome))

    edges = [0.0] + list(bucket_edges) + [1.0]
    results = []
    for i, bucket in enumerate(buckets):
        n = len(bucket)
        predicted_mean = sum(p for p, _ in bucket) / n if n else float("nan")
        observed_rate = sum(1 for _, o in bucket if o) / n if n else float("nan")
        results.append(CalibrationBucket(
            label=f"[{edges[i]:.2f}, {edges[i + 1]:.2f})", predicted_mean=predicted_mean,
            observed_rate=observed_rate, n=n,
        ))
    return results


def source_dispersion(probabilities: list[float]) -> float:
    """Standard deviation of independently-estimated fair probabilities
    for the SAME outcome across multiple sources or methods. High
    dispersion means the sources disagree a lot - a signal that
    whichever one the pipeline currently trusts could be wrong, not
    just that the estimate is noisy."""
    if len(probabilities) < 2:
        return 0.0
    mean = sum(probabilities) / len(probabilities)
    variance = sum((p - mean) ** 2 for p in probabilities) / len(probabilities)
    return variance ** 0.5
