"""Core data model for odds snapshots, shared by every site's scraper.

A RawEvent is a match as reported by one site. A MarketSnapshot is one
market (match winner / handicap / totals) on one event on one site,
captured at a point in time. Everything downstream (matching, edge
calculation) is built on these two shapes so scrapers only need to
translate site-specific payloads into them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class MarketType(str, Enum):
    MATCH_WINNER = "match_winner"      # 1X2
    ASIAN_HANDICAP = "asian_handicap"
    TOTALS = "totals"                  # over/under


class Selection(str, Enum):
    HOME = "home"
    AWAY = "away"
    DRAW = "draw"
    OVER = "over"
    UNDER = "under"


@dataclass(frozen=True)
class RawEvent:
    """A match as reported by one site, before cross-site matching."""

    site: str
    sport: str
    competition: str
    kickoff_utc: datetime
    raw_home_team: str
    raw_away_team: str
    event_id: str  # site's own identifier, kept for traceability/debugging


@dataclass(frozen=True)
class Outcome:
    selection: Selection
    odds: float


@dataclass(frozen=True)
class MarketSnapshot:
    """One market on one event on one site, at one point in time.

    `line` is None for match_winner. For asian_handicap it is always
    expressed from the HOME team's perspective (negative = home favored)
    regardless of how the source site quoted it — see
    normalize.canonical_handicap_line. For totals it is the total itself
    (e.g. 2.5).
    """

    event: RawEvent
    market_type: MarketType
    line: Optional[float]
    outcomes: tuple[Outcome, ...]
    captured_at: datetime
    max_bet_size: Optional[float] = None  # site's stated cap on this market, when exposed


# ============================================================
# SCHEMA V2 (2026-07-25 external-audit remediation, Phase 1)
# ============================================================
# Mirrors the schema-v2 tables added to vb/schema.sql - see that
# file's own header comment for the append-only discipline every one
# of these entities must uphold. Every `id` field is minted via
# vb.identity.new_id() at the moment of true creation, never derived
# from a counter or resumed/reconstructed - see identity.py's
# docstring for why that distinction is the actual fix for the
# audit's F-02 finding (a process-restart identity collision that
# could silently overwrite an earlier opportunity's history).
#
# Named with a `V2`/`2` suffix wherever a v1 name already exists in
# this module or in vb.matching (MarketSnapshot, EventMatch,
# MatchTier) to keep the frozen legacy dataset's types and the new
# ones unambiguous even if both ever need to be imported together.


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class CaptureRun:
    """One real (scheduled or manual) invocation of the capture+
    pipeline cycle - versioned and statused, unlike v1 where "a Python
    process ran" was an untracked, unversioned event (F-06, F-16)."""

    id: str
    started_at: datetime
    git_sha: str
    schema_version: int
    scheduled_for: Optional[str] = None
    finished_at: Optional[datetime] = None
    status: RunStatus = RunStatus.RUNNING


@dataclass
class SourceRun:
    """The result of one specific scraper within a capture_run. A
    signal must never be computed from a source_run that isn't
    SUCCESS - this is what F-01's freshness gate and F-16's "no green
    run on partial failure" fix both check against."""

    id: str
    capture_run_id: str
    site: str
    mode: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: RunStatus = RunStatus.RUNNING
    event_count: Optional[int] = None
    snapshot_count: Optional[int] = None
    http_error_count: int = 0
    error_code: Optional[str] = None
    error_summary: Optional[str] = None


@dataclass(frozen=True)
class EventVersionV2:
    """A source's own view of one event, versioned - a kickoff-time or
    name correction creates a NEW row rather than overwriting the old
    one (F-03's requirement that a later correction can't silently
    rewrite a decision that was made under the earlier belief)."""

    id: str
    site: str
    event_id: str
    valid_from: datetime
    source_run_id: str
    sport: str
    competition: str
    kickoff_utc: datetime
    home_team: str
    away_team: str


