"""F-01 fix: a signal must never be computed from arbitrarily old or
mutually-skewed quotes.

v1's find_leg_edges()/run_cycle() paired whatever load_latest_market_
snapshots() returned - "the last known snapshot per key, however long
ago that was" - with no check on its own age or on how far apart the
benchmark and comparison readings were. The 2026-07-25 external audit
reproduced this directly: pairing a 1-hour-old benchmark quote with a
20-hour-old comparison quote opened a real opportunity at edge_a =
9.52%, entirely possibly not a genuinely tradeable price gap at all -
just two readings from very different real moments.

This module is deliberately pure/stateless: given two candidate
snapshot timestamps and a strategy's configured limits, decide whether
the pair is eligible or not, and if not, exactly why. The caller
(vb/episode.py's EpisodeTracker) is responsible for recording that
decision as a signal_observation either way - a rejected pair is still
an auditable observation, not a silent skip (see
vb.storage.save_signal_observation's docstring).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass(frozen=True)
class FreshnessLimits:
    max_age_s: float
    max_skew_s: float
    min_lead_time_s: float


@dataclass(frozen=True)
class FreshnessResult:
    eligible: bool
    reject_reason: Optional[str] = None  # one of vb.models.RejectReason's values, or None if eligible

    @property
    def rejected(self) -> bool:
        return not self.eligible


def check_freshness(
    benchmark_received_at: datetime,
    comparison_received_at: datetime,
    kickoff_utc: datetime,
    now: datetime,
    limits: FreshnessLimits,
) -> FreshnessResult:
    """The actual F-01 gate. All four checks are independent and
    checked in a fixed order so a caller can always explain exactly
    which one failed - "stale" and "skewed" are different failure
    modes with different real-world causes (a scraper that's fallen
    behind vs. two scrapers that ran at genuinely different times) and
    should never be conflated into one generic rejection.
    """
    benchmark_age = (now - benchmark_received_at).total_seconds()
    if benchmark_age > limits.max_age_s:
        return FreshnessResult(eligible=False, reject_reason="benchmark_stale")

    comparison_age = (now - comparison_received_at).total_seconds()
    if comparison_age > limits.max_age_s:
        return FreshnessResult(eligible=False, reject_reason="comparison_stale")

    skew = abs((benchmark_received_at - comparison_received_at).total_seconds())
    if skew > limits.max_skew_s:
        return FreshnessResult(eligible=False, reject_reason="snapshot_skew")

    lead_time = (kickoff_utc - now).total_seconds()
    if lead_time < limits.min_lead_time_s:
        # Not one of the audit's named RejectReason values (that enum
        # covers snapshot freshness/skew/source failure specifically) -
        # a too-close-to-kickoff rejection is a distinct, real reason
        # worth its own label rather than being forced into one of
        # those three.
        return FreshnessResult(eligible=False, reject_reason="insufficient_lead_time")

    return FreshnessResult(eligible=True, reject_reason=None)
