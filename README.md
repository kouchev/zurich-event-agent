# Zurich event agent

Scrapes music & culture programmes from Zurich venues once a day and publishes them
as a searchable page (`site/index.html`).

| File | Purpose |
|---|---|
| `config.toml` | exclude / highlight keywords, days ahead, sources on/off |
| `agent.py` | one parser per venue + SQLite storage + page rendering |
| `template.html` | the page layout |
| `run.sh` | daily scan (logs to `cron.log`) |
| `serve.sh` | serves `site/` on 127.0.0.1:8765 |
| `events.db` | all events seen; `first_seen` drives the NEW badge |

Sources: Opernhaus, Tonhalle-Orchester, Schauspielhaus, Moods, Kaufleuten, Bogen F,
Rote Fabrik, Gessnerallee, Hallenstadion (next ~12 shows only), Museum für Gestaltung,
Theater Rigiblick. Kunsthaus and Tonhalle Maag are excluded because their robots.txt
disallows crawlers.

The agent identifies itself honestly and obeys robots.txt. If a site changes its layout,
its parser yields 0 events and the page footer marks that source as failed; test a single
parser with `.venv/bin/python agent.py --only <source>`.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh            # first scan
./serve.sh &        # page at http://127.0.0.1:8765
# schedule: crontab -e  →  0 7 * * * /path/to/run.sh   and   @reboot /path/to/serve.sh
```
