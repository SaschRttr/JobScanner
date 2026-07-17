# Job-Scanner – Dokumentation

## Übersicht

Der Job-Scanner ist eine automatisierte Pipeline die Stellenanzeigen von konfigurierten Firmen-Websites abruft, per KI bewertet und als HTML-Report darstellt. Optional können angepasste Bewerbungsunterlagen (Lebenslauf, Anschreiben) generiert werden.

Das System läuft auf einem Raspberry Pi und ist über den Browser erreichbar.

---

## Architektur

```
scanner.py → rohtext_holen.py → vergaben_check.py → extraktor.py → bewertung.py → report.py
```

(`anpasser.py` läuft separat, nur on-demand über die WebUI.) Jedes Script ist ein
eigenständiger Schritt. Sie können einzeln oder als komplette Pipeline über die WebUI
gestartet werden (siehe `PIPELINE`-Liste in `webui.py`).

---

## Dateien & Verzeichnisse

| Datei / Ordner | Beschreibung |
|---|---|
| `config.txt` | Öffentliche Konfiguration (Firmen, Suchbegriffe, Ausschlussbegriffe) – **in Git**, keine Secrets |
| `config_secrets.txt` | Persönliche/geheime Konfiguration (API-Keys, Zugangsdaten, Heimadresse, Prompts) – **nicht in Git** |
| `scanner.py` | Schritt 1: Stellenanzeigen finden (Playwright/API) |
| `rohtext_holen.py` | Schritt 1b: Vollständigen Seitentext für einzelne Stellen laden |
| `vergaben_check.py` | Schritt 1c: Erreichbarkeit bekannter Stellen per HTTP prüfen |
| `extraktor.py` | Schritt 2: Stellentext aus Rohtext extrahieren |
| `bewertung.py` | Schritt 3: KI-Bewertung der Stelle |
| `report.py` | Schritt 4: HTML-Report erstellen, optional Mail senden |
| `anpasser.py` | Schritt 5: Lebenslauf an Stelle anpassen (nur bei Profil-Score ≥ 70), on-demand |
| `docx_patcher.py` | Erzeugt Lebenslauf/Anschreiben `.docx` mit Tracked Changes aus der `.txt`-Vorlage |
| `webui.py` | Flask-Webserver (Port 5000), steuert die Pipeline |
| `dashboard.py` | Streamlit-Dashboard zur Pipeline-Visualisierung (separater Service) |
| `db.py` | SQLite-Datenbankmodul |
| `status_def.py` | Zentrale Status-Definitionen (Labels, Farben, Emojis) |
| `browser.py` | Gemeinsame Playwright-Browser-Konstanten für Scanner/Rohtext/Vergaben-Check |
| `email_status.py` | E-Mail-basierte Bewerbungsstatus-Erkennung (Absender-Domain-Abgleich) |
| `uebersetzer.py` | Einmalige KI-Übersetzung der Lebenslauf-Vorlage ins Englische |
| `jobscanner.db` | SQLite-Datenbank (einzige Quelle der Wahrheit) |
| `stellen.json` | Export aus der DB – alle gefundenen Stellen |
| `bekannte_stellen.json` | Export aus der DB – Status pro URL (welche Pipeline-Stufe erreicht) |
| `strukturen.json` | Gelernte Seitenstrukturen für schnellere Extraktion |
| `whitelist_standorte.txt` | Erlaubte Standorte (Whitelist), ein Ort pro Zeile |
| `lebenslauf_vorlage.txt` | Lebenslauf-Vorlage mit Markern für den Anpasser |
| `lebenslauf_vorlage_en.txt` | Englische Übersetzung (generiert via `uebersetzer.py`) |
| `lebenslauf.txt` | Aktueller Lebenslauf (Basis für Bewertungs-Prompt) |
| `bewerbungen/` | Generierte Bewerbungsunterlagen pro Stelle |
| `scan.log` | Log-Ausgabe der aktuellen Pipeline |
| `report.html` | Generierter HTML-Report |
| `jobscanner.service` | systemd-Service für automatischen Start |

