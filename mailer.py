"""Email the "My picks" calendar through Resend's HTTPS API (outgoing SMTP ports are blocked here).

The API key lives in resend.key (not in git). Sender and daily limit come from [email] in config.toml.
"""
import datetime as dt, html, json, re, secrets, threading, time, urllib.error, urllib.request

from agent import CFG, ROOT, TZ, ics_calendar

EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[A-Za-z]{2,}$")
MAX_RECIPIENTS = 5
_sent_today = {"day": None, "count": 0}
_lock = threading.Lock()


SHARED = ROOT / "site" / "shared"   # one-off calendar snapshots linked from emails
KEEP_DAYS = 90


def settings() -> dict:
    return ({"from": "Zurich Events <events@3dation.com>", "max_per_day": 20,
             "site_url": "https://events.3dation.com"} | CFG.get("email", {}))


def snapshot(ics: str) -> str:
    """Save the calendar under an unguessable name in site/shared/ and return its public URL.
    A link instead of an attachment: mail apps show an .ics attachment as an invitation for
    its first event only. The link holds just these events, never the site key."""
    SHARED.mkdir(parents=True, exist_ok=True)
    for old in SHARED.glob("*.ics"):
        if time.time() - old.stat().st_mtime > KEEP_DAYS * 86400:
            old.unlink(missing_ok=True)
    name = f"{secrets.token_urlsafe(12)}.ics"
    (SHARED / name).write_text(ics)
    return f"{settings()['site_url'].rstrip('/')}/shared/{name}"


def api_key() -> str:
    try:
        return (ROOT / "resend.key").read_text().strip()
    except FileNotFoundError:
        return ""


def parse_recipients(raw) -> list:
    """Accepts a list or a string separated by commas, semicolons or spaces; returns valid addresses."""
    items = raw if isinstance(raw, list) else re.split(r"[,;\s]+", str(raw or ""))
    out = list(dict.fromkeys(a.strip() for a in items if isinstance(a, str) and a.strip()))
    if not out or len(out) > MAX_RECIPIENTS or not all(EMAIL_RE.match(a) for a in out):
        raise ValueError(f"enter 1–{MAX_RECIPIENTS} valid email addresses")
    return out


def _when(e) -> str:
    start = dt.datetime.fromisoformat(e["start"]).astimezone(TZ)
    if e["end"]:
        return f"{start:%a %d.%m.} – {dt.datetime.fromisoformat(e['end']).astimezone(TZ):%a %d.%m.%Y}"
    return f"{start:%a %d.%m.%Y}" + ("" if e["allday"] else f", {start:%H:%M}")


def build(events: list, now: dt.datetime, publish=None):
    """Subject, HTML and text of the email; publish(ics) -> URL makes the "Add to calendar" link."""
    upcoming = [e for e in events
                if dt.datetime.fromisoformat(e["end"] or e["start"]) >= now - dt.timedelta(hours=6)]
    subject = f"Zurich Events · My picks ({len(upcoming)} upcoming)"
    ics = ics_calendar("Zurich Events · My picks", upcoming, now)
    link = publish(ics) if publish and upcoming else None
    rows = "".join(
        f'<tr><td style="padding:8px 14px 8px 0;vertical-align:top;white-space:nowrap;color:#6b6b70">'
        f'{html.escape(_when(e))}</td><td style="padding:8px 0;vertical-align:top">'
        f'<a href="{html.escape(e["url"])}" style="color:#b4321e;font-weight:600;text-decoration:none">'
        f'{html.escape(e["title"])}</a>'
        + (f'<br><span style="color:#6b6b70">{html.escape(e["subtitle"])}</span>' if e["subtitle"] else "")
        + f'<br><span style="color:#6b6b70">{html.escape(e["venue"])}</span></td></tr>'
        for e in upcoming)
    body = (f'<div style="font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;color:#1d1d1f;max-width:640px">'
            f'<h2 style="font-size:19px;margin:0 0 4px">My picks</h2>'
            f'<p style="color:#6b6b70;margin:0 0 14px">{len(upcoming)} upcoming events.</p>'
            + (f'<p style="margin:0 0 16px"><a href="{html.escape(link)}" style="display:inline-block;'
               f'background:#b4321e;color:#fff;text-decoration:none;font-weight:600;padding:8px 14px;'
               f'border-radius:8px">📅 Add all {len(upcoming)} to your calendar</a></p>' if link and upcoming else "")
            + (f'<table style="border-collapse:collapse;font-size:14px">{rows}</table>' if rows
               else "<p>No upcoming events selected.</p>")
            + '<p style="color:#9a9aa2;font-size:12px;margin-top:20px">Sent from '
              '<a href="https://events.3dation.com/" style="color:#9a9aa2">events.3dation.com</a></p></div>')
    text = "My picks\n\n" + "\n\n".join(
        f"{_when(e)}\n{e['title']}" + (f"\n{e['subtitle']}" if e["subtitle"] else "") + f"\n{e['venue']}\n{e['url']}"
        for e in upcoming) + (f"\n\nAdd all to your calendar: {link}" if link else "") \
        + "\n\nSent from https://events.3dation.com/\n"
    return subject, body, text


def send(to: list, events: list) -> str:
    key = api_key()
    if not key:
        raise RuntimeError("email is not set up yet (resend.key missing on the server)")
    cfg, now = settings(), dt.datetime.now(TZ)
    with _lock:
        if _sent_today["day"] != now.date():
            _sent_today.update(day=now.date(), count=0)
        if _sent_today["count"] >= cfg["max_per_day"]:
            raise RuntimeError(f"daily limit of {cfg['max_per_day']} emails reached")
        _sent_today["count"] += 1
    subject, body, text = build(events, now, publish=snapshot)
    req = urllib.request.Request(
        "https://api.resend.com/emails", method="POST",
        data=json.dumps({"from": cfg["from"], "to": to, "subject": subject, "html": body,
                         "text": text}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "zurich-event-agent/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("id", "")
    except urllib.error.HTTPError as exc:
        try:
            msg = json.load(exc).get("message", "")
        except ValueError:
            msg = ""
        raise RuntimeError(f"Resend refused the email ({exc.code}){': ' + msg if msg else ''}") from None
