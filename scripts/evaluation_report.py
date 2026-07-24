"""Print the Method A vs B evaluation: bucketed by benchmark odds range,
Method A's own stats vs. the subset where Method B also agreed.

Usage: python scripts/evaluation_report.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.evaluation import DEFAULT_BUCKETS, DEFAULT_KELLY_FRACTION, evaluate
from vb.storage import init_db

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"


def _fmt_stats(stats) -> str:
    if stats.n == 0:
        return "no settled bets yet"
    hit = f"{stats.hit_rate:.0%}" if stats.hit_rate is not None else "n/a"
    return (
        f"n={stats.n}  avg_edge_a={stats.avg_edge_a:+.1%}  avg_edge_b={stats.avg_edge_b:+.1%}  hit_rate={hit}\n"
        f"    flat:  roi={stats.flat_roi:+.1%}  profit={stats.total_profit:+.2f}u  (staked={stats.total_staked:.0f}u)\n"
        f"    kelly: roi={stats.kelly_roi:+.1%}  profit={stats.total_profit_kelly:+.2f}u  (staked={stats.total_staked_kelly:.2f}u)"
    )


def main() -> None:
    conn = init_db(DB_PATH)
    report = evaluate(conn)

    print(f"(kelly scenario uses fractional-Kelly at {DEFAULT_KELLY_FRACTION:.0%} of full Kelly)")
    print()
    print("=== Overall ===")
    print(f"Method A (all captured bets):        {_fmt_stats(report.overall_a)}")
    print(f"Method B agrees (edge_b >= 3% too):   {_fmt_stats(report.overall_b_agrees)}")
    print()

    for label, _low, _high in DEFAULT_BUCKETS:
        print(f"=== {label} (benchmark odds {_low}-{_high}) ===")
        print(f"  Method A:        {_fmt_stats(report.by_bucket_a[label])}")
        print(f"  Method B agrees: {_fmt_stats(report.by_bucket_b_agrees[label])}")
        print()

    if report.excluded_unsettled_or_unmatched:
        print(f"(skipped {report.excluded_unsettled_or_unmatched} settled leg(s) with no snapshot data)")


if __name__ == "__main__":
    main()
