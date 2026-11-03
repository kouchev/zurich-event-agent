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


def fetch(url: str, data: dict | None = None) -> str:
    """GET (or POST form data to) a URL politely: honest user agent, robots.txt
    (incl. its Crawl-delay), per-host delay."""
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
            # RFC 9309 §2.3.1: robots.txt unavailable (4xx) → no restrictions;
            # server error (5xx) → assume complete disallow.
            rp.allow_all = 400 <= e.code < 500
            rp.disallow_all = not rp.allow_all
        _robots[host] = rp
    if not _robots[host].can_fetch(UA, url):
        raise PermissionError(f"robots.txt disallows {url}")
    delay = max(CFG["general"]["request_delay_seconds"], _robots[host].crawl_delay(UA) or 0)
    wait = delay - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de,en"},
                                 data=urllib.parse.urlencode(data).encode() if data is not None else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", "replace")
    _last_hit[host] = time.time()
    return body


def soup(url: str, data: dict | None = None) -> BeautifulSoup:
    s = BeautifulSoup(fetch(url, data), "html.parser")
    for t in s(["style", "svg", "noscript"]):
        t.decompose()
    return s


def text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).replace("\u00ad", "").split()) if node else ""


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
    return paged(lambda n: _tonhalle_page(f"/en/concerts/kalender/page{n}" if n > 1
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


def eth():
    # Public JSON API behind https://ethz.ch/en/news-and-events/events.html
    url = ("https://idapps.ethz.ch/pcm-pub-services/v2/entries?rs-first=0&rs-size=9999"
           "&filters%5B0%5D.min-till-end=0&filters%5B0%5D.max-till-start=999&lang=en&client-id=wcms"
           "&filters%5B0%5D.cals=1&comp-ext=true")
    out = []
    for e in json.loads(fetch(url))["entry-array"]:
        cls, loc = e.get("classification", {}), e.get("location", {})
        title = e["content"]["title"]
        link = f"https://ethz.ch/en/news-and-events/events/details.{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60] or 'event'}.{e['id']}.html"
        where = loc.get("internal", {})
        room = " ".join(filter(None, (where.get("building"), where.get("floor"), where.get("room")))) \
            or (loc.get("address") or {}).get("1st-line", "")
        venue = "ETH Zürich" + (f" – {where['area-desc']}" if where.get("area-desc") else "")
        audience = cls.get("target-group-desc") or ""
        subtitle = " · ".join(filter(None, (room, audience if audience != "Public" else "", cls.get("entry-lang-desc"))))
        cat = cls.get("entry-type-desc") or "Event"
        when = e.get("date-time-indication", {})
        if "opening-hours" in when:          # exhibitions: one long-running item
            oh = when["opening-hours"]
            start = dt.datetime.fromisoformat(oh["date-from"]).replace(tzinfo=TZ)
            end = dt.datetime.fromisoformat(oh["date-to"]).replace(hour=23, minute=59, tzinfo=TZ)
            out.append(ev("eth", venue, title, start, link, subtitle=subtitle, category=cat, allday=True, end=end))
            continue
        for d in when.get("date-with-times-array", []):
            hh, mm = map(int, (d.get("time-from") or "00:00").split(":"))
            y, mo, da = map(int, d["date"].split("-"))
            out.append(ev("eth", venue, title, local(y, mo, da, hh, mm), link, subtitle=subtitle,
                          category=cat, allday=not d.get("time-from")))
    return out


def uzh():
    # Public JSON API behind https://www.uzh.ch/en/events/agenda.html
    kinds = {"ringvorlesung": "Lecture series", "antrittsvorlesung": "Inaugural lecture"}
    out = []
    for e in json.loads(fetch("https://www.webroot.uzh.ch/apps/agenda/api/V5/event/"))["events"]:
        if not e.get("dtstart"):
            continue
        start = dt.datetime.fromtimestamp(int(e["dtstart"]) / 1000, TZ)
        org = html.unescape(re.sub(r"<[^>]+>", " ", e.get("organisation_name") or "")).split("  ")[0].strip()
        where = e.get("address") or e.get("building") or ("Online" if e.get("is_virtual") else "")
        subtitle = " · ".join(filter(None, (html.unescape(e.get("speaker") or "").strip(), org, where.lstrip("@ "))))
        cat = kinds.get(e.get("series_type")) or html.unescape(e.get("serie_name") or "") or "Event"
        out.append(ev("uzh", "Universität Zürich", html.unescape(e["title"]), start,
                      f"https://www.uzh.ch/en/events/agenda.html?event={e['id']}",
                      subtitle=subtitle, category=cat))
    return out


# ---- theatres in Zürich, Winterthur and Baden

def clock(s: str):
    """(hh, mm) from a time like '19:30', '19.30 Uhr', '20h', '19h30' or '20 Uhr'; None if absent.
    Only call it on text that holds no date, since '07.11' also looks like a time."""
    m = re.search(r"\b(\d{1,2})(?:[:.](\d{2})|h(\d{2})?|\s*Uhr)", s or "")
    if not m or int(m[1]) > 23:
        return None
    return int(m[1]), int(m[2] or m[3] or 0)


def at(y, mo, d, t):
    """Local start for a date and a clock() result (None → midnight, flagged all-day by the caller)."""
    return local(y, mo, d, *(t or (0, 0)))


def ld_events(s: BeautifulSoup):
    """schema.org *Event objects from a page's JSON-LD blocks."""
    for tag in s.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(tag.string or "")
        except ValueError:
            continue
        for e in d if isinstance(d, list) else d.get("@graph", [d]):
            if isinstance(e, dict) and str(e.get("@type", "")).endswith("Event"):
                yield e


def neumarkt():
    base = "https://theaterneumarkt.ch"
    out = []
    for it in soup(base + "/kalender/").select("div.el-event-teaser-item[date-date]"):
        title, a = it.select_one(".play-title"), it.select_one("a.link-box[href]")
        if not (title and a):
            continue
        sub = title.select_one(".subtitle")
        for x in title.select(".subtitle, .note"):
            x.extract()
        y, mo, d = map(int, it["date-date"].split("-"))
        t = clock(text(it.select_one(".time")))
        status = text(it.select_one(".status-desktop"))      # e.g. "Premiere"
        out.append(ev("neumarkt", "Theater Neumarkt", text(title), at(y, mo, d, t), base + a["href"],
                      subtitle=" · ".join(filter(None, (text(sub), status, text(it.select_one(".location"))))),
                      category="Theatre", allday=not t))
    return out


def hechtplatz():
    base = "https://theaterhechtplatz.ch"
    out = []
    for it in soup(base + "/spielplan/").select("div.item.schedule[data-date]"):
        a = it.select_one("h3.title a[href]")
        if not a:
            continue
        out.append(ev("hechtplatz", "Theater am Hechtplatz", text(a),
                      dt.datetime.fromisoformat(it["data-date"]).astimezone(TZ),
                      urllib.parse.urljoin(base, a["href"]), subtitle=text(it.select_one("p.subtitle")),
                      category="Cabaret / Comedy / Theatre"))
    return out


def bernhard():
    return paged(lambda n: _bernhard_page(f"https://www.bernhard-theater.ch/spielplan/{f'page{n}' if n > 1 else ''}"))


def _bernhard_page(url):
    # A month heading ("November 2026") precedes its items, but a page that starts mid-month has
    # none; an item without a day ("noday") is a further show on the previous item's day.
    out, year, month, day = [], None, None, None
    months = {m: i for i, m in enumerate("jan feb mär apr mai jun jul aug sep okt nov dez".split(), 1)}
    s = soup(url)
    for node in s.select("h2.new_month, div.el-eventlistitem"):
        if node.name == "h2":
            m = re.search(r"(\w+)\s+(\d{4})", text(node))
            if m and m[1][:3].lower() in months:
                month, year = months[m[1][:3].lower()], int(m[2])
            continue
        d = node.select_one(".date h3")
        if d and text(d).isdigit():
            day = int(text(d))
            tiny = node.select(".date .tiny")
            if len(tiny) > 1 and text(tiny[1])[:3].lower() in months:
                month = months[text(tiny[1])[:3].lower()]
        a = node.select_one("a.detail_link[href]")
        if not (a and month and day):
            continue
        t = clock(text(node.select_one(".date .time")))
        out.append(ev("bernhard", "Bernhard Theater", text(node.select_one(".details h2")),
                      at(year or infer_year(day, month), month, day, t), urllib.parse.urljoin(url, a["href"]),
                      subtitle=text(node.select_one(".details p")), category="Theatre / Comedy",
                      allday=not t))
    return out


def winkelwiese():
    out = []
    for li in soup("https://winkelwiese.ch/kalender").select("li.table-list-item"):
        a = li.select_one("a.production-title[href]")
        when = text(li.select_one(".list-item-date"))
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(.*)", when)
        if not (a and m):
            continue
        d, mo = int(m[1]), int(m[2])
        t = clock(m[3])
        col = a.find_parent(class_="list-item-col")     # notes like "Ausverkauft", "zum letzten Mal"
        note = " · ".join(text(x) for x in col.find_all("span", recursive=False)
                          if "calendar-links" not in (x.get("class") or [])) if col else ""
        out.append(ev("winkelwiese", "Theater Winkelwiese", text(a), at(infer_year(d, mo), mo, d, t), a["href"],
                      subtitle=note, category="Theatre", allday=not t))
    return out


def millers():
    base = "https://www.millers.ch"
    out = []
    for e in ld_events(soup(base + "/spielplan")):
        url = urllib.parse.urljoin(base, (e.get("offers") or {}).get("url") or "/spielplan")
        out.append(ev("millers", "Millers", html.unescape(e["name"]),
                      dt.datetime.fromisoformat(e["startDate"]).astimezone(TZ), url,
                      category="Theatre / Music / Comedy"))
    return out


def stadelhofen():
    # Theater Stadelhofen (the former Zürcher Puppentheater): puppet and object theatre.
    out = []
    for art in soup("https://theater-stadelhofen.ch/auf-der-buehne/").select("article.tease-indy-performance"):
        a = art.select_one("a[href*='/veranstaltungen/']")
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.", text(art.select_one(".tease-performance__start__date")))
        if not (a and m):
            continue
        d, mo = int(m[1]), int(m[2])
        t = clock(text(art.select_one(".tease-performance__start__time")))
        ticket = text(art.select_one(".tease-performance__ticket"))   # "Für Schulen" marks school-only shows
        sub = " · ".join(filter(None, (text(art.select_one(".tease-performance__title__compagnie")),
                                       text(art.select_one(".tease-performance__infos__from_age")),
                                       ticket if "schul" in ticket.lower() else "")))
        out.append(ev("stadelhofen", "Theater Stadelhofen", text(art.select_one("h3")),
                      at(infer_year(d, mo), mo, d, t), a["href"], subtitle=sub,
                      category="Puppet / Object theatre", allday=not t))
    return out


def stok():
    # The list (WordPress "Modern Events Calendar") shows 12 events and loads the rest by a POST
    # to admin-ajax.php with the list's settings; robots.txt allows admin-ajax.php.
    url = "https://theaterstok.ch/programm/"
    page = fetch(url)
    atts = re.search(r'atts: *"([^"]*)"', page)
    if not atts:
        raise ValueError("list settings not found on the programme page")
    q = {k: v for k, v in urllib.parse.parse_qsl(atts[1])
         if v and ("[list]" in k or "sk-options" not in k) and "sf-options" not in k and "_edit" not in k}
    q.update({"action": "mec_list_load_more", "mec_start_date": dt.date.today().isoformat(), "mec_offset": "0",
              "current_month_divider": "0", "apply_sf_date": "0", "atts[sk-options][list][limit]": "300"})
    data = json.loads(fetch("https://theaterstok.ch/wp-admin/admin-ajax.php", data=q))
    out = []
    for art in BeautifulSoup(data["html"], "html.parser").select("article.mec-event-article"):
        a = art.select_one(".mec-event-title a[href]")
        occ = re.search(r"occurrence=(\d{4})-(\d{2})-(\d{2})", a["href"]) if a else None
        if not occ:
            continue
        t = clock(text(art.select_one(".mec-start-time")))
        for x in art.select(".mec-labels-normal"):     # "Extern", "Kollektiv-Mitglied": who hosts it
            x.extract()
        out.append(ev("stok", "Theater Stok", text(a), at(*map(int, occ.groups()), t), a["href"],
                      subtitle=text(art.select_one(".mec-event-description"))[:200],
                      category="Stage", allday=not t))
    return out


def purpur():
    # Children's theatre; one block per production with its dates ("Sa, 14. November 2026 15:00").
    url = "https://theater-purpur.ch/theater"
    months = {m: i for i, m in enumerate("januar februar märz april mai juni juli august september oktober "
                                          "november dezember".split(), 1)}
    out = []
    for h in soup(url).select("h3.media-heading"):
        row = h.find_parent(class_="row")
        dates = row.find_next_sibling(class_="row") if row else None
        if not dates:
            continue
        age = text(dates.select_one(".alterButton"))
        for sp in dates.select(".daten span"):
            m = re.search(r"(\d{1,2})\.\s*(\w+)\s+(\d{4})\s*(.*)", text(sp))
            if not (m and m[2].lower() in months):
                continue
            t = clock(m[4])
            out.append(ev("purpur", "Theater Purpur", text(h), at(int(m[3]), months[m[2].lower()], int(m[1]), t),
                          url, subtitle=(f"ab {age.rstrip('+')} Jahren" if age.rstrip("+").isdigit() else
                                         "für alle" if age else ""),
                          category="Children's theatre", allday=not t))
    return out


def tanzhaus():
    # JSON feed behind the site's calendar (/wp-json/glunz/v1/event-dates/month); it also lists
    # trainings, courses and workshops, which are left out here.
    out, seen = [], set()
    first = dt.date.today().replace(day=1)
    for i in range(CFG["general"]["days_ahead"] // 28 + 1):
        y, mo = first.year + (first.month - 1 + i) // 12, (first.month - 1 + i) % 12 + 1
        if dt.date(y, mo, 1) > horizon().date():
            break
        data = json.loads(fetch(f"https://www.tanzhaus-zuerich.ch/wp-json/glunz/v1/event-dates/month"
                                f"?year={y}&month={mo}&lang=de"))
        for e in data.get("dates", []) if isinstance(data, dict) else data:
            kinds = set(filter(None, (e.get("event_types") or "").split("|")))
            if e["id"] in seen or not kinds & {"veranstaltungen", "zugaben"} or e.get("is_cancelled") == "1":
                continue
            seen.add(e["id"])
            start = dt.datetime.fromisoformat(e["date_start"]).replace(tzinfo=TZ)
            artists = ", ".join(filter(None, (e.get("artist_names") or "").split("|")))
            formats = ", ".join(f.replace("-", " ").title() for f in filter(None, (e.get("event_formats") or "").split("|")))
            out.append(ev("tanzhaus", "Tanzhaus Zürich", html.unescape(e["post_title"]), start, e["permalink"],
                          subtitle=artists, category=formats or "Dance"))
    return out


def sogar():
    base = "https://www.sogar.ch"
    months = {m: i for i, m in enumerate("januar februar märz april mai juni juli august september oktober "
                                          "november dezember".split(), 1)}
    out, year = [], None
    for node in soup(base + "/programm/").select("div.pro-month, article.event-article"):
        if "pro-month" in node.get("class", []):
            m = re.search(r"(\d{4})", text(node))
            year = int(m[1]) if m else None
            continue
        date = node.select_one(".pro-event-date")
        a = node.select_one(".pro-event-title")
        m = re.match(r"(\d{1,2})\.(\d{1,2})", text(date))
        if not (year and m and a):
            continue
        t = clock(text(date.select_one(".br")))
        detail = [p for p in (text(p) for p in node.select(".pro-event-detail p p")) if p]
        link = a.find_parent("a", href=True)
        out.append(ev("sogar", "sogar theater", text(a), at(year, int(m[2]), int(m[1]), t),
                      urllib.parse.urljoin(base, link["href"]) if link else base + "/programm/",
                      subtitle=" · ".join(detail[:-1] + [x for x in [text(node.select_one(".pro-event-status"))]
                                                         if x.lower() != "tickets"]),
                      category=(detail[-1].title() if detail else "Theatre"), allday=not t))
    return out


def keller62():
    base = "https://keller62.ch"
    out = []
    for art in soup(base + "/programm").select("article.program"):
        title = text(art.select_one("[itemprop=name]"))
        links = [a["href"] for a in art.select(".desktop-newsDescr a.res[href]")]
        starts = [m["content"] for m in art.select("meta[itemprop=startDate]")]
        for i, s in enumerate(starts):
            url = base + links[i] if len(links) == len(starts) else base + "/programm"
            out.append(ev("keller62", "Keller62", title, dt.datetime.fromisoformat(s).astimezone(TZ), url,
                          subtitle=" · ".join(filter(None, (text(art.select_one("h4[itemprop=performer]")),
                                                            text(art.select_one(".zh-prem"))))),
                          category="Theatre / Cabaret"))
    return out


def theater11():
    # Programme of Theater 11 on musical.ch. Multi-day runs ("24. - 29.11.2026") have no per-show
    # times there; they are listed like exhibitions, from the first to the last day.
    base = "https://www.musical.ch"
    out = []
    for it in soup(base + "/theater11zuerich").select("div.more-events-item"):
        a = it.find("a", href=True)
        title = text(it.find("h5"))
        for x in it.select(".title"):
            x.extract()
        when = text(it)
        url = urllib.parse.urljoin(base + "/", a["href"]) if a else base + "/theater11zuerich"
        if "utm_" in url or "affiliate=" in url:
            url = url.split("?")[0]
        rng = re.match(r"(\d{1,2})\.(?:(\d{1,2})\.(\d{2,4})?)?\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})", when)
        one = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s*(?:\|\s*(.*))?", when)
        if rng:
            y2 = int(rng[6]) % 100 + 2000
            mo1 = int(rng[2] or rng[5])
            y1 = int(rng[3]) % 100 + 2000 if rng[3] else (y2 if mo1 <= int(rng[5]) else y2 - 1)
            start, end = local(y1, mo1, int(rng[1])), local(y2, int(rng[5]), int(rng[4]), 23, 59)
            out.append(ev("theater11", "Theater 11", title, start, url, subtitle="Several shows",
                          category="Musical / Show", allday=True, end=end))
        elif one:
            t = clock(one[4] or "")
            out.append(ev("theater11", "Theater 11", title, at(int(one[3]), int(one[2]), int(one[1]), t), url,
                          category="Musical / Show", allday=not t))
    return out


def theater_winterthur():
    # The agenda is loaded month by month with a POST to /neueartikelladen (allowed by robots.txt).
    page = soup("https://www.theaterwinterthur.ch/spielplan-agenda")
    out = []
    for m in dict.fromkeys(x["data-month"] for x in page.select("[data-month]")):
        y, mo = map(int, m.split("-"))
        if dt.date(y, mo, 1) > horizon().date():
            break
        s = soup("https://www.theaterwinterthur.ch/neueartikelladen",
                 data={"action": "show_list", "tab": "datum", "abonnementId": "0", "brasserie": "0", "month": m})
        for it in s.select("div.calendar-item"):
            f = {i["name"]: i.get("value", "") for i in it.select("form input[name]")}
            a = it.select_one(".calendar-headline h2 a[href]")
            if not (a and f.get("datumvon")):
                continue
            y2, mo2, d2 = map(int, f["datumvon"].split("-"))
            t = clock(f.get("zeitvon", ""))
            cats = [text(li) for li in it.select(".nav-tertiary li")]
            ort = f.get("ort", "")
            out.append(ev("theater_winterthur", "Theater Winterthur", text(a), at(y2, mo2, d2, t), a["href"],
                          subtitle=" · ".join(filter(None, (text(it.select_one("p.untertitel")),
                                                            "" if ort == "Theater Winterthur" else ort))),
                          category=" · ".join(cats[:1]) or "Theatre", allday=not t))
    return out


def casinotheater():
    out = []
    for it in soup("https://www.casinotheater.ch/spielplan/").select("div.spielplan__item[data-year]"):
        a = it.select_one("a.performance-link[href]") or it
        t = clock(text(it.select_one(".spielplan-details__info__date")).split(".")[-1])
        url = (a.get("href") or it.get("data-performance-link") or "https://www.casinotheater.ch/spielplan/")
        out.append(ev("casinotheater", "Casinotheater Winterthur", text(it.select_one("h1")),
                      at(int(it["data-year"]), int(it["data-month"]), int(it["data-day"]), t),
                      url.split("?backID=")[0] + f"#performance-{it.get('id', '').split('-')[-1]}",
                      subtitle=text(it.select_one(".spielplan-details__content__description")),
                      category=re.sub(r"[\s\d]+$", "", (it.get("data-cat") or "comedy").replace("-", " ")).title()
                      .replace("Komoedie", "Komödie"), allday=not t))
    return out


def theater_am_gleis():
    out = []
    for art in soup("https://theater-am-gleis.ch/spielplan").select("article.tease-indy_event"):
        a = art.select_one(".indy-event__title a[href]")
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.", text(art.select_one(".indy-event__datetime .date")))
        if not (a and m):
            continue
        d, mo = int(m[1]), int(m[2])
        t = clock(text(art.select_one(".indy-event__datetime .time")))
        out.append(ev("theater_am_gleis", "Theater am Gleis Winterthur", text(a.find("h2") or a),
                      at(infer_year(d, mo), mo, d, t), a["href"],
                      subtitle=text(art.select_one(".indy-event__troupe")),
                      category=text(art.select_one(".indy-event-badge")) or "Theatre", allday=not t))
    return out


def kellertheater_winterthur():
    out = []
    for art in soup("https://keller.theater/events/").select("article.tease-indy-event"):
        a = art.select_one("h2 a[href]")
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.", text(art.select_one(".event-date")))
        if not (a and m):          # e.g. the weekly tango evening has no date
            continue
        d, mo = int(m[1]), int(m[2])
        meta = art.select_one(".event-meta")
        for x in meta.select(".ticketlink"):
            x.extract()
        lines = [x.strip() for x in meta.get_text("\n").split("\n") if x.strip()]
        t = next((clock(x) for x in lines if re.fullmatch(r"\d{1,2}[:.]\d{2} Uhr", x)), None)
        sub = [x for x in lines if not re.search(r"\d{1,2}[:.]\d{2}", x)]
        out.append(ev("kellertheater_winterthur", "Kellertheater Winterthur", text(a),
                      at(infer_year(d, mo), mo, d, t), a["href"], subtitle=" · ".join(sub)[:200],
                      category=text(art.select_one(".labels-wrapper")).title() or "Theatre", allday=not t))
    return out


def tkz():
    # Theater Kanton Zürich (based in Winterthur) tours the canton; the place goes into the subtitle.
    base = "https://www.tkz.ch"
    months = {m: i for i, m in enumerate("januar februar märz april mai juni juli august september oktober "
                                          "november dezember".split(), 1)}
    out = []
    for it in soup(base + "/spielplan/").select("div.ticket-item"):
        m = re.search(r"(\d{1,2})\.\s*(\w+)\s+(\d{4})(?:,\s*(\d{1,2})[.:](\d{2}))?",
                      text(it.select_one(".date-info")))
        a = it.select_one("a[href^='/spielplan/']")
        if not (m and m[2].lower() in months):
            continue
        t = (int(m[4]), int(m[5])) if m[4] else None
        place = text(it.select_one(".infos-2 h3"))
        town = re.sub(r"^.*\d{4}\s+", "", text(it.select_one(".infos-2 .address")))
        out.append(ev("tkz", "Theater Kanton Zürich", text(it.select_one(".infos-1 h3")),
                      at(int(m[3]), months[m[2].lower()], int(m[1]), t),
                      urllib.parse.urljoin(base, a["href"] if a else "/spielplan/") + f"#{it.get('id', '')}",
                      subtitle=" · ".join(filter(None, (text(it.select_one(".infos-1 .subtitle")),
                                                        text(it.select_one(".infos-1 .premiere-info")),
                                                        place if place == town else ", ".join(filter(None, (place, town)))))),
                      category="Theatre", allday=not t))
    return out


def kurtheater():
    base = "https://www.kurtheater.ch"
    out = []
    for it in soup(base + "/spielplan/").select("div.bm-item[data-href]"):
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{2}),?\s*(.*)", text(it.select_one(".bm-date-row p")))
        if not m:
            continue
        t = clock(m[4])
        ps = [text(p) for p in it.select(".bm-metadata > p")]
        out.append(ev("kurtheater", "Kurtheater Baden", text(it.find("h2")),
                      at(2000 + int(m[3]), int(m[2]), int(m[1]), t), urllib.parse.urljoin(base, it["data-href"]),
                      subtitle=" · ".join(ps[1:] or ps)[:200], category="Stage", allday=not t))
    return out


def thik():
    base = "https://www.thik.ch"
    months = {m: i for i, m in enumerate("jan feb mär apr mai jun jul aug sep okt nov dez".split(), 1)}
    out = []
    for it in soup(base + "/programm").select("div.event-item.hasDate"):
        a = it.select_one("h2 a[href]")
        if not a:
            continue
        kind = [c for c in it["class"] if c not in ("alle", "hasDate", "event-item") and c.isalpha()]
        parts = [text(p) for p in it.select(".event-participant")]
        for d in it.select(".event-date"):
            m = re.match(r"(\d{1,2})\s+(\w{3})", text(d.select_one(".event-day")))
            if not (m and m[2].lower() in months):
                continue
            day, mo = int(m[1]), months[m[2].lower()]
            t = clock(text(d.select_one(".event-hour")))
            out.append(ev("thik", "ThiK Theater im Kornhaus Baden", text(a), at(infer_year(day, mo), mo, day, t),
                          urllib.parse.urljoin(base, a["href"]), subtitle=" · ".join(parts)[:200],
                          category="Für Schulen" if "Schulen" in kind else (", ".join(kind) or "Theatre"),
                          allday=not t))
    return out


PARSERS = {f.__name__: f for f in (opernhaus, tonhalle, schauspielhaus, moods, kaufleuten, bogenf,
                                   rotefabrik, gessnerallee, hallenstadion, museum_gestaltung, rigiblick,
                                   kunsthaus, eth, uzh,
                                   neumarkt, hechtplatz, bernhard, winkelwiese, millers, stadelhofen, stok,
                                   purpur, tanzhaus, sogar, keller62, theater11,
                                   theater_winterthur, casinotheater, theater_am_gleis, kellertheater_winterthur,
                                   tkz, kurtheater, thik)}


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
    run_cols = [r[1] for r in con.execute("PRAGMA table_info(runs)")]
    for col in ("parsed INTEGER", "seconds REAL"):
        if col.split()[0] not in run_cols:
            con.execute(f"ALTER TABLE runs ADD COLUMN {col}")
    return con


def matches(e, terms, whole_words=False):
    hay = " ".join((e["title"], e["subtitle"], e["category"], e["venue"])).lower()
    if whole_words:   # "oper" should not match "Operationalizing" or "Property"
        return any(re.search(rf"\b{re.escape(t.lower())}\b", hay) for t in terms)
    return any(t.lower() in hay for t in terms)


def highlight_terms(source) -> list:
    """Star words of the page this source is shown on (the first page if it is on none)."""
    pages = CFG["pages"]
    page = next((p for p in pages if source in p["sources"]), pages[0])
    return page.get("highlight", [])


def collect(con):
    now = dt.datetime.now(TZ)
    flt = CFG["filter"]
    for name, parser in PARSERS.items():
        if not CFG["sources"].get(name, True):
            continue
        t0 = time.time()
        try:
            events = parser()
            if not events:
                raise ValueError("parsed 0 events – the page layout may have changed")
        except Exception as exc:
            log.error("%s failed: %s", name, exc)
            con.execute("INSERT INTO runs (ts, source, ok, count, message, parsed, seconds) VALUES (?,?,?,?,?,?,?)",
                        (now.isoformat(), name, 0, 0, f"{type(exc).__name__}: {exc}"[:300], 0, time.time() - t0))
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
                 int(e["allday"]), e["url"], e["category"], int(matches(e, highlight_terms(name), whole_words=True)),
                 now.isoformat(), now.isoformat(), e["end"] and e["end"].isoformat()))
            kept += 1
        log.info("%s: %d parsed, %d kept", name, len(events), kept)
        con.execute("INSERT INTO runs (ts, source, ok, count, message, parsed, seconds) VALUES (?,?,?,?,?,?,?)",
                    (now.isoformat(), name, 1, kept, "", len(events), time.time() - t0))
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
    pages = CFG["pages"]
    assigned = {s for p in pages for s in p["sources"]}
    for s in sorted(set(PARSERS) - assigned):
        log.warning("source %s is not on any page; showing it on %s", s, pages[0]["title"])
    files = {p["id"]: f"{p['id']}.html" for p in pages}
    tpl = (ROOT / "template.html").read_text()
    out = ROOT / "site"
    out.mkdir(exist_ok=True)
    updated = html.escape(last_run[:16].replace("T", " ") if last_run else "never")
    cards = []
    for i, p in enumerate(pages):
        srcs = set(p["sources"]) | ((set(PARSERS) - assigned) if i == 0 else set())
        mine = [e for e in events if e["source"] in srcs]
        starred = [e for e in mine if e["highlight"]]
        cards.append(home_card(p, files[p["id"]], mine, now))
        nav = nav_html(pages, files, current=p["id"])
        page = (tpl.replace("/*EVENTS*/[]", json.dumps(mine, ensure_ascii=False).replace("</", "<\\/"))
                   .replace("/*STATUS*/[]", json.dumps([dict(s) for s in status if s["source"] in srcs],
                                                       ensure_ascii=False))
                   .replace("__PAGEID__", html.escape(p["id"]))
                   .replace("__PAGETITLE__", html.escape(p["title"]))
                   .replace("__DESCRIPTION__", html.escape(p.get("description", "")))
                   .replace("__NAV__", nav)
                   .replace("__UPDATED__", updated)
                   .replace("__FEED__", f"{p['id']}-starred.ics")
                   .replace("__FEEDCOUNT__", str(len(starred))))
        write_atomic(out / files[p["id"]], page)
        write_atomic(out / f"{p['id']}-starred.ics",
                     ics_calendar(f"Zurich Events · {p['title']} ★", starred, now))
        log.info("rendered %d events to %s (%d in calendar feed)", len(mine), out / files[p["id"]], len(starred))
    failed = [s["source"] for s in status if not s["ok"]]
    health = (f'<a class="health bad" href="diagnostic.html">⚠ {len(failed)} of {len(status)} sources failed</a>'
              if failed else f'<a class="health" href="diagnostic.html">✓ all {len(status)} sources OK</a>')
    home = ((ROOT / "home.html").read_text().replace("__CARDS__", "\n".join(cards))
            .replace("__UPDATED__", updated).replace("__HEALTH__", health))
    write_atomic(out / "index.html", home)
    log.info("rendered home page with %d sections", len(pages))
    render_diagnostic(con, pages, files, updated, now)
    # My picks: the events selected with + (loaded in the browser from server.py's /api/picks)
    write_atomic(out / "my.html", (ROOT / "picks.html").read_text()
                 .replace("__NAV__", nav_html(pages, files, "my")).replace("__UPDATED__", updated))


