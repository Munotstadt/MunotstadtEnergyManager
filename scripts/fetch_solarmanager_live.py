"""
Solar Manager CLOUD API -> Turso

Fragt den aktuellen Datenpunkt von der Solar Manager Cloud API
(https://cloud.solar-manager.ch/) ab und speichert ihn in einer
Turso-Datenbank (libSQL).

Auth: Basic Auth mit base64("email:api-key"), wie von Solar Manager
vorgesehen (Variante 4 aus deren Doku). Bestätigt funktionierend
gegen den Endpunkt /v1/stream/gateway/{gateway_id}.

Benoetigte Umgebungsvariablen (siehe .env.example / GitHub Secrets):
    SM_EMAIL          -> Solar Manager Login-E-Mail
    SM_API_KEY        -> Solar Manager Cloud API Key
    SM_GATEWAY_ID       -> Solar Manager Geraete-/Gateway-ID (SM-ID)
    SM_BASE_URL        -> Standard: https://cloud.solar-manager.ch
    SM_POINT_PATH       -> Standard: /v1/stream/gateway/{gateway_id}
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
SM_POINT_PATH = _env_or_default(
    "SM_POINT_PATH", "/v1/stream/gateway/{gateway_id}"
).format(gateway_id=SM_GATEWAY_ID)

TURSO_DATABASE_URL = os.environ["TURSO_DATABASE_URL"]
TURSO_AUTH_TOKEN = os.environ["TURSO_AUTH_TOKEN"]


def basic_auth_header() -> str:
    raw = f"{SM_EMAIL}:{SM_API_KEY}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def fetch_point() -> dict:
    resp = requests.get(
        f"{SM_BASE_URL}{SM_POINT_PATH}",
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
        CREATE TABLE IF NOT EXISTS solarmanager_live_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            timestamp TEXT,
            interface_version INTEGER,
            interval_secs INTEGER,
            current_battery_charge_discharge REAL,
            current_grid_power REAL,
            current_power_consumption REAL,
            current_pv_generation REAL,
            raw_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS solarmanager_live_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            device_id TEXT,
            signal TEXT,
            current_power REAL,
            soc INTEGER,
            e_wh REAL,
            i_wh REAL,
            raw_json TEXT
        )
        """
    )
    conn.commit()


def store_point(conn, point: dict):
    now = dt.datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT INTO solarmanager_live_points
        (fetched_at, timestamp, interface_version, interval_secs,
         current_battery_charge_discharge, current_grid_power,
         current_power_consumption, current_pv_generation, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            point.get("TimeStamp"),
            point.get("Interface Version"),
            point.get("intervalSecs"),
            point.get("currentBatteryChargeDischarge"),
            point.get("currentGridPower"),
            point.get("currentPowerConsumption"),
            point.get("currentPvGeneration"),
            json.dumps(point),
        ),
    )

    devices = point.get("devices") or []
    if isinstance(devices, list):
        for device in devices:
            conn.execute(
                """
                INSERT INTO solarmanager_live_devices
                (fetched_at, device_id, signal, current_power, soc, e_wh, i_wh, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    device.get("_id"),
                    device.get("signal"),
                    device.get("currentPower") or device.get("currentPowerInvSm"),
                    device.get("soc"),
                    device.get("eWh"),
                    device.get("iWh"),
                    json.dumps(device),
                ),
            )
    conn.commit()


def main():
    point = fetch_point()
    print("Datenpunkt erhalten:", json.dumps(point)[:300], "...")

    conn = libsql.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    ensure_schema(conn)
    store_point(conn, point)
    print("Datenpunkt in Turso gespeichert.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTP-Fehler: {e} -- Antwort: {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)
