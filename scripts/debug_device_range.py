"""
EINMALIGER DEBUG-Test: /v3/devices/{deviceId}/data/range

Fragt einen Schweizer Kalendertag (Europe/Zurich, DST-aware) fuer ein
einzelnes Geraet ab und schreibt sowohl die komplette Rohantwort als
auch (falls erkennbar) jeden einzelnen Datenpunkt in eine TEMPORAERE
Turso-Tabelle `debug_device_range_test` -- zum Anschauen per SQL, ohne
a-Shell/curl auf dem iPad noetig.

Nicht Teil der produktiven Pipeline. Kann nach der Auswertung mit
  DROP TABLE debug_device_range_test;
wieder entfernt werden.

Benoetigte Umgebungsvariablen (identisch zu den anderen Skripten):
    SM_EMAIL, SM_API_KEY, SM_BASE_URL (optional)
    TURSO_DATABASE_URL, TURSO_AUTH_TOKEN

Zusaetzlich (per workflow_dispatch Input):
    DEBUG_DEVICE_ID   -> welches Geraet getestet wird
    DEBUG_DATE        -> Schweizer Kalendertag im Format YYYY-MM-DD
    DEBUG_INTERVAL    -> 10, 300 oder 900 (Sekunden)
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

TURSO_DATABASE_URL = os.environ["TURSO_DATABASE_URL"]
TURSO_AUTH_TOKEN = os.environ["TURSO_AUTH_TOKEN"]

DEVICE_ID = os.environ["DEBUG_DEVICE_ID"]
DEBUG_DATE = _env_or_default("DEBUG_DATE", "")
INTERVAL = int(_env_or_default("DEBUG_INTERVAL", "900"))


def basic_auth_header() -> str:
    raw = f"{SM_EMAIL}:{SM_API_KEY}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def zurich_day_bounds_utc(date_str: str):
    """Schweizer Kalendertag (00:00-24:00 Lokalzeit, DST-aware) ->
    UTC-Zeitstempel im API-Format. Ohne date_str: gestriger Tag."""
    if date_str:
        local_date = dt.date.fromisoformat(date_str)
    else:
        local_date = (dt.datetime.now(ZURICH_TZ) - dt.timedelta(days=1)).date()

    start_local = dt.datetime.combine(local_date, dt.time(0, 0), tzinfo=ZURICH_TZ)
    end_local = start_local + dt.timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return (
        local_date,
        start_local.astimezone(dt.timezone.utc).strftime(fmt),
        end_local.astimezone(dt.timezone.utc).strftime(fmt),
    )


def fetch_device_range(device_id: str, from_iso: str, to_iso: str, interval: int):
    url = f"{SM_BASE_URL}/v3/devices/{device_id}/data/range"
    resp = requests.get(
        url,
        params={"from": from_iso, "to": to_iso, "interval": interval},
        headers={"accept": "application/json", "authorization": basic_auth_header()},
        timeout=30,
    )
    print(f"GET {resp.url}")
    print(f"Status: {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def ensure_debug_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS debug_device_range_test (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            device_id TEXT NOT NULL,
            local_date TEXT NOT NULL,
            from_iso TEXT NOT NULL,
            to_iso TEXT NOT NULL,
            interval_s INTEGER NOT NULL,
            point_index INTEGER,
            point_json TEXT,
            full_response_json TEXT
        )
        """
    )
    conn.commit()


def extract_points(payload):
    """Versucht, eine Liste von Einzelpunkten aus der Antwort zu
    extrahieren -- Format ist noch unbekannt, daher defensiv."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "values", "points", "result", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return None


def main():
    local_date, from_iso, to_iso = zurich_day_bounds_utc(DEBUG_DATE)
    print(f"Teste Geraet {DEVICE_ID} fuer {local_date} (Schweizer Zeit)")
    print(f"  from (UTC): {from_iso}")
    print(f"  to   (UTC): {to_iso}")
    print(f"  interval:   {INTERVAL}s")

    payload = fetch_device_range(DEVICE_ID, from_iso, to_iso, INTERVAL)
    print("\n--- Rohantwort (erste 2000 Zeichen) ---")
    raw_str = json.dumps(payload, indent=2)
    print(raw_str[:2000])

    points = extract_points(payload)
    if points is not None:
        print(f"\n{len(points)} Datenpunkte erkannt.")
        if points:
            print("Erster Punkt:", json.dumps(points[0]))
            print("Letzter Punkt:", json.dumps(points[-1]))
    else:
        print("\nKonnte keine Punkte-Liste automatisch erkennen -- volle Antwort in Tabelle gespeichert, bitte manuell in full_response_json anschauen.")

    conn = libsql.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    ensure_debug_schema(conn)
    now = dt.datetime.utcnow().isoformat()

    # Gesamte Rohantwort immer als Referenzzeile speichern (point_index = NULL)
    conn.execute(
        """
        INSERT INTO debug_device_range_test
        (fetched_at, device_id, local_date, from_iso, to_iso, interval_s, point_index, point_json, full_response_json)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        """,
        (now, DEVICE_ID, local_date.isoformat(), from_iso, to_iso, INTERVAL, raw_str),
    )

    # Falls erkennbar: zusaetzlich jeden Punkt einzeln speichern (leichter per SQL durchsuchbar)
    if points:
        for idx, point in enumerate(points):
            conn.execute(
                """
                INSERT INTO debug_device_range_test
                (fetched_at, device_id, local_date, from_iso, to_iso, interval_s, point_index, point_json, full_response_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (now, DEVICE_ID, local_date.isoformat(), from_iso, to_iso, INTERVAL, idx, json.dumps(point)),
            )
    conn.commit()
    print(f"\nGespeichert in debug_device_range_test ({1 + len(points or [])} Zeilen).")
    print("Zum Anschauen z.B.:")
    print("  SELECT point_index, point_json FROM debug_device_range_test WHERE point_index IS NOT NULL ORDER BY point_index;")
    print("Zum Aufraeumen danach:")
    print("  DROP TABLE debug_device_range_test;")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTP-Fehler: {e} -- Antwort: {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)
