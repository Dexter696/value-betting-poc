from datetime import datetime, timedelta, timezone

import math

import pytest

from vb.closing import (
    SourceClosingPrice,
    closing_line_value,
    consensus_closing_odds,
    process_cycle_closings,
    record_closing_for_episode,
    record_closing_snapshot,
)
from vb.episode import EpisodeTracker, LegReadingV2
from vb.identity import content_hash, new_id
from vb.models import (
    CanonicalEvent,
    CaptureRun,
    EventVersionV2,
    MarketSnapshotV2,
    MarketType,
    Outcome,
    RawEvent,
    RunStatus,
    Selection,
    SourceRun,
    StrategyDefinition,
)
from vb.pipeline import market_key
from vb.storage import (
    finish_source_run,
    get_or_create_strategy_definition,
    init_db,
    list_signal_observations,
    save_canonical_event,
    save_capture_run,
    save_event_version,
    save_market_snapshot_v2,
    save_source_run,
)

T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
BENCHMARK_EVENT = RawEvent(
    site="pinnacle.com", sport="soccer", competition="Premier League", kickoff_utc=T0,
    raw_home_team="Liverpool", raw_away_team="Everton", event_id="p1",
)
MARKET_IDENTITY = market_key(BENCHMARK_EVENT, MarketType.MATCH_WINNER, None, Selection.HOME, "swisslos.ch")


def _db(tmp_path):
    return init_db(tmp_path / "vb.sqlite")


def _strategy_id(conn):
    config = {"threshold": 0.03}
    s = StrategyDefinition(
        id=new_id(), signal_model="raw-v1", threshold=0.03, max_age_s=90, max_skew_s=60,
        min_lead_time_s=300, config=config, config_hash=content_hash(config), created_at=T0,
    )
    return get_or_create_strategy_definition(conn, s)


def _snapshot(conn, at, site, odds):
    run_id = new_id()
    save_capture_run(conn, CaptureRun(id=run_id, started_at=at, git_sha="test", schema_version=2))
    source_run_id = new_id()
    save_source_run(conn, SourceRun(id=source_run_id, capture_run_id=run_id, site=site, mode="quick", started_at=at))
    finish_source_run(conn, source_run_id, RunStatus.SUCCESS, at, event_count=1, snapshot_count=1)
    event_version_id = new_id()
    save_event_version(conn, EventVersionV2(
        id=event_version_id, site=site, event_id="p1" if site == "pinnacle.com" else "s1", valid_from=at,
        source_run_id=source_run_id, sport="soccer", competition="Premier League", kickoff_utc=T0,
        home_team="Liverpool", away_team="Everton",
    ))
    snap_id = new_id()
    save_market_snapshot_v2(conn, MarketSnapshotV2(
        id=snap_id, source_run_id=source_run_id, event_version_id=event_version_id, market_type=MarketType.MATCH_WINNER,
        line=None, outcomes=(Outcome(Selection.HOME, odds), Outcome(Selection.DRAW, 3.4), Outcome(Selection.AWAY, 3.6)),
        received_at=at,
    ))
    return snap_id


def test_consensus_closing_odds_is_the_median_not_the_mean():
    prices = [SourceClosingPrice("a", 2.00), SourceClosingPrice("b", 2.10), SourceClosingPrice("c", 5.00)]
    # median of [2.00, 2.10, 5.00] is 2.10 - the mean (~3.03) would be
    # dragged far off by the one outlier
    assert consensus_closing_odds(prices) == 2.10


def test_consensus_closing_odds_averages_the_middle_two_for_an_even_count():
    prices = [SourceClosingPrice("a", 2.00), SourceClosingPrice("b", 2.20)]
    assert consensus_closing_odds(prices) == 2.10


def test_consensus_closing_odds_requires_at_least_one_price():
    with pytest.raises(ValueError):
        consensus_closing_odds([])


def test_closing_line_value_is_positive_when_accepted_price_was_better():
    assert closing_line_value(accepted_odds=2.30, consensus_odds=2.10) > 0


def test_closing_line_value_is_negative_when_accepted_price_was_worse():
    assert closing_line_value(accepted_odds=2.00, consensus_odds=2.10) < 0


def test_closing_line_value_is_zero_when_accepted_price_matches_consensus():
    assert closing_line_value(accepted_odds=2.10, consensus_odds=2.10) == 0.0


def test_record_closing_snapshot_persists_the_consensus_and_every_source(tmp_path):
    conn = _db(tmp_path)
    canonical_id = new_id()
    save_canonical_event(conn, CanonicalEvent(id=canonical_id, sport="soccer", created_at=T0))

    prices = [SourceClosingPrice("swisslos.ch", 2.10), SourceClosingPrice("loro.ch", 2.20)]
    snapshot_id = record_closing_snapshot(
        conn, canonical_id, MarketType.MATCH_WINNER, None, Selection.HOME, T0, prices,
    )

    row = conn.execute(
        "SELECT canonical_event_id, consensus_odds, source_json FROM closing_snapshot WHERE id = ?", (snapshot_id,)
    ).fetchone()
    assert row[0] == canonical_id
    assert math.isclose(row[1], 2.15, abs_tol=1e-9)  # median of two -> average of both
    assert "swisslos.ch" in row[2] and "loro.ch" in row[2]


