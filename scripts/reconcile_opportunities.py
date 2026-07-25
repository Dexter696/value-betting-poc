"""Standalone runner for merge_databases.reconcile_opportunity_groups() -
collapses opportunity rows that share the same (market_key,
first_cross_at) down to one surviving instance, recovering every
group member's snapshots into the survivor first. See merge_databases.py's
2026-07-25 bug note for why this is needed and why it's safe to run
against any database (idempotent; a no-op if there's nothing to collapse).

Usage: python scripts/reconcile_opportunities.py <db_path>
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.merge_databases import reconcile_opportunity_groups


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        return
    conn = sqlite3.connect(sys.argv[1])
    counts = reconcile_opportunity_groups(conn)
    for key, n in counts.items():
        print(f"{key}: {n}")


if __name__ == "__main__":
    main()
