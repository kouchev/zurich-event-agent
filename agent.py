#!/usr/bin/env python3
"""Zurich event agent: scrape venue programmes, filter, store, render a static page.

Run:  .venv/bin/python agent.py            (fetch + render)
      .venv/bin/python agent.py --render   (re-render page from the database only)
"""
import argparse, datetime as dt, hashlib, html, json, logging, re, sqlite3, sys, time
import tomllib, urllib.parse, urllib.request, urllib.robotparser
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Zurich")
CFG = tomllib.loads((ROOT / "config.toml").read_text())
UA = CFG["general"]["user_agent"]
log = logging.getLogger("agent")


# ---------------------------------------------------------------- fetching

_robots: dict[str, urllib.robotparser.RobotFileParser] = {}
_last_hit: dict[str, float] = {}


def fetch(url: str) -> str:
    """GET a URL politely: honest user agent, robots.txt, per-host delay."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    if host not in _robots:
        # Read robots.txt with our own UA: RobotFileParser.read() sends Python's default UA,
        # which some sites reject with 403, and it then treats the whole site as disallowed.
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(f"{parts.scheme}://{host}/robots.txt", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                rp.parse(r.read().decode("utf-8", "replace").splitlines())
        except urllib.error.HTTPError as e:
            rp.disallow_all = e.code in (401, 403)   # same semantics as RobotFileParser.read()
            rp.allow_all = e.code >= 400 and not rp.disallow_all
        _robots[host] = rp
    if not _robots[host].can_fetch(UA, url):
        raise PermissionError(f"robots.txt disallows {url}")
    wait = CFG["general"]["request_delay_seconds"] - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de,en"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", "replace")
    _last_hit[host] = time.time()
    return body


def soup(url: str) -> BeautifulSoup:
    s = BeautifulSoup(fetch(url), "html.parser")
    for t in s(["style", "svg", "noscript"]):
        t.decompose()
    return s


def text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def ev(source, venue, title, start, url, subtitle="", category="", allday=False, end=None):
    """end is set for long-running items (exhibitions); they stay listed until it passes."""
    return dict(source=source, venue=venue, title=title.strip(), subtitle=subtitle.strip(),
                start=start, url=url, category=category, allday=allday, end=end)


def horizon() -> dt.datetime:
    return dt.datetime.now(TZ) + dt.timedelta(days=CFG["general"]["days_ahead"])


def paged(parse_page, max_pages=None):
    """Call parse_page(n) for n = 1, 2, ... until a page is empty or passes the horizon."""
    out = []
    for n in range(1, (max_pages or CFG["general"]["max_pages"]) + 1):
        events = parse_page(n)
        out += events
        if not events or max(e["start"] for e in events) > horizon():
            break
    return out


def infer_year(day: int, month: int) -> int:
    """For dates printed without a year: pick the next occurrence (allowing 1 month back)."""
    today = dt.date.today()
    year = today.year
    if dt.date(year, month, day) < today - dt.timedelta(days=31):
        year += 1
    return year


def local(y, mo, d, hh=0, mm=0) -> dt.datetime:
    return dt.datetime(y, mo, d, hh, mm, tzinfo=TZ)


# ---------------------------------------------------------------- parsers
# Each parser returns a list of event dicts; failures are caught per source.

def opernhaus():
    return paged(lambda n: _opernhaus_page("https://www.opernhaus.ch/en/spielplan/calendar/"
                                           + (f"page{n}" if n > 1 else "")))


def _opernhaus_page(url):
    out = []
    for tag in soup(url).find_all("script", type="application/ld+json"):
        try:
            d = json.loads(tag.string or "")
        except ValueError:
            continue
        for e in d if isinstance(d, list) else [d]:
            if not str(e.get("@type", "")).endswith("Event"):
                continue
            start = dt.datetime.fromisoformat(e["startDate"]).replace(tzinfo=TZ)
            room = (e.get("location") or {}).get("name", "")
            out.append(ev("opernhaus", "Opernhaus Zürich", html.unescape(e["name"]), start,
                          e.get("url", url), subtitle=room, category="Opera / Ballet / Concert"))
    return out


def tonhalle():
    return paged(lambda n: _tonhalle_page(f"/en/concerts/kalender/page{n}?action=filtercalendar" if n > 1
                                          else "/en/concerts/kalender/"))


def _tonhalle_page(path):
    base = "https://www.tonhalle-orchester.ch"
    out = []
    for row in soup(base + path).select("div.row.data"):
        e = row.select_one("div.event[data-timestamp]")
        a = row.select_one("a[href*='/concerts/kalender/']")
        if not (e and a):
            continue
        start = dt.datetime.fromtimestamp(int(e["data-timestamp"]), TZ)
        members = text(row.select_one("div.member")).replace(" ,", ",")
        out.append(ev("tonhalle", "Tonhalle Zürich", text(e.find("h3")), start,
                      urllib.parse.urljoin(base, a["href"]), subtitle=members,
                      category="Classical"))
    return out


def schauspielhaus():
    base = "https://www.schauspielhaus.ch"
    out = []
    for it in soup(base + "/de/spielplan").select("article.calendar-item"):
        a = it.select_one("a.calendar-item__title")
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.", text(it.select_one(".calendar-item__date")))
        if not (a and m):
            continue
        d, mo = int(m[1]), int(m[2])
        t = re.search(r"(\d{1,2})[.:](\d{2})\s*$", text(it.select_one(".calendar-item__date")))
        hh, mm = (int(t[1]), int(t[2])) if t else (0, 0)
        stage = text(it.select_one(".calendar-item__stage"))
        out.append(ev("schauspielhaus", f"Schauspielhaus Zürich – {stage.title()}".rstrip(" –"),
                      text(a).title() if text(a).isupper() else text(a),
                      local(infer_year(d, mo), mo, d, hh, mm), urllib.parse.urljoin(base, a["href"]),
                      subtitle=text(it.select_one(".calendar-item__col3 .calendar-item__text")),
                      category="Theatre", allday=not t))
    return out


def moods():
    base = "https://www.moods.ch"
    out, seen = [], set()
    for a in soup(base + "/en/program").find_all("a", href=re.compile(r"^/en/program/.+-\d{4}-\d{2}-\d{2}$"),
                                                   title=True):
        li = a.find_parent("li")
        if a["href"] in seen or not (li and li.find("time")):
            continue
        seen.add(a["href"])
        y, mo, d = map(int, a["href"][-10:].split("-"))
        title = html.unescape(a["title"])
        parts = list(li.stripped_strings)
        t = next((re.fullmatch(r"(\d{1,2}):(\d{2})", p) for p in parts if re.fullmatch(r"\d{1,2}:\d{2}", p)), None)
        # strings after the title, before the quote/time, are genre tags
        after = parts[parts.index(title) + 1:] if title in parts else []
        genres = list(dict.fromkeys(p for p in after[:4] if len(p) < 30 and p not in "«»" and not re.match(r"\d", p)))
        before = parts[parts.index(title) - 1] if title in parts and parts.index(title) > 3 else ""
        out.append(ev("moods", "Moods Jazzclub", title,
                      local(y, mo, d, *(map(int, t.groups()) if t else (0, 0))), base + a["href"],
                      subtitle=before, category=" · ".join(genres) or "Jazz", allday=not t))
    return out


def kaufleuten():
    out = []
    for art in soup("https://kaufleuten.ch/events/").select("article.event"):
        a, t = art.select_one("a.event-link"), art.find("time", datetime=True)
        if not (a and t):
            continue
        title = text(art.find("h3"))
        cat = title.rsplit("•", 1)[1].strip() if "•" in title else ""
        out.append(ev("kaufleuten", "Kaufleuten", title.split("•")[0], dt.datetime.fromisoformat(t["datetime"]),
                      a["href"], subtitle=text(art.select_one(".subtitle")), category=cat or "Culture"))
    return out


def bogenf():
    base = "https://www.bogenf.ch"
    out = []
    for it in soup(base + "/konzerte").select("div.upcoming-list-item"):
        a = it.find("a", href=True)
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s*\|\s*(\d{1,2}):(\d{2})", text(it.find("time")))
        if not (a and m):
            continue
        d, mo, y, hh, mm = map(int, m.groups())
        out.append(ev("bogenf", "Bogen F", text(it.select_one(".headlines")), local(y, mo, d, hh, mm),
                      urllib.parse.urljoin(base, a["href"]), category="Concert"))
    return out


def rotefabrik():
    today = dt.date.today()
    to = today + dt.timedelta(days=CFG["general"]["days_ahead"])
    data = json.loads(fetch(f"https://kalender.rotefabrik.ch/api/events?from={today}&to={to}"))
    out = []
    for d in data.values() if isinstance(data, dict) else data:
        e = d.get("rf_event") or {}
        if d.get("deleted_at") or not e.get("title"):
            continue
        # "date" is local midnight expressed in UTC
        day = dt.datetime.fromisoformat(d["date"].replace("Z", "+00:00")).astimezone(TZ).date()
        hh, mm = map(int, (d.get("from") or "00:00").split(":")[:2])
        cats = ", ".join(c["name"] for c in e.get("categories") or [])
        out.append(ev("rotefabrik", f"Rote Fabrik – {e.get('location_info') or ''}".rstrip(" –"),
                      e["title"], local(day.year, day.month, day.day, hh, mm),
                      f"https://rotefabrik.ch/programm#/events/{e['id']}",
                      subtitle=e.get("subtitle") or "", category=cats or "Culture",
                      allday=bool(d.get("allday")) or not d.get("from")))
    return out


def gessnerallee():
    base = "https://gessnerallee.ch"
    out = []
    for a in soup(base + "/programm").select("h2 > a[href^='/veranstaltungen/']"):
        row = a.find_parent("h2").parent.parent
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", text(row))
        if not m:
            continue
        t = re.search(r"\b(\d{1,2}):(\d{2})\b", text(row))
        spans = [text(s) for s in a.find_all("span")]
        out.append(ev("gessnerallee", "Gessnerallee", spans[0] if spans else text(a),
                      local(2000 + int(m[3]), int(m[2]), int(m[1]), *(map(int, t.groups()) if t else (0, 0))),
                      base + a["href"], subtitle=" ".join(spans[1:]), category="Performance / Talk",
                      allday=not t))
    return out


def hallenstadion():
    # Page 1 is the events page; further pages come from the site's own "load more" AJAX call.
    ajax = ("https://hallenstadion.ch/wp-admin/admin-ajax.php?action=events_filter&filter_by_genre="
            "&filter_by_events_type=&filter_by_date=&from_date=&to_date=&search=&paged=")
    return paged(lambda n: _hallenstadion_page("https://hallenstadion.ch/events/" if n == 1 else ajax + str(n)))


def _hallenstadion_page(url):
    out = []
    months = {m: i for i, m in enumerate(
        "jan feb mär apr mai jun jul aug sep okt nov dez".split(), 1)}
    for c in soup(url).select("div.c-event"):
        a = c.select_one("a.c-event-content[href]")
        day, mon, yr = (text(c.select_one(s)) for s in (".schedule--day", ".schedule--month", ".schedule--year"))
        mo = months.get(mon[:3].lower().replace("mar", "mär"))
        if not (a and day.isdigit() and mo and yr.isdigit()):
            continue
        t = re.search(r"(\d{1,2}):(\d{2})", text(c.select_one(".c-event-content-bottom-info--time")))
        out.append(ev("hallenstadion", "Hallenstadion", text(c.find("h5")),
                      local(int(yr), mo, int(day), *(map(int, t.groups()) if t else (0, 0))),
                      a["href"].split("?")[0], category="Concert / Show", allday=not t))
    return out


def museum_gestaltung():
    # the site's page numbers are zero-based: ?page=1 is the second page
    return paged(lambda n: _museum_gestaltung_page(f"/de/kalender?page={n - 1}" if n > 1 else "/de/kalender"))


def _museum_gestaltung_page(path):
    base = "https://museum-gestaltung.ch"
    out = []
    for li in soup(base + path).select("ol > li"):
        a = li.find("a", href=re.compile(r"^/de/events/.+-\d{4}-\d{2}-\d{2}$"))
        if not a:
            continue
        y, mo, d = map(int, a["href"][-10:].split("-"))
        t = re.search(r"\b(\d{1,2}):(\d{2})\b", text(li))
        # strings look like: long date, time, short date ("Di, 3.11."), time, category, title, audience, location, ...
        parts = list(li.stripped_strings)
        i = next((k for k, p in enumerate(parts) if re.fullmatch(r"\w{2}, \d{1,2}\.\d{1,2}\.", p)), None)
        if i is not None and len(parts) > i + 3:
            cat, title, rest = parts[i + 2], parts[i + 3], parts[i + 4:i + 6]
        else:
            cat, title, rest = "", a["href"].rsplit("/", 1)[1][:-11].replace("-", " ").title(), []
        out.append(ev("museum_gestaltung", "Museum für Gestaltung", title,
                      local(y, mo, d, *(map(int, t.groups()) if t else (0, 0))),
                      base + a["href"], subtitle=" · ".join(rest), category=cat or "Design",
                      allday=not t))
    return out


def rigiblick():
    base = "https://www.theater-rigiblick.ch"
    out, ym = [], None
    for tr in soup(base + "/spielplan/kalenderansicht").select("table.spielplan tr"):
        if re.fullmatch(r"\d{4}-\d{2}", tr.get("id", "")):
            ym = tuple(map(int, tr["id"].split("-")))
            continue
        day, a = tr.select_one("td.datum .projekt-titel"), tr.select_one("td.projekt a[href]")
        if not (ym and day and a and text(day).isdigit()):
            continue
        t = re.search(r"(\d{1,2}):(\d{2})", text(tr.select_one("td.datum")))
        out.append(ev("rigiblick", "Theater Rigiblick", text(a),
                      local(ym[0], ym[1], int(text(day)), *(map(int, t.groups()) if t else (0, 0))),
                      urllib.parse.urljoin(base, a["href"]), category="Theatre / Music theatre",
                      allday=not t))
    return out


def kunsthaus():
    # Exhibitions only. JSON-LD gives every exhibition (incl. past) with a start date;
    # cards for the ones running now carry "Until dd.mm.yyyy".
    url = "https://www.kunsthaus.ch/en/besuch-planen/ausstellungen/"
    s = soup(url)
    until = {}
    for g in s.select("div.hgroup"):
        a = g.select_one("h2 a[href]")
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text(g.select_one("time.date")))
        if a and m:
            until[a["href"]] = (local(int(m[3]), int(m[2]), int(m[1]), 23, 59),
                                text(g.select_one("p.description")))
    today = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for tag in s.find_all("script", type="application/ld+json"):
        try:
            e = json.loads(tag.string or "")
        except ValueError:
            continue
        if e.get("@type") != "ExhibitionEvent" or not e.get("startDate"):
            continue
        start = dt.datetime.fromisoformat(e["startDate"][:10]).replace(tzinfo=TZ)
        end, sub = until.get(e.get("@id"), (None, ""))
        if start < today and not end:
            continue          # past exhibition
        out.append(ev("kunsthaus", "Kunsthaus Zürich", html.unescape(e["name"]), start, e["@id"],
                      subtitle=sub or "Exhibition opens", category="Exhibition", allday=True, end=end))
    return out


PARSERS = {f.__name__: f for f in (opernhaus, tonhalle, schauspielhaus, moods, kaufleuten, bogenf,
                                   rotefabrik, gessnerallee, hallenstadion, museum_gestaltung, rigiblick,
                                   kunsthaus)}


# ---------------------------------------------------------------- storage

def db() -> sqlite3.Connection:
    con = sqlite3.connect(ROOT / "events.db")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, source TEXT, venue TEXT, title TEXT, subtitle TEXT,
            start TEXT, allday INTEGER, url TEXT, category TEXT, highlight INTEGER,
            first_seen TEXT, last_seen TEXT);
        CREATE TABLE IF NOT EXISTS runs (
            ts TEXT, source TEXT, ok INTEGER, count INTEGER, message TEXT);
    """)
    if "end" not in [r[1] for r in con.execute("PRAGMA table_info(events)")]:
        con.execute('ALTER TABLE events ADD COLUMN "end" TEXT')
    return con