@dataclass(frozen=True)
class CanonicalEvent:
    """A stable real-world fixture identity that multiple sources'
    EventVersionV2 rows link to via EventMatchV2 (F-14)."""

    id: str
    sport: str
    created_at: datetime


class MatchRole(str, Enum):
    BENCHMARK = "benchmark"
    COMPARISON = "comparison"


class MatchOrientation(str, Enum):
    SAME = "same"
    SWAPPED = "swapped"


class MatchTierV2(str, Enum):
    AUTO = "auto"
    REVIEW = "review"
    REJECTED = "rejected"


@dataclass(frozen=True)
class EventMatchV2:
    """One source's link into a CanonicalEvent, with orientation
    recorded explicitly (F-14) so a directional market (handicap,
    1X2) can never be interpreted without knowing whether home/away
    came out swapped between sites."""

    id: str
    canonical_event_id: str
    event_version_id: str
    role: MatchRole
    orientation: MatchOrientation
    score: float
    score_components: dict
    model_version: str
    tier: MatchTierV2
    decided_at: datetime
    review_status: Optional[str] = None  # "approved" | "rejected" | None


@dataclass(frozen=True)
class MarketSnapshotV2:
    """Immutable market price, tied to the source_run/event_version
    that produced it, with both a source-reported observed_at (when
    the source exposes one) and a local received_at set at the moment
    of receipt - the pair F-01's freshness/skew gate is computed
    from. Never pruned or deleted, unlike v1's raw_market_snapshot."""

    id: str
    source_run_id: str
    event_version_id: str
    market_type: MarketType
    line: Optional[float]
    outcomes: tuple[Outcome, ...]
    received_at: datetime
    max_bet_size: Optional[float] = None
    observed_at: Optional[datetime] = None
    request_started_at: Optional[datetime] = None
    request_finished_at: Optional[datetime] = None


@dataclass(frozen=True)
class StrategyDefinition:
    """One immutable, hashed strategy configuration. A change to any
    parameter is a NEW row with a new id, never an edit - so a
    SignalEpisode's strategy_version always points at exactly the
    rules that were active when it was created (F-06)."""

    id: str
    signal_model: str
    threshold: float
    max_age_s: float
    max_skew_s: float
    min_lead_time_s: float
    config: dict
    config_hash: str
    created_at: datetime


class EpisodeEndReason(str, Enum):
    DROPPED_BELOW_THRESHOLD = "dropped_below_threshold"
    MARKET_SUSPENDED = "market_suspended"
    EVENT_STARTED = "event_started"


@dataclass
class SignalEpisode:
    """One continuous online above-threshold period - the schema-v2
    replacement for v1's Opportunity. `id` is a UUID minted once, at
    true creation - NEVER derived from a process-local counter (that
    was F-02's entire root cause: a fresh process has no memory of a
    prior process's counter state, so it could re-mint an id an
    earlier, unrelated episode already used). `market_identity_id`
    encodes canonical_event + market_type + line + selection +
    comparison site; the (strategy_version, market_identity_id,
    started_at) uniqueness is what makes a genuine re-crossing safely
    distinguishable from an accidental double-open, with no sequence
    number involved at all."""

    id: str
    strategy_version: str
    market_identity_id: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    end_reason: Optional[EpisodeEndReason] = None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


class RejectReason(str, Enum):
    BENCHMARK_STALE = "benchmark_stale"
    COMPARISON_STALE = "comparison_stale"
    SNAPSHOT_SKEW = "snapshot_skew"
    SOURCE_FAILED = "source_failed"
    INSUFFICIENT_LEAD_TIME = "insufficient_lead_time"


@dataclass(frozen=True)
class SignalObservation:
    """One (benchmark_snapshot, comparison_snapshot) pair evaluated
    under one edge model - append-only replacement for v1's
    OpportunitySnapshot (which was deleted-and-rewritten wholesale on
    every save, the exact mechanism that let F-02's history-overwrite
    destroy an earlier episode's trajectory). A REJECTED pair (stale,
    skewed, or a failed source) is still recorded here with
    eligible=False and a reject_reason - a reject is an auditable
    observation, not silence (F-01)."""

    id: str
    decision_time: datetime
    benchmark_snapshot_id: str
    comparison_snapshot_id: str
    edge_model: str
    edge: float
    eligible: bool
    episode_id: Optional[str] = None
    reject_reason: Optional[RejectReason] = None


