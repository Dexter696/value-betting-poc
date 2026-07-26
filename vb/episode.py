"""Schema-v2 replacement for vb.opportunity.OpportunityTracker.

The direct fix for the audit's F-02 finding. v1's OpportunityTracker
kept its "next instance number" in an in-memory counter
(`_instance_seq`) that always restarted at 0 in a fresh process; a
re-crossing after a close, handled by a later, separate process (every
scheduled pipeline run is a fresh Python process), had no way to know
that number should NOT be 0 again, so it could re-mint an instance_id
an earlier, unrelated opportunity had already used - and
save_opportunity()'s upsert-plus-delete-and-rewrite-snapshots then
silently destroyed that earlier opportunity's real trajectory.

EpisodeTracker has no in-memory identity state at all. Every ingest()
call asks the database, fresh, "is there currently an open episode for
this market_identity_id+strategy_version?" (vb.storage.
find_open_signal_episode) - and if it needs to open a new one, mints a
UUID via vb.identity.new_id(), which cannot collide with any prior
identity regardless of how many separate processes have touched this
market before. There is nothing to "resume" and nothing to "forget."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .identity import new_id
from .models import EpisodeEndReason, RejectReason, SignalEpisode, SignalObservation
from .storage import (
    close_signal_episode,
    find_open_signal_episode,
    open_signal_episode,
    save_signal_observation,
)

# Readings whose decision_time isn't strictly newer than the open
# episode's last recorded observation are a no-op, UNLESS they signal
# market_suspended/event_started - mirrors v1's OpportunityTracker.ingest()
# duplicate-timestamp guard (vb/opportunity.py) and the same rationale:
# those two signals are computed from wall-clock time at pipeline-run
# time, not from the reading's own timestamp, so a match can legitimately
# transition to "started" between two calls that otherwise see the exact
# same frozen quotes - that transition must still get through so the
# episode actually closes.


@dataclass(frozen=True)
class LegReadingV2:
    """One post-freshness-gate reading for a specific market_identity_id
    under one strategy. `eligible`/`reject_reason` come from
    vb.freshness.check_freshness() - an ineligible reading is still
    recorded as a signal_observation (never silently dropped), it just
    never opens or extends an episode.
    """

    received_at: datetime
    edge: float
    benchmark_snapshot_id: str
    comparison_snapshot_id: str
    edge_model: str
    eligible: bool = True
    reject_reason: Optional[RejectReason] = None
    market_suspended: bool = False
    event_started: bool = False


@dataclass
class EpisodeIngestResult:
    episode_id: Optional[str]
    observation_id: Optional[str]
    opened: bool = False
    closed: bool = False


class EpisodeTracker:
    """One tracker per (strategy_version, market_identity_id) pair,
    but - unlike v1's OpportunityTracker - genuinely stateless across
    calls: every method re-derives what it needs from the database.
    Safe (indeed, intended) to construct a brand-new instance on every
    single ingest() call from a fresh process; nothing here depends on
    being the same Python object across calls.
    """

    def __init__(self, conn, strategy_version: str, market_identity_id: str, threshold: float):
        self.conn = conn
        self.strategy_version = strategy_version
        self.market_identity_id = market_identity_id
        self.threshold = threshold

    def _last_observation_time(self, episode_id: str) -> Optional[datetime]:
        row = self.conn.execute(
            "SELECT MAX(decision_time) FROM signal_observation WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return datetime.fromisoformat(row[0])

    def _record_observation(self, reading: LegReadingV2, episode_id: Optional[str]) -> str:
        obs_id = new_id()
        save_signal_observation(self.conn, SignalObservation(
            id=obs_id, decision_time=reading.received_at,
            benchmark_snapshot_id=reading.benchmark_snapshot_id,
            comparison_snapshot_id=reading.comparison_snapshot_id,
            edge_model=reading.edge_model, edge=reading.edge, eligible=reading.eligible,
            episode_id=episode_id, reject_reason=reading.reject_reason,
        ))
        return obs_id

    def ingest(self, reading: LegReadingV2) -> EpisodeIngestResult:
        existing = find_open_signal_episode(self.conn, self.strategy_version, self.market_identity_id)

        if not reading.eligible:
            # F-01: a rejected pair is still an auditable observation,
            # tied to the currently-open episode if there is one (so its
            # trajectory shows the reject), but never opens or extends one.
            obs_id = self._record_observation(reading, existing.id if existing else None)
            return EpisodeIngestResult(episode_id=existing.id if existing else None, observation_id=obs_id)

        if existing is None:
            if reading.market_suspended or reading.event_started or reading.edge < self.threshold:
                return EpisodeIngestResult(episode_id=None, observation_id=None)
            episode_id = new_id()
            open_signal_episode(self.conn, SignalEpisode(
                id=episode_id, strategy_version=self.strategy_version,
                market_identity_id=self.market_identity_id, started_at=reading.received_at,
            ))
            obs_id = self._record_observation(reading, episode_id)
            return EpisodeIngestResult(episode_id=episode_id, observation_id=obs_id, opened=True)

        last_at = self._last_observation_time(existing.id)
        if (
            not reading.event_started
            and not reading.market_suspended
            and last_at is not None
            and reading.received_at <= last_at
        ):
            return EpisodeIngestResult(episode_id=existing.id, observation_id=None)

        if reading.market_suspended:
            obs_id = self._record_observation(reading, existing.id)
            close_signal_episode(self.conn, existing.id, reading.received_at, EpisodeEndReason.MARKET_SUSPENDED)
            return EpisodeIngestResult(episode_id=existing.id, observation_id=obs_id, closed=True)
        if reading.event_started:
            obs_id = self._record_observation(reading, existing.id)
            close_signal_episode(self.conn, existing.id, reading.received_at, EpisodeEndReason.EVENT_STARTED)
            return EpisodeIngestResult(episode_id=existing.id, observation_id=obs_id, closed=True)
        if reading.edge < self.threshold:
            obs_id = self._record_observation(reading, existing.id)
            close_signal_episode(self.conn, existing.id, reading.received_at, EpisodeEndReason.DROPPED_BELOW_THRESHOLD)
            return EpisodeIngestResult(episode_id=existing.id, observation_id=obs_id, closed=True)

        obs_id = self._record_observation(reading, existing.id)
        return EpisodeIngestResult(episode_id=existing.id, observation_id=obs_id)
