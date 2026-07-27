import math
from datetime import datetime, timedelta, timezone

from vb.evaluation import flat_stake_profit
from vb.evaluation_v2 import (
    ExecutedBet,
    average_clv,
    average_slippage,
    build_report,
    clustered_roi_confidence_interval,
    event_level_counts,
    exposure_by_site_or_event,
    max_drawdown,
    rejection_rate,
)
from vb.models import ExecutionStatus
from vb.settlement import SettlementResult
from vb.storage import init_db

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    return init_db(tmp_path / "vb.sqlite")


def _bet(event_id, status=ExecutionStatus.ACCEPTED, requested_odds=2.30, accepted_odds=2.30,
         accepted_stake=1.0, outcome=SettlementResult.WON, consensus=None, decided_at=T0):
    return ExecutedBet(
        strategy_version="strategy-a", canonical_event_id=event_id, decided_at=decided_at,
        execution_status=status, requested_odds=requested_odds, accepted_odds=accepted_odds,
        accepted_stake=accepted_stake, outcome=outcome, consensus_closing_odds=consensus,
    )


def test_event_level_counts():
    bets = [_bet("e1"), _bet("e1"), _bet("e2")]
    counts = event_level_counts(bets)
    assert counts == {"total_decisions": 3, "unique_events": 2, "settled_bets": 3}


def test_rejection_rate():
    bets = [_bet("e1", status=ExecutionStatus.ACCEPTED), _bet("e2", status=ExecutionStatus.REJECTED)]
    assert rejection_rate(bets) == 0.5


def test_rejection_rate_empty_is_nan():
    assert math.isnan(rejection_rate([]))


def test_average_clv_only_counts_bets_with_known_consensus():
    bets = [
        _bet("e1", accepted_odds=2.30, consensus=2.10),  # CLV positive
        _bet("e2", accepted_odds=2.00, consensus=None),  # excluded - no consensus known
    ]
    clv = average_clv(bets)
    assert clv is not None
    assert clv > 0


def test_average_clv_is_none_when_no_bet_has_a_known_consensus():
    bets = [_bet("e1", consensus=None)]
    assert average_clv(bets) is None


def test_average_slippage_reflects_the_price_move_direction():
    bets = [_bet("e1", requested_odds=2.30, accepted_odds=2.10)]  # worse price -> negative slippage
    slippage = average_slippage(bets)
    assert slippage is not None
    assert slippage < 0


def test_max_drawdown_is_zero_when_profit_only_ever_climbs():
    bets = [
        _bet("e1", outcome=SettlementResult.WON, decided_at=T0),
        _bet("e2", outcome=SettlementResult.WON, decided_at=T0 + timedelta(hours=1)),
    ]
    assert max_drawdown(bets) == 0.0


def test_max_drawdown_measures_the_decline_from_the_running_peak():
    # WON (+1.30), then three LOST (-1.0 each) - peak is 1.30, trough is
    # 1.30 - 3.0 = -1.70, so drawdown magnitude is 3.0
    bets = [
        _bet("e1", outcome=SettlementResult.WON, decided_at=T0),
        _bet("e2", outcome=SettlementResult.LOST, decided_at=T0 + timedelta(hours=1)),
        _bet("e3", outcome=SettlementResult.LOST, decided_at=T0 + timedelta(hours=2)),
        _bet("e4", outcome=SettlementResult.LOST, decided_at=T0 + timedelta(hours=3)),
    ]
    assert math.isclose(max_drawdown(bets), -3.0, abs_tol=1e-9)


def test_exposure_by_site_or_event_groups_stake_correctly():
    bets = [_bet("e1", accepted_stake=1.0), _bet("e1", accepted_stake=2.0), _bet("e2", accepted_stake=5.0)]
    exposure = exposure_by_site_or_event(bets, lambda b: b.canonical_event_id)
    assert exposure == {"e1": 3.0, "e2": 5.0}


def test_clustered_roi_confidence_interval_point_estimate_matches_direct_calculation():
    bets = [_bet("e1", accepted_odds=2.30, accepted_stake=1.0, outcome=SettlementResult.WON)]
    point, lower, upper = clustered_roi_confidence_interval(bets, n_bootstrap=100)
    expected = flat_stake_profit(SettlementResult.WON, 2.30, 1.0) / 1.0
    assert math.isclose(point, expected, abs_tol=1e-9)
    assert lower <= point <= upper


def test_clustered_roi_confidence_interval_empty_bets_returns_nan():
    point, lower, upper = clustered_roi_confidence_interval([])
    assert math.isnan(point) and math.isnan(lower) and math.isnan(upper)


def test_clustered_roi_widens_with_more_event_dispersion():
    # all bets on ONE event -> the bootstrap can only ever resample
    # that one event, so the interval should collapse to a point;
    # many independent events with mixed outcomes should show real
    # spread.
    one_event = [_bet("e1", outcome=SettlementResult.WON, accepted_stake=1.0)] * 5
    _, lo1, hi1 = clustered_roi_confidence_interval(one_event, n_bootstrap=200)

    mixed_events = [
        _bet(f"e{i}", outcome=(SettlementResult.WON if i % 2 == 0 else SettlementResult.LOST), accepted_stake=1.0)
        for i in range(10)
    ]
    _, lo2, hi2 = clustered_roi_confidence_interval(mixed_events, n_bootstrap=200)

    assert math.isclose(hi1 - lo1, 0.0, abs_tol=1e-9)
    assert (hi2 - lo2) > (hi1 - lo1)


def test_build_report_total_profit_equals_the_sum_of_row_level_profit(tmp_path):
    conn = _db(tmp_path)
    bets = [
        _bet("e1", accepted_odds=2.30, accepted_stake=1.0, outcome=SettlementResult.WON),
        _bet("e2", accepted_odds=2.00, accepted_stake=1.0, outcome=SettlementResult.LOST),
        _bet("e3", status=ExecutionStatus.REJECTED, accepted_odds=None, accepted_stake=None, outcome=None),
    ]

    report = build_report(
        conn, bets, strategy_version="strategy-a", code_sha="abc123", config={"threshold": 0.03},
        db_snapshot_hash="deadbeef", data_cutoff=T0, created_at=T0,
    )

    expected_profit = (
        flat_stake_profit(SettlementResult.WON, 2.30, 1.0) + flat_stake_profit(SettlementResult.LOST, 2.00, 1.0)
    )
    assert math.isclose(report["metrics"]["total_profit"], expected_profit, abs_tol=1e-9)
    assert report["metrics"]["total_decisions"] == 3
    assert report["metrics"]["settled_bets"] == 2

    row = conn.execute("SELECT code_sha, config_hash, db_snapshot_hash FROM evaluation_run WHERE id = ?", (report["evaluation_run_id"],)).fetchone()
    assert row == ("abc123", report["config_hash"], "deadbeef")