---

## Konfiguration (`config.txt` + `config_secrets.txt`)

Die Konfiguration ist auf zwei Dateien aufgeteilt, beide mit Abschnitten in
`[name]` / `[\name]` Markern. `lade_config()` (in `utils.py`) liest zuerst
`config.txt`, dann `config_secrets.txt` und führt beide zu einem Config-Dict
zusammen.

- **`config.txt`** – öffentlicher Teil ohne Secrets (Suchbegriffe, Ausschlussbegriffe,
  Standort-Filter, Firmenliste, API-Firmen, Firmendomains, Anschreiben-Adressen).
  Liegt in Git und kann z.B. über GitHub synchronisiert werden.
- **`config_secrets.txt`** – persönlicher/geheimer Teil (API-Keys, E-Mail-Zugangsdaten,
  Heimadresse, Raspi-IP, alle KI-Prompts). **Nicht in Git**, muss lokal angelegt werden.

### Globale Einstellungen (`config.txt`)

```
LLM_BEWERTUNG = true         # KI-Bewertung ein/aus
EMAIL_AKTIV = true           # E-Mail bei Änderungen senden
```

### Globale Einstellungen (`config_secrets.txt`)

```
API_KEY = sk-ant-...         # Anthropic API-Key
GOOGLE_MAPS_KEY = ...         # Google Maps Distance Matrix API-Key
FAHRZEIT_STARTPUNKT = ...     # Heimadresse für Fahrzeit-Berechnung
RASPI_IP = 192.168.x.x       # IP des Raspberry Pi (für Download-Links)
EMAIL_ABSENDER = ...
EMAIL_PASSWORT = ...
EMAIL_EMPFAENGER = ...
```

### Abschnitte

| Abschnitt | Datei | Beschreibung |
|---|---|---|
| `[suchbegriffe]` | config.txt | Begriffe die in Stellentiteln gesucht werden |
| `[ausschlussbegriffe]` | config.txt | Begriffe die Stellen ausschließen. Mit `+` AND-Verknüpfung |
| `[verbotene_standorte]` | config.txt | Städte die gefiltert werden (Blacklist) |
| `[firmen]` | config.txt | Firmenname und URL der Karriereseite (Playwright) |
| `[api_firmen]` | config.txt | Firmen mit direkter API-Abfrage (JSON-Konfiguration) |
| `[firma_domains]` | config.txt | Domain-Zuordnung für E-Mail-Filterung |
| `[firma_anschreiben]` | config.txt | Ansprechpartner/Adresse pro Firma fürs Anschreiben |
| `[prompt]` | config_secrets.txt | KI-Bewertungsprompt. Aufbau: Bewertungsregeln → AUSGABEFORMAT (JSON-Schema) → `{lebenslauf}` → `{stellentext}`. Der Teil vor `=== STELLENANZEIGE ===` wird als System-Prompt gesendet (Prompt-Caching) |
| `[anschreiben_prompt]` / `[anschreiben_prompt_en]` | config_secrets.txt | KI-Prompt für Anschreiben-Generierung (DE/EN) |
| `[steckbrief_prompt]` | config_secrets.txt | KI-Prompt für den Firmen-Steckbrief |

Erlaubte Standorte (Whitelist) stehen **nicht** in `config.txt`, sondern in
`whitelist_standorte.txt` (ein Ort pro Zeile).

---

## Pipeline-Schritte im Detail

### Schritt 1: `scanner.py`

- Ruft alle konfigurierten Firmen-URLs ab
- Unterstützt zwei Methoden:
  - **Playwright**: JavaScript-gerenderte Seiten (Standard für `[firmen]`)
  - **API**: Direkte API-Abfragen für bestimmte Firmen (hardcodiert in `API_FIRMEN`)
