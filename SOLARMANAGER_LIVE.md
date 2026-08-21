# Solar Manager Live-Pipeline (Cloud API → Turso)

Ergänzung zur bestehenden `MunotstadtEnergyManager`-Pipeline. Läuft
**unabhängig und parallel** zur bestehenden täglichen
Solarmanager-CSV-Verarbeitung (`process_solarmanager.py` →
`solarmanager_data`-Tabelle bzw. `data/solarmanager_daily.json`) –
diese bleibt vollständig unverändert.

Diese Pipeline holt stattdessen alle 10 Minuten per GitHub Actions
den **Live-Datenpunkt** direkt von der Solar Manager Cloud API
(`GET /v1/stream/gateway/{gateway_id}`, Basic Auth mit
`base64(email:api-key)`, getestet und bestätigt funktionierend) und
schreibt ihn in zwei neue, separate Tabellen:

- `solarmanager_live_points` – ein Datensatz pro Abfrage
  (Zeitstempel, aktuelle PV-Produktion/-Verbrauch/-Netzleistung/
  -Batterieleistung, Rohdaten als JSON)
- `solarmanager_live_devices` – ein Datensatz pro Gerät pro Abfrage

Diese Tabellennamen wurden bewusst von der bestehenden
`solarmanager_data`-Tabelle abgegrenzt, um keine Kollision mit der
produktiven Tages-Aggregations-Pipeline zu riskieren.

## Neue Dateien

- `scripts/fetch_solarmanager_live.py`
- `scripts/fetch_solarmanager_daily_stats.py`
- `scripts/requirements-solarmanager-live.txt`
- `.github/workflows/fetch-solarmanager-live.yml`
- `.github/workflows/fetch-solarmanager-daily-stats.yml`
- `.env.solarmanager-live.example`

## Setup

### 1. Cloud API Key
In den Solar-Manager-Profileinstellungen einen Cloud-API-Key
erstellen (bereits erledigt, verifiziert per PowerShell-curl-Test).

### 2. Turso
Nutzt dieselbe Datenbank wie die bestehende Pipeline:
`libsql://munotstadtenergydb-munotstadt.aws-eu-west-1.turso.io`

Falls noch kein passender Token vorhanden ist:
```bash
turso db tokens create munotstadtenergydb
```

### 3. GitHub Secrets setzen
Unter **Settings → Secrets and variables → Actions → Secrets**:

- `SM_EMAIL`
- `SM_API_KEY`
- `SM_GATEWAY_ID` (`00000000F6524E6C`)
- `TURSO_DATABASE_URL`
- `TURSO_AUTH_TOKEN`

(`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` evtl. bereits als Secrets
vorhanden, falls die bestehende Pipeline schon welche nutzt –
dann einfach denselben Wert verwenden bzw. wiederverwenden.)

**Variables** (optional):
- `SM_BASE_URL`
- `SM_POINT_PATH`

### 4. Workflow
`.github/workflows/fetch-solarmanager-live.yml` läuft automatisch
alle 10 Minuten, zusätzlich manuell über "Run workflow" startbar.

## Dashboard-Anbindung

Noch nicht Teil dieser Ergänzung. `index.html` zeigt die
Live-Tabellen aktuell nicht an – analog zu `kostal_data` könnte das
bei Bedarf als eigene Kachel/Chart ergänzt werden, sobald genug
Live-Datenpunkte gesammelt sind.

## Rollierende 7-Tage-Statistik (einmal täglich morgens)

`.github/workflows/fetch-solarmanager-daily-stats.yml` läuft täglich
um 5:30 UTC und aktualisiert **immer die letzten 7 abgeschlossenen
Kalendertage** (rollierendes Fenster) — sowohl als Gesamtsumme als
auch pro Gerät. Jeder Lauf überschreibt bestehende Tage per UPSERT
(kein Duplikat), sodass z.B. ein zuvor nur teilweise erfasster Tag
beim nächsten Lauf mit dem finalen Wert aktualisiert wird.

**`solarmanager_daily_stats`** (Gesamtsumme, ein Datensatz pro Tag,
`stat_date` eindeutig):
- Verbrauch, Produktion, Eigenverbrauch (Wh)
- Eigenverbrauchsquote, Autarkiegrad (%)

Quelle: `GET /v1/statistics/gateways/{smId}?accuracy=day&from=...&to=...`
(ein Aufruf pro Tag, 7 Aufrufe/Lauf)

**`solarmanager_daily_stats_by_device`** (pro Gerät, `stat_date` +
`device_id` eindeutig):
- Verbrauch (Wh) je Gerät je Tag

Quelle: `GET /v1/consumption/sensor/{sensorId}?period=week` (ein
Aufruf pro Gerät liefert die letzten ~7 Tage auf einmal, 8
Aufrufe/Lauf). Nicht alle Geräte liefern Daten über diesen Endpunkt
— reine Steuerungs-/Anzeigegeräte (Schalter, Batterie-Controller
ohne aktive Ladung) liefern `data: []`, was das Skript automatisch
überspringt.

Manuell testen:
```bash
python scripts/fetch_solarmanager_daily_stats.py
```

## Tageswerte berechnen

Für Zeiträume, die die tägliche Statistik-Pipeline noch nicht erfasst
hat (oder für flexiblere Aggregationen), lässt sich alternativ auch
aus den Live-Momentanwerten aggregieren, z.B.:

```sql
SELECT date(fetched_at) AS tag,
       AVG(current_pv_generation) AS durchschnitt_watt
FROM solarmanager_live_points
WHERE date(fetched_at) = date('now', '-1 day')
GROUP BY tag;
```
