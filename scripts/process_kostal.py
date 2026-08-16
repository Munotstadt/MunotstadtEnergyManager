#!/usr/bin/env python3
"""
Munotstadt Kostal - Processing Script

Liest neue Kostal-Wechselrichter CSV-Exports aus uploads_kostal/ (Format:
Semikolon-getrennt, Dezimalkomma, eine Spalte "DateTime" mit z.B. "Feb. 01"
plus eine Spalte pro Jahr, z.B. "2024 [Wh]", "2025 [Wh]", "2026 [Wh]"),
wandelt sie in Tageswerte (Date_ISO -> Energie) um und schreibt sie per
UPSERT in die Turso-Tabelle kostal_data. Verarbeitete Dateien wandern nach
processed_kostal/, Log nach logs/run_log_kostal.txt.

Dedup-Strategie: Date_ISO ist PRIMARY KEY in kostal_data. Ein erneuter
Upload (gleiche oder überlappende Datei) überschreibt bestehende Tage
(ON CONFLICT ... DO UPDATE) statt einen zweiten Eintrag anzulegen - es
können also nie doppelte Zeilen für dasselbe Datum entstehen.
"""

import glob
import json
import os
import re
import shutil
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import libsql_client as libsql

TZ_ZURICH = ZoneInfo("Europe/Zurich")

UPLOAD_DIR = "uploads_kostal"
PROCESSED_DIR = "processed_kostal"
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "run_log_kostal.txt")

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

# Englische und deutsche Monatsabkürzungen (erste 3 Buchstaben, klein) ->
# Monatsnummer. Der Kostal-Export liefert bislang englische Abkürzungen
# ("Jan.", "Feb.", ... "May" ohne Punkt), die deutschen Varianten sind zur
# Sicherheit mit dabei, falls die Portal-Sprache mal umgestellt wird.
MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "maer": 3, "mär": 3, "apr": 4,
    "may": 5, "mai": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "oct": 10, "okt": 10, "nov": 11, "dec": 12, "dez": 12,
}

DATETIME_RE = re.compile(r"^([A-Za-zÀ-ÿ]+)\.?\s+(\d{1,2})$")
YEAR_COL_RE = re.compile(r"^(\d{4})\s*\[Wh\]$")


def now_zurich():
    return datetime.now(TZ_ZURICH)


