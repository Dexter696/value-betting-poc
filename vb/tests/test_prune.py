from datetime import datetime, timedelta, timezone

from vb.models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection
from vb.storage import init_db, prune_raw_snapshots, save_raw_capture

NOW = datetime.now(timezone.utc)


def _event(event_id="e1"):
    return RawEvent(
        site="pinnacle.com", sport="soccer", competition="Test League",
        kickoff_utc=NOW, raw_home_team="A", raw_away_team="B", event_id=event_id,
    )


def _snapshot(event, captured_at, home_odds=2.0):
    return MarketSnapshot(
        event=event, market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(Outcome(Selection.HOME, home_odds), Outcome(Selection.DRAW, 3.3), Outcome(Selection.AWAY, 3.9)),
        captured_at=captured_at,
    )


def test_prune_deletes_old_rows_keeps_latest_per_key(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    event = _event()

    old = NOW - timedelta(hours=48)
    recent = NOW - timedelta(hours=1)

    save_raw_capture(conn, event, [_snapshot(event, old, home_odds=2.0)])
    save_raw_capture(conn, event, [_snapshot(event, recent, home_odds=2.1)])

    deleted = prune_raw_snapshots(conn, keep_hours=24)

    assert deleted == 1
    remaining = conn.execute("SELECT captured_at FROM raw_market_snapshot").fetchall()
    assert len(remaining) == 1
    assert remaining[0][0] == recent.isoformat()


def test_prune_keeps_latest_even_if_outside_window(tmp_path):
    # If EVERY sample for a key is old (e.g. a match that stopped being
    # captured), the single latest one must survive regardless - it's
    # what load_latest_market_snapshots would still return.
    conn = init_db(tmp_path / "vb.sqlite")
    event = _event()

    ancient = NOW - timedelta(hours=100)
    old = NOW - timedelta(hours=48)

    save_raw_capture(conn, event, [_snapshot(event, ancient, home_odds=2.0)])
    save_raw_capture(conn, event, [_snapshot(event, old, home_odds=2.2)])

    deleted = prune_raw_snapshots(conn, keep_hours=24)

    assert deleted == 1
    remaining = conn.execute("SELECT captured_at, outcomes_json FROM raw_market_snapshot").fetchall()
    assert len(remaining) == 1
    assert remaining[0][0] == old.isoformat()  # the latest of the two, even though it's outside the window


def test_prune_is_independent_per_market_key(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    event = _event()
    old = NOW - timedelta(hours=48)
    recent = NOW - timedelta(hours=1)

    match_winner_old = _snapshot(event, old)
    totals_old = MarketSnapshot(
        event=event, market_type=MarketType.TOTALS, line=2.5,
        outcomes=(Outcome(Selection.OVER, 1.9), Outcome(Selection.UNDER, 1.9)),
        captured_at=old,
    )
    save_raw_capture(conn, event, [match_winner_old, totals_old])
    save_raw_capture(conn, event, [_snapshot(event, recent)])  # only match_winner gets a recent update

    deleted = prune_raw_snapshots(conn, keep_hours=24)

    # totals' only (old) row must survive since it's still the latest for that key
    assert deleted == 1  # only the old match_winner row was deleted
    remaining = conn.execute("SELECT market_type FROM raw_market_snapshot ORDER BY market_type").fetchall()
    assert remaining == [("match_winner",), ("totals",)]


def test_prune_returns_zero_when_nothing_to_delete(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    event = _event()
    save_raw_capture(conn, event, [_snapshot(event, NOW)])

    assert prune_raw_snapshots(conn, keep_hours=24) == 0


def test_prune_ranks_by_captured_at_not_id(tmp_path):
    # F-10 (2026-07-25 external audit): a merge can insert a historical
    # row AFTER a genuinely more recent one already present, giving the
    # older row the HIGHER autoincrement id. Pruning must not assume
    # higher id == more recent - it must keep whichever row is actually
    # latest by captured_at, even if that row has the LOWER id.
    conn = init_db(tmp_path / "vb.sqlite")
    event = _event()

    genuinely_recent = NOW - timedelta(hours=1)
    save_raw_capture(conn, event, [_snapshot(event, genuinely_recent, home_odds=2.1)])  # inserted first -> lower id

    historical_but_inserted_later = NOW - timedelta(hours=48)
    save_raw_capture(conn, event, [_snapshot(event, historical_but_inserted_later, home_odds=2.0)])  # higher id, older time

    deleted = prune_raw_snapshots(conn, keep_hours=24)

    assert deleted == 1
    remaining = conn.execute("SELECT captured_at FROM raw_market_snapshot").fetchall()
    assert len(remaining) == 1
    assert remaining[0][0] == genuinely_recent.isoformat()  # the truly latest row survives despite its lower id
