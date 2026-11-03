# Zurich event agent

Scrapes music & culture programmes from Zurich venues once a day and publishes them
as a searchable page (`site/index.html`).

| File | Purpose |
|---|---|
| `config.toml` | exclude / highlight keywords, days ahead, sources on/off |
| `agent.py` | one parser per venue + SQLite storage + page rendering |
| `template.html` | the page layout |
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
