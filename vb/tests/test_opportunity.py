from datetime import datetime, timedelta, timezone

from vb.models import MarketType, Selection
from vb.opportunity import LegReading, MovementSource, OpportunityTracker, ResolutionReason

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _tracker(market_key="m1"):
    return OpportunityTracker(
        market_key=market_key,
        sport="soccer",
        benchmark_site="pinnacle.com",
        comparison_site="swisslos.ch",
        market_type=MarketType.MATCH_WINNER,
        line=None,
        selection=Selection.HOME,
    )


def _reading(minute, edge_a, edge_b, benchmark_odds, comparison_odds, **kwargs):
    return LegReading(
        captured_at=T0 + timedelta(minutes=minute),
        edge_a=edge_a,
        edge_b=edge_b,
        benchmark_odds=benchmark_odds,
        comparison_odds=comparison_odds,
        **kwargs,
    )


def test_readings_below_threshold_never_open_an_opportunity():
    tracker = _tracker()
    for m in range(3):
        tracker.ingest(_reading(m, 0.01, 0.01, 2.00, 2.02))

    assert tracker.completed == []
    assert tracker._open is None


def test_full_lifecycle_trajectory_peak_and_convergence():
    tracker = _tracker()

    tracker.ingest(_reading(0, 0.04, 0.03, 2.00, 2.10))          # first cross
    tracker.ingest(_reading(1, 0.06, 0.05, 2.00, 2.15))          # comparison moves further
    tracker.ingest(_reading(2, 0.10, 0.09, 2.00, 2.25))          # peak; comparison still moving
    tracker.ingest(_reading(3, 0.05, 0.04, 2.05, 2.20))          # benchmark catches up too (both moved)
    tracker.ingest(_reading(4, 0.02, 0.01, 2.05, 2.10))          # converges below threshold

    assert tracker.completed and tracker._open is None
    opp = tracker.completed[0]

    assert opp.instance_id == "m1#1"
    assert opp.market_key == "m1"
    assert len(opp.snapshots) == 5  # every reading logged, including the closing one
    assert opp.entry_edge_a == 0.04
    assert opp.entry_edge_b == 0.03
    assert opp.peak_edge_a == 0.10
    assert opp.time_to_peak == timedelta(minutes=2)
    assert opp.convergence_time == timedelta(minutes=4)
    assert opp.resolution_reason == ResolutionReason.DROPPED_BELOW_THRESHOLD
    assert not opp.is_open

    # movement attribution: first snapshot has nothing to compare against
    assert opp.snapshots[0].movement_source == MovementSource.NEITHER
    assert opp.snapshots[1].movement_source == MovementSource.COMPARISON
    assert opp.snapshots[2].movement_source == MovementSource.COMPARISON
    assert opp.snapshots[3].movement_source == MovementSource.BOTH
    assert opp.snapshots[4].movement_source == MovementSource.COMPARISON


def test_sampling_continues_past_far_higher_thresholds():
    tracker = _tracker()
    tracker.ingest(_reading(0, 0.031, 0.03, 2.00, 2.06))
    tracker.ingest(_reading(1, 0.20, 0.19, 2.00, 2.40))  # far past any realistic bet threshold
    tracker.ingest(_reading(2, 0.25, 0.24, 2.00, 2.50))

    assert tracker._open is not None
    assert len(tracker._open.snapshots) == 3  # nothing got skipped once it "already qualified"


def test_recross_creates_new_linked_instance():
    tracker = _tracker()
    tracker.ingest(_reading(0, 0.04, 0.03, 2.00, 2.10))
    tracker.ingest(_reading(1, 0.02, 0.01, 2.00, 2.02))  # converge, closes #1

    tracker.ingest(_reading(5, 0.02, 0.01, 2.00, 2.02))  # still below, no-op
    tracker.ingest(_reading(6, 0.05, 0.04, 2.00, 2.10))  # re-cross, opens #2

    assert len(tracker.completed) == 1
    assert tracker.completed[0].instance_id == "m1#1"
    assert tracker._open is not None
    assert tracker._open.instance_id == "m1#2"
    assert tracker._open.market_key == "m1"  # linked via shared market_key


