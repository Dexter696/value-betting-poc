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

    `received_at` is the last real ODDS observation's own timestamp -
    NOT necessarily when the pipeline process actually ran. `now` (F-03)
    is that real wall-clock time, used only for closing an episode on
    market_suspended/event_started: the transition itself happened at
    `now`, using data that may have been captured much earlier (no new
    snapshot since kickoff is the common case, not an edge case).
    `kickoff_utc` is the benchmark event's kickoff, used as the audit's
    prescribed floor on an event_started close (`ended_at = max(now,
    kickoff_utc)`) so a late pipeline run can never record an episode as
    having ended before the match it's closing on actually started.
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
    now: Optional[datetime] = None
    kickoff_utc: Optional[datetime] = None


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

    def _find_observation_by_snapshot_pair(self, episode_id: str, reading: LegReadingV2) -> Optional[str]:
        """The real identity check for "has this exact reading already
        been recorded" (found by self-review, 2026-07-29): the
        market_suspended/event_started close branches used to decide
        reuse-vs-record by comparing `reading.received_at` to the last
        observation's own timestamp, which is only a proxy for "same
        snapshot pair" - two genuinely DIFFERENT snapshot pairs sharing
        a timestamp (unlikely but not impossible) would have been
        silently treated as identical, closing the episode against the
        wrong pointer. This queries signal_observation's own UNIQUE key
        directly instead."""
        row = self.conn.execute(
            "SELECT id FROM signal_observation WHERE episode_id = ? AND benchmark_snapshot_id = ? "
            "AND comparison_snapshot_id = ? AND edge_model = ?",
            (episode_id, reading.benchmark_snapshot_id, reading.comparison_snapshot_id, reading.edge_model),
        ).fetchone()
        return row[0] if row is not None else None

    def _observation_id_for_close(self, reading: LegReadingV2, episode_id: str) -> str:
        """Reuse the existing observation for this exact snapshot pair
        if one's already recorded (F-03: don't fabricate a duplicate
        market observation just because the episode is closing); record
        a genuinely new one otherwise. Shared by the market_suspended
        and event_started branches below - both need exactly this same
        decision."""
        obs_id = self._find_observation_by_snapshot_pair(episode_id, reading)
        return obs_id if obs_id is not None else self._record_observation(reading, episode_id)

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
            #
            # Guarded the same way the eligible path below is guarded:
            # if the underlying data hasn't advanced since the last
            # recorded observation (the market's "latest" snapshot pair
            # is unchanged - a real possibility under an irregular
            # capture cadence, F-07), re-recording it would violate
            # signal_observation's UNIQUE(episode_id, benchmark_snapshot_id,
            # comparison_snapshot_id, edge_model) constraint and crash
            # the whole pipeline cycle (found live 2026-07-28) - skip as
            # a no-op instead. Only relevant when there's an open
            # episode to check against; a NULL episode_id never
            # collides with another NULL under SQL's own NULL != NULL
            # semantics, so no guard is needed when existing is None.
            if existing is not None:
                last_at = self._last_observation_time(existing.id)
                if last_at is not None and reading.received_at <= last_at:
                    return EpisodeIngestResult(episode_id=existing.id, observation_id=None)
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
            # F-03: don't fabricate a new market observation just because
            # the state changed - reuse the existing observation for this
            # exact snapshot pair if one's already recorded (still a
            # valid, real observation for vb.closing to key off).
            obs_id = self._observation_id_for_close(reading, existing.id)
            transition_at = reading.now or reading.received_at
            close_signal_episode(self.conn, existing.id, transition_at, EpisodeEndReason.MARKET_SUSPENDED)
            return EpisodeIngestResult(episode_id=existing.id, observation_id=obs_id, closed=True)
        if reading.event_started:
            obs_id = self._observation_id_for_close(reading, existing.id)
            # F-03's precise fix: ended_at = max(process_now, kickoff_at)
            # - the state transition happened at `now`, using data that
            # may have been captured well before kickoff (the common
            # case, not an edge case), and must never be recorded as
            # ending before the kickoff it's closing on.
            transition_now = reading.now or reading.received_at
            ended_at = max(transition_now, reading.kickoff_utc) if reading.kickoff_utc is not None else transition_now
            close_signal_episode(self.conn, existing.id, ended_at, EpisodeEndReason.EVENT_STARTED)
            return EpisodeIngestResult(episode_id=existing.id, observation_id=obs_id, closed=True)
        if reading.edge < self.threshold:
            obs_id = self._record_observation(reading, existing.id)
            close_signal_episode(self.conn, existing.id, reading.received_at, EpisodeEndReason.DROPPED_BELOW_THRESHOLD)
            return EpisodeIngestResult(episode_id=existing.id, observation_id=obs_id, closed=True)

        obs_id = self._record_observation(reading, existing.id)
        return EpisodeIngestResult(episode_id=existing.id, observation_id=obs_id)
