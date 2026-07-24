"""Local review UI: a tiny HTTP server (stdlib only, no dependencies)
serving a one-at-a-time approve/decline interface for the event-match
review queue, showing full match context (home/away for BOTH sites) so a
human can actually judge whether it's the same fixture at a glance,
instead of reading bare site+event_id pairs.

Decisions write directly to the database via the existing
storage.set_review_status() - no copy-pasting IDs back and forth.

Usage: python scripts/review_ui.py
Then open http://127.0.0.1:8765 in your browser.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from vb.storage import init_db, list_pending_reviews, set_review_status

DB_PATH = Path(__file__).parent.parent / "data" / "vb.sqlite"
PORT = 8765
TEMPLATE_PATH = Path(__file__).parent / "review_ui_template.html"


def _match_context(conn, site, event_id):
    row = conn.execute(
        "SELECT raw_home_team, raw_away_team, competition, kickoff_utc FROM raw_event WHERE site = ? AND event_id = ?",
        (site, event_id),
    ).fetchone()
    if row is None:
        return {"site": site, "home": None, "away": None, "competition": None, "kickoff": None}
    return {"site": site, "home": row[0], "away": row[1], "competition": row[2], "kickoff": row[3]}


def _pending_payload():
    conn = init_db(DB_PATH)
    try:
        pending = list_pending_reviews(conn)
        return [
            {
                "id": r["id"],
                "score": r["score"],
                "reasons": r["reasons"],
                "benchmark": _match_context(conn, r["benchmark_site"], r["benchmark_event_id"]),
                "comparison": _match_context(conn, r["comparison_site"], r["comparison_event_id"]),
            }
            for r in pending
        ]
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the console quiet

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(TEMPLATE_PATH.read_text(encoding="utf-8"))
        elif path == "/api/pending":
            self._send_json(_pending_payload())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/decide":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            review_id = int(data["id"])
            action = data["action"]
            if action not in ("approved", "rejected"):
                self._send_json({"error": "invalid action"}, status=400)
                return
            conn = init_db(DB_PATH)
            try:
                set_review_status(conn, review_id, action)
            finally:
                conn.close()
            self._send_json({"ok": True})
        else:
            self.send_response(404)
            self.end_headers()


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Review UI running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
