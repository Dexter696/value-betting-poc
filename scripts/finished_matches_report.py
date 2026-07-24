"""Print the finished-matches report: every settled match, each tracked
opportunity leg on it, every book's odds at entry, entry/peak edge, time
to convergence, and the result.

Usage: python scripts/finished_matches_report.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.reporting import build_finished_matches_report
from vb.storage import init_db

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"


def main() -> None:
    conn = init_db(DB_PATH)
    reports = build_finished_matches_report(conn)

    if not reports:
        print("No settled matches yet.")
        return

    for r in reports:
        print(f"{r.home_team} {r.home_goals}-{r.away_goals} {r.away_team}  ({r.competition})")
        for leg in r.legs:
            odds = ", ".join(f"{b.site}={b.odds if b.odds is not None else 'n/a'}" for b in leg.book_odds)
            conv = leg.convergence if leg.convergence is not None else ("still open" if leg.is_open else "n/a")
            outcome = leg.outcome.value if leg.outcome else "unsettled"
            print(f"  {leg.market_type.value} {leg.line} {leg.selection.value}: {outcome}")
            print(f"    entry={leg.entry_edge_a:.1%}  peak={leg.peak_edge_a:.1%}  convergence={conv}")
            print(f"    odds: {odds}")
        print()


if __name__ == "__main__":
    main()
