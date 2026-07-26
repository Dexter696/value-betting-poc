"""Closing consensus collection — Phase 5 step 8 of the audit's
remediation roadmap (2026-07-25).

Records a market's consensus closing price — every available site's
own final price for a leg, right before the market stops being
tradeable — independent of whether a bet was ever placed on it. This
is what closing-line value (CLV) analysis needs: how a bet's accepted
price compared to where the market ultimately settled is the standard
sports-betting proxy for "this is a real, skillful edge, not noise
that happened to pay off once" — a signal genuinely orthogonal to
whether any individual bet won or lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .identity import new_id
from .models import ClosingSnapshot, MarketType, Selection
from .storage import save_closing_snapshot


@dataclass(frozen=True)
class SourceClosingPrice:
    site: str
    odds: float


def consensus_closing_odds(prices: list[SourceClosingPrice]) -> float:
    """The consensus is the MEDIAN of every available site's own final
    price for this leg, not the mean — one outlier or stale site (a
    scraper glitch right at kickoff is exactly when capture is least
    reliable) can't single-handedly move the CLV baseline every bet
    gets compared against.
    """
    if not prices:
        raise ValueError("consensus_closing_odds: need at least one source price")
    values = sorted(p.odds for p in prices)
    n = len(values)
    mid = n // 2
    if n % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def closing_line_value(accepted_odds: float, consensus_odds: float) -> float:
    """CLV as a fraction: how much better (positive) or worse
    (negative) the accepted price was than where the market ultimately
    closed. `accepted_odds` should come from BetExecution.accepted_odds
    (vb.execution) — the actually-executed price, not the originally-
    observed one, for the same reason headline P&L uses it."""
    return (accepted_odds / consensus_odds) - 1.0


def record_closing_snapshot(
    conn,
    canonical_event_id: str,
    market_type: MarketType,
    line,
    selection: Selection,
    captured_at: datetime,
    prices: list[SourceClosingPrice],
) -> str:
    """Compute the consensus from `prices` and persist it as a single
    insert-only ClosingSnapshot row, with every contributing source's
    own price kept in `source_json` for later audit (was the consensus
    actually representative, or dragged around by one bad source)."""
    consensus = consensus_closing_odds(prices)
    snapshot_id = new_id()
    save_closing_snapshot(conn, ClosingSnapshot(
        id=snapshot_id, canonical_event_id=canonical_event_id, market_type=market_type, line=line,
        selection=selection, captured_at=captured_at, consensus_odds=consensus,
        source_json={"sources": [{"site": p.site, "odds": p.odds} for p in prices]},
    ))
    return snapshot_id
