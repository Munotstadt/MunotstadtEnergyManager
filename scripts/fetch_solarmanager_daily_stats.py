"""
Solar Manager CLOUD API -> Turso (rollierende N-Tage-Statistik)

Laeuft einmal taeglich (01:00 UTC) und aktualisiert standardmaessig die
letzten 10 abgeschlossenen Kalendertage (Schweizer Lokalzeit,
Europe/Zurich, DST-aware -- siehe last_n_complete_days_zurich()):
  1. Gesamtsumme (Gateway) -> solarmanager_daily_stats
  2. Pro Geraet             -> solarmanager_daily_stats_by_device
  3. Zusammengefuehrt       -> solarmanager_data (kWh, ueber
     solarmanager_devices.Bezeichnung den richtigen Spalten
     zugeordnet)

Alle Schreibvorgaenge sind UPSERTs (kein Duplikat bei erneutem Lauf) --
das Fenster kann daher per ROLLING_DAYS env var vergroessert werden, um
bereits gespeicherte Tage rueckwirkend zu korrigieren (z.B. einmaliger
manueller Lauf mit ROLLING_DAYS=30 nach einem Bugfix).

Endpunkte:
  - GET /v1/statistics/gateways/{smId}?accuracy=day&from=...&to=...
    (Gesamtsumme, ein Aufruf pro Tag)
  - GET /v3/devices/{deviceId}/data/range?from=...&to=...&interval=900
    (Rohdatenpunkte pro Geraet, ein Aufruf pro Tag pro Geraet; ersetzt
    das ehemals genutzte /v1/consumption/sensor/{sensorId}?period=...,
    dessen Tagesgrenzen server-seitig unkontrollierbar waren. Jeder
    Punkt enthaelt 'iWh' -- den fertigen Verbrauch DIESES 15'-Intervalls
    -- der direkt pro Tag aufsummiert wird, kein Differenzbilden noetig.
    Verifiziert per Debug-Test am 21.08.2026.)

GridFrom_kWh / GridTo_kWh werden abgeleitet, da die Statistik-API
keinen direkten Netzbezug/-einspeisungswert liefert:
  GridFrom (Netzbezug)     = consumption - selfConsumption
  GridTo   (Netzeinspeisung) = production   - selfConsumption

Benoetigte Umgebungsvariablen:
    SM_EMAIL, SM_API_KEY, SM_GATEWAY_ID, SM_BASE_URL (optional)
    TURSO_DATABASE_URL, TURSO_AUTH_TOKEN
"""

import os
import sys
import json
import base64
import datetime as dt
from zoneinfo import ZoneInfo

import requests
import libsql_experimental as libsql

ZURICH_TZ = ZoneInfo("Europe/Zurich")


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

ROLLING_DAYS = int(_env_or_default("ROLLING_DAYS", "10"))

# Bezeichnung (aus solarmanager_devices, endet auf "_Wh") -> Spalte in
# solarmanager_data (endet auf "_kWh"). Nur diese drei Geraete haben
# feste Spalten in solarmanager_data.
DEVICE_COLUMN_MAP = {
    "Entfeuchter_Waschen_Wh": "Entfeuchter_Waschen_kWh",
    "Wasserpumpe_Wh": "Wasserpumpe_kWh",
    "Ladestation_Wh": "Ladestation_kWh",
}

SOURCE_LABEL = "Solar Manager Tagesstatistik -> Turso"


def basic_auth_header() -> str:
    raw = f"{SM_EMAIL}:{SM_API_KEY}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def auth_headers() -> dict:
    return {"accept": "application/json", "authorization": basic_auth_header()}


def last_n_complete_days_zurich(n: int):
    """Liste von (day_date, from_iso, to_iso) fuer die letzten n
    ABGESCHLOSSENEN Kalendertage in Schweizer Lokalzeit (Europe/Zurich,
    DST-aware -- CET im Winter, CEST im Sommer).

    Die Tagesgrenzen werden erst NACH der lokalen Berechnung nach UTC
    konvertiert (die API erwartet UTC-Zeitstempel). Vorher wurde direkt
    in UTC-Kalendertagen gerechnet (00:00-24:00 UTC), was in der
    Schweizer Zeitzone einer Verschiebung von 1h (Winter) bzw. 2h
    (Sommer) gegenueber dem echten Kalendertag entsprach -- z.B. wurden
    dadurch die ersten zwei Stunden eines Schweizer Tages faelschlich
    dem Vortag zugerechnet."""
    now_local = dt.datetime.now(ZURICH_TZ)
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    days = []
    for i in range(n, 0, -1):
        day_start_local = today_local - dt.timedelta(days=i)
        day_end_local = today_local - dt.timedelta(days=i - 1)
        day_start_utc = day_start_local.astimezone(dt.timezone.utc)
        day_end_utc = day_end_local.astimezone(dt.timezone.utc)
        days.append((day_start_local.date(), day_start_utc.strftime(fmt), day_end_utc.strftime(fmt)))
    return days