- Speichert neue Stellen in der DB (Export nach `stellen.json` / `bekannte_stellen.json`)
- Markiert nicht mehr gefundene Stellen als vergeben (Status 0)

**Status nach diesem Schritt:** 1 (URL bekannt) oder 2 (Rohtext vorhanden, je nach Quelle)

### Schritt 1b: `rohtext_holen.py`

- Lädt den vollständigen Seitentext (Rohtext) per Playwright nach, für Stellen mit
  keinem oder zu kurzem Rohtext (API-Quellen liefern oft nur Platzhalter)
- `--url URL` für eine einzelne Stelle, `--force` erzwingt Neuladen auch bei Status 3+

**Status nach diesem Schritt:** 2 (Rohtext vorhanden)

### Schritt 1c: `vergaben_check.py`

- Prüft per HTTP, ob bekannte aktive Stellen (Status 1–6, nicht gelöscht) noch erreichbar sind
- Erkennt vergebene Stellen anhand des HTTP-Status, ohne Playwright zu benötigen
- `--alle` prüft zusätzlich alle nicht gelöschten Stellen unabhängig vom Status

### Schritt 2: `extraktor.py`

- Verarbeitet alle Stellen mit Status 2
- Extrahiert den relevanten Stellentext aus dem Rohtext
- Bekannte Seitenstrukturen werden direkt aus `strukturen.json` verwendet
- Unbekannte Strukturen: KI extrahiert und lernt die Struktur für spätere Aufrufe

**Status nach diesem Schritt:** 3 (Stellentext vorhanden)

### Schritt 3: `bewertung.py`

- Verarbeitet alle Stellen mit Status 3
- Sendet Stellentext + Lebenslauf an die KI (Claude Haiku)
- Gibt zurück: zwei Scores (siehe unten), Empfehlung, Stärken, Lücken, Punkteabzüge, Lebenslauf-Anpassungshinweise
- Stellen mit **Profil-Score ≥ 70** erhalten die Empfehlung „bewerben" (Status 4, sonst 5)

#### Das Zwei-Score-System

Jede Bewertung liefert zwei Scores, die **verschiedene Fragen** beantworten – kein Vorher/Nachher:

| Score | Frage | Sichtweise |
|---|---|---|
| **Lebenslauf-Score** (`score`) | Komme ich durchs CV-Screening? | Recruiter, der **nur den Lebenslauf-Text** liest – ohne das Profilwissen aus dem Prompt |
| **Profil-Score** (`score_nach_anpassung`) | Lohnt sich die Bewerbung? | Volles Profil (Stärken, Arbeitstyp explorativ statt Serie) – das, was der Recruiter erst im Gespräch erfährt |

Der **Profil-Score entscheidet** über Empfehlung und Status – „lohnt es sich?" ist eine
Profil-Frage, keine Screening-Frage. Der Profil-Score ist zugleich die **Obergrenze**, an die
man den Lebenslauf-Score durch Anpassen des Lebenslaufs annähern kann.

Die Differenz der beiden Scores ist die eigentliche Information:

| Konstellation | Bedeutung | Konsequenz |
|---|---|---|
| Lebenslauf **<** Profil (z.B. 62 → 68) | **Sichtbarkeits-Lücke:** Der Lebenslauf verkauft vorhandene Erfahrung unter Wert | Anpassen lohnt sich – `lebenslauf_anpassungen` sagt konkret, was sichtbar gemacht werden soll |
| Lebenslauf **>** Profil (z.B. 78 → 58) | Käme durchs Screening, aber die Stelle passt nicht zum Arbeitstyp (z.B. Serienbetreuung statt explorativer Arbeit) | Nicht bewerben – das Problem ist nicht das Dokument, sondern die Stelle |
| Lebenslauf **=** Profil, beide niedrig | **Echte Kompetenz-Lücken** (z.B. nie benutzte Spezialsoftware) drücken beide Scores | Kein Umformulieren der Welt ändert daran etwas |

