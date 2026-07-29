import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from merge_databases import merge  # noqa: E402

from vb.storage import init_db  # noqa: E402


def _raw_event_row(conn, site, event_id):
    return conn.execute(
        "SELECT sport, competition, kickoff_utc, raw_home_team, raw_away_team FROM raw_event WHERE site = ? AND event_id = ?",
        (site, event_id),
    ).fetchone()


def test_raw_event_conflict_source_wins_matching_live_upsert_semantics(tmp_path):
    # Regression for F-11 (2026-07-29): a merge used to always keep
    # dest's row on a raw_event conflict, unlike the live capture path
    # (save_raw_capture's ON CONFLICT DO UPDATE), which always takes the
    # latest capture. A merge in the "wrong" direction could silently
    # keep a stale/corrupted value (e.g. the Swisslos timezone bug's
    # wrong kickoff_utc) even after the other stream had the fix.
    source_conn = init_db(tmp_path / "source.sqlite")
    dest_conn = init_db(tmp_path / "dest.sqlite")

    dest_conn.execute(
        "INSERT INTO raw_event (site, event_id, sport, competition, kickoff_utc, raw_home_team, raw_away_team) "
        "VALUES ('swisslos.ch', 'e1', 'soccer', 'Kazakhstan - Premier League', '2026-08-01T15:00:00+00:00', 'Kairat Almaty', 'Omonia Nicosia')"
    )
    dest_conn.commit()
    dest_conn.close()

    source_conn.execute(
        "INSERT INTO raw_event (site, event_id, sport, competition, kickoff_utc, raw_home_team, raw_away_team) "
        "VALUES ('swisslos.ch', 'e1', 'soccer', 'Kazakhstan - Premier League', '2026-08-01T17:00:00+00:00', 'Kairat Almaty', 'Omonia Nicosia')"
    )
    source_conn.commit()
    source_conn.close()

    merge(str(tmp_path / "source.sqlite"), str(tmp_path / "dest.sqlite"))

    check_conn = init_db(tmp_path / "dest.sqlite")
    row = _raw_event_row(check_conn, "swisslos.ch", "e1")
    assert row[2] == "2026-08-01T17:00:00+00:00"  # source's corrected kickoff won


def test_raw_event_identical_rows_are_not_reported_as_touched(tmp_path):
    source_conn = init_db(tmp_path / "source.sqlite")
    dest_conn = init_db(tmp_path / "dest.sqlite")
    for conn in (source_conn, dest_conn):
        conn.execute(
            "INSERT INTO raw_event (site, event_id, sport, competition, kickoff_utc, raw_home_team, raw_away_team) "
            "VALUES ('pinnacle.com', 'e1', 'soccer', 'Premier League', '2026-08-01T15:00:00+00:00', 'A', 'B')"
        )
        conn.commit()
        conn.close()

    counts = merge(str(tmp_path / "source.sqlite"), str(tmp_path / "dest.sqlite"))
    assert counts["raw_event"] == 0


def test_raw_event_conflict_on_sport_alone_still_updates(tmp_path):
    # Regression found by self-review (2026-07-29): the conflict WHERE
    # clause originally omitted `sport` even though it's in the SET
    # list - a source row differing ONLY in sport would evaluate the
    # WHERE as false and silently keep dest's stale value.
    source_conn = init_db(tmp_path / "source.sqlite")
    dest_conn = init_db(tmp_path / "dest.sqlite")

    dest_conn.execute(
        "INSERT INTO raw_event VALUES ('pinnacle.com', 'e1', 'soccer', 'Premier League', '2026-08-01T15:00:00+00:00', 'A', 'B')"
    )
    dest_conn.commit()
    dest_conn.close()

    source_conn.execute(
        "INSERT INTO raw_event VALUES ('pinnacle.com', 'e1', 'football', 'Premier League', '2026-08-01T15:00:00+00:00', 'A', 'B')"
    )
    source_conn.commit()
    source_conn.close()

    counts = merge(str(tmp_path / "source.sqlite"), str(tmp_path / "dest.sqlite"))
    assert counts["raw_event"] == 1

    check_conn = init_db(tmp_path / "dest.sqlite")
    row = _raw_event_row(check_conn, "pinnacle.com", "e1")
    assert row[0] == "football"  # source's corrected sport won


def _review_row(conn, bench_id):
    return conn.execute(
        "SELECT status, reviewed_at, last_seen_at, first_seen_at FROM event_match_review "
        "WHERE benchmark_site = 'pinnacle.com' AND benchmark_event_id = ?",
        (bench_id,),
    ).fetchone()


def _insert_review(conn, bench_id, status, first_seen_at, last_seen_at, reviewed_at=None):
    conn.execute(
        "INSERT INTO event_match_review (benchmark_site, benchmark_event_id, comparison_site, comparison_event_id, "
        "score, reasons_json, first_seen_at, last_seen_at, status, reviewed_at) VALUES "
        "('pinnacle.com', ?, 'swisslos.ch', 's1', 82.0, '[]', ?, ?, ?, ?)",
        (bench_id, first_seen_at, last_seen_at, status, reviewed_at),
    )
    conn.commit()


