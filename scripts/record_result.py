"""Record a match's final score and settle every leg that was ever
tracked as an opportunity for it (across every comparison site).

Usage:
  python scripts/record_result.py <pinnacle_event_id> <home_goals> <away_goals>

Example:
  python scripts/record_result.py 1632011616 2 1
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.storage import init_db, record_match_result

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"
BENCHMARK_SITE = "pinnacle.com"


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        return

    event_id, home_goals, away_goals = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

    conn = init_db(DB_PATH)
    row = conn.execute(
        "SELECT raw_home_team, raw_away_team, competition FROM raw_event WHERE site = ? AND event_id = ?",
        (BENCHMARK_SITE, event_id),
    ).fetchone()
    if row is None:
        print(f"No event {event_id} found for {BENCHMARK_SITE} — check the id (see raw_event table).")
        return
    home, away, competition = row

    settled_count = record_match_result(conn, BENCHMARK_SITE, event_id, home_goals, away_goals, source="manual")
    print(f"{home} {home_goals}-{away_goals} {away} ({competition})")
    print(f"settled {settled_count} leg(s) that had been tracked as opportunities for this match")


if __name__ == "__main__":
    main()
