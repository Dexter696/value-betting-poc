from datetime import datetime, timezone

from vb.identity import new_id
from vb.models import CanonicalEvent, MarketType, Selection
from vb.settlement import SettlementResult
from vb.settlement_evidence import (
    archive_raw_response,
    record_result_evidence,
    record_settlement_version,
    settlement_key,
)
from vb.storage import current_settlement_version, init_db, save_canonical_event

T0 = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    return init_db(tmp_path / "vb.sqlite")


def _event(conn):
    event_id = new_id()
    save_canonical_event(conn, CanonicalEvent(id=event_id, sport="soccer", created_at=T0))
    return event_id


def test_archive_raw_response_is_deterministic_and_content_addressed():
    payload = b'{"home": 2, "away": 1}'
    assert archive_raw_response(payload) == archive_raw_response(payload)
    assert archive_raw_response(payload) != archive_raw_response(b'{"home": 1, "away": 1}')


def test_settlement_key_is_stable_for_the_same_leg():
    key1 = settlement_key("event-1", MarketType.MATCH_WINNER, None, Selection.HOME)
    key2 = settlement_key("event-1", MarketType.MATCH_WINNER, None, Selection.HOME)
    assert key1 == key2


def test_record_result_evidence_and_settlement_version_round_trip(tmp_path):
    conn = _db(tmp_path)
    event_id = _event(conn)

    evidence_id = record_result_evidence(
        conn, event_id, provider="espn", retrieved_at=T0, status="final",
        home_goals=2, away_goals=1, raw_payload_hash=archive_raw_response(b"raw espn response"),
    )
    version_id = record_settlement_version(
        conn, event_id, MarketType.MATCH_WINNER, None, Selection.HOME, evidence_id,
        home_goals=2, away_goals=1, created_at=T0,
    )

    current = current_settlement_version(conn, settlement_key(event_id, MarketType.MATCH_WINNER, None, Selection.HOME))
    assert current.id == version_id
    assert current.result == SettlementResult.WON.value
    assert current.supersedes_id is None


def test_a_correction_creates_a_new_version_that_supersedes_the_old_one_without_deleting_it(tmp_path):
    conn = _db(tmp_path)
    event_id = _event(conn)
    key = settlement_key(event_id, MarketType.MATCH_WINNER, None, Selection.HOME)

    wrong_evidence = record_result_evidence(conn, event_id, provider="espn", retrieved_at=T0, status="final", home_goals=1, away_goals=1)
    first_version = record_settlement_version(
        conn, event_id, MarketType.MATCH_WINNER, None, Selection.HOME, wrong_evidence, home_goals=1, away_goals=1, created_at=T0,
    )

    corrected_evidence = record_result_evidence(
        conn, event_id, provider="manual", retrieved_at=T0, status="final", home_goals=2, away_goals=1,
        source_url="https://example.com/final-score", reviewer="mirek",
    )
    second_version = record_settlement_version(
        conn, event_id, MarketType.MATCH_WINNER, None, Selection.HOME, corrected_evidence,
        home_goals=2, away_goals=1, created_at=T0,
    )

    # the old row is still there, unedited
    old_row = conn.execute("SELECT result FROM settlement_version WHERE id = ?", (first_version,)).fetchone()
    assert old_row[0] == SettlementResult.LOST.value  # 1-1 draw, HOME selection lost

    current = current_settlement_version(conn, key)
    assert current.id == second_version
    assert current.result == SettlementResult.WON.value  # corrected to 2-1 home win
    assert current.supersedes_id == first_version

    total_versions = conn.execute("SELECT COUNT(*) FROM settlement_version WHERE settlement_key = ?", (key,)).fetchone()[0]
    assert total_versions == 2


def test_settle_raising_on_an_invalid_selection_propagates_from_record_settlement_version(tmp_path):
    import pytest
    conn = _db(tmp_path)
    event_id = _event(conn)
    evidence_id = record_result_evidence(conn, event_id, provider="espn", retrieved_at=T0, status="final", home_goals=2, away_goals=1)

    with pytest.raises(ValueError):
        record_settlement_version(
            conn, event_id, MarketType.TOTALS, 2.5, Selection.HOME, evidence_id,  # HOME is invalid for totals
            home_goals=2, away_goals=1, created_at=T0,
        )