def fetch_device_range(device_id: str, from_iso: str, to_iso: str, interval: int = 900) -> list:
    """Holt Rohdatenpunkte fuer ein Geraet ueber /v3/devices/{deviceId}/data/range
    (ersetzt /v1/data/sensor/{sensorId}/range). Nimmt explizite from/to-
    Zeitstempel entgegen -- im Gegensatz zum alten /v1/consumption/sensor
    (period=week/month) haben wir hier volle Kontrolle ueber die
    Tagesgrenze und koennen dieselbe korrekte Zuerich-Lokalzeit-Grenze
    verwenden wie beim Gateway-Total.

    Jeder Punkt enthaelt u.a. 'iWh' (Verbrauch DIESES Intervalls in Wh --
    kein Zaehler, direkt aufsummierbar) und 'iWhTotal' (kumulativer
    Lifetime-Zaehler, hier nicht benoetigt). Verifiziert per Debug-Test
    am 21.08.2026 gegen Geraet 65d8b14d8c3c733fc7e1a3c2."""
    resp = requests.get(
        f"{SM_BASE_URL}/v3/devices/{device_id}/data/range",
        params={"from": from_iso, "to": to_iso, "interval": interval},
        headers=auth_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "values", "points", "result", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def sum_device_day_wh(points: list) -> float:
    """Summiert das bereits fertige Intervall-Feld 'iWh' ueber alle
    Punkte eines Tages (kein Differenzbilden noetig, iWh ist bereits
    der Verbrauch des jeweiligen Intervalls, nicht der Zaehlerstand)."""
    return sum((p.get("iWh") or 0) for p in points)


def fetch_gateway_day_stats(from_iso: str, to_iso: str) -> dict:
    resp = requests.get(
        f"{SM_BASE_URL}/v1/statistics/gateways/{SM_GATEWAY_ID}",
        params={"accuracy": "day", "from": from_iso, "to": to_iso},
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
    # solarmanager_data und solarmanager_devices werden vorausgesetzt
    # (bereits vorhanden lt. Vorgabe) -- hier defensiv nur pruefen/anlegen
    # falls sie fehlen, mit exakt der vorgegebenen Struktur.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS solarmanager_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE,
            Bezeichnung TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS solarmanager_data (
            Date_ISO TEXT PRIMARY KEY,
            Date_Display TEXT NOT NULL,
            Consumption_kWh REAL DEFAULT 0 NOT NULL,
            Production_kWh REAL DEFAULT 0 NOT NULL,
            GridFrom_kWh REAL DEFAULT 0 NOT NULL,
            GridTo_kWh REAL DEFAULT 0 NOT NULL,
            Entfeuchter_Waschen_kWh REAL DEFAULT 0 NOT NULL,
            Wasserpumpe_kWh REAL DEFAULT 0 NOT NULL,
            Ladestation_kWh REAL DEFAULT 0 NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT
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


def get_device_bezeichnung_map(conn) -> dict:
    """device_id -> Bezeichnung (getrimmt), aus solarmanager_devices."""
    rows = conn.execute(
        "SELECT device_id, Bezeichnung FROM solarmanager_devices"
    ).fetchall()
    return {row[0]: row[1].strip() for row in rows if row[1] and row[1].strip()}


def upsert_solarmanager_data(conn, stat_date: str, gateway_stats: dict,
                              device_wh_by_column: dict):
    """Fuehrt Gateway- und Geraete-Tageswerte in solarmanager_data
    zusammen (kWh), inkl. abgeleitetem GridFrom/GridTo."""
    consumption_wh = gateway_stats.get("consumption") or 0
    production_wh = gateway_stats.get("production") or 0
    self_consumption_wh = gateway_stats.get("selfConsumption") or 0

    consumption_kwh = consumption_wh / 1000
    production_kwh = production_wh / 1000
    grid_from_kwh = max(consumption_wh - self_consumption_wh, 0) / 1000
    grid_to_kwh = max(production_wh - self_consumption_wh, 0) / 1000

    entfeuchter_kwh = device_wh_by_column.get("Entfeuchter_Waschen_kWh", 0) / 1000
    wasserpumpe_kwh = device_wh_by_column.get("Wasserpumpe_kWh", 0) / 1000
    ladestation_kwh = device_wh_by_column.get("Ladestation_kWh", 0) / 1000

    date_obj = dt.date.fromisoformat(stat_date)
    date_display = date_obj.strftime("%d.%m.%Y")
    updated_at = dt.datetime.utcnow().strftime("%d.%m.%Y %H:%M:%S")

    conn.execute(
        """
        INSERT INTO solarmanager_data
        (Date_ISO, Date_Display, Consumption_kWh, Production_kWh,
         GridFrom_kWh, GridTo_kWh, Entfeuchter_Waschen_kWh,
         Wasserpumpe_kWh, Ladestation_kWh, updated_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(Date_ISO) DO UPDATE SET
            Date_Display = excluded.Date_Display,
            Consumption_kWh = excluded.Consumption_kWh,
            Production_kWh = excluded.Production_kWh,
            GridFrom_kWh = excluded.GridFrom_kWh,
            GridTo_kWh = excluded.GridTo_kWh,
            Entfeuchter_Waschen_kWh = excluded.Entfeuchter_Waschen_kWh,
            Wasserpumpe_kWh = excluded.Wasserpumpe_kWh,
            Ladestation_kWh = excluded.Ladestation_kWh,
            updated_at = excluded.updated_at,
            source = excluded.source
        """,
        (
            stat_date,
            date_display,
            consumption_kwh,
            production_kwh,
            grid_from_kwh,
            grid_to_kwh,
            entfeuchter_kwh,
            wasserpumpe_kwh,
            ladestation_kwh,
            updated_at,
            SOURCE_LABEL,
        ),
    )
    conn.commit()


def main():
    conn = libsql.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    ensure_schema(conn)

    days = last_n_complete_days_zurich(ROLLING_DAYS)
    day_dates_iso = [d[0].isoformat() for d in days]

    # --- 1) Gesamtsumme (Gateway), Tag fuer Tag ---
    gateway_stats_by_date = {}
    for day_date, from_iso, to_iso in days:
        print(f"Gateway-Statistik fuer {day_date} ...")
        stats = fetch_gateway_day_stats(from_iso, to_iso)
        upsert_gateway_day(conn, day_date.isoformat(), stats)
        gateway_stats_by_date[day_date.isoformat()] = stats
        print(f"  -> gespeichert: {json.dumps(stats)}")

    # --- 2) Pro Geraet, Tag fuer Tag (dieselben korrekten Zuerich-
    #        Tagesgrenzen wie beim Gateway-Total, ueber /v3/devices/.../data/range) ---
    device_wh_by_date_and_device = {}  # {stat_date: {device_id: wh}}
    for device_id in DEVICE_IDS:
        n_days_ok = 0
        for day_date, from_iso, to_iso in days:
            stat_date = day_date.isoformat()
            print(f"Geraete-Statistik fuer {device_id} ({stat_date}) ...")
            try:
                points = fetch_device_range(device_id, from_iso, to_iso, interval=900)
            except requests.HTTPError as e:
                print(f"  -> Fehler, ueberspringe: {e}", file=sys.stderr)
                continue
            wh = sum_device_day_wh(points)
            upsert_device_day(conn, stat_date, device_id, wh)
            device_wh_by_date_and_device.setdefault(stat_date, {})[device_id] = wh
            n_days_ok += 1
        print(f"  -> {n_days_ok}/{len(days)} Tage gespeichert")

    # --- 3) Zusammenfuehren in solarmanager_data ---
    bezeichnung_by_device = get_device_bezeichnung_map(conn)

    for stat_date in day_dates_iso:
        gateway_stats = gateway_stats_by_date.get(stat_date, {})
        device_wh_for_day = device_wh_by_date_and_device.get(stat_date, {})

        device_wh_by_column = {}
        for device_id, wh in device_wh_for_day.items():
            bezeichnung = bezeichnung_by_device.get(device_id)
            column = DEVICE_COLUMN_MAP.get(bezeichnung)
            if column:
                device_wh_by_column[column] = wh

        upsert_solarmanager_data(conn, stat_date, gateway_stats, device_wh_by_column)
        print(f"solarmanager_data aktualisiert fuer {stat_date}")

    print(f"Fertig: rollierende {ROLLING_DAYS}-Tage-Statistik aktualisiert.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTP-Fehler: {e} -- Antwort: {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)
