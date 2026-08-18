# Geplante Features (Stand 2026-08-10)

Zwei Features besprochen, noch **nichts implementiert** – wir machen morgen weiter.

---

## Feature 1: Firmen ohne Standortfilter anzeigen

**Ziel:** Für bestimmte Firmen (z.B. Liebherr) sollen auch Stellen **außerhalb**
des erlaubten Standorts angezeigt und ganz normal bewertet werden.

### Ist-Stand ("halb implementiert")
- Standort-Ablehnung passiert zentral über `ablehnungsgrund()` in `utils.py:190`.
- Abgelehnte Stellen:
  - bei Discovery → landen in der `ausgeschlossen`-Liste (nicht in `stellen`),
    laufen also **nicht** durch Rohtext → Extraktion → Bewertung.
  - Bestandsstellen → `bereinige_verbotene_standorte()` (`scanner.py:1023`)
    markiert sie als `nicht_passend` mit `nicht_passend_grund`.
- Report hat bereits eine eigene Sektion "nicht passend (Standort)"
  (`report.py:791-796`, `_ist_standort_grund`).
- Grund wird also schon sauber getrackt – was fehlt, ist ein **Schalter pro Firma**,
  der den Standortfilter überspringt.

### OFFENE ENTSCHEIDUNG (morgen klären)
**Wie markiert man eine Firma als "Standortfilter ignorieren"?**
- Option A: Neuer Abschnitt in `config.txt`, z.B.
  ```
  [firmen_ohne_standortfilter]
  Liebherr
  [\firmen_ohne_standortfilter]
  ```
  → gilt für Playwright- UND API-Firmen, ändert kein bestehendes Zeilenformat.
- Option B: Flag in der `[firmen]`-Zeile, z.B. `Liebherr | https://... | kein_standortfilter`
  → kompakt, aber ändert Zeilenformat und greift nicht direkt bei `[api_firmen]`.

### Umsetzungspunkte (wenn Entscheidung steht)
- [ ] Config-Parsing in `utils.py` (`lade_config` / `_parse_config_datei`)
      um die neue Firmen-Markierung erweitern (neuer Key, z.B. `firmen_ohne_standortfilter`).
- [ ] `ablehnungsgrund()` bzw. Aufrufstellen so anpassen, dass für markierte Firmen
      der **Standort**-Teil übersprungen wird (Ausschlussbegriffe weiterhin greifen!).
      Betrifft alle Discovery-Pfade in `scanner.py` (API: ~392, HTML-Tabelle: ~522,
      hr4you: ~615, workday: ~725, Playwright: ~991).
- [ ] `bereinige_verbotene_standorte()` (`scanner.py:1023`) für markierte Firmen
      überspringen, damit Bestandsstellen nicht nachträglich entfernt werden.
- [ ] Prüfen, ob diese Stellen im Report sinnvoll einsortiert werden
      (normal aktiv vs. eigene Sektion "außerhalb Umkreis, aber gewünscht"?).
- [ ] readme.md: neuen Config-Abschnitt dokumentieren.

---

## Feature 2: Export abgelehnter Stellen für Prompt-Verbesserung

**Ziel:** JSON mit **Joblink + Stellenbeschreibung + Grund**, um damit den
KI-Bewertungs-Prompt zu verbessern.

### Entschieden
- **Nur CLI-Script** (kein WebUI-Button).
- Umfang: **manuelle + automatische Gründe**
  - manuell: `bewerbungsstatus.nicht_beworben_grund` (von dir per "Nicht beworben"-Grund eingetragen)
  - automatisch: `stellen.nicht_passend_grund` (Ausschlussbegriff / Standort)
  - Priorität: manueller Grund gewinnt über automatischen.

### Umsetzungspunkte
- [ ] Neues Script `export_ablehnungen.py`:
      SQL-JOIN `stellen` LEFT JOIN `bewerbungsstatus` über `url`,
      alle Zeilen mit nicht-leerem Grund, Ausgabe `ablehnungen.json` mit
      Feldern: `url`, `titel`, `firma`, `stellentext`, `grund`, `grund_quelle`.
- [ ] Flag `--nur-manuell` optional, sowie `-o/--ausgabe` für eigenen Pfad.
- [ ] Kurz testen, dass JSON sauber (UTF-8, keine leeren Gründe) rauskommt.

> Hinweis: Ein fertiger Entwurf des Scripts existierte kurz, wurde auf Wunsch
> wieder gelöscht ("erstmal nichts ändern"). Logik oben ist dokumentiert und
> morgen schnell wiederhergestellt.

---

## TODO (offen): ATS-Bestand analysieren und Handler priorisieren

**Ziel:** Systematisch herausfinden, welche ATS/Job-Widgets die konfigurierten
Firmen (config.txt `[firmen]` + `[api_firmen]`) nutzen und wo der generische
Scanner bei der Discovery scheitert – daraus ableiten, welche ATS-Handler sich
lohnen (statt vorab Handler für ungenutzte Anbieter zu bauen).

**Kontext:** Bisher gibt es nur Punkt-Lösungen: onlyfy-iframe-Erkennung
(`_finde_ats_widget`/`_onlyfy_alle_url` in scanner.py) und Sitecore-patternlib-
Shadow-Paginierung (`_klick_naechste_seite_shadow`). Generischer Fallback ist
das KI-Muster-Lernen. Es fehlt eine **erweiterbare ATS-Registry**
(Widget-Host → Handler), die beim Scan erkennt, welcher ATS eingebettet ist,
und die passende „alle Stellen laden"-Strategie fährt.

**Vorgehen:**
- [ ] Alle config-Firmen scannen und protokollieren: erkanntes ATS/Widget
      (iframe-Hosts), Anzahl gefundener vs. tatsächlicher Stellen, wo Discovery/
      Paginierung scheitert.
- [ ] Häufige DE-ATS prüfen: softgarden, Personio, Greenhouse, Lever, Prescreen,
      Concludis, rexx, d.vinci, SmartRecruiters, Join, b-ite, umantis.
- [ ] Aus Ergebnis Prioritäten ableiten und die erweiterbare ATS-Registry bauen
      (onlyfy als erster Eintrag), pro Anbieter je ein kleiner Handler.

> Entscheidung vom User: erst Bestand analysieren, dann gezielt Handler bauen.
> Wird später fortgesetzt.