def test_record_closing_for_episode_uses_the_benchmark_and_comparison_prices_from_the_closing_observation(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    bench_snap = _snapshot(conn, T0, "pinnacle.com", 2.10)
    comp_snap = _snapshot(conn, T0, "swisslos.ch", 2.30)
    open_result = tracker.ingest(LegReadingV2(
        received_at=T0, edge=0.05, benchmark_snapshot_id=bench_snap, comparison_snapshot_id=comp_snap, edge_model="raw-v1",
    ))
    assert open_result.opened

    # market genuinely stops trading - event_started closes the episode
    closing_bench_snap = _snapshot(conn, T0 + timedelta(minutes=1), "pinnacle.com", 2.05)
    closing_comp_snap = _snapshot(conn, T0 + timedelta(minutes=1), "swisslos.ch", 2.25)
    close_result = tracker.ingest(LegReadingV2(
        received_at=T0 + timedelta(minutes=1), edge=0.05, benchmark_snapshot_id=closing_bench_snap,
        comparison_snapshot_id=closing_comp_snap, edge_model="raw-v1", event_started=True,
    ))
    assert close_result.closed

    observations = list_signal_observations(conn, close_result.episode_id)
    closing_observation = next(o for o in observations if o.id == close_result.observation_id)

    snapshot_id = record_closing_for_episode(conn, close_result.episode_id, closing_observation, now=T0 + timedelta(minutes=1))

    assert snapshot_id is not None
    row = conn.execute("SELECT consensus_odds, source_json FROM closing_snapshot WHERE id = ?", (snapshot_id,)).fetchone()
    assert math.isclose(row[0], (2.05 + 2.25) / 2, abs_tol=1e-9)
    assert "pinnacle.com" in row[1] and "swisslos.ch" in row[1]


def test_record_closing_for_episode_is_none_when_the_episode_closed_for_a_different_reason(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    bench_snap = _snapshot(conn, T0, "pinnacle.com", 2.10)
    comp_snap = _snapshot(conn, T0, "swisslos.ch", 2.30)
    open_result = tracker.ingest(LegReadingV2(
        received_at=T0, edge=0.05, benchmark_snapshot_id=bench_snap, comparison_snapshot_id=comp_snap, edge_model="raw-v1",
    ))

    # edge drops below threshold - NOT a real market close, just our
    # tracking of it ending
    drop_bench_snap = _snapshot(conn, T0 + timedelta(minutes=1), "pinnacle.com", 2.10)
    drop_comp_snap = _snapshot(conn, T0 + timedelta(minutes=1), "swisslos.ch", 2.11)
    close_result = tracker.ingest(LegReadingV2(
        received_at=T0 + timedelta(minutes=1), edge=0.01, benchmark_snapshot_id=drop_bench_snap,
        comparison_snapshot_id=drop_comp_snap, edge_model="raw-v1",
    ))
    assert close_result.closed

    observations = list_signal_observations(conn, close_result.episode_id)
    closing_observation = next(o for o in observations if o.id == close_result.observation_id)

    snapshot_id = record_closing_for_episode(conn, close_result.episode_id, closing_observation, now=T0 + timedelta(minutes=1))

    assert snapshot_id is None
    assert conn.execute("SELECT COUNT(*) FROM closing_snapshot").fetchone()[0] == 0


def test_process_cycle_closings_counts_only_real_event_started_closes(tmp_path):
    conn = _db(tmp_path)
    strategy_id = _strategy_id(conn)
    tracker = EpisodeTracker(conn, strategy_id, MARKET_IDENTITY, threshold=0.03)

    bench_snap = _snapshot(conn, T0, "pinnacle.com", 2.10)
    comp_snap = _snapshot(conn, T0, "swisslos.ch", 2.30)
    open_result = tracker.ingest(LegReadingV2(
        received_at=T0, edge=0.05, benchmark_snapshot_id=bench_snap, comparison_snapshot_id=comp_snap, edge_model="raw-v1",
    ))

    closing_bench_snap = _snapshot(conn, T0 + timedelta(minutes=1), "pinnacle.com", 2.05)
    closing_comp_snap = _snapshot(conn, T0 + timedelta(minutes=1), "swisslos.ch", 2.25)
    close_result = tracker.ingest(LegReadingV2(
        received_at=T0 + timedelta(minutes=1), edge=0.05, benchmark_snapshot_id=closing_bench_snap,
        comparison_snapshot_id=closing_comp_snap, edge_model="raw-v1", event_started=True,
    ))

    recorded = process_cycle_closings(conn, [open_result, close_result], now=T0 + timedelta(minutes=1))

    assert recorded == 1
    assert conn.execute("SELECT COUNT(*) FROM closing_snapshot").fetchone()[0] == 1