Optimierung per Lebenslauf-Anpassung ist also nur bei **Sichtbarkeits-Lücken** möglich, nie bei
echten Kompetenz-Lücken – der Prompt rechnet nur Abzüge zurück, die durch Sichtbarmachen
tatsächlich entfallen.

Weichen die Scores voneinander ab, liefert die KI zusätzlich **`profil_hinweise`** – eine
Liste der Auf- (+) und Abwertungen (−) mit Bezug auf das konkrete Signal in der Stellenanzeige
und den passenden bzw. widersprechenden Punkt der Profilbeschreibung. Im Report erscheinen sie
unter „Details anzeigen" als **Profil-Auf-/Abwertungen** (grün/rot eingefärbt).

**Hinweis:** Bewertungen von vor der Umstellung (Juli 2026) folgen noch der alten
„Score vorher/nachher Anpassung"-Logik; die neue Lesart gilt erst nach einer Neubewertung
(Button „🔁 Neu bewerten" im Report oder nächster Scan).

**Status nach diesem Schritt:** 4 (Profil-Score ≥ 70) oder 5 (darunter)

### Schritt 4: `report.py`

- Erstellt `report.html` aus allen Stellen in `stellen.json`
- Zeigt Top-10 nach dem höheren der beiden Scores, danach alle Firmen
- Score-Anzeige pro Stelle: `Lebenslauf: 62% → Profil: 68%` (bei identischen Werten nur „Score: X%")
- Sendet bei Änderungen (neue / vergebene Stellen) eine E-Mail
- Setzt das `neu`-Flag zurück nach Report-Erstellung

### Schritt 5: `anpasser.py`

- Verarbeitet alle Stellen mit **Profil-Score** ≥ 70 und Anpassungshinweisen
- Passt gezielt nur relevante Abschnitte der `lebenslauf_vorlage.txt` an
- Speichert das Ergebnis als `bewerbungen/Firma/Titel/Lebenslauf.txt`
- Wird **nicht automatisch** ausgeführt – nur on-demand über die WebUI (Checkbox)

---

## Lebenslauf-Vorlage

Die Vorlage (`lebenslauf_vorlage.txt`) verwendet Marker um Abschnitte zu kennzeichnen:

```
---KOMPETENZPROFIL---
Inhalt...
---/KOMPETENZPROFIL---
```

Verfügbare Marker:

| Marker | Beschreibung |
|---|---|
| `KONTAKT` | Name, Adresse, Telefon, E-Mail |
| `KOMPETENZPROFIL` | Einleitungstext |
| `STELLE_1_AUFGABEN` bis `STELLE_5_AUFGABEN` | Aufgaben pro Stelle |
| `AUSBILDUNG_1` bis `AUSBILDUNG_5` | Ausbildungseinträge |
| `FAEHIGKEITEN` | Fähigkeiten-Sektion |
| `SPRACHEN` | Sprachkenntnisse |

Der Anpasser verändert **nur** die Abschnitte die laut KI-Bewertung relevant sind.

---

## WebUI (`webui.py`)

Erreichbar unter `http://192.168.x.x:5000`

### Funktionen

| Funktion | Beschreibung |
|---|---|
| **Scan starten** | Startet die komplette Pipeline im Hintergrund, zeigt Live-Log |
| **Stelle manuell einfügen** | URL, Firmenname und Jobtitel eingeben → startet Teil-Pipeline |
| **Lebenslauf & Anschreiben erstellen** | Checkbox bei jeder Stelle (unabhängig vom Score) → generiert `.txt` und `.docx` |
| **Bewertung starten / 🔁 Neu bewerten** | Button bei Stellen mit Stellentext → (erneute) KI-Bewertung, Prompt wird live aus `config.txt` gelesen |
| **🔄 Neu laden & bewerten** | Bei Stellen ohne Stellentext: kompletter Teil-Durchlauf (Rohtext → Extraktion → Bewertung) für eine URL |
| **🧠 Steckbrief generieren** | Firmenbeschreibung, Matchbegründung, Interview-Fragen per KI |
| **📉 Geringen Match einblenden** | Blendet Stellen mit Score ≤ 65% ein/aus (siehe `GERINGER_MATCH_SCHWELLE`) |
| **Filter & Sortierung** | Nach Bewerbungsstufe, Scanner-Status, Firma, Score, Fahrzeit (Auto/ÖPNV) |
| **Notizen** | Freitextnotizen pro Stelle, werden in der DB gespeichert |
| **Bewerbungsstatus** | Dropdown: Beworben / Kennenlerngespräch / Einladung / Zusage / Absage |
| **Stellentext anzeigen** | Aufklappbarer extrahierter Stellentext |
| **Standort nachtragen** | Bei Stellen ohne erkannten Standort manuell eintragen |
| **Passend / Nicht passend umschalten** | KI-Empfehlung manuell übersteuern (Status 4 ↔ 5) |
| **🔖 Merken / Merkliste** | Button pro Stelle setzt/entfernt ein Merkliste-Flag (unabhängig vom Scanner-Status). Zähler + Checkbox „Nur Merkliste" in der Filterleiste. Wird automatisch entfernt, sobald die Stelle auf „Beworben" gesetzt wird |
| **Als vergeben markieren** | Manuell, falls die automatische Prüfung eine vergebene Stelle nicht erkennt |
| **Neue Firma testen** | Karriere-URL + Firmenname eingeben, Playwright sucht Jobtitel-Links, optional in `config.txt` übernehmen |

### Routes

| Route | Methode | Beschreibung |
|---|---|---|
| `/` | GET | Liefert `report.html` |
| `/starten` | GET | Startet komplette Pipeline (SSE-Log) |
| `/stoppen` | GET | Bricht den laufenden Scan nach dem aktuellen Script ab |
| `/stream` | GET | SSE-Stream des Scan-Logs |
| `/firmen` | GET | Alle Firmennamen als JSON (API- + Playwright-Firmen) |
| `/firma-testen` | GET | Scannt eine einzelne Firma, SSE-Stream |
| `/firmen-testen-stream` | GET | Testet eine neue Karriere-URL per Playwright, listet gefundene Jobtitel |
| `/firmen-config-hinzufuegen` | POST | Trägt eine neue Firma in `[firmen]` in `config.txt` ein |
| `/bewerbung-erstellen` | GET | Generiert Lebenslauf.txt + .docx für eine Stelle |
| `/download` | GET | Liefert eine Datei zum Download |
| `/status` | GET | Gibt Bewerbungsstatus aus DB zurück |
| `/status` | POST | Speichert Stufe, Kommentar oder Nicht-beworben-Grund |
| `/api/pruefe-stelle` | POST | Prüft eine URL auf Erreichbarkeit (HTTP-Status) |
| `/steckbrief-erstellen` | POST | Erstellt einen KI-Steckbrief für eine Stelle |
| `/bewertung-erstellen` | POST | (Neu-)Bewertung einer einzelnen Stelle – Prompt wird live geladen |
| `/stelle-einfuegen` | POST | Trägt eine Stelle manuell ein (URL, Firma, Titel) |
| `/manuell-stream` | GET | SSE-Stream: Teil-Pipeline für manuell eingetragene Stellen |
| `/stelle-neu-laden` | POST | Setzt eine Stelle auf Status 1 zurück (löscht Rohtext, `nicht_passend`) |
| `/stelle-einzeln-stream` | GET | SSE-Stream: Rohtext → Extraktion → Bewertung für eine URL |
| `/standort-setzen` | POST | Trägt einen Arbeitsort nach, prüft gegen Whitelist/Blacklist |
| `/passend-setzen` | POST | Schaltet Status 4 ↔ 5 manuell um |
| `/merken-setzen` | POST | Setzt/entfernt das Merkliste-Flag (`gemerkt`) einer Stelle |
| `/vergeben-setzen` | POST | Markiert eine Stelle manuell als vergeben |

---

## Datenbank (`jobscanner.db`)

SQLite-Datenbank, einzige Quelle der Wahrheit; `stellen.json` und `bekannte_stellen.json`
werden bei jeder Änderung daraus exportiert (siehe TODOs). Fünf Tabellen:

**`stellen`** – Alle Stellenanzeigen mit Rohtext, Stellentext, Status, Standort, Steckbrief, Pfaden zu Bewerbungsunterlagen. `gemerkt` (Timestamp oder NULL) ist ein vom Status unabhängiges Merkliste-Flag – wird manuell per 🔖-Button gesetzt/entfernt und automatisch gelöscht, sobald die Stelle auf „Beworben" gesetzt wird

**`bewertungen`** – KI-Bewertung pro Stelle (beide Scores, Stärken, Lücken, Punkteabzüge, Profil-Hinweise, Lebenslauf-Anpassungen, Sprache)

**`bewerbungsstatus`** – Bewerbungsfortschritt pro Stelle (Stufe, Timestamps, Kommentar, Nicht-beworben-Grund)

**`status_historie`** – Log aller Status-Änderungen

**`fahrzeit_cache`** – Gecachte Google-Maps-Fahrzeiten (Auto/ÖPNV) pro Zieladresse

Status-Definitionen (Labels, Farben, Emojis) liegen zentral in `status_def.py` – von `db.py`,
`report.py`, `webui.py`, `dashboard.py` und `vergaben_check.py` genutzt.

### Status-Codes

| Code | Bedeutung |
|---|---|
| 0 | Vergeben / nicht mehr erreichbar |
| 1 | Nur Link gefunden, noch kein Rohtext |
| 2 | Rohtext geholt, kein Stellentext extrahiert |
| 3 | Stellentext extrahiert, noch nicht bewertet |
| 4 | KI bewertet → Profil-Score ≥ 75%, bewerben empfohlen |
| 5 | KI bewertet → Profil-Score < 65%, nicht bewerben |
| 6 | Beworben, Stelle noch aktiv |
| 7 | Beworben, Stelle weg / vergeben (Ghosting) |
| 8 | Absage erhalten |
| 9 | Vergeben, nie beworben (per HTTP bestätigt) |
| 10 | Nicht beworben (manuell entschieden) |
| 11 | Grenzfall: Profil-Score 65–75%, manuell zu prüfen (KI-Bewertung streut in diesem Band zu stark für eine automatische Entscheidung) |

---

## Deployment (Raspberry Pi)

### Service starten / stoppen

```bash
sudo systemctl start jobscanner.service
sudo systemctl stop jobscanner.service
sudo systemctl restart jobscanner.service
sudo systemctl status jobscanner.service
```

### Manuell starten (für Tests)

```bash
cd ~/Jobsuche
source venv/bin/activate
python webui.py
```

### Einzelne Scripts manuell ausführen

```bash
source ~/Jobsuche/venv/bin/activate
python scanner.py
python rohtext_holen.py
python vergaben_check.py
python extraktor.py
python bewertung.py
python report.py
python anpasser.py
```

### Dateien deployen (von Windows)

Per WinSCP oder scp:

```bash
scp C:\Users\sasch\Documents\Python\Jobsuche\Jobsuche_V2\webui.py sascha@192.168.165.146:/home/sascha/Jobsuche/
```

---

## Offene TODOs

- Stellen-Felder (Titel, Firma) im Report editierbar machen
- `stellen.json` und `bekannte_stellen.json` vollständig durch SQLite ersetzen
- PDF-URLs beim manuellen Einfügen automatisch extrahieren
- iCloud Mail-Check für automatischen E-Mail-Abgleich
- Workday-Handler ausbauen (Bosch, Daimler, ZF)
