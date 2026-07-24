"""Automatically settle finished matches using ESPN's public scoreboard
API (vb.sources.results), falling back to leaving a match pending for
manual entry (scripts/record_result.py) when it can't be automated -
unmapped competition, no ESPN fixture found, or no confident team match.

Usage:
  python scripts/auto_settle.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.sources.results import find_result
from vb.storage import init_db, list_unsettled_matches, record_match_result

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"
BENCHMARK_SITE = "pinnacle.com"


def main() -> None:
    conn = init_db(DB_PATH)
    unsettled = list_unsettled_matches(conn, BENCHMARK_SITE)

    settled, still_pending = 0, []
    for event in unsettled:
        result = find_result(event.competition, event.raw_home_team, event.raw_away_team, event.kickoff_utc)
        if result is None:
            still_pending.append(event)
            continue
        home_goals, away_goals = result
        legs = record_match_result(conn, BENCHMARK_SITE, event.event_id, home_goals, away_goals, source="auto:espn")
        print(f"settled: {event.raw_home_team} {home_goals}-{away_goals} {event.raw_away_team} "
              f"({event.competition}) - {legs} leg(s)")
        settled += 1

    print(f"\n{settled} match(es) auto-settled, {len(still_pending)} still need manual entry")
    for event in still_pending:
        print(f"  pending: {event.raw_home_team} vs {event.raw_away_team} ({event.competition}) "
              f"[{event.site}:{event.event_id}]")


if __name__ == "__main__":
    main()
