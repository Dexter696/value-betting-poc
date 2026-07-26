from datetime import datetime, timedelta, timezone

from vb.execution import LatencyModel, idempotency_key, record_decision, settled_profit, verify_and_execute
from vb.identity import content_hash, new_id
from vb.models import (
    BetDecisionChoice,
    CaptureRun,
    EventVersionV2,
    ExecutionStatus,
    MarketSnapshotV2,
    MarketType,
    Outcome,
    Selection,
    SignalObservation,
    SourceRun,
    StrategyDefinition,
)
from vb.settlement import SettlementResult
from vb.storage import (
    get_or_create_strategy_definition,
    init_db,
    save_capture_run,
    save_event_version,
    save_market_snapshot_v2,
    save_signal_observation,
    save_source_run,
)

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
MARKET_IDENTITY = "canonical-1:match_winner:None:home:vs:swisslos.ch"


def _db(tmp_path):
    return init_db(tmp_path / "vb.sqlite")


def _strategy_id(conn):
    config = {"threshold": 0.03}
    s = StrategyDefinition(
        id=new_id(), signal_model="raw-v1", threshold=0.03, max_age_s=90, max_skew_s=60,
        min_lead_time_s=300, config=config, config_hash=content_hash(config), created_at=T0,
    )
    return get_or_create_strategy_definition(conn, s)


def _real_snapshot(conn):
    run_id = new_id()
    save_capture_run(conn, CaptureRun(id=run_id, started_at=T0, git_sha="test", schema_version=2))
    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=run_id, site="pinnacle.com", mode="quick", started_at=T0))
    ev_id = new_id()
    save_event_version(conn, EventVersionV2(
        id=ev_id, site="pinnacle.com", event_id="e1", valid_from=T0, source_run_id=source_run_id,
        sport="soccer", competition="Test League", kickoff_utc=T0 + timedelta(hours=3), home_team="A", away_team="B",
    ))
    snap_id = new_id()
    save_market_snapshot_v2(conn, MarketSnapshotV2(
        id=snap_id, source_run_id=source_run_id, event_version_id=ev_id, market_type=MarketType.MATCH_WINNER,
        line=None, outcomes=(Outcome(Selection.HOME, 2.0), Outcome(Selection.DRAW, 3.4), Outcome(Selection.AWAY, 3.6)),
        received_at=T0,
    ))
    return snap_id


def _real_observation(conn):
    snap = _real_snapshot(conn)
    obs_id = new_id()
    save_signal_observation(conn, SignalObservation(
        id=obs_id, decision_time=T0, benchmark_snapshot_id=snap, comparison_snapshot_id=snap,
        edge_model="raw-v1", edge=0.05, eligible=True, episode_id=None,
    ))
    return obs_id


def _record(conn, strategy_id, obs_id=None):
    return record_decision(
        conn, strategy_id, MARKET_IDENTITY, obs_id or _real_observation(conn), decided_at=T0,
        decision=BetDecisionChoice.BET, reason="test", intended_odds=2.30, intended_stake=1.0,
    )


def test_idempotency_key_is_deterministic_for_the_same_pair():
    key1 = idempotency_key("strategy-a", MARKET_IDENTITY)
    key2 = idempotency_key("strategy-a", MARKET_IDENTITY)
    assert key1 == key2


def test_idempotency_key_differs_across_strategies_or_markets():
    assert idempotency_key("strategy-a", MARKET_IDENTITY) != idempotency_key("strategy-b", MARKET_IDENTITY)
    assert idempotency_key("strategy-a", "other-market") != idempotency_key("strategy-a", MARKET_IDENTITY)


def test_record_decision_inserts_a_real_row(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    decision_id = _record(conn, strategy_id)

    assert decision_id is not None
    row = conn.execute("SELECT decision, reason, idempotency_key FROM bet_decision WHERE id = ?", (decision_id,)).fetchone()
    assert row[0] == "bet"


def test_record_decision_is_idempotent_a_second_call_returns_none(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    first = _record(conn, strategy_id)
    second = _record(conn, strategy_id)  # different observation, SAME market+strategy

    assert first is not None
    assert second is None
    count = conn.execute("SELECT COUNT(*) FROM bet_decision").fetchone()[0]
    assert count == 1


def test_verify_and_execute_accepts_when_the_price_held(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    decision_id = _record(conn, strategy_id)

    execution = verify_and_execute(
        conn, decision_id, requested_at=T0, requested_odds=2.30, requested_stake=1.0,
        fetch_current_odds=lambda: 2.30, latency=LatencyModel(delay=timedelta(seconds=5)),
    )

    assert execution.status == ExecutionStatus.ACCEPTED
    assert execution.accepted_odds == 2.30
    assert execution.responded_at == T0 + timedelta(seconds=5)


def test_verify_and_execute_caps_accepted_odds_at_requested_even_if_price_improved(tmp_path):
    # Conservative-slippage convention (Phase 5 step 7): never credit a
    # favorable price move the live system can't be certain it would
    # have actually captured.
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    decision_id = _record(conn, strategy_id)

    execution = verify_and_execute(
        conn, decision_id, requested_at=T0, requested_odds=2.30, requested_stake=1.0,
        fetch_current_odds=lambda: 2.50, latency=LatencyModel(delay=timedelta(seconds=5)),
    )

    assert execution.status == ExecutionStatus.ACCEPTED
    assert execution.accepted_odds == 2.30  # capped, not 2.50


def test_verify_and_execute_applies_unfavorable_slippage_in_full(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    decision_id = _record(conn, strategy_id)

    execution = verify_and_execute(
        conn, decision_id, requested_at=T0, requested_odds=2.30, requested_stake=1.0,
        fetch_current_odds=lambda: 2.10, latency=LatencyModel(delay=timedelta(seconds=5)),
    )

    assert execution.status == ExecutionStatus.PRICE_CHANGED
    assert execution.accepted_odds == 2.10


def test_verify_and_execute_rejects_when_the_market_is_gone(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    decision_id = _record(conn, strategy_id)

    execution = verify_and_execute(
        conn, decision_id, requested_at=T0, requested_odds=2.30, requested_stake=1.0,
        fetch_current_odds=lambda: None, latency=LatencyModel(delay=timedelta(seconds=5)),
    )

    assert execution.status == ExecutionStatus.REJECTED
    assert execution.accepted_odds is None

    row = conn.execute("SELECT status, accepted_odds FROM bet_execution WHERE id = ?", (execution.id,)).fetchone()
    assert row == ("rejected", None)


def test_settled_profit_uses_accepted_odds_not_requested_odds(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    decision_id = _record(conn, strategy_id)
    execution = verify_and_execute(
        conn, decision_id, requested_at=T0, requested_odds=2.30, requested_stake=1.0,
        fetch_current_odds=lambda: 2.10, latency=LatencyModel(delay=timedelta(seconds=5)),
    )

    profit = settled_profit(execution, SettlementResult.WON)

    assert profit == 1.10  # (2.10 - 1) * 1.0 stake, NOT (2.30-1)


def test_settled_profit_is_zero_for_a_rejected_execution(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    decision_id = _record(conn, strategy_id)
    execution = verify_and_execute(
        conn, decision_id, requested_at=T0, requested_odds=2.30, requested_stake=1.0,
        fetch_current_odds=lambda: None, latency=LatencyModel(delay=timedelta(seconds=5)),
    )

    assert settled_profit(execution, SettlementResult.WON) == 0.0
