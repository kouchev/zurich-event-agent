"""Web server for site/ plus the "my picks" calendar.

Serves the static pages like `python -m http.server`, and adds:
  GET  /api/picks?key=K           -> {"ids": [...]}  the events selected with "+" on the pages
  POST /api/picks?key=K           body {"add": [...], "remove": [...]}
  GET  /my.ics?key=K              -> calendar feed of the selected events (subscribe to this URL)
  POST /api/email?key=K           body {"to": "a@x.ch, b@y.ch"}  emails the upcoming picks (mailer.py)

K is the secret in picks.key (created on first start). Without it nobody can read or change the
selection. The selection is stored in picks.json; event details come from events.db.
"""
import datetime as dt, hmac, json, os, re, secrets, sqlite3, threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import mailer
from agent import ROOT, TZ, ics_calendar

KEY_FILE, PICKS_FILE = ROOT / "picks.key", ROOT / "picks.json"
ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
LOCK = threading.Lock()


def secret() -> str:
    if not KEY_FILE.exists():
        KEY_FILE.write_text(secrets.token_urlsafe(18) + "\n")
        KEY_FILE.chmod(0o600)
    return KEY_FILE.read_text().strip()


KEY = secret()


def load_picks() -> list:
    try:
        return json.loads(PICKS_FILE.read_text())["ids"]
    except (FileNotFoundError, ValueError, KeyError):
        return []


def save_picks(ids: list):
    tmp = PICKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ids": ids}, indent=0))
    tmp.replace(PICKS_FILE)


def picked_events(ids: list) -> list:
    if not ids:
        return []
    con = sqlite3.connect(ROOT / "events.db")
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(f"SELECT * FROM events WHERE id IN ({','.join('?' * len(ids))}) ORDER BY start",
                           ids).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


class Handler(SimpleHTTPRequestHandler):
    def authorized(self, query) -> bool:
        return hmac.compare_digest(query.get("key", [""])[0].encode(), KEY.encode())

    def send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code: int, obj):
        self.send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path.startswith("/shared/") and not re.fullmatch(r"/shared/[\w-]+\.ics", url.path):
            return self.send_json(404, {"error": "not found"})   # no folder listing of email snapshots
        if url.path not in ("/api/picks", "/my.ics"):
            return super().do_GET()
        if not self.authorized(query):
            return self.send_json(403, {"error": "wrong or missing key"})
        with LOCK:
            ids = load_picks()
        if url.path == "/api/picks":   # ids, plus the event details for the My picks tab
            return self.send_json(200, {"ids": ids, "events": picked_events(ids)})
        cal = ics_calendar("Zurich Events · My picks", picked_events(ids), dt.datetime.now(TZ))
        self.send(200, cal.encode(), "text/calendar; charset=utf-8")

    do_HEAD = do_GET

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path not in ("/api/picks", "/api/email"):
            return self.send_json(404, {"error": "not found"})
        if not self.authorized(parse_qs(url.query)):
            return self.send_json(403, {"error": "wrong or missing key"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 200_000:
            return self.send_json(400, {"error": "bad body"})
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return self.send_json(400, {"error": "bad json"})
        if url.path == "/api/email":
            return self.email(body)
        try:
            add = [i for i in body.get("add", []) if isinstance(i, str) and ID_RE.match(i)]
            remove = {i for i in body.get("remove", []) if isinstance(i, str)}
        except (ValueError, AttributeError):
            return self.send_json(400, {"error": "bad json"})
        with LOCK:
            ids = [i for i in load_picks() if i not in remove]
            ids += [i for i in dict.fromkeys(add) if i not in ids]
            save_picks(ids)
        self.send_json(200, {"ids": ids})

    def email(self, body):
        try:
            to = mailer.parse_recipients(body.get("to") if isinstance(body, dict) else None)
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        with LOCK:
            ids = load_picks()
        try:
            mailer.send(to, picked_events(ids))
        except Exception as exc:
            self.log_message("email failed: %s", exc)
            return self.send_json(500, {"error": str(exc)})
        self.log_message("emailed my picks to %d recipient(s)", len(to))
        self.send_json(200, {"sent": to})


if __name__ == "__main__":
    bind, port = os.environ.get("BIND", "127.0.0.1"), int(os.environ.get("PORT", "8765"))
    (ROOT / "site").mkdir(exist_ok=True)
    httpd = ThreadingHTTPServer((bind, port), partial(Handler, directory=str(ROOT / "site")))
    print(f"serving {ROOT / 'site'} on http://{bind}:{port}", flush=True)
    httpd.serve_forever()
