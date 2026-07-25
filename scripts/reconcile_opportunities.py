"""Standalone runner for merge_databases.py's reconciliation passes -
reconcile_opportunity_groups() (collapses opportunity rows sharing the
same (market_key, first_cross_at) down to one surviving instance,
recovering every group member's snapshots into the survivor first) and
reconcile_snapshot_duplicates() (collapses opportunity_snapshot rows
sharing the same (opportunity_instance_id, captured_at) down to one).
See merge_databases.py's 2026-07-25 bug notes for why both are needed;
both are idempotent and safe to run against any database (a no-op if
there's nothing to collapse).

Usage: python scripts/reconcile_opportunities.py <db_path>
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.merge_databases import reconcile_opportunity_groups, reconcile_snapshot_duplicates


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        return
    conn = sqlite3.connect(sys.argv[1])
    counts = reconcile_opportunity_groups(conn)
    counts.update(reconcile_snapshot_duplicates(conn))
    for key, n in counts.items():
        print(f"{key}: {n}")


if __name__ == "__main__":
    main()