def matches(e, terms):
    hay = " ".join((e["title"], e["subtitle"], e["category"], e["venue"])).lower()
    return any(t.lower() in hay for t in terms)


def collect(con):
    now = dt.datetime.now(TZ)
    flt = CFG["filter"]
    for name, parser in PARSERS.items():
        if not CFG["sources"].get(name, True):
            continue
        try:
            events = parser()
            if not events:
                raise ValueError("parsed 0 events – the page layout may have changed")
        except Exception as exc:
            log.error("%s failed: %s", name, exc)
            con.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (now.isoformat(), name, 0, 0, str(exc)[:300]))
            continue
        kept = 0
        for e in events:
            current = (now - dt.timedelta(hours=6) <= e["start"] <= horizon()
                       or (e["end"] and e["start"] <= now <= e["end"]))
            if not current or matches(e, flt["exclude"]):
                continue
            eid = hashlib.sha1(f"{e['source']}|{e['url']}|{e['start'].isoformat()}|{e['title']}".encode()).hexdigest()[:16]
            con.execute("""
                INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, subtitle=excluded.subtitle,
                    start=excluded.start, allday=excluded.allday, category=excluded.category,
                    highlight=excluded.highlight, last_seen=excluded.last_seen, "end"=excluded."end" """,
                (eid, e["source"], e["venue"], e["title"], e["subtitle"], e["start"].isoformat(),
                 int(e["allday"]), e["url"], e["category"], int(matches(e, flt["highlight"])),
                 now.isoformat(), now.isoformat(), e["end"] and e["end"].isoformat()))
            kept += 1
        log.info("%s: %d parsed, %d kept", name, len(events), kept)
        con.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (now.isoformat(), name, 1, kept, ""))
    con.commit()


