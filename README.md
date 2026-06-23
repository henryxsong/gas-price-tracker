# Gas Price Tracker

A self-hosted web app that tracks gas prices across multiple stations, sends push notifications, and shows price history — all behind Google OAuth so only you can see your data.

## Features

- **Dashboard** — live price cards for each tracked station, with a "best price" summary across all fuel types (regular, midgrade, premium, diesel, E85)
- **Station management** — search and add stations from GasBuddy or Costco; Tulalip Market is added automatically on first login
- **Price history** — per-station and per-fuel-type charts over a configurable time window
- **Automatic refresh** — APScheduler runs price fetches on a configurable interval (default: every 60 minutes)
- **Push notifications via [ntfy](https://ntfy.sh)** — daily price summaries and price-threshold alerts when a fuel type drops below a set price
- **Per-user data isolation** — each Google account gets its own stations, settings, and price history
- **Docker Compose deployment** — one command to run, SQLite database persisted to a local volume

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Uvicorn |
| Database | SQLite via SQLAlchemy (async) |
| Scheduler | APScheduler |
| Scraping | httpx + BeautifulSoup4 |
| Templates | Jinja2 |
| Auth | Google OAuth 2.0 |
| Notifications | ntfy |

## Quick Start (Docker)

**1. Clone and copy the env file**

```bash
git clone <repo-url>
cd gas-price-tracker
cp .env.example .env
```

**2. Fill in `.env`**

```env
PORT=6767
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
SESSION_SECRET=<random 32-byte hex string>
BASE_URL=http://localhost:6767
```

Generate a session secret:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Set up Google OAuth at [console.cloud.google.com](https://console.cloud.google.com):
- Application type: **Web application**
- Authorized redirect URI: `http://localhost:6767/auth/callback`

**3. Run**

```bash
docker compose up -d
```

The app is available at `http://localhost:6767`.

## Quick Start (Local)

```bash
cd gas-price-tracker
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r app/requirements.txt
cp .env.example .env  # fill in values
./start.sh
```

The app runs on port `8081` by default when started via `start.sh` (override with `PORT=`).

## Settings

Settings are per-user and configurable from the `/settings` page:

| Setting | Default | Description |
|---|---|---|
| Auto-refresh | Enabled | Fetch prices automatically |
| Refresh interval | 60 min | How often to refresh |
| ntfy server URL | `https://ntfy.sh` | ntfy server to use |
| ntfy topic | — | Your ntfy topic |
| ntfy token | — | Optional auth token |
| Daily notification | Disabled | Send a daily price summary |
| Notification schedule | `0 8 * * *` | Cron expression for daily summary |
| Notify on refresh | Disabled | Send a notification after every refresh |
| Price thresholds | — | Alert when a fuel type drops below a price |

## Supported Station Sources

| Type | Notes |
|---|---|
| `gasbuddy` | Search by name/location via GasBuddy |
| `costco` | Costco gas stations |
| `tulalip` | Tulalip Market (added by default) |

## Data

Prices and station data are stored in a SQLite database at `data/gas_prices.db`. When running via Docker Compose, this file is mounted as a volume and persists across container restarts.