def test_event_match_review_reviewed_source_wins_over_pending_dest(tmp_path):
    # Regression for F-11: a human's real approve/reject decision must
    # never be silently discarded just because the database it lives in
    # happened to be passed as source rather than dest.
    dest_conn = init_db(tmp_path / "dest.sqlite")
    _insert_review(dest_conn, "e1", "pending", "2026-08-01T10:00:00+00:00", "2026-08-01T10:00:00+00:00")
    dest_conn.close()

    source_conn = init_db(tmp_path / "source.sqlite")
    _insert_review(source_conn, "e1", "approved", "2026-08-01T10:00:00+00:00", "2026-08-01T11:00:00+00:00", reviewed_at="2026-08-01T11:05:00+00:00")
    source_conn.close()

    merge(str(tmp_path / "source.sqlite"), str(tmp_path / "dest.sqlite"))

    check_conn = init_db(tmp_path / "dest.sqlite")
    status, reviewed_at, last_seen_at, _ = _review_row(check_conn, "e1")
    assert status == "approved"
    assert reviewed_at == "2026-08-01T11:05:00+00:00"


def test_event_match_review_dest_reviewed_decision_never_overwritten_by_pending_source(tmp_path):
    dest_conn = init_db(tmp_path / "dest.sqlite")
    _insert_review(dest_conn, "e1", "rejected", "2026-08-01T10:00:00+00:00", "2026-08-01T10:00:00+00:00", reviewed_at="2026-08-01T10:30:00+00:00")
    dest_conn.close()

    source_conn = init_db(tmp_path / "source.sqlite")
    _insert_review(source_conn, "e1", "pending", "2026-08-01T10:00:00+00:00", "2026-08-01T12:00:00+00:00")
    source_conn.close()

    merge(str(tmp_path / "source.sqlite"), str(tmp_path / "dest.sqlite"))

    check_conn = init_db(tmp_path / "dest.sqlite")
    status, reviewed_at, _, _ = _review_row(check_conn, "e1")
    assert status == "rejected"
    assert reviewed_at == "2026-08-01T10:30:00+00:00"


def test_event_match_review_both_pending_takes_later_last_seen_at(tmp_path):
    dest_conn = init_db(tmp_path / "dest.sqlite")
    _insert_review(dest_conn, "e1", "pending", "2026-08-01T10:00:00+00:00", "2026-08-01T10:00:00+00:00")
    dest_conn.close()

    source_conn = init_db(tmp_path / "source.sqlite")
    _insert_review(source_conn, "e1", "pending", "2026-08-01T10:00:00+00:00", "2026-08-01T14:00:00+00:00")
    source_conn.close()

    merge(str(tmp_path / "source.sqlite"), str(tmp_path / "dest.sqlite"))

    check_conn = init_db(tmp_path / "dest.sqlite")
    status, _, last_seen_at, _ = _review_row(check_conn, "e1")
    assert status == "pending"
    assert last_seen_at == "2026-08-01T14:00:00+00:00"


def test_event_match_review_reconciles_first_seen_at_to_the_earliest_side(tmp_path):
    # Regression found by self-review (2026-07-29): the first version of
    # this fix reconciled last_seen_at/status/reviewed_at but silently
    # dropped source's first_seen_at even when it was genuinely earlier
    # - the same lossy-merge failure mode F-11 exists to close, just on
    # a different column.
    dest_conn = init_db(tmp_path / "dest.sqlite")
    _insert_review(dest_conn, "e1", "pending", "2026-08-01T10:00:00+00:00", "2026-08-01T10:00:00+00:00")
    dest_conn.close()

    source_conn = init_db(tmp_path / "source.sqlite")
    # source saw it earlier AND later reviewed it
    _insert_review(source_conn, "e1", "approved", "2026-08-01T09:00:00+00:00", "2026-08-01T11:00:00+00:00", reviewed_at="2026-08-01T11:05:00+00:00")
    source_conn.close()

    merge(str(tmp_path / "source.sqlite"), str(tmp_path / "dest.sqlite"))

    check_conn = init_db(tmp_path / "dest.sqlite")
    status, _, _, first_seen_at = _review_row(check_conn, "e1")
    assert status == "approved"
    assert first_seen_at == "2026-08-01T09:00:00+00:00"  # source's earlier sighting won, not dest's later one


def test_event_match_review_both_pending_reconciles_first_seen_at_too(tmp_path):
    dest_conn = init_db(tmp_path / "dest.sqlite")
    _insert_review(dest_conn, "e1", "pending", "2026-08-01T10:00:00+00:00", "2026-08-01T10:00:00+00:00")
    dest_conn.close()

    source_conn = init_db(tmp_path / "source.sqlite")
    # earlier first_seen_at but NOT a later last_seen_at - the old
    # both-pending branch only fired the UPDATE when last_seen_at was
    # later, which would have skipped this reconciliation entirely.
    _insert_review(source_conn, "e1", "pending", "2026-08-01T08:00:00+00:00", "2026-08-01T09:00:00+00:00")
    source_conn.close()

    merge(str(tmp_path / "source.sqlite"), str(tmp_path / "dest.sqlite"))

    check_conn = init_db(tmp_path / "dest.sqlite")
    status, _, last_seen_at, first_seen_at = _review_row(check_conn, "e1")
    assert status == "pending"
    assert first_seen_at == "2026-08-01T08:00:00+00:00"
    assert last_seen_at == "2026-08-01T10:00:00+00:00"  # dest's later last_seen_at correctly preserved
