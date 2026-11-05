# Zurich event agent

Scrapes event programmes from Zurich venues and universities once a day and publishes them
as a small multi-page site in `site/`: a home page (`index.html`) with a card per section, and one
page per `[[pages]]` block in `config.toml` (currently `culture.html`, `theater.html` and `education.html`), with tabs
between them. Each page has its own `highlight` star words (whole-word match); viewers can add
more star topics on the page itself, saved in their browser only. A "From" time field hides events that start earlier
(e.g. 18:00 for evenings only); events without a start time stay visible. Viewers can select events
(the + on each card, kept in their browser) and download them as one `.ics` calendar file, or
download a single event with its 📅 link. Each page also publishes `<id>-starred.ics`, a calendar
feed of its starred events (built-in `highlight` words only) that calendar apps can subscribe to;
it is rewritten on every run. The sources give no end times, so timed events get 2 hours
(`EVENT_HOURS` in `agent.py` and `template.html`). A Diagnostic tab
(`diagnostic.html`) shows per source whether the last scan passed, events found/kept, coverage,
the last 14 scans and any error; the home page links to it with a health summary.

| File | Purpose |
|---|---|
| `config.toml` | exclude / highlight keywords, days ahead, sources on/off, pages and their sources |
| `agent.py` | one parser per venue + SQLite storage + page rendering |
| `template.html`, `home.html`, `diagnostic.html` | page layouts |
| `run.sh` | daily scan (logs to `cron.log`) |
| `serve.sh` | runs `server.py` on port 8765 (localhost by default, `BIND=0.0.0.0` for network) |
| `mailer.py` | builds and sends the My picks email through Resend |
| `server.py` | serves `site/` plus the synced selection (`/api/picks`) and the "My picks" feed (`/my.ics`) |
| `tunnel.sh` | runs the Cloudflare Tunnel that publishes the site at https://events.3dation.com |
| `events.db` | all events seen; `first_seen` drives the NEW badge |

Sources: Opernhaus, Tonhalle-Orchester, Schauspielhaus, Moods, Kaufleuten, Bogen F,
Rote Fabrik, Gessnerallee, Hallenstadion, Museum für Gestaltung, Theater Rigiblick,
Kunsthaus (exhibitions only), ETH Zürich and Universität Zürich (both via their public
event-calendar JSON APIs). Paginated sites are followed up to `max_pages` or until
`days_ahead` is reached. Gessnerallee (~2 weeks) and Moods (~1 month) publish less far ahead.
Exhibitions carry an end date and appear under "On now" while running.

Theatres (Theater page): in Zürich Theater Neumarkt, Theater am Hechtplatz, Bernhard Theater,
Theater Winkelwiese, Millers, Theater Stadelhofen (the former Zürcher Puppentheater), Theater Stok,
Theater Purpur, Tanzhaus Zürich (its calendar JSON feed; trainings and courses are left out),
sogar theater, Keller62 and Theater 11 (from musical.ch; multi-day runs are listed from first to
last day, like exhibitions); in Winterthur Theater Winterthur, Casinotheater, Theater am Gleis,
Kellertheater and Theater Kanton Zürich (which tours the canton; the place is in the subtitle);
in Baden Kurtheater and ThiK Theater im Kornhaus. Theater Stok and Theater Winterthur load
their lists with a POST request, as their own pages do. School performances are dropped by
the `exclude` words.

Not usable: the Zürich Tourismus open-data API (`zuerich.com/en/api/v2/data`) lists
venues and places but no dated events; Landesmuseum renders its listing client-side;
tonhalle-maag.ch is no longer the venue's site. ZHdK's Theater der Künste is listed only through
zhdk.ch's event API, which robots.txt disallows (`/api`), and theaterderkuenste.ch is no longer
the theatre's domain. No "Figurentheater Baden" with its own programme was found (the nearest is
Figurentheater Wettingen).

The agent identifies itself honestly and obeys robots.txt (RFC 9309: a 4xx on robots.txt
means no restrictions, a 5xx means stay away), including a `Crawl-delay` longer than
`request_delay_seconds`. If a site changes its layout,
its parser yields 0 events and the page footer marks that source as failed; test a single
parser with `.venv/bin/python agent.py --only <source>`.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh            # first scan
./serve.sh &        # page at http://127.0.0.1:8765 (localhost only)
BIND=0.0.0.0 ./serve.sh &   # or: reachable from the network on port 8765
```

Schedule with `crontab -e`:

```
0 7 * * * /path/to/run.sh
@reboot /path/to/serve.sh
@reboot /path/to/tunnel.sh
```

## My picks calendar

On the self-hosted site, `server.py` keeps the events selected with + on the server, in
`picks.json`, and serves them as a calendar feed at `/my.ics?key=SECRET`. Subscribe to that URL
in a calendar app and it contains exactly the events you picked. `SECRET` is created on the
first start in `picks.key`. Open the site once per browser or device as
`https://events.3dation.com/?key=SECRET`. The page stores the key, removes it from the address
bar, syncs the selection, and shows the feed link in the footer. Without the key the selection
stays in the browser, as on GitHub Pages, which has no server. To lock out a leaked key, delete
`picks.key` and restart `serve.sh`; a new one is created.

The **My picks** tab (`my.html`, from `picks.html`) shows the selection as a month calendar
and an agenda list, with ✓ to remove an event. It loads the events from `/api/picks`, so it
too works only on the self-hosted site.

**Email.** On the My picks tab, ✉ sends the upcoming picks to 1–5 addresses: a list in the email
plus an "Add all to your calendar" link to a snapshot `.ics` in `site/shared/` (random name,
no key, deleted after 90 days; an attachment showed up as an invitation for its first event only). Outgoing SMTP is blocked on this host, so `mailer.py` sends
through Resend's HTTPS API. Set-up: verify `3dation.com` at resend.com, create an API key with
sending access, and save it in `resend.key` (not in git). The sender and a daily limit are set
under `[email]` in `config.toml`.

## Domain (Cloudflare Tunnel)

The site is public at **https://events.3dation.com**. `3dation.com` uses Cloudflare's nameservers,
and `cloudflared` (in `~/.local/bin`, no root needed) opens an outbound tunnel that forwards the
hostname to `localhost:8765`. The web server itself listens on localhost only, so nothing is
exposed on the server's IP and Cloudflare provides HTTPS.

Set-up, done once:

```
cloudflared tunnel login                     # or copy cert.pem to ~/.cloudflared/
cloudflared tunnel create zurich-events
cloudflared tunnel route dns zurich-events events.3dation.com
```

`~/.cloudflared/config.yml` (not in the repo):

```yaml
tunnel: <tunnel-id>
credentials-file: /home/claude/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: events.3dation.com
    service: http://localhost:8765
  - service: http_status:404
```

The page has no login; anyone with the address can view it. To restrict it, add a
Cloudflare Access application for `events.3dation.com` (Zero Trust → Access → Applications).

## Hosting on GitHub Pages

`.github/workflows/pages.yml` runs the scan on GitHub Actions every day (about 07:00 Zurich time)
and publishes `site/` to GitHub Pages. You can also start it by hand with **Run workflow** on the
Actions tab. A push to `main` that changes the code, config or templates only re-renders the
pages from the saved data. `events.db` is kept between runs on the `data` branch, which holds a
single commit that each run replaces. Delete that branch to start over with an empty database.

One-time setup: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
The site is public even if the repository is private. Pages for a private repository needs a
paid GitHub plan.
