"""Web server for site/ plus personal picks, calendar feeds, email and user admin.

Serves the static pages like `python -m http.server`, and adds (K = the user's personal key):
  GET  /api/me?key=K              -> {"name", "role"}
  GET  /api/picks?key=K           -> {"ids": [...], "events": [...]}  events selected with "+"
  POST /api/picks?key=K           body {"add": [...], "remove": [...]}
  GET  /my.ics?key=K              -> calendar feed of the user's picks (subscribe to this URL)
  POST /api/email?key=K           body {"to": "a@x.ch, b@y.ch"}  emails the upcoming picks (mailer.py)
admin only:
  GET  /api/users?key=K           -> all users with keys, pick counts, last use
  POST /api/users?key=K           body {"action": "add", "name", "role"} | {"action": "rekey"|"delete", "id"}

Users live in users.json (name, role, key); each user's picks in picks/<id>.json. On the first start
the old single key (picks.key) and selection (picks.json) become the admin user. Event details come
from events.db.
"""
import datetime as dt, hmac, json, os, re, secrets, sqlite3, threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import mailer
from agent import ROOT, TZ, ics_calendar

USERS_FILE, PICKS_DIR = ROOT / "users.json", ROOT / "picks"
ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
LOCK = threading.Lock()


# ---------------------------------------------------------------- storage