def log(msg):
    ts = now_zurich().strftime("%d.%m.%Y %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse_datetime_cell(raw):
    """'Feb. 01' -> (month, day). Liefert None bei unbekanntem/ungültigem
    Format (z.B. kaputte Zeilen wie '28' ohne Monat am Dateiende)."""
    if not isinstance(raw, str):
        return None
    m = DATETIME_RE.match(raw.strip())
    if not m:
        return None
    month_key = m.group(1).strip(".").lower()[:3]
    month = MONTH_MAP.get(month_key)
    if month is None:
        return None
    day = int(m.group(2))
    return month, day


def load_kostal_csv(csv_path):
    """Liest eine Kostal-CSV (Semikolon, Dezimalkomma) und liefert eine Liste
    von dicts {Date_ISO, Date_Display, Energy_Wh}."""
    df = pd.read_csv(csv_path, sep=";", decimal=",", quotechar='"', encoding="utf-8-sig")
    df.columns = [str(c).strip() for c in df.columns]

    if "DateTime" not in df.columns:
        raise ValueError(f"{csv_path}: Spalte 'DateTime' fehlt - kein gültiger Kostal-Export?")

    year_cols = [c for c in df.columns if YEAR_COL_RE.match(c)]
    if not year_cols:
        raise ValueError(f"{csv_path}: keine Jahres-Spalten im Format 'YYYY [Wh]' gefunden.")

    rows = {}
    skipped_rows = 0
    skipped_cells = 0

    for _, r in df.iterrows():
        parsed = parse_datetime_cell(r["DateTime"])
        if parsed is None:
            skipped_rows += 1
            continue
        month, day = parsed

        for col in year_cols:
            value = r[col]
            if pd.isna(value):
                continue
            year = int(YEAR_COL_RE.match(col).group(1))
            try:
                d = date(year, month, day)
            except ValueError:
                # z.B. 29. Februar in einem Nicht-Schaltjahr - Zelle ignorieren.
                skipped_cells += 1
                continue

            date_iso = d.isoformat()
            rows[date_iso] = {
                "Date_ISO": date_iso,
                "Date_Display": d.strftime("%d.%m.%Y"),
                "Energy_Wh": round(float(value), 2),
            }

    if skipped_rows:
        log(f"  {os.path.basename(csv_path)}: {skipped_rows} Zeile(n) ohne gültiges DateTime-Format ignoriert.")
    if skipped_cells:
        log(f"  {os.path.basename(csv_path)}: {skipped_cells} Zelle(n) mit ungültigem Kalenderdatum ignoriert.")

    return list(rows.values())


class ResilientConnection:
    """Wrapper um libsql_client mit Retry/Reconnect - siehe process_solarmanager.py
    für die Begründung (transiente Hrana/EOF-Fehler nach Idle-Phasen)."""

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


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kostal_data (
  Date_ISO TEXT PRIMARY KEY,
  Date_Display TEXT,
  Energy_Wh REAL,
  Energy_kWh REAL,
  updated_at TEXT
);
"""

UPSERT_SQL = """
INSERT INTO kostal_data (Date_ISO, Date_Display, Energy_Wh, Energy_kWh, updated_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(Date_ISO) DO UPDATE SET
  Date_Display = excluded.Date_Display,
  Energy_Wh = excluded.Energy_Wh,
  Energy_kWh = excluded.Energy_kWh,
  updated_at = excluded.updated_at;
"""


def upsert_rows(conn, rows):
    now_str = now_zurich().strftime("%d.%m.%Y %H:%M:%S")
    for row in rows:
        conn.execute(UPSERT_SQL, [
            row["Date_ISO"], row["Date_Display"],
            row["Energy_Wh"], round(row["Energy_Wh"] / 1000, 3),
            now_str,
        ])


def main():
    if not TURSO_URL or not TURSO_TOKEN:
        log("FEHLER: TURSO_DATABASE_URL / TURSO_AUTH_TOKEN nicht gesetzt.")
        sys.exit(1)

    os.makedirs(PROCESSED_DIR, exist_ok=True)
    csv_files = sorted(glob.glob(os.path.join(UPLOAD_DIR, "*.csv")))

    conn = ResilientConnection(TURSO_URL, TURSO_TOKEN)

    try:
        conn.execute(CREATE_TABLE_SQL)
    except Exception as e:
        log(f"FEHLER: Verbindungstest/Tabellen-Setup zu Turso fehlgeschlagen: {e}")
        sys.exit(1)

    if not csv_files:
        log("Keine neuen Kostal-Uploads gefunden.")
        conn.close()
        return

    total_days = 0

    for path in csv_files:
        try:
            log(f"Verarbeite {path} ...")
            rows = load_kostal_csv(path)
            if not rows:
                log(f"  -> keine gültigen Tageswerte in {path}, Datei wird trotzdem verschoben.")
            else:
                upsert_rows(conn, rows)
                total_days += len(rows)
                dates = sorted(r["Date_ISO"] for r in rows)
                log(f"  -> {len(rows)} Tag(e) geschrieben ({dates[0]} - {dates[-1]})")

            dest = os.path.join(PROCESSED_DIR, os.path.basename(path))
            shutil.move(path, dest)
        except Exception as e:
            log(f"  FEHLER bei {path}: {e}")
            # Datei bleibt in uploads_kostal/, damit sie nicht stillschweigend verloren geht.
            continue

    conn.close()
    log(f"Fertig. {len(csv_files)} Datei(en) verarbeitet, {total_days} Tageswerte upserted.")


if __name__ == "__main__":
    main()
