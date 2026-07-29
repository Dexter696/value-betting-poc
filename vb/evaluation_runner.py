"""The real join vb.evaluation_v2 has been missing since it was built:
assembles ExecutedBet rows from actual bet_decision/bet_execution/
signal_episode/settlement_version/closing_snapshot data, so
vb.evaluation_v2.build_report() has something real to evaluate instead
of only synthetic test fixtures.

Resolves each bet's canonical_event_id via
vb.settlement_evidence.get_or_create_canonical_event on its own
benchmark market snapshot — safe and idempotent even for a bet that
hasn't settled yet (the bootstrap only needs a real event_version to
exist, not a real settlement); outcome and consensus_closing_odds stay
None until settlement/closing-consensus actually happen for that leg,
exactly as vb.evaluation_v2.ExecutedBet's own docstring expects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .evaluation_v2 import ExecutedBet, build_report
from .models import ExecutionStatus
from .pipeline import parse_market_identity
from .settlement import SettlementResult
from .settlement_evidence import get_or_create_canonical_event, settlement_key
from .storage import current_settlement_version, load_event_version, load_market_snapshot_v2


def assemble_executed_bets(conn, strategy_version: str, now: datetime) -> list[ExecutedBet]:
    rows = conn.execute(
        """
        SELECT bd.id, bd.decided_at, so.benchmark_snapshot_id, se.market_identity_id,
               be.status, be.requested_odds, be.accepted_odds, be.accepted_stake
        FROM bet_decision bd
        JOIN signal_observation so ON so.id = bd.signal_observation_id
        JOIN signal_episode se ON se.id = so.episode_id
        LEFT JOIN bet_execution be ON be.decision_id = bd.id
        WHERE bd.strategy_version = ?
        """,
        (strategy_version,),
    ).fetchall()

    bets: list[ExecutedBet] = []
    for decision_id, decided_at, benchmark_snapshot_id, market_identity_id, status, req_odds, acc_odds, acc_stake in rows:
        benchmark_snapshot = load_market_snapshot_v2(conn, benchmark_snapshot_id)
        if benchmark_snapshot is None:
            continue

        event_version = load_event_version(conn, benchmark_snapshot.event_version_id)
        canonical_id = get_or_create_canonical_event(conn, benchmark_snapshot.event_version_id, sport=event_version.sport, now=now)
        parsed = parse_market_identity(market_identity_id)

        outcome: Optional[SettlementResult] = None
        current = current_settlement_version(
            conn, settlement_key(canonical_id, parsed.market_type, parsed.line, parsed.selection)
        )
        if current is not None:
            outcome = SettlementResult(current.result)

        closing_row = conn.execute(
            "SELECT consensus_odds FROM closing_snapshot WHERE canonical_event_id = ? AND market_type = ? "
            "AND selection = ? AND line IS ? ORDER BY captured_at DESC LIMIT 1",
            (canonical_id, parsed.market_type.value, parsed.selection.value, parsed.line),
        ).fetchone()
        consensus_closing_odds = closing_row[0] if closing_row is not None else None

        bets.append(ExecutedBet(
            strategy_version=strategy_version, canonical_event_id=canonical_id,
            decided_at=datetime.fromisoformat(decided_at), execution_status=ExecutionStatus(status) if status else ExecutionStatus.REJECTED,
            requested_odds=req_odds if req_odds is not None else 0.0, accepted_odds=acc_odds, accepted_stake=acc_stake,
            outcome=outcome, consensus_closing_odds=consensus_closing_odds,
        ))
    return bets


def run_evaluation(
    conn, strategy_version: str, code_sha: str, config: dict, db_snapshot_hash: str, data_cutoff: datetime, now: datetime,
) -> dict:
    """assemble_executed_bets() + vb.evaluation_v2.build_report() in one
    call - the real live entry point a reporting script or dashboard
    build step would use."""
    bets = assemble_executed_bets(conn, strategy_version, now)
    return build_report(conn, bets, strategy_version, code_sha, config, db_snapshot_hash, data_cutoff, now)