class BetDecisionChoice(str, Enum):
    BET = "bet"
    SKIP = "skip"


@dataclass(frozen=True)
class BetDecision:
    """Exactly one decision per idempotency_key. F-09's "at most one
    bet per event+market+line+selection+site+strategy_version" policy
    is enforced by what the caller hashes into that key, not by this
    table alone."""

    id: str
    strategy_version: str
    signal_observation_id: str
    decided_at: datetime
    decision: BetDecisionChoice
    reason: str
    idempotency_key: str
    intended_odds: Optional[float] = None
    intended_stake: Optional[float] = None


class ExecutionStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PARTIAL = "partial"
    PRICE_CHANGED = "price_changed"


@dataclass(frozen=True)
class BetExecution:
    """The paper (or, later, real) execution outcome of one
    BetDecision. Populated by Phase 5's execution model
    (vb/execution.py) - the type exists now so Phase 1's schema and
    Phase 5's behavior can be built independently without a later
    breaking change to this shape."""

    id: str
    decision_id: str
    requested_at: datetime
    status: ExecutionStatus
    requested_odds: float
    requested_stake: float
    responded_at: Optional[datetime] = None
    accepted_odds: Optional[float] = None
    accepted_stake: Optional[float] = None
    external_bet_id: Optional[str] = None


@dataclass(frozen=True)
class ResultEvidence:
    """The auditable replacement for v1 settlement.source being a bare
    string like "manual" or "manual:websearch" (F-17). An automatic
    source records `raw_payload_hash` (content-addressed archive of the
    real response - see vb.settlement_evidence); a manual correction
    is expected to carry `source_url`, `reviewer`, and `reviewed_at`
    instead, per the audit's own remediation text ("manual correction
    should require a URL, reviewer, and reason")."""

    id: str
    canonical_event_id: str
    provider: str
    retrieved_at: datetime
    status: str
    provider_event_id: Optional[str] = None
    source_url: Optional[str] = None
    home_goals: Optional[int] = None
    away_goals: Optional[int] = None
    raw_payload_hash: Optional[str] = None
    reviewer: Optional[str] = None
    reviewed_at: Optional[datetime] = None


@dataclass(frozen=True)
class SettlementVersion:
    """Versioned settlement result (F-17) - a correction creates a NEW
    row with `supersedes_id` pointing at the row it replaces; the old
    row is never edited or deleted, so a later correction can never
    silently rewrite what an earlier evaluation actually used."""

    id: str
    settlement_key: str
    evidence_id: str
    algorithm_version: str
    result: str  # a SettlementResult.value - kept as str to avoid a settlement.py <-> models.py import cycle
    created_at: datetime
    supersedes_id: Optional[str] = None


@dataclass(frozen=True)
class EvaluationRun:
    """One evaluation report's full provenance (F-06/F-20) - code SHA,
    config hash, exact DB snapshot hash, and data cutoff, embedded in
    the report itself so it can never be mistaken for describing a
    different code/data state than it actually used."""

    id: str
    code_sha: str
    config_hash: str
    db_snapshot_hash: str
    data_cutoff: datetime
    created_at: datetime
    metrics: dict


@dataclass(frozen=True)
class ClosingSnapshot:
    """A market's consensus closing price - the reference point CLV
    (closing-line value, vb.closing) is measured against. Populated by
    Phase 5's closing-consensus collection (vb/closing.py), independent
    of whether a bet was ever placed on this leg."""

    id: str
    canonical_event_id: str
    market_type: MarketType
    line: Optional[float]
    selection: Selection
    captured_at: datetime
    consensus_odds: float
    source_json: dict
