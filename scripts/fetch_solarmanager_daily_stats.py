"""
Solar Manager CLOUD API -> Turso (Tagesstatistik Vortag)

Holt einmal taeglich (morgens) die aggregierten Statistikwerte des
VORTAGS von der Solar Manager Cloud API und speichert sie in Turso.

Endpunkt: GET /v1/statistics/gateways/{smId}?accuracy=day&from=...&to=...
Liefert fertige Tagessummen: consumption, production, selfConsumption,
selfConsumptionRate, autarchyDegree -- kein eigenes Aufsummieren noetig.

Benoetigte Umgebungsvariablen (siehe .env.solarmanager-live.example):
    SM_EMAIL          -> Solar Manager Login-E-Mail
    SM_API_KEY        -> Solar Manager Cloud API Key
    SM_GATEWAY_ID       -> Solar Manager Geraete-/Gateway-ID (SM-ID)
    SM_BASE_URL        -> Standard: https://cloud.solar-manager.ch
    TURSO_DATABASE_URL     -> z.B. libsql://<db>-<org>.turso.io
    TURSO_AUTH_TOKEN      -> Turso Auth Token
"""

import os
import sys
import json
import base64
import datetime as dt

import requests
import libsql_experimental as libsql


def _env_or_default(name: str, default: str) -> str:
    """Wie os.environ.get, behandelt aber leere Strings (z.B. nicht
    gesetzte GitHub Actions 'vars') ebenfalls als 'nicht gesetzt'."""
    value = os.environ.get(name)
    return value if value else default


SM_BASE_URL = _env_or_default("SM_BASE_URL", "https://cloud.solar-manager.ch")
SM_EMAIL = os.environ["SM_EMAIL"]
SM_API_KEY = os.environ["SM_API_KEY"]
SM_GATEWAY_ID = os.environ["SM_GATEWAY_ID"]

TURSO_DATABASE_URL = os.environ["TURSO_DATABASE_URL"]
TURSO_AUTH_TOKEN = os.environ["TURSO_AUTH_TOKEN"]


def basic_auth_header() -> str:
    raw = f"{SM_EMAIL}:{SM_API_KEY}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def yesterday_range_utc():
    """Liefert (from, to) fuer den gestrigen Kalendertag in UTC,
    im ISO-Format mit Millisekunden wie von der API erwartet."""
    today_utc = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    yesterday_start = today_utc - dt.timedelta(days=1)
    yesterday_end = today_utc
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return yesterday_start.strftime(fmt), yesterday_end.strftime(fmt), yesterday_start.date()


def fetch_daily_stats(from_iso: str, to_iso: str) -> dict:
    resp = requests.get(
        f"{SM_BASE_URL}/v1/statistics/gateways/{SM_GATEWAY_ID}",
        params={"accuracy": "day", "from": from_iso, "to": to_iso},
        headers={
            "accept": "application/json",
            "authorization": basic_auth_header(),
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS solarmanager_daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            stat_date TEXT NOT NULL UNIQUE,
            consumption_wh REAL,
            production_wh REAL,
            self_consumption_wh REAL,
            self_consumption_rate REAL,
            autarchy_degree REAL,
            raw_json TEXT
        )
        """
    )
    conn.commit()


def store_daily_stats(conn, stat_date: str, stats: dict):
    now = dt.datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT INTO solarmanager_daily_stats
        (fetched_at, stat_date, consumption_wh, production_wh,
         self_consumption_wh, self_consumption_rate, autarchy_degree, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stat_date) DO UPDATE SET
            fetched_at = excluded.fetched_at,
            consumption_wh = excluded.consumption_wh,
            production_wh = excluded.production_wh,
            self_consumption_wh = excluded.self_consumption_wh,
            self_consumption_rate = excluded.self_consumption_rate,
            autarchy_degree = excluded.autarchy_degree,
            raw_json = excluded.raw_json
        """,
        (
            now,
            stat_date,
            stats.get("consumption"),
            stats.get("production"),
            stats.get("selfConsumption"),
            stats.get("selfConsumptionRate"),
            stats.get("autarchyDegree"),
            json.dumps(stats),
        ),
    )
    conn.commit()


def main():
    from_iso, to_iso, stat_date = yesterday_range_utc()
    print(f"Hole Tagesstatistik fuer {stat_date} ({from_iso} bis {to_iso})")

    stats = fetch_daily_stats(from_iso, to_iso)
    print("Tagesstatistik erhalten:", json.dumps(stats))

    conn = libsql.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    ensure_schema(conn)
    store_daily_stats(conn, stat_date.isoformat(), stats)
    print("Tagesstatistik in Turso gespeichert.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTP-Fehler: {e} -- Antwort: {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)
