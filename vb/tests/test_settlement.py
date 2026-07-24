from vb.models import MarketType, Selection
from vb.settlement import (
    SettlementResult,
    settle,
    settle_handicap,
    settle_match_winner,
    settle_totals,
)


def test_match_winner_home_win():
    assert settle_match_winner(Selection.HOME, 2, 1) == SettlementResult.WON
    assert settle_match_winner(Selection.AWAY, 2, 1) == SettlementResult.LOST
    assert settle_match_winner(Selection.DRAW, 2, 1) == SettlementResult.LOST


def test_match_winner_draw():
    assert settle_match_winner(Selection.DRAW, 1, 1) == SettlementResult.WON
    assert settle_match_winner(Selection.HOME, 1, 1) == SettlementResult.LOST


def test_totals_over_under():
    assert settle_totals(Selection.OVER, 2.5, 2, 1) == SettlementResult.WON   # total 3 > 2.5
    assert settle_totals(Selection.UNDER, 2.5, 2, 1) == SettlementResult.LOST
    assert settle_totals(Selection.OVER, 2.5, 1, 1) == SettlementResult.LOST  # total 2 < 2.5
    assert settle_totals(Selection.UNDER, 2.5, 1, 1) == SettlementResult.WON


def test_totals_whole_line_push():
    assert settle_totals(Selection.OVER, 3.0, 2, 1) == SettlementResult.PUSH
    assert settle_totals(Selection.UNDER, 3.0, 2, 1) == SettlementResult.PUSH


def test_handicap_whole_line_home_favorite_covers():
    # -1.0 home: home must win by 2+ to fully cover, wins by exactly 1 -> push
    assert settle_handicap(Selection.HOME, -1.0, 2, 1) == SettlementResult.PUSH
    assert settle_handicap(Selection.HOME, -1.0, 3, 1) == SettlementResult.WON
    assert settle_handicap(Selection.AWAY, -1.0, 3, 1) == SettlementResult.LOST


def test_handicap_half_line_no_push_possible():
    assert settle_handicap(Selection.HOME, -0.5, 1, 1) == SettlementResult.LOST
    assert settle_handicap(Selection.AWAY, -0.5, 1, 1) == SettlementResult.WON
    assert settle_handicap(Selection.HOME, -0.5, 2, 1) == SettlementResult.WON


def test_handicap_quarter_line_half_won():
    # -0.25 home, home wins by 1+: both the 0 and -0.5 halves win -> full WON
    assert settle_handicap(Selection.HOME, -0.25, 2, 1) == SettlementResult.WON


def test_handicap_quarter_line_half_lost_on_draw():
    # -0.25 home, draw: the 0-line pushes, the -0.5-line loses -> HALF_LOST
    assert settle_handicap(Selection.HOME, -0.25, 1, 1) == SettlementResult.HALF_LOST
    # mirrors to the away side as HALF_WON
    assert settle_handicap(Selection.AWAY, -0.25, 1, 1) == SettlementResult.HALF_WON


def test_handicap_quarter_line_negative_side_half_lost():
    # -0.75 home, draw: -0.5 half loses, -1.0 half loses -> full LOST
    assert settle_handicap(Selection.HOME, -0.75, 1, 1) == SettlementResult.LOST
    # home wins by 1: -0.5 half wins, -1.0 half pushes -> HALF_WON
    assert settle_handicap(Selection.HOME, -0.75, 2, 1) == SettlementResult.HALF_WON


def test_settle_dispatches_by_market_type():
    assert settle(MarketType.MATCH_WINNER, None, Selection.HOME, 2, 0) == SettlementResult.WON
    assert settle(MarketType.TOTALS, 2.5, Selection.OVER, 2, 1) == SettlementResult.WON
    assert settle(MarketType.ASIAN_HANDICAP, -1.0, Selection.HOME, 2, 1) == SettlementResult.PUSH
