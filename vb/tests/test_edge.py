from datetime import datetime, timezone

from vb.edge import devig_proportional, devigged_edge, overround, raw_edge
from vb.models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _event():
    return RawEvent(
        site="pinnacle.com", sport="soccer", competition="Test League",
        kickoff_utc=T0, raw_home_team="Home", raw_away_team="Away", event_id="1",
    )


def test_overround_of_a_fair_market_is_zero():
    assert round(overround([2.0, 2.0]), 6) == 0.0


def test_overround_of_a_vigged_market_is_positive():
    # 2.10 / 3.40 / 3.60 -> implied sum > 1
    ov = overround([2.10, 3.40, 3.60])
    assert ov > 0
    assert round(ov, 4) == round(1 / 2.10 + 1 / 3.40 + 1 / 3.60 - 1, 4)


def test_devig_proportional_sums_to_one():
    fair = devig_proportional([2.10, 3.40, 3.60])
    assert abs(sum(fair) - 1.0) < 1e-9


def test_raw_edge_zero_when_comparison_matches_benchmark():
    assert round(raw_edge(2.0, 2.0), 6) == 0.0


def test_raw_edge_positive_when_comparison_offers_more():
    # benchmark implies 50%; comparison pays 2.10 -> 5% edge
    assert round(raw_edge(2.0, 2.10), 4) == 0.05


def test_raw_edge_overstates_value_vs_devigged_on_a_vigged_market():
    # Same scenario as the methodology's stated concern: Method A treats
    # the benchmark's own (margin-inflated) odds as fair, so it reports a
    # bigger edge than Method B once the benchmark's margin is removed.
    benchmark_market = MarketSnapshot(
        event=_event(), market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(
            Outcome(Selection.HOME, 2.10),
            Outcome(Selection.DRAW, 3.40),
            Outcome(Selection.AWAY, 3.60),
        ),
        captured_at=T0,
    )
    comparison_odds = 2.30

    a = raw_edge(2.10, comparison_odds)
    b = devigged_edge(benchmark_market, Selection.HOME, comparison_odds)

    assert a > b > 0
    assert round(a, 4) == 0.0952
    assert round(b, 4) == 0.045


def test_devigged_edge_can_be_negative_when_comparison_underpays():
    benchmark_market = MarketSnapshot(
        event=_event(), market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(
            Outcome(Selection.HOME, 2.10),
            Outcome(Selection.DRAW, 3.40),
            Outcome(Selection.AWAY, 3.60),
        ),
        captured_at=T0,
    )
    b = devigged_edge(benchmark_market, Selection.HOME, 1.90)
    assert b < 0
