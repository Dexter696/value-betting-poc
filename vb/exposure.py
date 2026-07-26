"""Event/site exposure policy — Phase 5 step 6 of the audit's
remediation roadmap (2026-07-25).

Caps how much stake can be concurrently at risk on a single event or a
single comparison site, so a cluster of correlated signals (many
markets on the same match, or a run of crossings on one book during a
data glitch) can't silently concentrate risk far beyond what a
flat-stake headline ROI figure would suggest.

Deliberately a pure function of the caller-supplied current positions
rather than something that queries the database itself: canonical
event identity isn't fully wired into bet_decision/bet_execution yet
(that lands once Phase 3's market_mapping.py output is actually
persisted into canonical_event/event_match rows, a separate follow-up
integration step — see vb/market_mapping.py's module docstring), so
this module's contract is kept independent of that plumbing. The
caller is responsible for assembling `current_positions` from whatever
accepted bet_executions are actually open at decision time.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExposureLimits:
    max_stake_per_event: float
    max_stake_per_site: float


@dataclass(frozen=True)
class ExposurePosition:
    event_id: str
    site: str
    stake: float


@dataclass(frozen=True)
class ExposureCheckResult:
    allowed: bool
    reason: str = ""


def check_exposure(
    current_positions: list[ExposurePosition],
    candidate_event_id: str,
    candidate_site: str,
    candidate_stake: float,
    limits: ExposureLimits,
) -> ExposureCheckResult:
    """Would adding `candidate_stake` on (candidate_event_id,
    candidate_site) push either total over its limit? Checks event
    exposure first (a tighter, more specific concentration risk) before
    site exposure, and reports whichever limit is actually violated so
    a caller can log a real reason, not just "rejected."""
    event_total = sum(p.stake for p in current_positions if p.event_id == candidate_event_id) + candidate_stake
    if event_total > limits.max_stake_per_event:
        return ExposureCheckResult(
            allowed=False,
            reason=f"event exposure {event_total:.2f} would exceed limit {limits.max_stake_per_event:.2f}",
        )

    site_total = sum(p.stake for p in current_positions if p.site == candidate_site) + candidate_stake
    if site_total > limits.max_stake_per_site:
        return ExposureCheckResult(
            allowed=False,
            reason=f"site exposure {site_total:.2f} would exceed limit {limits.max_stake_per_site:.2f}",
        )

    return ExposureCheckResult(allowed=True)
