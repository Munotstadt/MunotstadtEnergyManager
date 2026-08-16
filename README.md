# Munotstadt_EnergyManager

Web-Upload für Energie-CSV-Exports → automatische Verarbeitung → Speicherung
in Turso (`munotstadtenergydb`). Zwei unabhängige Quellen/Pipelines:

- **Solarmanager (EKZ)** → Tabelle `solarmanager_data` (15'-Rohdaten, zu
  Tageswerten aggregiert).
- **Kostal (Wechselrichter-Ertrag)** → Tabelle `kostal_data` (Tagesertrag,
  direkt aus dem Kostal-Portal-Export).

## Ablauf

1. `solarmanageruploader.html` (GitHub Pages) — Upload-Interface mit zwei
   Bereichen (Solarmanager / Kostal), committed die rohe CSV via GitHub
   Contents API (Client-seitig, mit persönlichem Access Token) nach
   `uploads/` bzw. `uploads_kostal/`.
2. Push nach `uploads/**.csv` triggert `.github/workflows/process-upload.yml`,
   Push nach `uploads_kostal/**.csv` triggert
   `.github/workflows/process-kostal-upload.yml`.
3. `scripts/process_solarmanager.py` berechnet Tageswerte **auf 15'-Ebene**
   (Netzbezug/Einspeisung pro Intervall, dann summiert — nicht aus
   Tagessummen, da sonst z.B. nächtlicher Netzbezug falsch verrechnet wird)
   und schreibt sie per UPSERT in Turso.
   `scripts/process_kostal.py` liest die Kostal-Export-Tabelle (eine Spalte
   pro Jahr) direkt in Tageswerte um und schreibt sie ebenfalls per UPSERT.
4. Verarbeitete Dateien wandern nach `processed/` bzw. `processed_kostal/`,
   Log nach `logs/run_log.txt` bzw. `logs/run_log_kostal.txt`.

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
- `kostal_data` muss nicht manuell angelegt werden: `scripts/process_kostal.py`
  führt bei jedem Lauf `CREATE TABLE IF NOT EXISTS kostal_data (...)` aus.

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

`https://munotstadt.github.io/Munotstadt_EnergyManager/` (Dashboard, Startseite) bzw. `.../solarmanageruploader.html` (Upload) öffnen, Token
eintragen, im gewünschten Bereich (Solarmanager oder Kostal) die passende
CSV-Datei auswählen und hochladen. Verarbeitung läuft automatisch
(ca. 1–2 Minuten), Status unter dem Actions-Tab des Repos.

### Kostal-CSV-Format
Export aus dem Kostal-Portal (Tagesertrag, Jahresvergleich), Semikolon-
getrennt mit Dezimalkomma:

```
"DateTime";"2024 [Wh]";"2025 [Wh]";"2026 [Wh]"
"Feb. 01";1352,3909;1193,5109;5428,2896
...
```

Pro Datei i.d.R. ein Monat (Zeilen = Tage) mit einer Spalte pro Jahr. Zeilen
ohne gültiges `<Monat>. <Tag>`-Format (z.B. abgeschnittene/kaputte
Fusszeilen) und leere Zellen werden beim Import ignoriert und geloggt
(`logs/run_log_kostal.txt`).

## Dashboards

`index.html` (Startseite, GitHub Pages) visualisiert
`data/solarmanager_daily.json`:

- KPI-Kacheln: Verbrauch/Produktion letzte 7 Tage, Autarkiegrad (30 Tage), Total
- Tagesverlauf Verbrauch/Produktion mit Zeitraum-Umschaltung (30/90/365 Tage/Alles)
- Netzbezug/Einspeisung im gleichen Zeitraum
- Monatsvergleich (Saisonalität PV-Produktion sichtbar)
- Autarkiegrad pro Monat
- Wochentag-Profil (Ø kWh pro Wochentag über gesamten Zeitraum)
- Gerätenutzung pro Monat (Entfeuchter, Wasserpumpe, Ladestation, gestapelt)
- Tabelle letzte 14 Tage

Das Dashboard liest ausschliesslich das statische JSON — kein Turso-Zugriff
und kein Token im Browser nötig. `data/solarmanager_daily.json` wird bei
jedem Workflow-Lauf frisch aus der DB exportiert (auch wenn keine neue CSV
vorliegt, z.B. bei manuellem "Run workflow").



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

### Tabelle `kostal_data`

| Spalte | Typ | Beschreibung |
|---|---|---|
| Date_ISO | TEXT PK | YYYY-MM-DD |
| Date_Display | TEXT | DD.MM.YYYY |
| Energy_Wh | REAL | Tagesertrag laut Kostal-Export (Wh) |
| Energy_kWh | REAL | Tagesertrag umgerechnet (kWh) |
| updated_at | TEXT | DD.MM.YYYY HH:MM:SS |

Wie bei `solarmanager_data` ist `Date_ISO` PRIMARY KEY: ein Import
überschreibt (UPSERT) bestehende Tage statt sie zu duplizieren. Dasselbe
Datum kann also beliebig oft neu hochgeladen werden (z.B. bei überlappenden
Monats-Exporten), ohne dass Duplikate entstehen.
