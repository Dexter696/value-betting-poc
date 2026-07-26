import math
from datetime import datetime, timezone

from vb.edge import devig_proportional
from vb.fair_probability import (
    brier_score,
    calibration_report,
    devig_odds_ratio,
    devig_power,
    fair_edge,
    log_loss,
    source_dispersion,
)
from vb.models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection

FAVORITE_LONGSHOT_ODDS = [2.10, 3.40, 3.60]  # a real, mildly-vigged market


def test_devig_power_sums_to_one():
    probs = devig_power(FAVORITE_LONGSHOT_ODDS)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)


def test_devig_odds_ratio_sums_to_one():
    probs = devig_odds_ratio(FAVORITE_LONGSHOT_ODDS)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)


def test_power_and_odds_ratio_correct_favorite_longshot_bias_in_the_expected_direction():
    # Both methods assume the bookmaker's margin is loaded more heavily
    # onto longshots than favorites - correcting for that should give
    # the favorite (odds 2.10, index 0) a HIGHER fair probability than
    # plain proportional de-vig gives it, and the longshot (odds 3.60,
    # index 2) a correspondingly LOWER one.
    proportional = devig_proportional(FAVORITE_LONGSHOT_ODDS)
    power = devig_power(FAVORITE_LONGSHOT_ODDS)
    odds_ratio = devig_odds_ratio(FAVORITE_LONGSHOT_ODDS)

    for adjusted in (power, odds_ratio):
        assert adjusted[0] > proportional[0]  # favorite: higher
        assert adjusted[2] < proportional[2]  # longshot: lower


def test_devig_power_matches_proportional_when_there_is_no_overround():
    fair_odds = [2.0, 2.0]  # implied probs sum to exactly 1.0 already
    power = devig_power(fair_odds)
    assert math.isclose(power[0], 0.5, abs_tol=1e-6)
    assert math.isclose(power[1], 0.5, abs_tol=1e-6)


def _market(home, draw, away):
    event = RawEvent(
        site="pinnacle.com", sport="soccer", competition="Premier League",
        kickoff_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
        raw_home_team="Liverpool", raw_away_team="Everton", event_id="p1",
    )
    return MarketSnapshot(
        event=event, market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(Outcome(Selection.HOME, home), Outcome(Selection.DRAW, draw), Outcome(Selection.AWAY, away)),
        captured_at=event.kickoff_utc,
    )


def test_fair_edge_dispatches_to_the_requested_devig_method():
    market = _market(2.10, 3.40, 3.60)
    proportional_edge = fair_edge(market, Selection.HOME, comparison_odds=2.30, method="proportional")
    power_edge = fair_edge(market, Selection.HOME, comparison_odds=2.30, method="power")

    assert proportional_edge != power_edge
    # sanity: matches vb.edge.devigged_edge's own value for the proportional method
    from vb.edge import devigged_edge
    assert math.isclose(proportional_edge, devigged_edge(market, Selection.HOME, 2.30), abs_tol=1e-9)


def test_log_loss_penalizes_confident_wrong_predictions_more_than_unsure_ones():
    confident_wrong = log_loss(0.99, outcome_occurred=False)
    unsure_wrong = log_loss(0.6, outcome_occurred=False)
    assert confident_wrong > unsure_wrong


def test_log_loss_is_near_zero_for_a_confident_correct_prediction():
    assert log_loss(0.999, outcome_occurred=True) < 0.01


def test_brier_score_is_zero_for_a_perfect_prediction():
    assert brier_score(1.0, outcome_occurred=True) == 0.0
    assert brier_score(0.0, outcome_occurred=False) == 0.0


def test_brier_score_is_worse_for_a_confidently_wrong_prediction():
    assert brier_score(0.9, outcome_occurred=False) > brier_score(0.5, outcome_occurred=False)


def test_calibration_report_buckets_predictions_and_reports_predicted_vs_observed():
    predictions = [
        (0.1, False), (0.15, False), (0.12, True),  # low bucket - mostly didn't happen
        (0.85, True), (0.9, True), (0.82, False),    # high bucket - mostly happened
    ]
    report = calibration_report(predictions, bucket_edges=[0.5])

    low, high = report
    assert low.n == 3
    assert high.n == 3
    assert low.observed_rate < high.observed_rate
    assert math.isclose(low.predicted_mean, (0.1 + 0.15 + 0.12) / 3, abs_tol=1e-9)


def test_source_dispersion_is_zero_when_all_sources_agree():
    assert source_dispersion([0.45, 0.45, 0.45]) == 0.0


def test_source_dispersion_is_positive_when_sources_disagree():
    assert source_dispersion([0.30, 0.45, 0.60]) > 0.0
