from datetime import datetime, timedelta, timezone

from vb.models import MarketType, MarketSnapshot, Outcome, RawEvent, Selection
from vb.storage import init_db, save_raw_capture

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _event():
    return RawEvent(
        site="swisslos.ch",
        sport="soccer",
        competition="Qualifikation, Europa League",
        kickoff_utc=T0 + timedelta(hours=3),
        raw_home_team="Qarabag Agdam",
        raw_away_team="CSKA Sofia",
        event_id="16605557",
    )


def test_save_raw_capture_inserts_event_and_snapshots(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    event = _event()
    snapshot = MarketSnapshot(
        event=event, market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(Outcome(Selection.HOME, 1.42), Outcome(Selection.DRAW, 4.10), Outcome(Selection.AWAY, 6.70)),
        captured_at=T0,
    )

    save_raw_capture(conn, event, [snapshot])

    events = conn.execute("SELECT site, event_id, raw_home_team FROM raw_event").fetchall()
    assert events == [("swisslos.ch", "16605557", "Qarabag Agdam")]

    rows = conn.execute("SELECT market_type, line, outcomes_json FROM raw_market_snapshot").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "match_winner"
    assert rows[0][1] is None


def test_save_raw_capture_appends_across_cycles_not_replaces(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    event = _event()
    snap1 = MarketSnapshot(
        event=event, market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(Outcome(Selection.HOME, 1.42), Outcome(Selection.DRAW, 4.10), Outcome(Selection.AWAY, 6.70)),
        captured_at=T0,
    )
    snap2 = MarketSnapshot(
        event=event, market_type=MarketType.MATCH_WINNER, line=None,
        outcomes=(Outcome(Selection.HOME, 1.40), Outcome(Selection.DRAW, 4.20), Outcome(Selection.AWAY, 6.80)),
        captured_at=T0 + timedelta(minutes=1),
    )

    save_raw_capture(conn, event, [snap1])
    save_raw_capture(conn, event, [snap2])

    events = conn.execute("SELECT COUNT(*) FROM raw_event").fetchone()[0]
    snapshots = conn.execute("SELECT COUNT(*) FROM raw_market_snapshot").fetchone()[0]
    assert events == 1  # event upserted, not duplicated
    assert snapshots == 2  # snapshots accumulate as a time series
