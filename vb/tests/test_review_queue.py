from datetime import datetime, timedelta, timezone

import pytest

from vb.matching import MatchTier, score_event_pair
from vb.models import MarketSnapshot, MarketType, Outcome, RawEvent, Selection
from vb.pipeline import find_leg_edges, run_cycle
from vb.storage import (
    init_db,
    list_pending_reviews,
    load_approved_review_pairs,
    save_raw_capture,
    save_review_candidate,
    set_review_status,
)

T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)


def _review_candidate():
    # Heavily truncated names on both legs land in the narrow 0.70-0.75
    # band that's still REVIEW-tier after the 2026-07-24 recalibration
    # (see vb.matching AUTO_THRESHOLD) - most real-world abbreviations
    # (e.g. "Hapoel Tel Aviv" -> "H. Tel Aviv") now score high enough to
    # auto-accept directly, per the 64/64 manual-review validation.
    anchor = RawEvent(
        site="pinnacle.com", sport="soccer", competition="Conference League",
        kickoff_utc=T0, raw_home_team="Kryvbas Kryvyi Rih", raw_away_team="Shakhtar Donetsk", event_id="p1",
    )
    candidate = RawEvent(
        site="swisslos.ch", sport="soccer", competition="Conference League",
        kickoff_utc=T0, raw_home_team="Kryvbas", raw_away_team="Shakhtar", event_id="s1",
    )
    match = score_event_pair(anchor, candidate)
    assert match is not None and match.tier == MatchTier.REVIEW
    return match


def test_save_and_list_review_candidate(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    match = _review_candidate()

    save_review_candidate(conn, match)
    pending = list_pending_reviews(conn)

    assert len(pending) == 1
    assert pending[0]["benchmark_event_id"] == "p1"
    assert pending[0]["comparison_event_id"] == "s1"
    assert pending[0]["score"] == match.score


def test_resave_updates_score_not_duplicates(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    match = _review_candidate()

    save_review_candidate(conn, match)
    save_review_candidate(conn, match)

    assert len(list_pending_reviews(conn)) == 1


def test_approve_removes_from_pending_and_grants_trust(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    match = _review_candidate()
    save_review_candidate(conn, match)
    review_id = list_pending_reviews(conn)[0]["id"]

    set_review_status(conn, review_id, "approved")

    assert list_pending_reviews(conn) == []
    approved = load_approved_review_pairs(conn, "pinnacle.com", "swisslos.ch")
    assert ("p1", "s1") in approved


def test_reject_removes_from_pending_without_granting_trust(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    match = _review_candidate()
    save_review_candidate(conn, match)
    review_id = list_pending_reviews(conn)[0]["id"]

    set_review_status(conn, review_id, "rejected")

    assert list_pending_reviews(conn) == []
    assert load_approved_review_pairs(conn, "pinnacle.com", "swisslos.ch") == set()


def test_set_review_status_rejects_bad_status(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")
    match = _review_candidate()
    save_review_candidate(conn, match)
    review_id = list_pending_reviews(conn)[0]["id"]

    with pytest.raises(ValueError):
        set_review_status(conn, review_id, "maybe")


def test_run_cycle_queues_review_tier_and_skips_it_until_approved(tmp_path):
    conn = init_db(tmp_path / "vb.sqlite")

    def _moneyline(event, home, draw, away):
        return MarketSnapshot(
            event=event, market_type=MarketType.MATCH_WINNER, line=None,
            outcomes=(Outcome(Selection.HOME, home), Outcome(Selection.DRAW, draw), Outcome(Selection.AWAY, away)),
            captured_at=T0,
        )

    pinnacle_event = RawEvent(
        site="pinnacle.com", sport="soccer", competition="Conference League",
        kickoff_utc=T0, raw_home_team="Kryvbas Kryvyi Rih", raw_away_team="Shakhtar Donetsk", event_id="p1",
    )
    swisslos_event = RawEvent(
        site="swisslos.ch", sport="soccer", competition="Conference League",
        kickoff_utc=T0, raw_home_team="Kryvbas", raw_away_team="Shakhtar", event_id="s1",
    )
    save_raw_capture(conn, pinnacle_event, [_moneyline(pinnacle_event, 2.10, 3.40, 3.60)])
    save_raw_capture(conn, swisslos_event, [_moneyline(swisslos_event, 2.30, 3.40, 3.60)])

    touched = run_cycle(conn, "pinnacle.com", "swisslos.ch")
    assert touched == []  # REVIEW-tier match, not auto-processed
    pending = list_pending_reviews(conn)
    assert len(pending) == 1
    assert pending[0]["benchmark_event_id"] == "p1"

    set_review_status(conn, pending[0]["id"], "approved")

    touched_after_approval = run_cycle(conn, "pinnacle.com", "swisslos.ch")
    assert len(touched_after_approval) == 1  # now processed like an AUTO match
    assert touched_after_approval[0].selection == Selection.HOME
