"""Opportunity lifecycle: turns a stream of per-leg edge readings into
Opportunity instances with their full snapshot trajectory.

An opportunity is a continuous period during which one match+market+leg's
Method-A edge stays at or above THRESHOLD — not a single point. It opens
on first cross, keeps a snapshot for every reading while open (even as the
edge grows well past the entry threshold), and closes when the edge drops
back below threshold, the market is suspended, or the event starts. A
later re-crossing of the same leg is a new instance, linked to earlier
ones via a shared `market_key` so re-occurrence can be analyzed later.

Entry timing (first-cross vs. peak vs. every snapshot) is deliberately
NOT decided here — that is a post-processing choice made against the
stored trajectory, mirroring how Method A vs B is decided after capture,
not at capture time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from .models import MarketType, Selection

THRESHOLD = 0.03  # 3% Method-A edge, per the methodology's capture threshold

# Odds readings that differ by less than this are float noise, not a
# genuine price move; keeps movement attribution from flip-flopping on
# re-quotes of an unchanged price.
ODDS_MOVE_EPSILON = 1e-6


class ResolutionReason(str, Enum):
    DROPPED_BELOW_THRESHOLD = "dropped_below_threshold"
    MARKET_SUSPENDED = "market_suspended"
    EVENT_STARTED = "event_started"


class MovementSource(str, Enum):
    BENCHMARK = "benchmark"
    COMPARISON = "comparison"
    BOTH = "both"
    NEITHER = "neither"


@dataclass(frozen=True)
class LegReading:
    """One sample of a specific match+market+leg, merging the benchmark
    and comparison book's state at a point in time. This is what scrapers
    feed the tracker once matching.py has paired the two sides' markets.
    """

    captured_at: datetime
    edge_a: float
    edge_b: float
    benchmark_odds: float
    comparison_odds: float
    max_bet_size: Optional[float] = None
    full_market: dict = field(default_factory=dict)
    market_suspended: bool = False
    event_started: bool = False


@dataclass(frozen=True)
class OpportunitySnapshot:
    opportunity_instance_id: str
    captured_at: datetime
    edge_a: float
    edge_b: float
    benchmark_odds: float
    comparison_odds: float
    movement_source: MovementSource
    max_bet_size: Optional[float]
    full_market: dict


@dataclass
class Opportunity:
    """Header for one continuous above-threshold period. `market_key` is
    stable across re-occurrences of the same match+market+leg (many
    Opportunity instances can share one); `instance_id` is unique per
    continuous period.
    """

    instance_id: str
    market_key: str
    sport: str
    benchmark_site: str
    comparison_site: str
    market_type: MarketType
    line: Optional[float]
    selection: Selection
    first_cross_at: datetime
    snapshots: list[OpportunitySnapshot] = field(default_factory=list)
    resolved_at: Optional[datetime] = None
    resolution_reason: Optional[ResolutionReason] = None

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    @property
    def entry_edge_a(self) -> float:
        return self.snapshots[0].edge_a

    @property
    def entry_edge_b(self) -> float:
        return self.snapshots[0].edge_b

    @property
    def peak_snapshot(self) -> OpportunitySnapshot:
        return max(self.snapshots, key=lambda s: s.edge_a)

    @property
    def peak_edge_a(self) -> float:
        return self.peak_snapshot.edge_a

    @property
    def time_to_peak(self) -> timedelta:
        return self.peak_snapshot.captured_at - self.first_cross_at

    @property
    def convergence_time(self) -> Optional[timedelta]:
        if self.resolved_at is None:
            return None
        return self.resolved_at - self.first_cross_at


class OpportunityTracker:
    """Consumes an ordered stream of LegReading for a single market_key
    and emits completed Opportunity instances (with full trajectory) as
    they close. One tracker per watched leg; the caller fans out one per
    match+market+leg being monitored.
    """

    def __init__(
        self,
        market_key: str,
        sport: str,
        benchmark_site: str,
        comparison_site: str,
        market_type: MarketType,
        line: Optional[float],
        selection: Selection,
        threshold: float = THRESHOLD,
    ):
        self.market_key = market_key
        self.sport = sport
        self.benchmark_site = benchmark_site
        self.comparison_site = comparison_site
        self.market_type = market_type
        self.line = line
        self.selection = selection
        self.threshold = threshold

        self._open: Optional[Opportunity] = None
        self._instance_seq = 0
        self._last_reading: Optional[LegReading] = None
        self.completed: list[Opportunity] = []

    @property
    def current(self) -> Optional[Opportunity]:
        """Whichever Opportunity was touched by the most recent ingest():
        the still-open one, or the one that just closed if this call
        closed it. None if nothing has ever crossed threshold."""
        if self._open is not None:
            return self._open
        return self.completed[-1] if self.completed else None

    @classmethod
    def resume(cls, opportunity: Opportunity, threshold: float = THRESHOLD) -> "OpportunityTracker":
        """Rebuild a tracker from a previously-persisted, still-open
        Opportunity, so a fresh process (e.g. the next scheduled pipeline
        run) can keep appending to the same instance instead of starting
        a new one. `opportunity` must have resolved_at is None and at
        least one snapshot — load it via storage.load_open_opportunity.
        """
        if opportunity.resolved_at is not None:
            raise ValueError("cannot resume an already-resolved opportunity")
        if not opportunity.snapshots:
            raise ValueError("cannot resume an opportunity with no snapshots")

        tracker = cls(
            market_key=opportunity.market_key,
            sport=opportunity.sport,
            benchmark_site=opportunity.benchmark_site,
            comparison_site=opportunity.comparison_site,
            market_type=opportunity.market_type,
            line=opportunity.line,
            selection=opportunity.selection,
            threshold=threshold,
        )
        tracker._open = opportunity
        tracker._instance_seq = int(opportunity.instance_id.rsplit("#", 1)[-1])
        last = opportunity.snapshots[-1]
        tracker._last_reading = LegReading(
            captured_at=last.captured_at,
            edge_a=last.edge_a,
            edge_b=last.edge_b,
            benchmark_odds=last.benchmark_odds,
            comparison_odds=last.comparison_odds,
            max_bet_size=last.max_bet_size,
            full_market=last.full_market,
        )
        return tracker

    def ingest(self, reading: LegReading) -> None:
        # A reading that isn't newer than the last one we saw isn't a new
        # odds sample - it's the pipeline being re-run against data that
        # hasn't been re-captured yet (e.g. running the pipeline script
        # manually between scheduled cycles). Recording it anyway would
        # pad the trajectory with duplicate rows that look like real (but
        # suspiciously identical) samples, so it's a no-op. Exception:
        # event_started/market_suspended are computed from wall-clock time
        # at pipeline-run time, not from captured_at, so a match can
        # legitimately transition to "started" between two calls that see
        # the exact same frozen odds - those must still get through so
        # the tracker actually closes.
        if (
            not reading.event_started
            and not reading.market_suspended
            and self._last_reading is not None
            and reading.captured_at <= self._last_reading.captured_at
        ):
            return

        movement = self._movement_source(reading)

        if self._open is None:
            self._last_reading = reading
            if not reading.market_suspended and not reading.event_started and reading.edge_a >= self.threshold:
                self._instance_seq += 1
                self._open = Opportunity(
                    instance_id=f"{self.market_key}#{self._instance_seq}",
                    market_key=self.market_key,
                    sport=self.sport,
                    benchmark_site=self.benchmark_site,
                    comparison_site=self.comparison_site,
                    market_type=self.market_type,
                    line=self.line,
                    selection=self.selection,
                    first_cross_at=reading.captured_at,
                )
                self._append_snapshot(reading, movement)
            return

        if reading.market_suspended:
            self._close(reading, movement, ResolutionReason.MARKET_SUSPENDED)
        elif reading.event_started:
            self._close(reading, movement, ResolutionReason.EVENT_STARTED)
        elif reading.edge_a < self.threshold:
            self._close(reading, movement, ResolutionReason.DROPPED_BELOW_THRESHOLD)
        else:
            self._append_snapshot(reading, movement)

        self._last_reading = reading

    def _append_snapshot(self, reading: LegReading, movement: MovementSource) -> None:
        assert self._open is not None
        self._open.snapshots.append(
            OpportunitySnapshot(
                opportunity_instance_id=self._open.instance_id,
                captured_at=reading.captured_at,
                edge_a=reading.edge_a,
                edge_b=reading.edge_b,
                benchmark_odds=reading.benchmark_odds,
                comparison_odds=reading.comparison_odds,
                movement_source=movement,
                max_bet_size=reading.max_bet_size,
                full_market=reading.full_market,
            )
        )

    def _close(self, reading: LegReading, movement: MovementSource, reason: ResolutionReason) -> None:
        assert self._open is not None
        # Log the closing reading too so the trajectory shows the actual
        # decay path down to/through the threshold, not just the last
        # in-opportunity point.
        self._append_snapshot(reading, movement)
        self._open.resolved_at = reading.captured_at
        self._open.resolution_reason = reason
        self.completed.append(self._open)
        self._open = None

    def _movement_source(self, reading: LegReading) -> MovementSource:
        prev = self._last_reading
        if prev is None:
            return MovementSource.NEITHER
        benchmark_moved = abs(reading.benchmark_odds - prev.benchmark_odds) > ODDS_MOVE_EPSILON
        comparison_moved = abs(reading.comparison_odds - prev.comparison_odds) > ODDS_MOVE_EPSILON
        if benchmark_moved and comparison_moved:
            return MovementSource.BOTH
        if benchmark_moved:
            return MovementSource.BENCHMARK
        if comparison_moved:
            return MovementSource.COMPARISON
        return MovementSource.NEITHER
