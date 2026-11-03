# Zurich event agent

Scrapes event programmes from Zurich venues and universities once a day and publishes them
as a small multi-page site in `site/`: a home page (`index.html`) with a card per section, and one
page per `[[pages]]` block in `config.toml` (currently `culture.html` and `education.html`), with tabs
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
| `serve.sh` | serves `site/` on port 8765 (localhost by default, `BIND=0.0.0.0` for network) |
| `events.db` | all events seen; `first_seen` drives the NEW badge |

Sources: Opernhaus, Tonhalle-Orchester, Schauspielhaus, Moods, Kaufleuten, Bogen F,
Rote Fabrik, Gessnerallee, Hallenstadion, Museum für Gestaltung, Theater Rigiblick,
Kunsthaus (exhibitions only), ETH Zürich and Universität Zürich (both via their public
event-calendar JSON APIs). Paginated sites are followed up to `max_pages` or until
`days_ahead` is reached. Gessnerallee (~2 weeks) and Moods (~1 month) publish less far ahead.
Exhibitions carry an end date and appear under "On now" while running.

Not usable: the Zürich Tourismus open-data API (`zuerich.com/en/api/v2/data`) lists
venues and places but no dated events; Landesmuseum renders its listing client-side;
tonhalle-maag.ch is no longer the venue's site.

The agent identifies itself honestly and obeys robots.txt (RFC 9309: a 4xx on robots.txt
means no restrictions, a 5xx means stay away). If a site changes its layout,
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
@reboot BIND=0.0.0.0 /path/to/serve.sh
```

The page has no login; with `BIND=0.0.0.0` anyone who knows the address can view it.

## Hosting on GitHub Pages

`.github/workflows/pages.yml` runs the scan on GitHub Actions every day (about 07:00 Zurich time)
and publishes `site/` to GitHub Pages. You can also start it by hand with **Run workflow** on the
Actions tab. A push to `main` that changes the code, config or templates only re-renders the
pages from the saved data. `events.db` is kept between runs on the `data` branch, which holds a
single commit that each run replaces. Delete that branch to start over with an empty database.

One-time setup: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
The site is public even if the repository is private. Pages for a private repository needs a
paid GitHub plan.
