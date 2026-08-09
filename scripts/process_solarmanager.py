#!/usr/bin/env python3
"""
Munotstadt Solarmanager - Processing Script

Liest neue CSV-Exports aus uploads/, berechnet Tageswerte (kWh) auf Basis
der 15'-Rohdaten (nicht aus Tagessummen - siehe GridFrom/GridTo unten),
schreibt sie per UPSERT in die Turso-Tabelle solarmanager_data, verschiebt
verarbeitete Dateien nach processed/ und schreibt einen Log-Eintrag.
"""

import glob
import json
import os
import shutil
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import libsql_client as libsql

TZ_ZURICH = ZoneInfo("Europe/Zurich")

UPLOAD_DIR = "uploads"
PROCESSED_DIR = "processed"
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "run_log.txt")
DATA_DIR = "data"
JSON_EXPORT_PATH = os.path.join(DATA_DIR, "solarmanager_daily.json")

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

INTERVAL_HOURS = 0.25  # 15-Minuten-Raster


def now_zurich():
    """Aktuelle Zeit in Europe/Zurich - GitHub Actions Runner laufen in UTC,
    daher explizite Konvertierung fuer konsistente Zeitstempel."""
    return datetime.now(TZ_ZURICH)


def log(msg):
    ts = now_zurich().strftime("%d.%m.%Y %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def find_col(df, needle, exclude=None):
    """Findet die erste Spalte, deren Name 'needle' enthält (case-insensitive),
    optional unter Ausschluss von Spalten die 'exclude' enthalten."""
    for c in df.columns:
        if needle.lower() in c.lower():
            if exclude and exclude.lower() in c.lower():
                continue
            return c
    return None


def load_and_aggregate(csv_path):
    """Liest eine EKZ-Solarmanager-CSV und liefert ein DataFrame mit
    Tageswerten gemäss solarmanager_data-Schema."""
    df = pd.read_csv(csv_path)

    if "Date" not in df.columns:
        raise ValueError(f"{csv_path}: Spalte 'Date' fehlt - kein gültiger Solarmanager-Export?")

    # utc=True ist noetig, da EKZ-Exports ueber Sommer/Winterzeit-Wechsel
    # gemischte UTC-Offsets (+01:00 / +02:00) enthalten koennen - ohne utc=True
    # liefert pandas dann dtype=object statt datetime64 (.dt-Zugriff schlaegt fehl).
    # Anschliessend zurueck auf Europe/Zurich konvertieren, damit Tagesgrenzen
    # der lokalen (nicht der UTC-) Zeit entsprechen.
    df["Date"] = pd.to_datetime(df["Date"], utc=True).dt.tz_convert("Europe/Zurich")

    col_consumption = "Consumption"
    col_production = "Production"
    # Bevorzugt "Smart Plug"-Messwerte, da "Energy Measurement" in bisherigen
    # Exporten durchgehend 0 war (redundante/inaktive Sensoren).
    col_entfeuchter = find_col(df, "Entfeuchter", exclude="activeDevice") \
        or find_col(df, "Entfeuchter")
    col_wasserpumpe = find_col(df, "Wasserpumpe", exclude="activeDevice")
    col_ladestation = find_col(df, "Car Charging", exclude="activeDevice") \
        or find_col(df, "Ladestation", exclude="activeDevice")

    # Bei mehreren Treffern (Smart Plug + Energy Measurement) die "Smart Plug"
    # Variante bevorzugen, falls vorhanden.
    def prefer_smart_plug(df, needle):
        candidates = [c for c in df.columns if needle.lower() in c.lower()
                      and "activedevice" not in c.lower().replace(" ", "")]
        smart_plug = [c for c in candidates if "smart plug" in c.lower()]
        return smart_plug[0] if smart_plug else (candidates[0] if candidates else None)

    col_entfeuchter = prefer_smart_plug(df, "Entfeuchter")
    col_wasserpumpe = prefer_smart_plug(df, "Wasserpumpe")

    for name, col in [("Consumption", col_consumption), ("Production", col_production)]:
        if col is None or col not in df.columns:
            raise ValueError(f"{csv_path}: Pflichtspalte '{name}' nicht gefunden.")

    # kWh pro 15'-Intervall (Momentanleistung in W -> Energie)
    df["Consumption_kWh_iv"] = df[col_consumption].fillna(0) * INTERVAL_HOURS / 1000
    df["Production_kWh_iv"] = df[col_production].fillna(0) * INTERVAL_HOURS / 1000

    # WICHTIG: Netzbezug/Einspeisung müssen PRO INTERVALL berechnet und dann
    # summiert werden - nicht aus Tagessummen, sonst wird z.B. nächtlicher
    # Netzbezug durch tagsüberschüssige Produktion fälschlich verrechnet.
    df["GridFrom_kWh_iv"] = (df[col_consumption].fillna(0) - df[col_production].fillna(0)) \
        .clip(lower=0) * INTERVAL_HOURS / 1000
    df["GridTo_kWh_iv"] = (df[col_production].fillna(0) - df[col_consumption].fillna(0)) \
        .clip(lower=0) * INTERVAL_HOURS / 1000

    df["Entfeuchter_kWh_iv"] = (df[col_entfeuchter].fillna(0) if col_entfeuchter else 0) * INTERVAL_HOURS / 1000
    df["Wasserpumpe_kWh_iv"] = (df[col_wasserpumpe].fillna(0) if col_wasserpumpe else 0) * INTERVAL_HOURS / 1000
    df["Ladestation_kWh_iv"] = (df[col_ladestation].fillna(0) if col_ladestation else 0) * INTERVAL_HOURS / 1000

    df["Date_ISO"] = df["Date"].dt.strftime("%Y-%m-%d")
    df["Date_Display"] = df["Date"].dt.strftime("%d.%m.%Y")

    daily = df.groupby(["Date_ISO", "Date_Display"]).agg(
        Consumption_kWh=("Consumption_kWh_iv", "sum"),
        Production_kWh=("Production_kWh_iv", "sum"),
        GridFrom_kWh=("GridFrom_kWh_iv", "sum"),
        GridTo_kWh=("GridTo_kWh_iv", "sum"),
        Entfeuchter_Waschen_kWh=("Entfeuchter_kWh_iv", "sum"),
        Wasserpumpe_kWh=("Wasserpumpe_kWh_iv", "sum"),
        Ladestation_kWh=("Ladestation_kWh_iv", "sum"),
    ).reset_index()

    for c in ["Consumption_kWh", "Production_kWh", "GridFrom_kWh", "GridTo_kWh",
              "Entfeuchter_Waschen_kWh", "Wasserpumpe_kWh", "Ladestation_kWh"]:
        daily[c] = daily[c].round(3)

    return daily


class ResilientConnection:
    """Wrapper um libsql_client mit Retry/Reconnect - Turso/Hrana liefert
    gelegentlich transiente EOF- oder 'stream not found'-Fehler, die eine
    volle Neuverbindung brauchen (nicht nur einen Connect-Retry)."""

    def __init__(self, url, auth_token, max_retries=3, wait_seconds=20):
        self.url = url
        self.auth_token = auth_token
        self.max_retries = max_retries
        self.wait_seconds = wait_seconds
        self.client = None
        self._connect()

    def _connect(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        # HTTP-Transport statt WebSocket (Hrana/wss) verwenden: in GitHub Actions
        # (und generell hinter Proxies/Firewalls) ist der WS-Handshake anfaelliger
        # fuer generische "400 Invalid response status"-Fehler, die meist ein
        # Auth-/URL-Problem verschleiern. HTTP liefert klarere Fehlercodes
        # (z.B. 401 bei falschem Token) und ist robuster.
        http_url = self.url
        if http_url.startswith("libsql://"):
            http_url = "https://" + http_url[len("libsql://"):]
        elif http_url.startswith("wss://"):
            http_url = "https://" + http_url[len("wss://"):]
        elif http_url.startswith("ws://"):
            http_url = "http://" + http_url[len("ws://"):]
        self.client = libsql.create_client_sync(url=http_url, auth_token=self.auth_token)

    def execute(self, sql, args=None):
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self.client.execute(sql, args or [])
            except Exception as e:
                last_err = e
                log(f"DB-Fehler (Versuch {attempt}/{self.max_retries}): {e}")
                self._connect()
                if attempt < self.max_retries:
                    time.sleep(self.wait_seconds)
        raise last_err

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


UPSERT_SQL = """
INSERT INTO solarmanager_data
  (Date_ISO, Date_Display, Consumption_kWh, Production_kWh, GridFrom_kWh, GridTo_kWh,
   Entfeuchter_Waschen_kWh, Wasserpumpe_kWh, Ladestation_kWh, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(Date_ISO) DO UPDATE SET
  Date_Display = excluded.Date_Display,
  Consumption_kWh = excluded.Consumption_kWh,
  Production_kWh = excluded.Production_kWh,
  GridFrom_kWh = excluded.GridFrom_kWh,
  GridTo_kWh = excluded.GridTo_kWh,
  Entfeuchter_Waschen_kWh = excluded.Entfeuchter_Waschen_kWh,
  Wasserpumpe_kWh = excluded.Wasserpumpe_kWh,
  Ladestation_kWh = excluded.Ladestation_kWh,
  updated_at = excluded.updated_at;
"""


def upsert_daily(conn, daily_df):
    now_str = now_zurich().strftime("%d.%m.%Y %H:%M:%S")
    for _, row in daily_df.iterrows():
        conn.execute(UPSERT_SQL, [
            row["Date_ISO"], row["Date_Display"],
            float(row["Consumption_kWh"]), float(row["Production_kWh"]),
            float(row["GridFrom_kWh"]), float(row["GridTo_kWh"]),
            float(row["Entfeuchter_Waschen_kWh"]), float(row["Wasserpumpe_kWh"]),
            float(row["Ladestation_kWh"]), now_str,
        ])


def export_json(conn):
    """Exportiert die gesamte Tabelle als statisches JSON fuer das Dashboard
    (GitHub Pages liest diese Datei direkt - kein Live-DB-Zugriff aus dem
    Browser noetig, analog zum JSON-Snapshot-Muster der anderen Munotstadt-Projekte)."""
    result = conn.execute("SELECT * FROM solarmanager_data ORDER BY Date_ISO")
    rows = [row.asdict() for row in result.rows]
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "generated_at": now_zurich().strftime("%d.%m.%Y %H:%M:%S"),
        "row_count": len(rows),
        "data": rows,
    }
    with open(JSON_EXPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    log(f"JSON-Export geschrieben: {JSON_EXPORT_PATH} ({len(rows)} Zeilen).")


def main():
    if not TURSO_URL or not TURSO_TOKEN:
        log("FEHLER: TURSO_DATABASE_URL / TURSO_AUTH_TOKEN nicht gesetzt.")
        sys.exit(1)

    os.makedirs(PROCESSED_DIR, exist_ok=True)
    csv_files = sorted(glob.glob(os.path.join(UPLOAD_DIR, "*.csv")))

    conn = ResilientConnection(TURSO_URL, TURSO_TOKEN)

    try:
        conn.execute("SELECT 1")
    except Exception as e:
        log(f"FEHLER: Verbindungstest zu Turso fehlgeschlagen: {e}")
        log("Pruefe TURSO_DATABASE_URL (Format libsql://<db>-<org>.turso.io, ohne trailing slash) "
            "und TURSO_AUTH_TOKEN (Datenbank-Token, NICHT der Platform-API-Token von turso_admin).")
        sys.exit(1)

    if not csv_files:
        log("Keine neuen Uploads gefunden. Aktualisiere JSON-Export trotzdem (falls veraltet).")
        export_json(conn)
        conn.close()
        return

    total_days = 0

    for path in csv_files:
        try:
            log(f"Verarbeite {path} ...")
            daily = load_and_aggregate(path)
            upsert_daily(conn, daily)
            total_days += len(daily)
            log(f"  -> {len(daily)} Tag(e) geschrieben ({daily['Date_Display'].min()} - {daily['Date_Display'].max()})")

            dest = os.path.join(PROCESSED_DIR, os.path.basename(path))
            shutil.move(path, dest)
        except Exception as e:
            log(f"  FEHLER bei {path}: {e}")
            # Datei bleibt in uploads/, damit sie nicht stillschweigend verloren geht.
            continue

    export_json(conn)
    conn.close()
    log(f"Fertig. {len(csv_files)} Datei(en) verarbeitet, {total_days} Tageswerte upserted.")


if __name__ == "__main__":
    main()