def write_json(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    tmp.chmod(0o600)
    tmp.replace(path)


def new_key() -> str:
    return secrets.token_urlsafe(18)


def today() -> str:
    return dt.date.today().isoformat()


def load_users() -> list:
    return json.loads(USERS_FILE.read_text())["users"]


def save_users(users: list):
    write_json(USERS_FILE, {"users": users})


def migrate():
    """First start with users: the old single key and picks become the admin."""
    PICKS_DIR.mkdir(exist_ok=True)
    if USERS_FILE.exists():
        return
    old_key, old_picks = ROOT / "picks.key", ROOT / "picks.json"
    admin = {"id": secrets.token_hex(4), "name": "Admin", "role": "admin",
             "key": old_key.read_text().strip() if old_key.exists() else new_key(),
             "created": today(), "last_seen": ""}
    ids = json.loads(old_picks.read_text())["ids"] if old_picks.exists() else []
    write_json(PICKS_DIR / f"{admin['id']}.json", {"ids": ids})
    save_users([admin])
    for f in (old_key, old_picks):
        f.unlink(missing_ok=True)
    print(f"created admin user; personal link: /my.html?key={admin['key']}", flush=True)


def load_picks(uid: str) -> list:
    try:
        return json.loads((PICKS_DIR / f"{uid}.json").read_text())["ids"]
    except (FileNotFoundError, ValueError, KeyError):
        return []


def save_picks(uid: str, ids: list):
    write_json(PICKS_DIR / f"{uid}.json", {"ids": ids})


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


# ---------------------------------------------------------------- HTTP

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):   # never write keys into server.log
        super().log_message(fmt, *(re.sub(r"key=[^&\s\"]*", "key=…", str(a)) for a in args))

    def end_headers(self):
        # pages change with every scan or update: browsers must check for a new version each time
        path = urlsplit(self.path).path
        if self.command in ("GET", "HEAD") and (path.endswith((".html", ".ics", "/")) or "." not in path):
            if not any(h.startswith(b"Cache-Control") for h in getattr(self, "_headers_buffer", [])):
                self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def user(self, query):
        """The user whose key is in the query, or None. Notes the day of last use."""
        given = query.get("key", [""])[0].encode()
        with LOCK:
            users = load_users()
            for u in users:
                if hmac.compare_digest(given, u["key"].encode()):
                    if u.get("last_seen") != today():
                        u["last_seen"] = today()
                        save_users(users)
                    return u
        return None

    def send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code: int, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    API_GET = ("/api/me", "/api/picks", "/my.ics", "/api/users")

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path not in self.API_GET:
            return super().do_GET()
        u = self.user(parse_qs(url.query))
        if not u:
            return self.send_json(403, {"error": "wrong or missing key"})
        if url.path == "/api/me":
            return self.send_json(200, {"name": u["name"], "role": u["role"]})
        if url.path == "/api/users":
            return self.list_users(u)
        with LOCK:
            ids = load_picks(u["id"])
        if url.path == "/api/picks":   # ids, plus the event details for the My picks tab
            return self.send_json(200, {"ids": ids, "events": picked_events(ids), "name": u["name"],
                                        "role": u["role"]})
        cal = ics_calendar(f"Zurich Events · {u['name']}", picked_events(ids), dt.datetime.now(TZ))
        self.send(200, cal.encode(), "text/calendar; charset=utf-8")

    do_HEAD = do_GET

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path not in ("/api/picks", "/api/email", "/api/users"):
            return self.send_json(404, {"error": "not found"})
        u = self.user(parse_qs(url.query))
        if not u:
            return self.send_json(403, {"error": "wrong or missing key"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 200_000:
            return self.send_json(400, {"error": "bad body"})
        try:
            body = json.loads(self.rfile.read(length))
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            return self.send_json(400, {"error": "bad json"})
        if url.path == "/api/email":
            return self.email(u, body)
        if url.path == "/api/users":
            return self.change_users(u, body)
        add = [i for i in body.get("add", []) if isinstance(i, str) and ID_RE.match(i)]
        remove = {i for i in body.get("remove", []) if isinstance(i, str)}
        with LOCK:
            ids = [i for i in load_picks(u["id"]) if i not in remove]
            ids += [i for i in dict.fromkeys(add) if i not in ids]
            save_picks(u["id"], ids)
        self.send_json(200, {"ids": ids})

    def email(self, u, body):
        try:
            to = mailer.parse_recipients(body.get("to"))
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        with LOCK:
            ids = load_picks(u["id"])
        try:
            mailer.send(to, picked_events(ids))
        except Exception as exc:
            self.log_message("email failed: %s", exc)
            return self.send_json(500, {"error": str(exc)})
        self.log_message("%s emailed their picks to %d recipient(s)", u["name"], len(to))
        self.send_json(200, {"sent": to})

    # ---- admin

    def list_users(self, me):
        if me["role"] != "admin":
            return self.send_json(403, {"error": "admins only"})
        with LOCK:
            users = load_users()
            out = [{k: x.get(k, "") for k in ("id", "name", "role", "key", "created", "last_seen")}
                   | {"picks": len(load_picks(x["id"])), "me": x["id"] == me["id"]} for x in users]
        self.send_json(200, {"users": out})

    def change_users(self, me, body):
        if me["role"] != "admin":
            return self.send_json(403, {"error": "admins only"})
        action = body.get("action")
        with LOCK:
            users = load_users()
            if action == "add":
                name = re.sub(r"\s+", " ", str(body.get("name", ""))).strip()[:40]
                role = "admin" if body.get("role") == "admin" else "user"
                if not name:
                    return self.send_json(400, {"error": "enter a name"})
                if any(x["name"].lower() == name.lower() for x in users):
                    return self.send_json(400, {"error": f"there is already a user called {name}"})
                users.append({"id": secrets.token_hex(4), "name": name, "role": role, "key": new_key(),
                              "created": today(), "last_seen": ""})
            else:
                target = next((x for x in users if x["id"] == body.get("id")), None)
                if not target:
                    return self.send_json(404, {"error": "no such user"})
                if action == "rekey":
                    target["key"] = new_key()
                elif action == "delete":
                    if target["id"] == me["id"]:
                        return self.send_json(400, {"error": "you cannot delete yourself"})
                    users.remove(target)
                    (PICKS_DIR / f"{target['id']}.json").unlink(missing_ok=True)
                else:
                    return self.send_json(400, {"error": "unknown action"})
            save_users(users)
        self.log_message("admin %s: %s %s", me["name"], action, body.get("name") or body.get("id"))
        return self.list_users(me)


if __name__ == "__main__":
    migrate()
    bind, port = os.environ.get("BIND", "127.0.0.1"), int(os.environ.get("PORT", "8765"))
    (ROOT / "site").mkdir(exist_ok=True)
    httpd = ThreadingHTTPServer((bind, port), partial(Handler, directory=str(ROOT / "site")))
    print(f"serving {ROOT / 'site'} on http://{bind}:{port}", flush=True)
    httpd.serve_forever()