# ---------------------------------------------------------------- rendering

def render(con):
    now = dt.datetime.now(TZ)
    rows = con.execute('SELECT * FROM events WHERE start >= ? OR "end" >= ? ORDER BY start',
                       ((now - dt.timedelta(hours=6)).isoformat(), now.isoformat())).fetchall()
    last_run = con.execute("SELECT MAX(ts) FROM runs").fetchone()[0]
    status = con.execute("SELECT source, ok, count, message FROM runs WHERE ts = ?", (last_run,)).fetchall()
    events = [dict(r) for r in rows]
    for e in events:
        e["new"] = e["first_seen"] == last_run
    tpl = (ROOT / "template.html").read_text()
    page = (tpl.replace("/*EVENTS*/[]", json.dumps(events, ensure_ascii=False).replace("</", "<\\/"))
               .replace("/*STATUS*/[]", json.dumps([dict(s) for s in status], ensure_ascii=False))
               .replace("__UPDATED__", html.escape(last_run[:16].replace("T", " ") if last_run else "never")))
    out = ROOT / "site"
    out.mkdir(exist_ok=True)
    (out / "index.html.tmp").write_text(page)
    (out / "index.html.tmp").replace(out / "index.html")
    log.info("rendered %d events to %s", len(events), out / "index.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", action="store_true", help="only re-render the page")
    ap.add_argument("--only", help="run a single source (for testing)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "agent.log")])
    if args.only:
        for e in PARSERS[args.only]():
            print(e["start"].strftime("%a %d.%m.%Y %H:%M"), "|", e["title"], "|", e["subtitle"][:60],
                  "|", e["category"], "|", e["url"], "| until " + e["end"].strftime("%d.%m.%Y") if e["end"] else "")
        return
    con = db()
    if not args.render:
        collect(con)
    render(con)


if __name__ == "__main__":
    sys.exit(main())
