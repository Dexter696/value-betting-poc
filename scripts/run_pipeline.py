"""Run the matching+edge pipeline over whatever's latest in data/vb.sqlite
for each comparison site vs. Pinnacle (the benchmark), and print what it
found. Doesn't scrape anything itself — run the capture_*.py scripts
first.

Usage: python scripts/run_pipeline.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.pipeline import run_cycle
from vb.storage import init_db

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"
BENCHMARK_SITE = "pinnacle.com"
COMPARISON_SITES = ["swisslos.ch", "loro.ch"]


def main() -> None:
    conn = init_db(DB_PATH)

    for comparison_site in COMPARISON_SITES:
        touched = run_cycle(conn, BENCHMARK_SITE, comparison_site)
        print(f"\n=== {BENCHMARK_SITE} vs {comparison_site}: {len(touched)} opportunity instance(s) touched ===")
        for opp in sorted(touched, key=lambda o: o.entry_edge_a, reverse=True):
            status = "OPEN" if opp.is_open else f"CLOSED ({opp.resolution_reason.value})"
            print(
                f"  [{status}] {opp.market_key} "
                f"entry_edge_a={opp.entry_edge_a:.2%} peak_edge_a={opp.peak_edge_a:.2%} "
                f"snapshots={len(opp.snapshots)}"
            )


if __name__ == "__main__":
    main()
