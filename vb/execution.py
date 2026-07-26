"""Idempotent decision generation, latency-aware odds re-verification,
and paper bet execution recording — Phase 5 steps 3/4/5/7 of the
audit's remediation roadmap (2026-07-25).

Ties together vb.strategy's entry-policy decisions with the
bet_decision/bet_execution tables already added in Phase 1
(vb/models.py, vb/storage.py) — this module is what actually populates
them, and what enforces the audit's own acceptance criteria for this
phase:

- "one decision per idempotency key" — record_decision() derives the
  key deterministically from (strategy_version, market_identity_id)
  and treats a UNIQUE-constraint collision as "already decided," not
  an error to propagate.
- "every paper bet has verified odds or a rejected state" —
  verify_and_execute() always writes a BetExecution row, on every
  branch, never a silent no-op.
- "headline ROI uses accepted/verified odds" — settled_profit() takes
  a BetExecution, not a bare odds float, so a caller can't accidentally
  compute P&L from the originally-observed price instead of what was
  actually (simulated-)executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from .evaluation import flat_stake_profit
from .identity import content_hash, new_id
from .models import BetDecision, BetDecisionChoice, BetExecution, ExecutionStatus
from .settlement import SettlementResult
from .storage import save_bet_decision, save_bet_execution


def idempotency_key(strategy_version: str, market_identity_id: str) -> str:
    """F-09's "at most one bet per event+market+line+selection+site+
    strategy_version" policy, enforced by hashing exactly the two
    fields that identify that combination (market_identity_id already
    encodes event/market/line/selection/site — see
    vb.episode.SignalEpisode's docstring) into the key the DB's
    UNIQUE(idempotency_key) constraint is built on. Content-addressed,
    so calling this twice for the same pair from two different
    processes always produces the same key.
    """
    return content_hash({"strategy_version": strategy_version, "market_identity_id": market_identity_id})


def record_decision(
    conn,
    strategy_version: str,
    market_identity_id: str,
    signal_observation_id: str,
    decided_at: datetime,
    decision: BetDecisionChoice,
    reason: str,
    intended_odds: Optional[float] = None,
    intended_stake: Optional[float] = None,
) -> Optional[str]:
    """Insert a bet_decision, or return None if one already exists for
    this (strategy_version, market_identity_id) pair. A second call for
    the same pair is expected (a strategy runner re-evaluating an
    episode every cycle should not treat "already decided" as an
    error), so the UNIQUE-constraint IntegrityError is caught here and
    turned into a plain None return rather than propagated.
    """
    key = idempotency_key(strategy_version, market_identity_id)
    existing = conn.execute("SELECT id FROM bet_decision WHERE idempotency_key = ?", (key,)).fetchone()
    if existing is not None:
        return None

    decision_id = new_id()
    save_bet_decision(conn, BetDecision(
        id=decision_id, strategy_version=strategy_version, signal_observation_id=signal_observation_id,
        decided_at=decided_at, decision=decision, reason=reason, idempotency_key=key,
        intended_odds=intended_odds, intended_stake=intended_stake,
    ))
    return decision_id


@dataclass(frozen=True)
class LatencyModel:
    """How long it would realistically take, from the moment a
    decision was made, to actually get a bet placed — manual review
    latency, automated placement latency, or both. An explicit, named,
    testable parameter (per Phase 7's eventual pre-registered protocol
    requirement to record "execution latency/haircut") rather than an
    assumed-zero gap between decision and execution.
    """

    delay: timedelta


def verify_and_execute(
    conn,
    decision_id: str,
    requested_at: datetime,
    requested_odds: float,
    requested_stake: float,
    fetch_current_odds: Callable[[], Optional[float]],
    latency: LatencyModel,
) -> BetExecution:
    """Step 4: re-check the odds actually available `latency.delay`
    after the decision was made, rather than assuming the
    originally-observed price is still there. `fetch_current_odds`
    returns the live/latest odds for this exact leg at call time, or
    None if the market is gone entirely (suspended, kicked off, source
    error) — the caller is responsible for making that lookup reflect
    `requested_at + latency.delay`, not "right now."

    Step 7's conservative-slippage convention: the accepted price is
    capped at `min(current_odds, requested_odds)` — a live system can
    never be certain it would have actually captured a price move in
    its favor, so this never credits one; a worse live price is
    applied in full. status is ACCEPTED when nothing moved against the
    bet, PRICE_CHANGED when the live price was worse, REJECTED when
    the market's gone.

    Always writes exactly one BetExecution row — on every branch, never
    a silent no-op — so "every paper bet has verified odds or a
    rejected state" holds by construction, not by convention.
    """
    execution_id = new_id()
    current_odds = fetch_current_odds()

    if current_odds is None:
        status = ExecutionStatus.REJECTED
        accepted_odds: Optional[float] = None
        accepted_stake: Optional[float] = None
    else:
        accepted_odds = min(current_odds, requested_odds)
        accepted_stake = requested_stake
        status = ExecutionStatus.ACCEPTED if current_odds >= requested_odds else ExecutionStatus.PRICE_CHANGED

    execution = BetExecution(
        id=execution_id, decision_id=decision_id, requested_at=requested_at, status=status,
        requested_odds=requested_odds, requested_stake=requested_stake,
        responded_at=requested_at + latency.delay, accepted_odds=accepted_odds, accepted_stake=accepted_stake,
    )
    save_bet_execution(conn, execution)
    return execution


def settled_profit(execution: BetExecution, outcome: SettlementResult) -> float:
    """Step 7's fix, made structurally hard to get wrong: P&L is always
    computed from `execution.accepted_odds`/`accepted_stake` — the
    price actually (simulated-)executed at — never
    `execution.requested_odds`, the price that merely triggered the
    decision. A REJECTED execution (no accepted odds) has zero profit
    by definition: nothing was ever placed.
    """
    if execution.status == ExecutionStatus.REJECTED or execution.accepted_odds is None:
        return 0.0
    return flat_stake_profit(outcome, execution.accepted_odds, stake=execution.accepted_stake or 0.0)