def nav_html(pages, files, current) -> str:
    links = [("index.html", "Home", "home")] + [(files[p["id"]], p["title"], p["id"]) for p in pages] \
        + [("my.html", "My picks", "my"), ("diagnostic.html", "Diagnostic", "diagnostic")]
    return "".join(f'<a href="{href}"{" aria-current=\"page\"" if key == current else ""}>{html.escape(title)}</a>'
                   for href, title, key in links)


def render_diagnostic(con, pages, files, updated, now):
    """diagnostic.html: per-source result of the last scan, history and coverage."""
    page_of = {s: p for p in pages for s in p["sources"]}
    history = {}
    for r in con.execute("SELECT source, ok, ts FROM runs WHERE ts IN "
                         "(SELECT DISTINCT ts FROM runs ORDER BY ts DESC LIMIT 14) ORDER BY ts"):
        history.setdefault(r["source"], []).append(r)
    last_ts = con.execute("SELECT MAX(ts) FROM runs").fetchone()[0]
    rows, passed, total = [], 0, 0
    for name in PARSERS:
        enabled = CFG["sources"].get(name, True)
        last = con.execute("SELECT * FROM runs WHERE source = ? ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
        ok_ts = con.execute("SELECT MAX(ts) FROM runs WHERE source = ? AND ok = 1", (name,)).fetchone()[0]
        listed, until = con.execute(
            'SELECT COUNT(*), MAX(start) FROM events WHERE source = ? AND (start >= ? OR "end" >= ?)',
            (name, now.isoformat(), now.isoformat())).fetchone()
        p = page_of.get(name, pages[0])
        if not enabled:
            state = '<span class="pill off">disabled</span>'
        elif not last:
            state = '<span class="pill off">not run yet</span>'
        elif last["ts"] != last_ts:
            state = '<span class="pill off">not in last scan</span>'
        else:
            total += 1
            passed += last["ok"]
            state = '<span class="pill ok">✓ passed</span>' if last["ok"] else '<span class="pill bad">✗ failed</span>'
        fmt = lambda ts: ts[:16].replace("T", " ") if ts else "–"
        strip = "".join(f'<i class="{"ok" if r["ok"] else "bad"}" title="{fmt(r["ts"])}: '
                        f'{"passed" if r["ok"] else "failed"}"></i>' for r in history.get(name, []))
        found = last["parsed"] if last and last["parsed"] is not None else "–"
        rows.append(
            f'<tr><td><b>{html.escape(name)}</b></td>'
            f'<td><a href="{files.get(p["id"], "index.html")}">{html.escape(p["title"])}</a></td>'
            f'<td>{state}</td><td class="n">{found}</td><td class="n">{last["count"] if last else "–"}</td>'
            f'<td class="n">{listed}</td><td>{until[:10] if until else "–"}</td>'
            f'<td>{fmt(ok_ts) if not (last and last["ok"]) else "this scan"}</td>'
            f'<td class="n">{f"{last["seconds"]:.1f}s" if last and last["seconds"] is not None else "–"}</td>'
            f'<td><span class="strip">{strip}</span></td>'
            f'<td class="msg">{html.escape(last["message"] or "") if last else ""}</td></tr>')
    summary = (f'<span class="pill {"ok" if passed == total else "bad"}">{passed} of {total} passed</span> '
               f'in the last scan')
    page = ((ROOT / "diagnostic.html").read_text().replace("__NAV__", nav_html(pages, files, "diagnostic"))
            .replace("__UPDATED__", updated).replace("__SUMMARY__", summary).replace("__ROWS__", "\n".join(rows)))
    write_atomic(ROOT / "site" / "diagnostic.html", page)
    log.info("rendered diagnostic page: %d of %d sources passed", passed, total)


# ---------------------------------------------------------------- calendar feed (RFC 5545)

EVENT_HOURS = 2   # the sources give no end time; calendar entries get this default length


def ics_text(s) -> str:
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r", "").replace("\n", "\\n"))


def ics_fold(line: str) -> str:
    """Fold content lines at 75 octets without splitting a UTF-8 character."""
    parts, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > 75 - (1 if parts else 0):
            parts.append(cur); cur, size = "", 0
        cur += ch; size += n
    return "\r\n ".join(parts + [cur])


def ics_event(e, stamp: dt.datetime) -> list:
    utc = lambda d: d.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start = dt.datetime.fromisoformat(e["start"])
    if e["allday"] or e["end"]:   # all-day; exhibitions span until their end date
        last = dt.datetime.fromisoformat(e["end"]).date() if e["end"] else start.date()
        when = [f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{last + dt.timedelta(days=1):%Y%m%d}"]
    else:
        when = [f"DTSTART:{utc(start)}", f"DTEND:{utc(start + dt.timedelta(hours=EVENT_HOURS))}"]
    desc = "\n".join(x for x in (e["subtitle"], e["category"], e["url"]) if x)
    return ["BEGIN:VEVENT", f"UID:{e['id']}@zurich-event-agent", f"DTSTAMP:{utc(stamp)}", *when,
            f"SUMMARY:{ics_text(e['title'])}", f"LOCATION:{ics_text(e['venue'])}",
            f"DESCRIPTION:{ics_text(desc)}", f"URL:{e['url']}", "END:VEVENT"]


def ics_calendar(name: str, events, stamp: dt.datetime) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//zurich-event-agent//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{ics_text(name)}", "X-WR-TIMEZONE:Europe/Zurich",
             "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H"]
    for e in events:
        lines += ics_event(e, stamp)
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(l) for l in lines) + "\r\n"


