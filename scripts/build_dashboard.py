"""Build the browsable HTML dashboard: every tracked opportunity (open or
closed), each with its full pre-entry + trajectory snapshot history and
settlement result if known. Self-contained single-file output — all data
is embedded as JSON, no live DB connection needed to view it.

Usage: python scripts/build_dashboard.py [output_path]
Defaults to data/dashboard.html if no path given.
"""

import json
import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.models import MarketType, Selection
from vb.reporting import pre_entry_history_for_opportunity
from vb.storage import init_db, load_opportunity, load_settlement

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"
DEFAULT_OUTPUT = Path(__file__).parent.parent / "data" / "dashboard.html"


def _match_label(conn, site, event_id):
    row = conn.execute(
        "SELECT raw_home_team, raw_away_team, competition FROM raw_event WHERE site = ? AND event_id = ?",
        (site, event_id),
    ).fetchone()
    if row is None:
        return None, None, None
    return row[0], row[1], row[2]


def _snapshot_dict(s):
    return {
        "capturedAt": s.captured_at.astimezone(timezone.utc).isoformat(),
        "benchmarkOdds": s.benchmark_odds,
        "comparisonOdds": s.comparison_odds,
        "edgeA": s.edge_a,
        "edgeB": s.edge_b,
        "moved": s.movement_source.value,
    }


def _pre_entry_dict(h):
    return {
        "capturedAt": h.captured_at.astimezone(timezone.utc).isoformat(),
        "benchmarkOdds": h.benchmark_odds,
        "comparisonOdds": h.comparison_odds,
        "edgeA": h.edge_a,
    }


def collect_data(conn) -> dict:
    stats = {
        "matchesCaptured": conn.execute("SELECT COUNT(*) FROM raw_event").fetchone()[0],
        "oddsSnapshots": conn.execute("SELECT COUNT(*) FROM raw_market_snapshot").fetchone()[0],
        "liveOpportunities": conn.execute("SELECT COUNT(*) FROM opportunity WHERE resolved_at IS NULL").fetchone()[0],
        "resolvedOpportunities": conn.execute("SELECT COUNT(*) FROM opportunity WHERE resolved_at IS NOT NULL").fetchone()[0],
        "pendingReviews": conn.execute("SELECT COUNT(*) FROM event_match_review WHERE status = 'pending'").fetchone()[0],
    }

    opportunities = []
    instance_ids = [r[0] for r in conn.execute("SELECT instance_id FROM opportunity ORDER BY first_cross_at DESC")]

    for iid in instance_ids:
        opp = load_opportunity(conn, iid)
        benchmark_event_id = opp.market_key.split(":")[1]
        home, away, competition = _match_label(conn, opp.benchmark_site, benchmark_event_id)
        if home is None:
            continue

        outcome = load_settlement(conn, opp.benchmark_site, benchmark_event_id, opp.market_type, opp.line, opp.selection)

        try:
            pre_entry = pre_entry_history_for_opportunity(conn, opp, limit=5)
        except Exception:
            pre_entry = []

        home_goals = away_goals = None
        if outcome is not None:
            row = conn.execute(
                "SELECT home_goals, away_goals FROM settlement WHERE benchmark_site=? AND benchmark_event_id=? LIMIT 1",
                (opp.benchmark_site, benchmark_event_id),
            ).fetchone()
            if row:
                home_goals, away_goals = row

        opportunities.append({
            "instanceId": opp.instance_id,
            "home": home,
            "away": away,
            "competition": competition,
            "marketType": opp.market_type.value,
            "line": opp.line,
            "selection": opp.selection.value,
            "benchmarkSite": opp.benchmark_site,
            "comparisonSite": opp.comparison_site,
            "isOpen": opp.is_open,
            "resolutionReason": opp.resolution_reason.value if opp.resolution_reason else None,
            "entryEdgeA": opp.entry_edge_a,
            "peakEdgeA": opp.peak_edge_a,
            "firstCrossAt": opp.first_cross_at.astimezone(timezone.utc).isoformat(),
            "resolvedAt": opp.resolved_at.astimezone(timezone.utc).isoformat() if opp.resolved_at else None,
            "convergenceSeconds": opp.convergence_time.total_seconds() if opp.convergence_time else None,
            "outcome": outcome.value if outcome else None,
            "homeGoals": home_goals,
            "awayGoals": away_goals,
            "preEntry": [_pre_entry_dict(h) for h in pre_entry],
            "snapshots": [_snapshot_dict(s) for s in opp.snapshots],
        })

    return {"stats": stats, "opportunities": opportunities}


def main() -> None:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    conn = init_db(DB_PATH)
    data = collect_data(conn)

    template_path = Path(__file__).parent / "dashboard_template.html"
    template = template_path.read_text(encoding="utf-8")
    html = template.replace("__DASHBOARD_DATA__", json.dumps(data))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"wrote {len(data['opportunities'])} opportunities -> {output_path}")


if __name__ == "__main__":
    main()
