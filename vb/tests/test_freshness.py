from datetime import datetime, timedelta, timezone

from vb.freshness import FreshnessLimits, check_freshness

NOW = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
KICKOFF = NOW + timedelta(hours=3)
LIMITS = FreshnessLimits(max_age_s=90, max_skew_s=60, min_lead_time_s=300)


def test_eligible_when_everything_within_limits():
    result = check_freshness(
        benchmark_received_at=NOW - timedelta(seconds=30),
        comparison_received_at=NOW - timedelta(seconds=40),
        kickoff_utc=KICKOFF, now=NOW, limits=LIMITS,
    )
    assert result.eligible
    assert result.reject_reason is None


def test_stale_benchmark_rejected():
    # Direct reproduction of the audit's F-01 scenario: a 1h-old
    # benchmark quote must never be paired into a signal.
    result = check_freshness(
        benchmark_received_at=NOW - timedelta(hours=1),
        comparison_received_at=NOW - timedelta(seconds=10),
        kickoff_utc=KICKOFF, now=NOW, limits=LIMITS,
    )
    assert not result.eligible
    assert result.reject_reason == "benchmark_stale"


def test_stale_comparison_rejected():
    # The audit's other half of the same scenario: a 20h-old
    # comparison quote must never be paired either, independent of how
    # fresh the benchmark side is.
    result = check_freshness(
        benchmark_received_at=NOW - timedelta(seconds=10),
        comparison_received_at=NOW - timedelta(hours=20),
        kickoff_utc=KICKOFF, now=NOW, limits=LIMITS,
    )
    assert not result.eligible
    assert result.reject_reason == "comparison_stale"


def test_both_fresh_but_skewed_rejected():
    # Both individually within max_age, but too far apart from each
    # other - this is the case age-only checks would miss.
    result = check_freshness(
        benchmark_received_at=NOW - timedelta(seconds=5),
        comparison_received_at=NOW - timedelta(seconds=80),  # both < 90s old, but 75s apart > 60s max_skew
        kickoff_utc=KICKOFF, now=NOW, limits=LIMITS,
    )
    assert not result.eligible
    assert result.reject_reason == "snapshot_skew"


def test_exactly_at_age_boundary_is_eligible():
    result = check_freshness(
        benchmark_received_at=NOW - timedelta(seconds=90),  # exactly max_age_s, not over it
        comparison_received_at=NOW - timedelta(seconds=90),
        kickoff_utc=KICKOFF, now=NOW, limits=LIMITS,
    )
    assert result.eligible


def test_just_over_age_boundary_is_rejected():
    result = check_freshness(
        benchmark_received_at=NOW - timedelta(seconds=90.001),
        comparison_received_at=NOW - timedelta(seconds=10),
        kickoff_utc=KICKOFF, now=NOW, limits=LIMITS,
    )
    assert not result.eligible
    assert result.reject_reason == "benchmark_stale"


def test_insufficient_lead_time_rejected_even_with_fresh_quotes():
    result = check_freshness(
        benchmark_received_at=NOW - timedelta(seconds=5),
        comparison_received_at=NOW - timedelta(seconds=5),
        kickoff_utc=NOW + timedelta(seconds=100),  # well under the 300s min_lead_time_s
        now=NOW, limits=LIMITS,
    )
    assert not result.eligible
    assert result.reject_reason == "insufficient_lead_time"


def test_reject_reason_values_match_schema_check_constraint():
    # Every value this module can produce must be one the DB's
    # signal_observation.reject_reason CHECK constraint actually
    # accepts, or save_signal_observation() will raise at insert time
    # instead of failing this test cleanly.
    import re
    from pathlib import Path

    schema = (Path(__file__).parent.parent / "schema.sql").read_text(encoding="utf-8")
    match = re.search(r"CHECK \(reject_reason IN \(([^)]+)\)", schema)
    assert match is not None
    allowed = {v.strip().strip("'") for v in match.group(1).split(",")}

    produced = {"benchmark_stale", "comparison_stale", "snapshot_skew", "insufficient_lead_time"}
    assert produced <= allowed
