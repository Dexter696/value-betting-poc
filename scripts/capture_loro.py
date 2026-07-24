"""Run a single Loro football capture cycle and store it to data/vb.sqlite.

Usage: python scripts/capture_loro.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.sources.loro import LoroClient
from vb.storage import init_db, save_raw_capture

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"


def main() -> None:
    conn = init_db(DB_PATH)
    client = LoroClient(headless=True)
    rows = client.fetch_football()

    for row in rows:
        save_raw_capture(conn, row.event, row.snapshots)

    n_snapshots = sum(len(r.snapshots) for r in rows)
    print(f"captured {len(rows)} events, {n_snapshots} market snapshots -> {DB_PATH}")


if __name__ == "__main__":
    main()
