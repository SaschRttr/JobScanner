# Job-Scanner – Dokumentation

## Übersicht

Der Job-Scanner ist eine automatisierte Pipeline die Stellenanzeigen von konfigurierten Firmen-Websites abruft, per KI bewertet und als HTML-Report darstellt. Optional können angepasste Bewerbungsunterlagen (Lebenslauf, Anschreiben) generiert werden.

Das System läuft auf einem Raspberry Pi und ist über den Browser erreichbar.

---

## Architektur

```
scanner.py → extraktor.py → bewertung.py → report.py → anpasser.py
```

Jedes Script ist ein eigenständiger Schritt. Sie können einzeln oder als komplette Pipeline über die WebUI gestartet werden.

---

## Dateien & Verzeichnisse

| Datei / Ordner | Beschreibung |
|---|---|
| `config.txt` | Zentrale Konfiguration (API-Key, Firmen, Suchbegriffe, Prompt) |
| `scanner.py` | Schritt 1: Stellenanzeigen finden und Rohtext laden |
| `extraktor.py` | Schritt 2: Stellentext aus Rohtext extrahieren |
| `bewertung.py` | Schritt 3: KI-Bewertung der Stelle |
| `report.py` | Schritt 4: HTML-Report erstellen, optional Mail senden |
| `anpasser.py` | Schritt 5: Lebenslauf an Stelle anpassen (nur bei Score ≥ 70) |
| `webui.py` | Flask-Webserver (Port 5000), steuert die Pipeline |
| `db.py` | SQLite-Datenbankmodul |
| `jobscanner.db` | SQLite-Datenbank |
| `stellen.json` | Alle gefundenen Stellen (Haupt-Datenspeicher) |
| `bekannte_stellen.json` | Status pro URL (welche Pipeline-Stufe erreicht) |
| `strukturen.json` | Gelernte Seitenstrukturen für schnellere Extraktion |
| `lebenslauf_vorlage.txt` | Lebenslauf-Vorlage mit Markern für den Anpasser |
| `lebenslauf_vorlage_en.txt` | Englische Übersetzung (generiert via `uebersetzer.py`) |
| `lebenslauf.txt` | Aktueller Lebenslauf (Basis für Bewertungs-Prompt) |
| `bewerbungen/` | Generierte Bewerbungsunterlagen pro Stelle |
| `scan.log` | Log-Ausgabe der aktuellen Pipeline |
| `report.html` | Generierter HTML-Report |
| `jobscanner.service` | systemd-Service für automatischen Start |

---

## Konfiguration (`config.txt`)

Die Konfiguration ist in Abschnitte mit `[name]` / `[\name]` Markern aufgeteilt.

### Globale Einstellungen

```
API_KEY = sk-ant-...         # Anthropic API-Key
LLM_BEWERTUNG = true         # KI-Bewertung ein/aus
RASPI_IP = 192.168.x.x       # IP des Raspberry Pi (für Download-Links)
EMAIL_AKTIV = true           # E-Mail bei Änderungen senden
EMAIL_ABSENDER = ...
EMAIL_PASSWORT = ...
EMAIL_EMPFAENGER = ...
```

### Abschnitte

| Abschnitt | Beschreibung |
|---|---|
| `[suchbegriffe]` | Begriffe die in Stellentiteln gesucht werden |
| `[ausschlussbegriffe]` | Begriffe die Stellen ausschließen. Mit `+` AND-Verknüpfung |
| `[verbotene_standorte]` | Städte die gefiltert werden |
| `[firmen]` | Firmenname und URL der Karriereseite |
| `[prompt]` | KI-Bewertungsprompt mit `{stellentext}` und `{lebenslauf}` Platzhaltern |
| `[firma_domains]` | Domain-Zuordnung für E-Mail-Filterung |

---

## Pipeline-Schritte im Detail

### Schritt 1: `scanner.py`

