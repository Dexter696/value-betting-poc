"""Build the browsable/analysis HTML dashboard: every tracked opportunity
(open or closed), each with its full pre-entry + trajectory snapshot
history (all 3 books' full odds at each point, not just the tracked
selection), settlement result if known, and enough per-leg data embedded
for the page's own client-side P&L simulator (see dashboard_template.html)
to recompute flat vs. fractional-Kelly staking scenarios live in the
browser. Self-contained single-file output — all data is embedded as
JSON, no live DB connection needed to view it.

Usage: python scripts/build_dashboard.py [output_path]
Defaults to data/dashboard.html if no path given.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.evaluation import odds_bucket
from vb.models import RawEvent
from vb.reporting import ALL_SITES, _find_matching_event_id, outcomes_at_or_before, pre_entry_history_for_opportunity
from vb.storage import init_db, load_opportunity, load_settlement

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"
DEFAULT_OUTPUT = Path(__file__).parent.parent / "data" / "dashboard.html"


def _match_label(conn, site, event_id):
    row = conn.execute(
        "SELECT raw_home_team, raw_away_team, competition, kickoff_utc FROM raw_event WHERE site = ? AND event_id = ?",
        (site, event_id),
    ).fetchone()
    if row is None:
        return None, None, None, None
    return row[0], row[1], row[2], row[3]


def _settlement_source(conn, benchmark_site, benchmark_event_id, market_type, line, selection):
    row = conn.execute(
        "SELECT source FROM settlement WHERE benchmark_site=? AND benchmark_event_id=? AND market_type=? "
        "AND (line IS ? OR line = ?) AND selection=?",
        (benchmark_site, benchmark_event_id, market_type.value, line, line, selection.value),
    ).fetchone()
    return row[0] if row else None


def _books_dict(benchmark_site, benchmark_outcomes, comparison_site, comparison_outcomes, third_site, third_outcomes):
    return {
        benchmark_site: benchmark_outcomes,
        comparison_site: comparison_outcomes,
        third_site: third_outcomes,
    }


def _pre_entry_dict(h, benchmark_site, comparison_site, third_site, third_outcomes):
    return {
        "capturedAt": h.captured_at.astimezone(timezone.utc).isoformat(),
        "edgeA": h.edge_a,
        "books": _books_dict(benchmark_site, h.benchmark_outcomes, comparison_site, h.comparison_outcomes, third_site, third_outcomes),
    }


def _snapshot_dict(s, benchmark_site, comparison_site, third_site, third_outcomes):
    full = s.full_market or {}
    return {
        "capturedAt": s.captured_at.astimezone(timezone.utc).isoformat(),
        "edgeA": s.edge_a,
        "edgeB": s.edge_b,
        "moved": s.movement_source.value,
        "books": _books_dict(
            benchmark_site, (full.get("benchmark") or {}).get("outcomes"),
            comparison_site, (full.get("comparison") or {}).get("outcomes"),
            third_site, third_outcomes,
        ),
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

    # Matching against the third site is a fuzzy-scored lookup (see
    # reporting._find_matching_event_id) - cache per (site, benchmark
    # event id) so a match tracked across many snapshots only pays for
    # it once, not once per snapshot.
    third_event_id_cache: dict[tuple[str, str], str] = {}

    for iid in instance_ids:
        opp = load_opportunity(conn, iid)
        benchmark_event_id = opp.market_key.split(":")[1]
        home, away, competition, kickoff_utc = _match_label(conn, opp.benchmark_site, benchmark_event_id)
        if home is None:
            continue

        third_site = next(s for s in ALL_SITES if s not in (opp.benchmark_site, opp.comparison_site))

        cache_key = (third_site, benchmark_event_id)
        if cache_key not in third_event_id_cache:
            benchmark_event = RawEvent(
                site=opp.benchmark_site, sport=opp.sport, competition=competition,
                kickoff_utc=datetime.fromisoformat(kickoff_utc),
                raw_home_team=home, raw_away_team=away, event_id=benchmark_event_id,
            )
            third_event_id_cache[cache_key] = _find_matching_event_id(conn, third_site, benchmark_event)
        third_event_id = third_event_id_cache[cache_key]

        outcome = load_settlement(conn, opp.benchmark_site, benchmark_event_id, opp.market_type, opp.line, opp.selection)
        settled_source = _settlement_source(conn, opp.benchmark_site, benchmark_event_id, opp.market_type, opp.line, opp.selection)

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

        pre_entry_dicts = []
        for h in pre_entry:
            third_outcomes = outcomes_at_or_before(conn, third_site, third_event_id, opp.market_type, opp.line, h.captured_at)
            pre_entry_dicts.append(_pre_entry_dict(h, opp.benchmark_site, opp.comparison_site, third_site, third_outcomes))

        snapshot_dicts = []
        for s in opp.snapshots:
            third_outcomes = outcomes_at_or_before(conn, third_site, third_event_id, opp.market_type, opp.line, s.captured_at)
            snapshot_dicts.append(_snapshot_dict(s, opp.benchmark_site, opp.comparison_site, third_site, third_outcomes))

        opportunities.append({
            "instanceId": opp.instance_id,
            "home": home,
            "away": away,
            "competition": competition,
            "kickoffUtc": kickoff_utc,
            "marketType": opp.market_type.value,
            "line": opp.line,
            "selection": opp.selection.value,
            "benchmarkSite": opp.benchmark_site,
            "comparisonSite": opp.comparison_site,
            "thirdSite": third_site,
            "isOpen": opp.is_open,
            "resolutionReason": opp.resolution_reason.value if opp.resolution_reason else None,
            "entryEdgeA": opp.entry_edge_a,
            "entryEdgeB": opp.entry_edge_b,
            "peakEdgeA": opp.peak_edge_a,
            "entryBenchmarkOdds": opp.snapshots[0].benchmark_odds,
            "entryComparisonOdds": opp.snapshots[0].comparison_odds,
            "bucket": odds_bucket(opp.snapshots[0].benchmark_odds),
            "firstCrossAt": opp.first_cross_at.astimezone(timezone.utc).isoformat(),
            "resolvedAt": opp.resolved_at.astimezone(timezone.utc).isoformat() if opp.resolved_at else None,
            # `is not None`, not truthiness: a zero-duration convergence_time
            # (entry and resolution landed in the same reading - e.g. kickoff
            # fell inside a single 5-minute capture gap, so the first
            # reading that ever saw edge>=3% also saw event_started) is a
            # real, valid timedelta(0) - but timedelta(0) is falsy in
            # Python, so `if opp.convergence_time` silently turned 17 real
            # zero-convergence opportunities (23% of resolved ones, at last
            # count) into "no data" instead of "0 min".
            "convergenceSeconds": opp.convergence_time.total_seconds() if opp.convergence_time is not None else None,
            "outcome": outcome.value if outcome else None,
            "settledSource": settled_source,
            "homeGoals": home_goals,
            "awayGoals": away_goals,
            "preEntry": pre_entry_dicts,
            "snapshots": snapshot_dicts,
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
