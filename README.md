# Munotstadt_EnergyManager

Web-Upload für EKZ Solarmanager CSV-Exports → automatische Aggregation zu
Tageswerten (kWh) → Speicherung in Turso (`munotstadtenergydb`, Tabelle
`solarmanager_data`).

## Ablauf

1. `solarmanageruploader.html` (GitHub Pages) — Upload-Interface, committed die rohe CSV
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

### 5. Read-only Token fürs Dashboard
- Lese-Token erzeugen: `turso db tokens create munotstadtenergydb --read-only --expiration never`
  (oder via Turso Dashboard, Scope "read-only").
- Diesen Token in `index.html` bei der Konstante `TURSO_READONLY_TOKEN`
  eintragen und committen. Er landet damit sichtbar im öffentlichen Repo und
  im Browser-Quelltext — das ist bei einem reinen Lese-Token unkritisch,
  **niemals** den Schreib-Token aus Schritt 3 hier eintragen.

## Nutzung

`https://munotstadt.github.io/Munotstadt_EnergyManager/` (Dashboard, Startseite) bzw. `.../solarmanageruploader.html` (Upload) öffnen, Token
eintragen, CSV-Export vom Solarmanager auswählen, hochladen. Verarbeitung
läuft automatisch (ca. 1–2 Minuten), Status unter dem Actions-Tab des Repos.

## Dashboards

`index.html` (Startseite, GitHub Pages) visualisiert die Daten aus
`solarmanager_data`:

- KPI-Kacheln: Verbrauch/Produktion letzte 7 Tage, Autarkiegrad (30 Tage), Total
- Tagesverlauf Verbrauch/Produktion mit Zeitraum-Umschaltung (30/90/365 Tage/Alles)
- Netzbezug/Einspeisung im gleichen Zeitraum
- Monatsvergleich (Saisonalität PV-Produktion sichtbar)
- Autarkiegrad pro Monat
- Wochentag-Profil (Ø kWh pro Wochentag über gesamten Zeitraum)
- Gerätenutzung pro Monat (Entfeuchter, Wasserpumpe, Ladestation, gestapelt)
- Tabelle letzte 14 Tage

### Datenquelle: live von Turso, mit lokalem Cache

Das Dashboard fragt `solarmanager_data` direkt per HTTP über einen
**Lese-Token** aus Turso ab (`TURSO_HTTP_URL` / `TURSO_READONLY_TOKEN` in
`index.html`). Der Token steht bewusst im Client-Code — GitHub Pages ist rein
statisch, es gibt keinen Server, der ihn verstecken könnte. Deshalb **darf
dieser Token ausschliesslich Leserechte haben**:

```bash
turso db tokens create munotstadtenergydb --read-only --expiration never
```

(Der Schreib-Token aus den GitHub Secrets bleibt davon unberührt und landet
nie im Browser.)

Um Ladezeit und DB-Last zu reduzieren, cached das Dashboard die Antwort in
`localStorage` (`munotstadt_solarmanager_cache_v1`, 15 Minuten TTL,
stale-while-revalidate): Beim Öffnen werden zuerst die zuletzt bekannten
Daten sofort angezeigt, im Hintergrund wird bei Ablauf der TTL neu von Turso
geladen und der Cache aktualisiert. Der Footer zeigt an, ob die Daten "live
von Turso", "aus Cache" oder als "statischer Export" (Fallback) stammen.

Falls Turso nicht erreichbar ist (Netzwerk/CORS/Token-Problem), fällt das
Dashboard auf `data/solarmanager_daily.json` zurück. Dieses JSON wird
weiterhin bei jedem Workflow-Lauf frisch aus der DB exportiert (auch ohne
neue CSV, z.B. bei manuellem "Run workflow") und dient so als Offline-/
Notfall-Kopie der Daten.



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