def write_atomic(target: Path, content: str):
    target.with_suffix(".tmp").write_text(content)
    target.with_suffix(".tmp").replace(target)


def home_card(page, href, events, now) -> str:
    """Card for the home page: counts plus the next few upcoming items (starred first)."""
    upcoming = [e for e in events if dt.datetime.fromisoformat(e["start"]) >= now]
    picks = ([e for e in upcoming if e["highlight"]] + [e for e in upcoming if not e["highlight"]])[:4]
    picks.sort(key=lambda e: e["start"])
    new = sum(e["new"] for e in events)
    rows = "".join(
        f'<li><span class="when">{dt.datetime.fromisoformat(e["start"]).strftime("%a %d.%m")}</span>'
        f'<span class="what">{"★ " if e["highlight"] else ""}{html.escape(e["title"])}</span></li>'
        for e in picks)
    return (f'<a class="card" href="{href}"><h2>{html.escape(page["title"])}</h2>'
            f'<div class="desc">{html.escape(page.get("description", ""))}</div>'
            f'<div class="stats"><span class="num">{len(events)}</span><span class="unit">events</span>'
            f'{f"<span class=badge>{new} new</span>" if new else ""}</div>'
            f'<ul>{rows}</ul><div class="go">Open {html.escape(page["title"])} →</div></a>')


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
