"""
Solar Manager CLOUD API -> Turso (rollierende 7-Tage-Statistik)

Laeuft einmal taeglich (morgens) und aktualisiert die taeglichen
Verbrauchs-/Produktionswerte der letzten 7 Tage -- sowohl als
Gesamtsumme (Gateway) als auch pro Geraet. Bereits vorhandene Tage
werden per UPSERT aktualisiert (kein Duplikat), sodass z.B. ein noch
unvollstaendiger Tag beim naechsten Lauf mit dem finalen Wert
ueberschrieben wird.

Endpunkte:
  - GET /v1/statistics/gateways/{smId}?accuracy=day&from=...&to=...
    (Gesamtsumme, ein Aufruf pro Tag noetig)
  - GET /v1/consumption/sensor/{sensorId}?period=week
    (liefert die letzten ~7 Tage pro Geraet in einem Aufruf)

Benoetigte Umgebungsvariablen:
    SM_EMAIL, SM_API_KEY, SM_GATEWAY_ID, SM_BASE_URL (optional)
    TURSO_DATABASE_URL, TURSO_AUTH_TOKEN
"""

import os
import sys
import json
import base64
import datetime as dt

import requests
import libsql_experimental as libsql


def _env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


SM_BASE_URL = _env_or_default("SM_BASE_URL", "https://cloud.solar-manager.ch")
SM_EMAIL = os.environ["SM_EMAIL"]
SM_API_KEY = os.environ["SM_API_KEY"]
SM_GATEWAY_ID = os.environ["SM_GATEWAY_ID"]

TURSO_DATABASE_URL = os.environ["TURSO_DATABASE_URL"]
TURSO_AUTH_TOKEN = os.environ["TURSO_AUTH_TOKEN"]

# Geraete-IDs (sensorId == deviceId), aus den Live-Daten bekannt.
DEVICE_IDS = [
    "65168ab128ec518afa0f665f",
    "6516885afd811a3c709c5bc2",
    "65d8b14d8c3c733fc7e1a3c2",
    "651bdb00fd811a3c70ec582a",
    "65b54af8be4cb07b15618b39",
    "68dd100001eb837abd21b2fd",
    "68dd114ae232d2c6444e11a2",
    "658153db411180774fe01246",
]

ROLLING_DAYS = 7


def basic_auth_header() -> str:
    raw = f"{SM_EMAIL}:{SM_API_KEY}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def auth_headers() -> dict:
    return {"accept": "application/json", "authorization": basic_auth_header()}


def last_n_complete_days_utc(n: int):
    """Liste von (day_date, from_iso, to_iso) fuer die letzten n
    ABGESCHLOSSENEN Kalendertage in UTC (nicht der laufende Tag)."""
    today_utc = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    days = []
    for i in range(n, 0, -1):
        day_start = today_utc - dt.timedelta(days=i)
        day_end = today_utc - dt.timedelta(days=i - 1)
        days.append((day_start.date(), day_start.strftime(fmt), day_end.strftime(fmt)))
    return days


def fetch_gateway_day_stats(from_iso: str, to_iso: str) -> dict:
    resp = requests.get(
        f"{SM_BASE_URL}/v1/statistics/gateways/{SM_GATEWAY_ID}",
        params={"accuracy": "day", "from": from_iso, "to": to_iso},
        headers=auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_device_week(sensor_id: str) -> dict:
    resp = requests.get(
        f"{SM_BASE_URL}/v1/consumption/sensor/{sensor_id}",
        params={"period": "week"},
        headers=auth_headers(),
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS solarmanager_daily_stats_by_device (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            stat_date TEXT NOT NULL,
            device_id TEXT NOT NULL,
            consumption_wh REAL,
            UNIQUE(stat_date, device_id)
        )
        """
    )
    conn.commit()


def upsert_gateway_day(conn, stat_date: str, stats: dict):
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


def upsert_device_day(conn, stat_date: str, device_id: str, consumption_wh: float):
    now = dt.datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT INTO solarmanager_daily_stats_by_device
        (fetched_at, stat_date, device_id, consumption_wh)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(stat_date, device_id) DO UPDATE SET
            fetched_at = excluded.fetched_at,
            consumption_wh = excluded.consumption_wh
        """,
        (now, stat_date, device_id, consumption_wh),
    )
    conn.commit()


def main():
    conn = libsql.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    ensure_schema(conn)

    # --- Gesamtsumme (Gateway), Tag fuer Tag der letzten ROLLING_DAYS ---
    for day_date, from_iso, to_iso in last_n_complete_days_utc(ROLLING_DAYS):
        print(f"Gateway-Statistik fuer {day_date} ...")
        stats = fetch_gateway_day_stats(from_iso, to_iso)
        upsert_gateway_day(conn, day_date.isoformat(), stats)
        print(f"  -> gespeichert: {json.dumps(stats)}")

    # --- Pro Geraet, ein Aufruf liefert die letzten ~7 Tage ---
    for device_id in DEVICE_IDS:
        print(f"Geraete-Statistik fuer {device_id} (period=week) ...")
        try:
            result = fetch_device_week(device_id)
        except requests.HTTPError as e:
            print(f"  -> Fehler, ueberspringe Geraet: {e}", file=sys.stderr)
            continue

        for entry in result.get("data", []):
            stat_date = entry.get("createdAt")
            consumption = entry.get("consumption")
            if stat_date is None:
                continue
            upsert_device_day(conn, stat_date, device_id, consumption)
        print(f"  -> {len(result.get('data', []))} Tage gespeichert")

    print("Fertig: rollierende 7-Tage-Statistik aktualisiert.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTP-Fehler: {e} -- Antwort: {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)
