"""Run a single Pinnacle capture cycle for one or more leagues and store it
to data/vb.sqlite.

Usage: python scripts/capture_pinnacle.py [league_id ...]
Defaults to England - Premier League (1980) if no league ids are given.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.sources.pinnacle import PinnacleClient
from vb.storage import init_db, save_raw_capture

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"
DEFAULT_LEAGUE_IDS = [1980]  # England - Premier League


def main() -> None:
    league_ids = [int(a) for a in sys.argv[1:]] or DEFAULT_LEAGUE_IDS

    conn = init_db(DB_PATH)
    client = PinnacleClient()

    total_events = 0
    total_snapshots = 0
    for league_id in league_ids:
        rows = client.fetch_league(league_id)
        for row in rows:
            save_raw_capture(conn, row.event, row.snapshots)
        total_events += len(rows)
        total_snapshots += sum(len(r.snapshots) for r in rows)

    print(f"captured {total_events} events, {total_snapshots} market snapshots -> {DB_PATH}")


if __name__ == "__main__":
    main()
