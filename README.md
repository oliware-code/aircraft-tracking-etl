# Aircraft Tracking — an ETL pipeline on the OpenSky Network API

A small end-to-end data pipeline that ingests live aircraft state vectors from the
[OpenSky Network](https://opensky-network.org/) REST API into PostgreSQL on a schedule,
and serves the accumulated history back through a Flask web app with live maps.

Runs in production on a Raspberry Pi 5, ingesting ~10,000 state vectors per cycle.

```
                 ┌─────────────────────────────┐
 cron ──────────▶│ main.py                     │
                 │  OAuth2 client-credentials  │
                 │  flow with token caching    │──▶ OpenSky /states/all
                 │  insert w/ conflict handling│
                 └──────────────┬──────────────┘
                                ▼
                 ┌─────────────────────────────┐
                 │ PostgreSQL (schema.sql)     │
                 │  aircraft ◀─┐               │
                 │  states  ───┤ foreign keys  │
                 │  flight_routes ◀┘           │
                 └──────────────┬──────────────┘
                                ▼
                 ┌─────────────────────────────┐
                 │ app.py + queries.py (Flask) │
                 │  search, live map, trails   │
                 └─────────────────────────────┘
```

## Components

| Path | Role |
|---|---|
| `main.py` | Extract + load: fetches one snapshot of all aircraft states and inserts it |
| `schema.sql` | PostgreSQL schema: tables, keys, and the indexes the queries rely on |
| `cronjob_entry.sh` | Cron entry point (activates the venv, runs `main.py`) |
| `db_connection.py` | Connection factory reading `database.ini` |
| `queries.py` | All SQL for the read side; also usable as a CLI (`python queries.py status <icao24>`) |
| `app.py` | Flask app: search by callsign/icao24, per-aircraft history, live maps |
| `templates/`, `static/` | Leaflet-based map pages (`/`, `/named`, `/latest`) |

## Data model

Three tables (see `schema.sql`):

- **`aircraft`** — one row per airframe (`icao24` primary key), enriched by hand with
  registration, type, and a friendly name for aircraft I follow.
- **`flight_routes`** — one row per callsign, enriched with operator and origin/destination.
- **`states`** — the fact table: one row per aircraft per snapshot, composite primary key
  `(timestamp, icao24)`, foreign keys into both dimension tables.

The read queries normalize callsign/icao24 comparisons with `TRIM(UPPER(...))`, so the
schema includes matching functional indexes (`idx_states_callsign_norm`,
`idx_states_icao24_norm_ts`) to keep those lookups off sequential scans.

## Setup

Requires Python 3.11+ and a PostgreSQL server.

```bash
git clone <this repo> && cd aircraft-tracking-etl
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Database
createdb opensky
psql -d opensky -f schema.sql

# Local config (all gitignored)
cp database.ini.example database.ini        # fill in your DB host/user/password
cp credentials.py.example credentials.py    # fill in your OpenSky API client id/secret
cp notify_watchlist.yaml.example notify_watchlist.yaml    # icao24s to alert on status changes
```

Run one ingest cycle manually:

```bash
venv/bin/python main.py
```

Schedule it (every 2 minutes here; OpenSky rate limits are credit-based, so pick an
interval that fits your account):

```cron
*/2 * * * * /path/to/aircraft-tracking-etl/cronjob_entry.sh
```

Run the web app:

```bash
venv/bin/python app.py    # http://localhost:5000
```

Routes: `/` (search + history trail), `/named` (live map of the aircraft with friendly
names), `/latest` (map of the most recent full snapshot).

## Design decisions

- **Cron over a long-running loop.** The ingester started as a `while True` loop and was
  reworked into a run-once script triggered by cron: no daemon to babysit, a crashed run
  affects only one cycle, and the OS handles scheduling and restarts.
- **Cached OAuth2 tokens.** OpenSky uses the client-credentials flow with ~30-minute
  tokens. Tokens are cached to disk with their absolute expiry and reused across runs
  (with a 60-second safety buffer); a 401 clears the cache and forces a refresh on the
  next call. Without this, every cron run would burn a token exchange.
- **Idempotent dimension inserts.** New aircraft and callsigns are inserted with
  `ON CONFLICT DO NOTHING`, so re-running a cycle never duplicates dimensions, and
  `rowcount` cheaply reports what was genuinely new.
- **Timestamps converted to UTC before storage.** The API delivers Unix epoch seconds;
  `main.py` converts all three timestamp fields (`timestamp`, `time_position`,
  `last_contact`) to UTC-aware `datetime` objects via
  `datetime.fromtimestamp(epoch, tz=timezone.utc)` before inserting, so the database
  stores them as native `TIMESTAMPTZ`. History queries bucket by integer division using
  `EXTRACT(EPOCH FROM timestamp)::bigint / 300` to thin trails for plotting.
- **Read/write split.** The write path (`main.py`) and read path (`queries.py`) share
  only the connection factory, so the web app can evolve without touching ingestion.

## Notes

- The Flask app is a LAN tool; it binds to `0.0.0.0` with the debugger off (set
  `FLASK_DEBUG=1` explicitly when developing).
