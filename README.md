# Munotstadt_EnergyManager

Web-Upload für EKZ Solarmanager CSV-Exports → automatische Aggregation zu
Tageswerten (kWh) → Speicherung in Turso (`munotstadtenergydb`, Tabelle
`solarmanager_data`).

## Ablauf

1. `index.html` (GitHub Pages) — Upload-Interface, committed die rohe CSV
   nach `uploads/` via GitHub Contents API (Client-seitig, mit persönlichem
   Access Token).
2. Push nach `uploads/**.csv` triggert `.github/workflows/process-upload.yml`.
3. `scripts/process_solarmanager.py` berechnet Tageswerte **auf 15'-Ebene**
   (Netzbezug/Einspeisung pro Intervall, dann summiert — nicht aus
   Tagessummen, da sonst z.B. nächtlicher Netzbezug falsch verrechnet wird)
   und schreibt sie per UPSERT in Turso.
4. Verarbeitete Dateien wandern nach `processed/`, Log nach `logs/run_log.txt`.

## Setup

### 1. Repo erstellen
- Neues **public** Repo `Munotstadt_EnergyManager` unter der Organisation `Munotstadt`.
- Alle Dateien aus diesem Paket hochladen (Struktur beachten: `.github/workflows/...`).
- Settings → Pages → Source: `main` Branch, `/ (root)`.

### 2. Turso-Datenbank
- Neue Datenbank `munotstadtenergydb` anlegen (Turso Dashboard oder Platform API,
  analog zu `munotstadtmeteodb`).
- Schema anlegen: Inhalt von `schema.sql` ausführen (z.B. über den bestehenden
  `turso_admin` Ad-hoc-SQL-Workflow, einfach auf die neue DB zeigen lassen,
  oder direkt via Turso CLI/Dashboard SQL-Editor).

### 3. GitHub Secrets (im neuen Repo)
Settings → Secrets and variables → Actions:
- `TURSO_DATABASE_URL` — URL von `munotstadtenergydb`
- `TURSO_AUTH_TOKEN` — Auth-Token mit Schreibrechten auf `munotstadtenergydb`

### 4. Personal Access Token für den Upload
- Ein fine-grained GitHub PAT erstellen mit **Contents: Read & Write** auf
  `Munotstadt/Munotstadt_EnergyManager`.
- Dieses Token wird auf der Upload-Seite selbst eingegeben (optional lokal
  im Browser gespeichert) — es liegt nie im Repo.

## Nutzung

`https://munotstadt.github.io/Munotstadt_EnergyManager/` öffnen, Token
eintragen, CSV-Export vom Solarmanager auswählen, hochladen. Verarbeitung
läuft automatisch (ca. 1–2 Minuten), Status unter dem Actions-Tab des Repos.

## Tabelle `solarmanager_data`

| Spalte | Typ | Beschreibung |
|---|---|---|
| Date_ISO | TEXT PK | YYYY-MM-DD |
| Date_Display | TEXT | DD.MM.YYYY |
| Consumption_kWh | REAL | Hausverbrauch |
| Production_kWh | REAL | PV-Produktion |
| GridFrom_kWh | REAL | Netzbezug (15'-Basis) |
| GridTo_kWh | REAL | Einspeisung (15'-Basis) |
| Entfeuchter_Waschen_kWh | REAL | Smart Plug Entfeuchter |
| Wasserpumpe_kWh | REAL | Smart Plug Wasserpumpe |
| Ladestation_kWh | REAL | Car Charging Ladestation |
| updated_at | TEXT | DD.MM.YYYY HH:MM:SS |

Erneuter Upload eines Tages überschreibt den bestehenden Eintrag (UPSERT auf
`Date_ISO`) — mehrfaches Hochladen ist also unkritisch.
