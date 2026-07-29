"""Time-aligned feature dataset builder — Phase 4 step 4 of the
audit's remediation roadmap (2026-07-25).

Every value on a FeatureRow is computed ONLY from information that was
genuinely available at the observation's own decision_time — the
benchmark market as it was actually captured for that specific
observation, never anything from later in the episode's own future.
This is the same no-look-ahead discipline F-05 forced onto the entry-
policy state machines (vb.strategy), applied here to feature
engineering: a model trained on this dataset can never accidentally
learn from information a live system wouldn't have had yet.

Scoped to observations with a real episode_id — a signal_observation
recorded for an ineligible reading with NO open episode (episode_id is
NULL) has no other way to recover which market/selection it concerned
(the schema doesn't duplicate market identity onto every observation
row, only onto its episode), so those rows are skipped here, not
guessed at.

settlement_result is a best-effort join, not a guarantee: it's only
populated if this leg's canonical event has already been bootstrapped
(vb.settlement_evidence.get_or_create_canonical_event, called from
real settlement) AND actually settled. This module never creates a
canonical_event itself — that's settlement's job, not feature
engineering's — so an unsettled or not-yet-bootstrapped leg is a
perfectly valid, still-useful unlabeled row (a real dataset has both).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .fair_probability import DEVIG_METHODS, source_dispersion
from .pipeline import parse_market_identity
from .settlement_evidence import settlement_key
from .storage import current_settlement_version, list_all_signal_observations, load_market_snapshot_v2, load_signal_episode


@dataclass(frozen=True)
class FeatureRow:
    observation_id: str
    episode_id: str
    market_identity_id: str
    strategy_version: str
    decision_time: datetime
    edge: float
    eligible: bool
    reject_reason: Optional[str]
    fair_probabilities: dict  # {method_name: probability or None}
    fair_probability_dispersion: Optional[float]
    settlement_result: Optional[str]


def build_feature_row(conn, observation) -> Optional[FeatureRow]:
    """One feature row for a single SignalObservation, or None if it
    can't be built (no episode link - see module docstring - or the
    benchmark snapshot it referenced is somehow missing)."""
    if observation.episode_id is None:
        return None

    episode = load_signal_episode(conn, observation.episode_id)
    if episode is None:
        return None

    benchmark_snapshot = load_market_snapshot_v2(conn, observation.benchmark_snapshot_id)
    if benchmark_snapshot is None:
        return None

    parsed = parse_market_identity(episode.market_identity_id)
    odds_list = [o.odds for o in benchmark_snapshot.outcomes]
    selections = [o.selection for o in benchmark_snapshot.outcomes]

    fair_probabilities: dict = {}
    for method_name, devig_fn in DEVIG_METHODS.items():
        try:
            idx = selections.index(parsed.selection)
            probs = devig_fn(odds_list)
            fair_probabilities[method_name] = probs[idx]
        except (ValueError, ZeroDivisionError):
            fair_probabilities[method_name] = None

    known_probs = [v for v in fair_probabilities.values() if v is not None]
    dispersion = source_dispersion(known_probs) if len(known_probs) >= 2 else None

    settlement_result = None
    row = conn.execute(
        "SELECT canonical_event_id FROM event_match WHERE event_version_id = ? LIMIT 1",
        (benchmark_snapshot.event_version_id,),
    ).fetchone()
    if row is not None:
        key = settlement_key(row[0], parsed.market_type, parsed.line, parsed.selection)
        current = current_settlement_version(conn, key)
        if current is not None:
            settlement_result = current.result

    return FeatureRow(
        observation_id=observation.id, episode_id=episode.id, market_identity_id=episode.market_identity_id,
        strategy_version=episode.strategy_version, decision_time=observation.decision_time, edge=observation.edge,
        eligible=observation.eligible, reject_reason=observation.reject_reason.value if observation.reject_reason else None,
        fair_probabilities=fair_probabilities, fair_probability_dispersion=dispersion,
        settlement_result=settlement_result,
    )


def build_feature_dataset(conn, limit: Optional[int] = None) -> list[FeatureRow]:
    """Every buildable FeatureRow across the whole database, oldest
    first. `limit`, if given, caps how many signal_observation rows are
    considered (not how many rows come back - skipped observations
    still count against it), so a caller can page through a very large
    table without loading it all into a single query.
    """
    rows: list[FeatureRow] = []
    for observation in list_all_signal_observations(conn, limit=limit):
        feature_row = build_feature_row(conn, observation)
        if feature_row is not None:
            rows.append(feature_row)
    return rows
