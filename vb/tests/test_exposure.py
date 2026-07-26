from vb.exposure import ExposureLimits, ExposurePosition, check_exposure

LIMITS = ExposureLimits(max_stake_per_event=3.0, max_stake_per_site=5.0)


def test_allows_a_bet_within_both_limits():
    result = check_exposure([], "event-1", "swisslos.ch", candidate_stake=1.0, limits=LIMITS)
    assert result.allowed


def test_rejects_when_event_exposure_would_be_exceeded():
    current = [ExposurePosition(event_id="event-1", site="swisslos.ch", stake=2.5)]
    result = check_exposure(current, "event-1", "loro.ch", candidate_stake=1.0, limits=LIMITS)

    assert not result.allowed
    assert "event exposure" in result.reason


def test_rejects_when_site_exposure_would_be_exceeded_even_across_different_events():
    current = [
        ExposurePosition(event_id="event-1", site="swisslos.ch", stake=2.0),
        ExposurePosition(event_id="event-2", site="swisslos.ch", stake=2.5),
    ]
    result = check_exposure(current, "event-3", "swisslos.ch", candidate_stake=1.0, limits=LIMITS)

    assert not result.allowed
    assert "site exposure" in result.reason


def test_positions_on_a_different_event_or_site_do_not_count_against_this_candidate():
    current = [
        ExposurePosition(event_id="event-99", site="loro.ch", stake=100.0),  # unrelated, huge
    ]
    result = check_exposure(current, "event-1", "swisslos.ch", candidate_stake=1.0, limits=LIMITS)
    assert result.allowed


def test_exactly_at_the_limit_is_allowed_not_rejected():
    current = [ExposurePosition(event_id="event-1", site="swisslos.ch", stake=2.0)]
    result = check_exposure(current, "event-1", "swisslos.ch", candidate_stake=1.0, limits=LIMITS)
    assert result.allowed  # totals exactly 3.0, the event limit
