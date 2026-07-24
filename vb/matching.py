"""Cross-site market matching.

Two-stage process, matching the methodology's two hard problems:

1. match_events(): is "Bale - Young Boys, 20:00" on Swisslos the same
   fixture as "FC Basel v BSC Young Boys, 20:00" on Pinnacle? Scored on
   team-name similarity (after normalize.normalize_team_name), kickoff
   time proximity, and competition similarity. A hard time-window cutoff
   applies before any name scoring, since two fixtures 6 hours apart are
   never the same match no matter how similar the names look.

2. match_markets(): once two events are known to be the same fixture,
   which market on site A corresponds to which market on site B? Matched
   on market type plus canonical line (see normalize.canonical_handicap_line)
   so handicaps quoted from opposite perspectives still pair up, and a
   -1.5 line is never confused with a -2.0 line.

Every match gets a confidence tier: AUTO (trust it), REVIEW (queue for a
human — a mismatched market produces fake value, per the methodology's
own warning), or REJECT (dropped, not even surfaced).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from rapidfuzz import fuzz

from .models import MarketSnapshot, RawEvent
from .normalize import normalize_competition, normalize_team_name


class MatchTier(str, Enum):
    AUTO = "auto"
    REVIEW = "review"
    REJECT = "reject"


# Tunable thresholds. AUTO_THRESHOLD was recalibrated 2026-07-24 after a
# full manual review pass: 64 REVIEW-tier candidates (scores 0.71-0.90)
# were checked by hand and every single one was a correct match - 0
# rejected. That's real evidence the fuzzy scorer is reliable well below
# the original 0.90 cutoff, so the bar was lowered to 0.75, keeping only
# the genuinely low-confidence band (0.70-0.75) plus swap-suspicious
# matches (see below) in the review queue. Re-check this if a future
# review pass finds a real mismatch in that range.
TIME_TOLERANCE = timedelta(minutes=15)
AUTO_THRESHOLD = 0.75
REVIEW_THRESHOLD = 0.70
# If the reversed (home<->away) pairing scores notably higher than the
# direct one, something is off (a site listing teams in the opposite
# order) — too risky for handicap/1X2 mapping to auto-accept, so force
# review even if the raw score would otherwise clear AUTO_THRESHOLD.
SWAP_SUSPICION_MARGIN = 0.10


@dataclass(frozen=True)
class EventMatch:
    anchor: RawEvent
    candidate: RawEvent
    score: float
    tier: MatchTier
    reasons: tuple[str, ...]


def _name_similarity(a: str, b: str) -> float:
    return fuzz.token_sort_ratio(a, b) / 100.0


def _time_score(a_dt, b_dt) -> float | None:
    delta = abs((a_dt - b_dt).total_seconds())
    tolerance = TIME_TOLERANCE.total_seconds()
    if delta > tolerance:
        return None
    if delta <= 120:
        return 1.0
    return max(0.0, 1.0 - (delta - 120) / (tolerance - 120))


def score_event_pair(anchor: RawEvent, candidate: RawEvent) -> EventMatch | None:
    if anchor.sport != candidate.sport:
        return None

    t_score = _time_score(anchor.kickoff_utc, candidate.kickoff_utc)
    if t_score is None:
        return None

    a_home = normalize_team_name(anchor.raw_home_team)
    a_away = normalize_team_name(anchor.raw_away_team)
    c_home = normalize_team_name(candidate.raw_home_team)
    c_away = normalize_team_name(candidate.raw_away_team)

    direct = (_name_similarity(a_home, c_home) + _name_similarity(a_away, c_away)) / 2
    swapped = (_name_similarity(a_home, c_away) + _name_similarity(a_away, c_home)) / 2

    reasons: list[str] = []
    team_score = direct
    if swapped > direct + SWAP_SUSPICION_MARGIN:
        team_score = swapped
        reasons.append(
            "home/away appears swapped between sites - verify before trusting handicap/1X2 mapping"
        )

    comp_score = _name_similarity(
        normalize_competition(anchor.competition), normalize_competition(candidate.competition)
    )

    total = 0.70 * team_score + 0.15 * comp_score + 0.15 * t_score

    if reasons:
        tier = MatchTier.REVIEW if total >= REVIEW_THRESHOLD else MatchTier.REJECT
    elif total >= AUTO_THRESHOLD:
        tier = MatchTier.AUTO
    elif total >= REVIEW_THRESHOLD:
        tier = MatchTier.REVIEW
        reasons.append(f"combined score {total:.2f} below auto-accept threshold {AUTO_THRESHOLD}")
    else:
        tier = MatchTier.REJECT

    if tier == MatchTier.REJECT:
        return None

    return EventMatch(anchor=anchor, candidate=candidate, score=total, tier=tier, reasons=tuple(reasons))


def match_events(anchor_events: list[RawEvent], candidate_events: list[RawEvent]) -> list[EventMatch]:
    """Greedy best-match of each anchor event against a candidate site's events.

    Each anchor event maps to at most one candidate event (the best-scoring
    one still above REVIEW_THRESHOLD); a claimed candidate is removed from
    consideration for subsequent anchors, since two distinct anchor
    fixtures should never resolve to the same candidate fixture.
    """
    results: list[EventMatch] = []
    claimed: set[str] = set()

    for anchor in anchor_events:
        scored = [
            m
            for c in candidate_events
            if c.event_id not in claimed
            and (m := score_event_pair(anchor, c)) is not None
        ]
        if not scored:
            continue
        best = max(scored, key=lambda m: m.score)
        results.append(best)
        claimed.add(best.candidate.event_id)

    return results


# ---- Market-level matching (within an already event-matched pair) ----

LINE_TOLERANCE = 0.01  # exact match required; this only absorbs float rounding


@dataclass(frozen=True)
class MarketMatch:
    anchor: MarketSnapshot
    candidate: MarketSnapshot


def match_markets(
    anchor_markets: list[MarketSnapshot], candidate_markets: list[MarketSnapshot]
) -> tuple[list[MarketMatch], list[MarketSnapshot]]:
    """Pair up same-type, same-line markets between two event-matched sites.

    Returns (matches, unmatched_anchor_markets). A market with no line
    counterpart on the candidate site (e.g. it doesn't offer that
    handicap) isn't a matching failure, just not offered there — so those
    are returned separately rather than folded into the review queue.
    """
    matches: list[MarketMatch] = []
    unmatched: list[MarketSnapshot] = []
    claimed: set[int] = set()

    for a in anchor_markets:
        found: tuple[int, MarketSnapshot] | None = None
        for idx, c in enumerate(candidate_markets):
            if idx in claimed or c.market_type != a.market_type:
                continue
            if a.line is None and c.line is None:
                found = (idx, c)
                break
            if a.line is not None and c.line is not None and abs(a.line - c.line) <= LINE_TOLERANCE:
                found = (idx, c)
                break
        if found is not None:
            idx, c = found
            claimed.add(idx)
            matches.append(MarketMatch(anchor=a, candidate=c))
        else:
            unmatched.append(a)

    return matches, unmatched
