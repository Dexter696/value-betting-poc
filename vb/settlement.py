"""Settlement: given a market/line/selection and a match's final score,
determine whether that leg won, lost, or pushed. This is what lets stored
opportunities eventually be scored against real outcomes — the
methodology's evaluation step ("For each stored opportunity, also store
the settled result of the specific market/leg, not just the match
winner").

Only soccer full-time scores are handled (the only sport currently
scraped). Settlement is computed from (market_type, line, selection) +
the final score alone — it doesn't need to know which book's odds were
being evaluated, which is why it's keyed independently of any particular
opportunity instance (see vb.storage settlement functions).
"""

from __future__ import annotations

from enum import Enum

from .models import MarketType, Selection


class SettlementResult(str, Enum):
    WON = "won"
    LOST = "lost"
    PUSH = "push"
    HALF_WON = "half_won"    # quarter-line handicap: one half won, one pushed
    HALF_LOST = "half_lost"  # quarter-line handicap: one half lost, one pushed


def settle_match_winner(selection: Selection, home_goals: int, away_goals: int) -> SettlementResult:
    if home_goals == away_goals:
        winner = Selection.DRAW
    elif home_goals > away_goals:
        winner = Selection.HOME
    else:
        winner = Selection.AWAY
    return SettlementResult.WON if selection == winner else SettlementResult.LOST


def settle_totals(selection: Selection, line: float, home_goals: int, away_goals: int) -> SettlementResult:
    total = home_goals + away_goals
    if total == line:
        return SettlementResult.PUSH
    over_won = total > line
    if selection == Selection.OVER:
        return SettlementResult.WON if over_won else SettlementResult.LOST
    return SettlementResult.LOST if over_won else SettlementResult.WON


def _settle_handicap_half_line(selection: Selection, half_line: float, home_goals: int, away_goals: int) -> SettlementResult:
    """Settle a single whole/half handicap line (not a quarter line)."""
    adjusted_diff = (home_goals + half_line) - away_goals  # > 0 means home covers
    if adjusted_diff == 0:
        return SettlementResult.PUSH
    home_covers = adjusted_diff > 0
    if selection == Selection.HOME:
        return SettlementResult.WON if home_covers else SettlementResult.LOST
    return SettlementResult.LOST if home_covers else SettlementResult.WON


_QUARTER_COMBINATIONS = {
    frozenset({SettlementResult.WON}): SettlementResult.WON,
    frozenset({SettlementResult.LOST}): SettlementResult.LOST,
    frozenset({SettlementResult.WON, SettlementResult.PUSH}): SettlementResult.HALF_WON,
    frozenset({SettlementResult.LOST, SettlementResult.PUSH}): SettlementResult.HALF_LOST,
}


def settle_handicap(selection: Selection, line: float, home_goals: int, away_goals: int) -> SettlementResult:
    """Quarter lines (e.g. -0.25, -0.75) split the stake across the two
    adjacent whole/half lines — settled here as the combination of both
    halves' results (HALF_WON/HALF_LOST), the standard way to represent a
    quarter-line outcome without tracking split-stake P&L as two separate
    bets.
    """
    quarter_units = round(line * 4)
    is_quarter_line = quarter_units % 2 != 0
    if not is_quarter_line:
        return _settle_handicap_half_line(selection, line, home_goals, away_goals)

    lower = (quarter_units - 1) / 4
    upper = (quarter_units + 1) / 4
    r1 = _settle_handicap_half_line(selection, lower, home_goals, away_goals)
    r2 = _settle_handicap_half_line(selection, upper, home_goals, away_goals)
    return _QUARTER_COMBINATIONS[frozenset({r1, r2})]


def settle(market_type: MarketType, line, selection: Selection, home_goals: int, away_goals: int) -> SettlementResult:
    """Dispatch to the right settle_* function for `market_type`."""
    if market_type == MarketType.MATCH_WINNER:
        return settle_match_winner(selection, home_goals, away_goals)
    if market_type == MarketType.TOTALS:
        return settle_totals(selection, line, home_goals, away_goals)
    if market_type == MarketType.ASIAN_HANDICAP:
        return settle_handicap(selection, line, home_goals, away_goals)
    raise ValueError(f"unsupported market_type: {market_type}")
