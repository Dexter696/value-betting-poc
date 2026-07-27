import pytest

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


def test_f19_match_winner_rejects_a_totals_selection_instead_of_silently_losing():
    with pytest.raises(ValueError):
        settle_match_winner(Selection.OVER, 2, 1)


def test_f19_totals_rejects_a_non_totals_selection_instead_of_treating_it_as_under():
    # Old behavior: anything that wasn't OVER silently settled as
    # UNDER. A HOME selection reaching this function (a real
    # possibility if market-matching mismatched a leg) must raise, not
    # quietly settle as if it were UNDER.
    with pytest.raises(ValueError):
        settle_totals(Selection.HOME, 2.5, 2, 1)


def test_f19_handicap_rejects_a_non_handicap_selection_instead_of_treating_it_as_away():
    with pytest.raises(ValueError):
        settle_handicap(Selection.OVER, -1.0, 2, 1)


def test_f19_handicap_rejects_a_line_not_aligned_to_a_quarter_point():
    with pytest.raises(ValueError):
        settle_handicap(Selection.HOME, -0.6, 2, 1)  # not a valid quarter-point line


def test_f19_handicap_accepts_a_line_that_is_a_quarter_point_up_to_float_rounding():
    # 0.1 + 0.15 = 0.25000000000000003 in float - must not be rejected
    # just because it isn't bit-exact.
    line = 0.1 + 0.15
    assert settle_handicap(Selection.HOME, -line, 2, 1) == SettlementResult.WON