def test_market_suspended_closes_opportunity():
    tracker = _tracker()
    tracker.ingest(_reading(0, 0.04, 0.03, 2.00, 2.10))
    tracker.ingest(_reading(1, 0.05, 0.04, 2.00, 2.12, market_suspended=True))

    assert tracker._open is None
    assert tracker.completed[0].resolution_reason == ResolutionReason.MARKET_SUSPENDED


def test_event_started_closes_opportunity():
    tracker = _tracker()
    tracker.ingest(_reading(0, 0.04, 0.03, 2.00, 2.10))
    tracker.ingest(_reading(1, 0.05, 0.04, 2.00, 2.12, event_started=True))

    assert tracker._open is None
    assert tracker.completed[0].resolution_reason == ResolutionReason.EVENT_STARTED


def test_resume_continues_the_same_instance_across_a_fresh_tracker():
    original = _tracker()
    original.ingest(_reading(0, 0.04, 0.03, 2.00, 2.10))
    original.ingest(_reading(1, 0.06, 0.05, 2.00, 2.15))
    still_open = original._open
    assert still_open is not None

    resumed = OpportunityTracker.resume(still_open)
    resumed.ingest(_reading(2, 0.02, 0.01, 2.00, 2.02))  # converges, closes

    assert resumed._open is None
    closed = resumed.completed[0]
    assert closed.instance_id == "m1#1"  # same instance, not a new #2
    assert len(closed.snapshots) == 3  # the 2 resumed + the 1 new
    assert closed.entry_edge_a == 0.04  # entry preserved from before resume
    assert closed.resolution_reason == ResolutionReason.DROPPED_BELOW_THRESHOLD
    # movement attribution continues correctly against the pre-resume reading
    assert closed.snapshots[-1].movement_source == MovementSource.COMPARISON


def test_resume_rejects_already_resolved_opportunity():
    import pytest

    tracker = _tracker()
    tracker.ingest(_reading(0, 0.04, 0.03, 2.00, 2.10))
    tracker.ingest(_reading(1, 0.01, 0.01, 2.00, 2.02))  # closes
    closed = tracker.completed[0]

    with pytest.raises(ValueError):
        OpportunityTracker.resume(closed)


def test_ingest_ignores_a_reading_no_newer_than_the_last_one():
    # Reproduces a real bug found live: re-running the pipeline against
    # data that hasn't been re-captured yet fed the exact same reading
    # (same captured_at) repeatedly, padding the trajectory with dozens
    # of identical duplicate snapshots.
    tracker = _tracker()
    tracker.ingest(_reading(0, 0.04, 0.03, 2.00, 2.10))
    tracker.ingest(_reading(1, 0.06, 0.05, 2.00, 2.15))

    same_reading = _reading(1, 0.06, 0.05, 2.00, 2.15)  # same captured_at as last time
    for _ in range(5):
        tracker.ingest(same_reading)

    assert len(tracker._open.snapshots) == 2  # none of the repeats were appended


def test_ingest_ignores_a_reading_older_than_the_last_one():
    tracker = _tracker()
    tracker.ingest(_reading(5, 0.04, 0.03, 2.00, 2.10))
    tracker.ingest(_reading(3, 0.06, 0.05, 2.00, 2.15))  # captured_at earlier than the previous reading

    assert len(tracker._open.snapshots) == 1


def test_ingest_stale_reading_before_any_opportunity_opens_is_ignored():
    tracker = _tracker()
    tracker.ingest(_reading(5, 0.01, 0.01, 2.00, 2.02))  # below threshold, just updates _last_reading
    tracker.ingest(_reading(3, 0.05, 0.04, 2.00, 2.10))  # older timestamp, above threshold

    assert tracker._open is None  # never opened - the stale reading was ignored entirely
