"""Orientation-aware, globally-optimal event matching — the Phase 3 fix
for the audit's F-14 finding (2026-07-25).

vb.matching.match_events() has two problems this module fixes:

1. It's greedy and order-dependent: each anchor claims its own
   best-scoring candidate in turn, so a locally-good pick can starve a
   better global assignment. The audit's own example: anchor A scores
   0.90 against candidate X and 0.89 against Y; anchor B only scores
   0.88 against X. Greedy processes A first, claims X, leaves B
   unmatched — even though A→Y plus B→X is the better assignment
   overall. This module instead groups events into blocks (same
   competition, mutually within the kickoff time tolerance) and solves
   each block as a maximum-weight bipartite assignment
   (scipy.optimize.linear_sum_assignment), which is provably globally
   optimal within the block.

2. Even when the existing scorer detects a home/away swap between
   sites (vb.matching.score_event_pair already tries both orientations
   and keeps whichever scores higher), that fact only ever became a
   human-readable reason string — never queryable data. Downstream,
   vb.pipeline matches markets by Selection value directly (HOME on one
   site paired with HOME on the other), with no way to know a swap had
   been detected, so a genuinely swapped pair would silently mismatch
   which team's price is which. This module returns `orientation`
   (MatchOrientation.SAME / SWAPPED) as an explicit field on every
   match, plus `remap_selection`/`remap_handicap_line` to apply it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from scipy.optimize import linear_sum_assignment

from .matching import (
    AUTO_THRESHOLD,
    REVIEW_THRESHOLD,
    SWAP_SUSPICION_MARGIN,
    MatchTier,
    _name_similarity,
    _time_score,
)
from .models import MatchOrientation, RawEvent, Selection
from .normalize import normalize_competition, normalize_team_name


@dataclass(frozen=True)
class OrientedEventMatch:
    anchor: RawEvent
    candidate: RawEvent
    orientation: MatchOrientation
    score: float
    score_components: dict
    tier: MatchTier
    reasons: tuple[str, ...]


def score_oriented_pair(anchor: RawEvent, candidate: RawEvent) -> Optional[OrientedEventMatch]:
    """Same scoring as vb.matching.score_event_pair, but keeps the
    winning orientation as explicit data instead of discarding it after
    computing a reason string."""
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
    orientation = MatchOrientation.SAME
    team_score = direct
    if swapped > direct + SWAP_SUSPICION_MARGIN:
        team_score = swapped
        orientation = MatchOrientation.SWAPPED
        reasons.append("home/away detected swapped between sites - selections remapped accordingly")

    comp_score = _name_similarity(
        normalize_competition(anchor.competition), normalize_competition(candidate.competition)
    )

    total = 0.70 * team_score + 0.15 * comp_score + 0.15 * t_score
    components = {"team": team_score, "competition": comp_score, "time": t_score}

    if orientation is MatchOrientation.SWAPPED and abs(swapped - direct) <= SWAP_SUSPICION_MARGIN:
        # ambiguous - swap barely wins, too risky to auto-remap a
        # directional market without a human confirming it
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

    return OrientedEventMatch(
        anchor=anchor, candidate=candidate, orientation=orientation,
        score=total, score_components=components, tier=tier, reasons=tuple(reasons),
    )


COMPETITION_BLOCK_THRESHOLD = 0.5
# Loose fuzzy floor for grouping into a block - NOT the AUTO/REVIEW/
# REJECT decision, which is made later by score_oriented_pair's full
# weighted formula (competition is only 15% of that score). Calibrated
# against real production data: genuinely-the-same competition scores
# >= 0.76 even across languages/naming conventions ("Austria -
# Bundesliga" vs "Bundesliga Autriche" -> 1.0, "2. Bundesliga" vs
# "Bundesliga" -> 0.909), while clearly different competitions score
# <= 0.57 ("Premier League" vs "Serie A" -> 0.476, "La Liga" vs
# "Ligue 1" -> 0.571) - see vb/tests/test_market_mapping.py for the
# regression that pins this down. A stray pair that slips through this
# loose floor still gets correctly rejected by score_oriented_pair's
# own team-name/time signal, so this only needs to avoid EXCLUDING a
# real match, not to be precise.
_COMPETITION_SIMILARITY_CACHE: dict[tuple[str, str], float] = {}


def _blocks(anchor_events: list[RawEvent], candidate_events: list[RawEvent]) -> list[tuple[list[RawEvent], list[RawEvent]]]:
    """Group anchor+candidate events into blocks via connected
    components over the "same sport AND within the kickoff time
    tolerance AND plausibly the same competition" relation, so the
    bipartite assignment below only ever competes events that could
    plausibly be the same fixture. Uses connected components (not
    fixed-width time buckets) so two events 14 minutes apart never get
    artificially split across a bucket boundary just because a third
    event's timestamp happened to fall between them.

    Competition matching here is FUZZY (_name_similarity, same as
    score_oriented_pair's own team/competition scoring) against
    COMPETITION_BLOCK_THRESHOLD, never exact string equality - an
    earlier version of this function used `!=` on
    normalize_competition()'s output directly, which is NOT
    token-order-stable ("Austria - Bundesliga" normalizes to "austria
    bundesliga", "Bundesliga Autriche" to "bundesliga austria" - two
    different strings, same tokens) and silently produced zero blocks
    (hence zero matches) in real production data despite 80 genuine
    AUTO-tier matches existing between the same two sites that cycle.
    _time_score is checked first since it's far cheaper than the fuzzy
    string comparison and rejects the vast majority of pairs outright
    (most events across a whole day's captures aren't within 15
    minutes of each other).
    """
    all_events: list[RawEvent] = anchor_events + candidate_events
    normalized_competitions = [normalize_competition(e.competition) for e in all_events]
    parent = list(range(len(all_events)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    def competition_similarity(na: str, nb: str) -> float:
        key = (na, nb) if na <= nb else (nb, na)
        cached = _COMPETITION_SIMILARITY_CACHE.get(key)
        if cached is None:
            cached = _name_similarity(na, nb)
            _COMPETITION_SIMILARITY_CACHE[key] = cached
        return cached

    for i in range(len(all_events)):
        for j in range(i + 1, len(all_events)):
            a, b = all_events[i], all_events[j]
            if a.sport != b.sport:
                continue
            if _time_score(a.kickoff_utc, b.kickoff_utc) is None:
                continue
            if competition_similarity(normalized_competitions[i], normalized_competitions[j]) < COMPETITION_BLOCK_THRESHOLD:
                continue
            union(i, j)

    groups: dict[int, tuple[list[RawEvent], list[RawEvent]]] = {}
    for idx, event in enumerate(all_events):
        root = find(idx)
        anchors, candidates = groups.setdefault(root, ([], []))
        if idx < len(anchor_events):
            anchors.append(event)
        else:
            candidates.append(event)

    return [(a, c) for a, c in groups.values() if a and c]


# A finite (never -inf) sentinel for "no valid pairing" in the score
# matrix below. scipy.optimize.linear_sum_assignment must find a
# complete assignment (one entry per row, for however many rows the
# matrix has) even when most of a block's anchor/candidate pairs are
# genuinely impossible (different real fixtures that just happened to
# land in the same competition+time block) - an -inf entry makes that
# assignment infeasible outright ("cost matrix is infeasible") the
# moment any row or column has no real candidate at all, which is the
# common case once blocking no longer requires an exact competition
# match. A merely-very-bad finite score keeps the assignment solvable;
# _UNMATCHABLE_SCORE is far below any real score_oriented_pair result
# (always in roughly [0, 1]), and any row/column forced onto it is
# filtered back out below via the `scored.get((i, j))` lookup, which
# only has entries for pairs score_oriented_pair actually accepted.
_UNMATCHABLE_SCORE = -1e6


def match_events_v2(anchor_events: list[RawEvent], candidate_events: list[RawEvent]) -> list[OrientedEventMatch]:
    """Globally-optimal replacement for vb.matching.match_events(): each
    (competition, kickoff-window) block is solved as a maximum-weight
    bipartite assignment rather than greedy nearest-first, and every
    result carries its orientation as explicit data.
    """
    results: list[OrientedEventMatch] = []

    for block_anchors, block_candidates in _blocks(anchor_events, candidate_events):
        scored: dict[tuple[int, int], OrientedEventMatch] = {}
        score_matrix = [[_UNMATCHABLE_SCORE] * len(block_candidates) for _ in block_anchors]
        for i, anchor in enumerate(block_anchors):
            for j, candidate in enumerate(block_candidates):
                m = score_oriented_pair(anchor, candidate)
                if m is not None:
                    scored[(i, j)] = m
                    score_matrix[i][j] = m.score

        row_ind, col_ind = linear_sum_assignment(score_matrix, maximize=True)
        for i, j in zip(row_ind, col_ind):
            match = scored.get((i, j))
            if match is not None:
                results.append(match)

    return results


_SWAPPED_SELECTION = {
    Selection.HOME: Selection.AWAY,
    Selection.AWAY: Selection.HOME,
    Selection.DRAW: Selection.DRAW,
    Selection.OVER: Selection.OVER,
    Selection.UNDER: Selection.UNDER,
}


def remap_selection(selection: Selection, orientation: MatchOrientation) -> Selection:
    """F-14 step 7: when orientation is SWAPPED, a directional
    selection must be remapped to the OTHER site's actual team before
    it's compared - HOME on the anchor site is AWAY on a swapped
    candidate site. DRAW/OVER/UNDER are never directional, so they pass
    through unchanged regardless of orientation."""
    if orientation is MatchOrientation.SAME:
        return selection
    return _SWAPPED_SELECTION[selection]


def remap_handicap_line(line: Optional[float], orientation: MatchOrientation) -> Optional[float]:
    """F-14 step 7's other half: an Asian Handicap line is signed from
    the home team's perspective (vb.models.MarketSnapshot's own
    docstring) - if home/away is swapped between sites, the line's sign
    must flip too, or a -1.5 home favorite on one site gets compared
    against a nominal -1.5 on the other site when it should be +1.5
    from the (actually-away) team's perspective."""
    if orientation is MatchOrientation.SAME or line is None:
        return line
    return -line
