"""View and act on the event-match review queue (event_match_review table).

Usage:
  python scripts/review_queue.py list
  python scripts/review_queue.py approve <id>
  python scripts/review_queue.py reject <id>

`list` also prints each candidate's actual team names (looked up from
raw_event) so a human can tell at a glance whether it's really the same
fixture — the review table itself only stores site+event_id.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.storage import init_db, list_pending_reviews, set_review_status

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"


def _event_label(conn, site: str, event_id: str) -> str:
    row = conn.execute(
        "SELECT raw_home_team, raw_away_team, competition FROM raw_event WHERE site = ? AND event_id = ?",
        (site, event_id),
    ).fetchone()
    if row is None:
        return f"{site}:{event_id} (event details not found)"
    home, away, competition = row
    return f"{home} - {away} ({competition})"


def cmd_list(conn) -> None:
    pending = list_pending_reviews(conn)
    if not pending:
        print("No pending reviews.")
        return
    for r in pending:
        print(f"[{r['id']}] score={r['score']:.2f}  {', '.join(r['reasons']) or '(no specific reason logged)'}")
        print(f"    {r['benchmark_site']}: {_event_label(conn, r['benchmark_site'], r['benchmark_event_id'])}")
        print(f"    {r['comparison_site']}: {_event_label(conn, r['comparison_site'], r['comparison_event_id'])}")
        print()


def cmd_set_status(conn, review_id: int, status: str) -> None:
    set_review_status(conn, review_id, status)
    print(f"[{review_id}] marked {status}.")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    conn = init_db(DB_PATH)
    command = sys.argv[1]

    if command == "list":
        cmd_list(conn)
    elif command in ("approve", "reject"):
        if len(sys.argv) < 3:
            print(f"usage: python scripts/review_queue.py {command} <id>")
            return
        cmd_set_status(conn, int(sys.argv[2]), "approved" if command == "approve" else "rejected")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
