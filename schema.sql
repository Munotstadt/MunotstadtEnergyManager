-- Turso DB: munotstadtenergydb
-- Tabelle für aggregierte Tageswerte aus dem EKZ Solarmanager Export

CREATE TABLE IF NOT EXISTS solarmanager_data (
  Date_ISO                TEXT PRIMARY KEY,   -- YYYY-MM-DD (Sortier-/Join-Key)
  Date_Display             TEXT NOT NULL,      -- DD.MM.YYYY (Anzeige, Munotstadt-Konvention)
  Consumption_kWh           REAL NOT NULL DEFAULT 0,
  Production_kWh            REAL NOT NULL DEFAULT 0,
  GridFrom_kWh               REAL NOT NULL DEFAULT 0,   -- Netzbezug, auf 15'-Ebene berechnet
  GridTo_kWh                 REAL NOT NULL DEFAULT 0,   -- Einspeisung, auf 15'-Ebene berechnet
  Entfeuchter_Waschen_kWh     REAL NOT NULL DEFAULT 0,
  Wasserpumpe_kWh             REAL NOT NULL DEFAULT 0,
  Ladestation_kWh             REAL NOT NULL DEFAULT 0,
  updated_at                  TEXT NOT NULL       -- DD.MM.YYYY HH:MM:SS
);