- Ruft alle konfigurierten Firmen-URLs ab
- Unterstützt zwei Methoden:
  - **Playwright**: JavaScript-gerenderte Seiten (Standard für `[firmen]`)
  - **API**: Direkte API-Abfragen für bestimmte Firmen (hardcodiert in `API_FIRMEN`)
- Lädt den Rohtext jeder gefundenen Stellenanzeige
- Speichert neue Stellen in `stellen.json` und `bekannte_stellen.json`
- Markiert nicht mehr gefundene Stellen als vergeben (Status 0)

**Status nach diesem Schritt:** 1 (URL bekannt) oder 2 (Rohtext vorhanden)

### Schritt 2: `extraktor.py`

- Verarbeitet alle Stellen mit Status 2
- Extrahiert den relevanten Stellentext aus dem Rohtext
- Bekannte Seitenstrukturen werden direkt aus `strukturen.json` verwendet
- Unbekannte Strukturen: KI extrahiert und lernt die Struktur für spätere Aufrufe

**Status nach diesem Schritt:** 3 (Stellentext vorhanden)

### Schritt 3: `bewertung.py`

- Verarbeitet alle Stellen mit Status 3
- Sendet Stellentext + Lebenslauf an die KI (Claude Haiku)
- Gibt zurück: Score (0–100), Empfehlung, Stärken, Lücken, Lebenslauf-Anpassungshinweise
- Stellen mit Score ≥ 70 erhalten die Empfehlung „bewerben"

**Status nach diesem Schritt:** 4 (bewertet)

### Schritt 4: `report.py`

- Erstellt `report.html` aus allen Stellen in `stellen.json`
- Zeigt Top-10 nach Score, danach alle Firmen
- Sendet bei Änderungen (neue / vergebene Stellen) eine E-Mail
- Setzt das `neu`-Flag zurück nach Report-Erstellung

### Schritt 5: `anpasser.py`

- Verarbeitet alle Stellen mit Score ≥ 70 und Anpassungshinweisen
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
| **Lebenslauf & Anschreiben erstellen** | Checkbox bei Stelle mit Score ≥ 70 → generiert `.txt` und `.docx` |
| **Notizen** | Freitextnotizen pro Stelle, werden in der DB gespeichert |
| **Bewerbungsstatus** | Dropdown: Beworben / Kennenlerngespräch / Einladung / Zusage / Absage |
| **Stellentext anzeigen** | Aufklappbarer extrahierter Stellentext |

### Routes

| Route | Methode | Beschreibung |
|---|---|---|
| `/` | GET | Liefert `report.html` |
| `/starten` | GET | Startet komplette Pipeline |
| `/stream` | GET | SSE-Stream des Scan-Logs |
| `/status` | GET | Gibt Bewerbungsstatus aus DB zurück |
| `/status` | POST | Speichert Stufe oder Kommentar |
| `/bewerbung-erstellen` | GET | Generiert Lebenslauf.txt + .docx für eine Stelle |
| `/download` | GET | Liefert eine Datei zum Download |
| `/stelle-einfuegen` | POST | Trägt eine Stelle manuell ein |
| `/manuell-stream` | GET | SSE-Stream für manuellen Eintrag |

---

## Datenbank (`jobscanner.db`)

SQLite-Datenbank mit vier Tabellen:

**`stellen`** – Alle Stellenanzeigen mit Rohtext, Stellentext und Status

**`bewertungen`** – KI-Bewertung pro Stelle (Score, Stärken, Lücken, Anpassungshinweise)

**`bewerbungsstatus`** – Bewerbungsfortschritt pro Stelle (Stufe, Timestamps, Kommentar)

**`status_historie`** – Log aller Status-Änderungen

### Status-Codes

| Code | Bedeutung |
|---|---|
| 0 | Vergeben / nicht mehr verfügbar |
| 1 | URL bekannt, kein Rohtext |
| 2 | Rohtext vorhanden |
| 3 | Stellentext extrahiert |
| 4 | KI-Bewertung vorhanden |

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
- `API_FIRMEN` aus Hardcode in `config.txt` auslagern
- Streamlit-Dashboard für Pipeline-Visualisierung
