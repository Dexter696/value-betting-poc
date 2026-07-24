"""Edge calculation: Method A (raw) and Method B (de-vigged), per the
methodology. Both run over the same captured data — which one to trust is
a post-processing decision, not a capture-time one (see
`VB - methodology.docx` and the addendum).

Method A: the benchmark's own published odds for the specific leg are
treated as fair (margin ignored). edge = EV of the comparison book's odds
against that implied probability. Simple, but systematically over-flags
longshots as value, since bookmaker margin is usually loaded
disproportionately onto long odds (favorite-longshot bias).

Method B: the benchmark MARKET (all outcomes, not just the one leg) is
de-vigged first — margin removed via proportional scaling of implied
probabilities — to get a fair probability, then edge = EV of the
comparison odds against that. Controls for the bias Method A is prone to.
"""

from __future__ import annotations

from .models import MarketSnapshot, Selection


def implied_probability(odds: float) -> float:
    return 1.0 / odds


def overround(outcome_odds: list[float]) -> float:
    """A market's total margin/vig: how much implied probabilities sum to
    over 1.0 (0.05 = a 5% overround). One of the "full market" fields the
    methodology wants captured per book.
    """
    return sum(implied_probability(o) for o in outcome_odds) - 1.0


def devig_proportional(outcome_odds: list[float]) -> list[float]:
    """Proportional (multiplicative) de-vig: scale each outcome's implied
    probability down so they sum to exactly 1.0. The simplest, most
    common de-vig method — it only removes the overall margin, it does
    NOT itself correct for favorite-longshot bias (that's what having a
    separate Method A/B comparison, bucketed by odds range, is for).
    """
    implied = [implied_probability(o) for o in outcome_odds]
    total = sum(implied)
    return [p / total for p in implied]


def raw_edge(benchmark_odds: float, comparison_odds: float) -> float:
    """Method A. `benchmark_odds` is the leg's own published odds, treated
    as if fair. Returns EV as a fraction (0.05 = 5% edge)."""
    return comparison_odds * implied_probability(benchmark_odds) - 1.0


def devigged_edge(benchmark_market: MarketSnapshot, selection: Selection, comparison_odds: float) -> float:
    """Method B. `benchmark_market` must be the full market (all outcomes)
    for the leg being evaluated — the de-vig needs every outcome to
    normalize against, not just the one being priced.
    """
    odds_by_selection = {o.selection: o.odds for o in benchmark_market.outcomes}
    order = list(odds_by_selection.keys())
    fair_probs = devig_proportional([odds_by_selection[s] for s in order])
    fair_prob = fair_probs[order.index(selection)]
    return comparison_odds * fair_prob - 1.0
