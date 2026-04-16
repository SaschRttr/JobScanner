"""
anpasser.py  –  Job-Scanner (Schritt 5)
=========================================
Generiert für alle Stellen mit Status 4 und Score >= 70 einen
angepassten Lebenslauf als .txt-Datei.

Nutzt Marker in lebenslauf_vorlage.txt um gezielt nur die relevanten
Abschnitte zu verändern (spart Tokens, präzisere Ergebnisse).

Marker-Format:
  ---ABSCHNITT---
  Inhalt
  ---/ABSCHNITT---

Ausgabe: ~/Documents/Python/Jobsuche/bewerbungen/Firma/Titel/Lebenslauf.txt

Nutzung:
  python anpasser.py
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import anthropic as anthropic_lib
except ImportError:
    print("anthropic nicht installiert: pip install anthropic")
    sys.exit(1)


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD           = Path(__file__).parent
STELLEN_JSON         = BASIS_PFAD / "stellen.json"
VORLAGE_PFAD         = BASIS_PFAD / "lebenslauf_vorlage.txt"
ANSCHREIBEN_VORLAGE  = BASIS_PFAD / "anschreiben_vorlage.txt"
BEWERBUNGEN_DIR      = BASIS_PFAD / "bewerbungen"
CONFIG_PFAD          = Path(__file__).parent / "config.txt"

KI_MODELL  = "claude-sonnet-4-20250514"
MIN_SCORE  = 70


# =============================================================================
# CONFIG
# =============================================================================

def _lese_config_block(inhalt: str, name: str) -> str:
    """Extrahiert den Inhalt zwischen [name] und [\\name] aus config.txt."""
    start_tag = f"[{name}]"
    end_tag   = f"[\\{name}]"
    start = inhalt.find(start_tag)
    ende  = inhalt.find(end_tag)
    if start == -1 or ende == -1:
        return ""
    return inhalt[start + len(start_tag):ende].strip()


def lade_config() -> dict:
    if not CONFIG_PFAD.exists():
        print(f"❌ config.txt nicht gefunden: {CONFIG_PFAD}")
        sys.exit(1)
    inhalt = CONFIG_PFAD.read_text(encoding="utf-8")
    result = {"api_key": "", "anschreiben_prompt": ""}
    for zeile in inhalt.splitlines():
        z = zeile.strip()
        if z.upper().startswith("API_KEY"):
            result["api_key"] = z.split("=", 1)[1].strip()
    result["anschreiben_prompt"] = _lese_config_block(inhalt, "anschreiben_prompt")
    return result


# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def lade_json(pfad: Path, standard):
    if pfad.exists():
        try:
            return json.loads(pfad.read_text(encoding="utf-8"))
        except Exception:
            pass
    return standard


def sicherer_pfadname(text: str, max_len: int = 50) -> str:
    bereinigt = re.sub(r'[^\w\s\-]', '', text).strip()
    bereinigt = re.sub(r'\s+', '_', bereinigt)
    return bereinigt[:max_len]


def get_score(s: dict) -> int:
    b = s.get("bewertung") or {}
    return b.get("score_aktuell", b.get("score", 0))


# =============================================================================
# MARKER-LOGIK
# =============================================================================

def extrahiere_abschnitt(vorlage: str, marker: str) -> str | None:
    """Gibt den Inhalt zwischen ---MARKER--- und ---/MARKER--- zurück."""
    start = vorlage.find(f"---{marker}---")
    ende  = vorlage.find(f"---/{marker}---")
    if start == -1 or ende == -1:
        return None
    # Inhalt nach der öffnenden Marker-Zeile
    inhalt_start = vorlage.find("\n", start) + 1
    return vorlage[inhalt_start:ende].strip()


def ersetze_abschnitt(vorlage: str, marker: str, neuer_inhalt: str) -> str:
    """Ersetzt den Inhalt zwischen ---MARKER--- und ---/MARKER---."""
    start_marker = f"---{marker}---"
    ende_marker  = f"---/{marker}---"
    start = vorlage.find(start_marker)
    ende  = vorlage.find(ende_marker)
    if start == -1 or ende == -1:
        return vorlage  # Marker nicht gefunden → unverändert zurück
    inhalt_start = vorlage.find("\n", start) + 1
    return vorlage[:inhalt_start] + neuer_inhalt + "\n" + vorlage[ende:]


def bestimme_relevante_marker(anpassungen: list) -> list:
    """
    Leitet aus den Anpassungshinweisen ab welche Abschnitte geändert werden müssen.
    Gibt eine Liste von Marker-Namen zurück.
    """
    marker_map = {
        "kompetenzprofil":  ["profil", "summary", "einleitung", "zusammenfassung",
                             "kompetenz", "USP", "stärken"],
        "STELLE_1_AUFGABEN": ["bosch", "sofc", "fuel cell", "robert bosch",
                              "feuerbach", "aktuelle", "aktuell"],
        "STELLE_2_AUFGABEN": ["automotive steering", "schwäbisch gmünd", "testadapter",
                              "asic", "servolenkung"],
        "STELLE_3_AUFGABEN": ["power tec", "böblingen", "solarwechselrichter", "vde"],
        "STELLE_4_AUFGABEN": ["sma solar", "niestetal", "offgrid", "storage"],
        "FAEHIGKEITEN":      ["fähigkeit", "skill", "tool", "software", "technologie",
                              "python", "sql", "altium", "databricks", "tableau",
                              "werkzeug", "kenntnisse", "reihenfolge"],
    }

    relevante = set()
    anpassungen_lower = " ".join(anpassungen).lower()

    for marker, schlagworte in marker_map.items():
        if any(s in anpassungen_lower for s in schlagworte):
            relevante.add(marker.upper())

    # Fallback: wenn keine Zuordnung → Kompetenzprofil und Fähigkeiten
    if not relevante:
        relevante = {"KOMPETENZPROFIL", "FAEHIGKEITEN"}

    return list(relevante)


# =============================================================================
# KI: ABSCHNITT ANPASSEN
# =============================================================================

def passe_abschnitt_an(
    abschnitt_name: str,
    abschnitt_inhalt: str,
    anpassungen: list,
    stelle: dict,
    client
) -> str | None:
    """Lässt die KI einen einzelnen Abschnitt anpassen."""
    anpassungen_text = "\n".join(f"- {a}" for a in anpassungen)

    prompt = f"""Du bist ein professioneller Bewerbungsberater.

Passe diesen Abschnitt des Lebenslaufs für die folgende Stelle an:

Firma:    {stelle['firma']}
Stelle:   {stelle['titel']}
Abschnitt: {abschnitt_name}

Anpassungshinweise:
{anpassungen_text}

Regeln:
- Verändere NUR was die Anpassungshinweise für diesen Abschnitt verlangen
- Erfinde KEINE neuen Fakten oder Erfahrungen
- Behalte Formatierung (Bullet-Zeichen, Einrückung) exakt bei
- Gib NUR den angepassten Abschnitt zurück, ohne Marker-Zeilen, ohne Kommentar

=== AKTUELLER ABSCHNITT ===
{abschnitt_inhalt}"""

    try:
        antwort = client.messages.create(
            model=KI_MODELL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return antwort.content[0].text.strip()
    except Exception as e:
        print(f"  ❌ API-Fehler bei Abschnitt {abschnitt_name}: {e}")
        return None


# =============================================================================
# ANSCHREIBEN: FIRMENDATEN AUS CONFIG
# =============================================================================

_MONATE = {
    1: "Januar", 2: "Februar", 3: "März",    4: "April",
    5: "Mai",    6: "Juni",    7: "Juli",     8: "August",
    9: "September", 10: "Oktober", 11: "November", 12: "Dezember",
}

_DUMMY_FIRMA = {
    "firmenname":      "[Firmenname]",
    "abteilung":       "[Abteilung]",
    "ansprechpartner": "[Ansprechpartner]",
    "strasse":         "[Straße]",
    "plz":             "[PLZ]",
    "ort":             "[Ort]",
}


def lade_firma_anschreiben(firma_name: str) -> dict:
    """
    Liest [firma_anschreiben] aus config.txt.
    Findet den Eintrag per Teilstring-Match (case-insensitive).
    Gibt Dummy-Werte zurück wenn kein Eintrag passt.
    """
    if not CONFIG_PFAD.exists():
        return _DUMMY_FIRMA.copy()

    inhalt = CONFIG_PFAD.read_text(encoding="utf-8")
    start  = inhalt.find("[firma_anschreiben]")
    ende   = inhalt.find("[\\firma_anschreiben]")
    if start == -1 or ende == -1:
        return _DUMMY_FIRMA.copy()

    sektion     = inhalt[start + len("[firma_anschreiben]"):ende]
    firma_lower = firma_name.lower()

    for zeile in sektion.splitlines():
        z = zeile.strip()
        if not z or z.startswith("#"):
            continue
        teile = [t.strip() for t in z.split("|")]
        if len(teile) != 6:
            continue
        eintrag_lower = teile[0].lower()
        if eintrag_lower in firma_lower or firma_lower in eintrag_lower:
            return {
                "firmenname":      teile[0],
                "abteilung":       teile[1],
                "ansprechpartner": teile[2],
                "strasse":         teile[3],
                "plz":             teile[4],
                "ort":             teile[5],
            }
    return _DUMMY_FIRMA.copy()


# =============================================================================
# ANSCHREIBEN: AUFGABEN-POOL AUS LEBENSLAUF-VORLAGE
# =============================================================================

def extrahiere_aufgaben_pool(vorlage: str) -> str:
    """
    Extrahiert alle STELLE_X_AUFGABEN-Blöcke aus dem übergebenen Vorlagen-Text
    und gibt sie als formatierten Pool zurück.
    """
    pool_zeilen: list[str] = []

    for match in re.finditer(
        r"---STELLE_(\d+)_AUFGABEN---\n(.*?)---/STELLE_\1_AUFGABEN---",
        vorlage, re.DOTALL
    ):
        nummer = match.group(1)
        inhalt = match.group(2).strip()

        # Jobtitel-Zeile aus dem übergeordneten STELLE_X-Block als Header
        stelle_match = re.search(
            rf"---STELLE_{nummer}---\n(.*?)---STELLE_{nummer}_AUFGABEN---",
            vorlage, re.DOTALL
        )
        header = (
            stelle_match.group(1).strip().splitlines()[0]
            if stelle_match else f"Stelle {nummer}"
        )

        pool_zeilen.append(f"[{header}]")
        pool_zeilen.append(inhalt)
        pool_zeilen.append("")

    return "\n".join(pool_zeilen).strip()


# =============================================================================
# ANSCHREIBEN: GENERIEREN UND SPEICHERN
# =============================================================================

def generiere_anschreiben(
    stelle: dict,
    lebenslauf_vorlage: str,
    anschreiben_vorlage: str,
    config: dict,
    client,
    ordner: Path,
) -> bool:
    """
    Erzeugt Anschreiben.txt im Zielordner.
    - EMPFAENGER, DATUM, BETREFF werden direkt befüllt (kein KI)
    - Alle Absätze in einem einzigen KI-Aufruf via config["anschreiben_prompt"]
    - Marker-Zeilen werden aus der gespeicherten Datei entfernt
    Gibt True zurück wenn erfolgreich, False bei Fehler.
    """
    if not config.get("anschreiben_prompt"):
        print(f"  ⚠️  Kein [anschreiben_prompt] in config.txt – Anschreiben übersprungen")
        return False

    firma_d = lade_firma_anschreiben(stelle.get("firma", ""))

    # --- EMPFAENGER direkt befüllen ---
    empfaenger = (
        f"{firma_d['firmenname']}\n"
        f"{firma_d['abteilung']}\n"
        f"{firma_d['ansprechpartner']}\n"
        f"{firma_d['strasse']}\n"
        f"{firma_d['plz']} {firma_d['ort']}"
    )

    # --- DATUM direkt befüllen ---
    heute = datetime.now()
    datum = f"Stuttgart, {heute.day}. {_MONATE[heute.month]} {heute.year}"

    # --- BETREFF direkt befüllen ---
    betreff = stelle.get("titel", "")

    # --- Aufgaben-Pool aus Lebenslauf-Vorlage ---
    aufgaben_pool = extrahiere_aufgaben_pool(lebenslauf_vorlage)
    if not aufgaben_pool:
        print(f"  ⚠️  Kein Aufgaben-Pool in lebenslauf_vorlage – Anschreiben übersprungen")
        return False

    # --- KI-Prompt aus config befüllen ---
    b = stelle.get("bewertung") or {}
    staerken_text      = "\n".join(f"- {s}" for s in b.get("staerken", []))
    score_begruendung  = b.get("score_begruendung", "")

    prompt = config["anschreiben_prompt"]
    prompt = prompt.replace("{firma}",             stelle.get("firma", ""))
    prompt = prompt.replace("{titel}",             stelle.get("titel", ""))
    prompt = prompt.replace("{staerken}",          staerken_text or "(keine Angaben)")
    prompt = prompt.replace("{score_begruendung}", score_begruendung or "(keine Angaben)")
    prompt = prompt.replace("{aufgaben_pool}",     aufgaben_pool)

    # --- Einmaliger KI-Aufruf ---
    try:
        antwort = client.messages.create(
            model=KI_MODELL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text   = re.sub(r"```json|```", "", antwort.content[0].text.strip()).strip()
        inhalt = json.loads(text)
    except Exception as e:
        print(f"  ❌ API-Fehler Anschreiben: {e}")
        return False

    # --- Vorlage befüllen ---
    # ABSENDER, GRUSS, ANLAGEN bleiben unverändert aus der Vorlage
    angepasst = anschreiben_vorlage
    angepasst = ersetze_abschnitt(angepasst, "EMPFAENGER",       empfaenger)
    angepasst = ersetze_abschnitt(angepasst, "DATUM",            datum)
    angepasst = ersetze_abschnitt(angepasst, "BETREFF",          betreff)
    angepasst = ersetze_abschnitt(angepasst, "ANREDE",           inhalt.get("ANREDE",        ""))
    angepasst = ersetze_abschnitt(angepasst, "ABSATZ_1",         inhalt.get("ABSATZ_1",      ""))
    angepasst = ersetze_abschnitt(angepasst, "ABSATZ_2_INTRO",   inhalt.get("ABSATZ_2_INTRO",""))
    angepasst = ersetze_abschnitt(angepasst, "ABSATZ_2_BULLETS", inhalt.get("ABSATZ_2_BULLETS", ""))
    angepasst = ersetze_abschnitt(angepasst, "ABSATZ_3",         inhalt.get("ABSATZ_3",      ""))
    angepasst = ersetze_abschnitt(angepasst, "ABSATZ_4",         inhalt.get("ABSATZ_4",      ""))

    ziel = ordner / "Anschreiben.txt"
    ziel.write_text(angepasst, encoding="utf-8")
    print(f"  ✅ Anschreiben.txt gespeichert")
    return True


# =============================================================================
# EINZELSTELLE ANPASSEN  (wird von webui.py / Flask aufgerufen)
# =============================================================================

def passe_stelle_an(url: str) -> dict:
    """
    Passt den Lebenslauf für eine einzelne Stelle an.
    Gibt ein dict zurück:
      { "ok": True,  "pfad": "/pfad/zu/Lebenslauf.txt" }
      { "ok": False, "fehler": "Fehlermeldung" }
    """
    config = lade_config()
    if not config["api_key"]:
        return {"ok": False, "fehler": "Kein API-Key in config.txt"}

    stellen = lade_json(STELLEN_JSON, [])
    stelle  = next((s for s in stellen if s.get("url") == url), None)

    if not stelle:
        return {"ok": False, "fehler": f"Stelle nicht gefunden: {url}"}

    b           = stelle.get("bewertung") or {}
    anpassungen = b.get("lebenslauf_anpassungen", [])
    s_aktuell   = b.get("score_aktuell", b.get("score", 0))
    s_danach    = b.get("score_nach_anpassung", s_aktuell)
    firma       = stelle["firma"]
    titel       = stelle["titel"]

    ordner = BEWERBUNGEN_DIR / sicherer_pfadname(firma) / sicherer_pfadname(titel)
    ordner.mkdir(parents=True, exist_ok=True)
    ziel = ordner / "Lebenslauf.txt"

    sprache = b.get("sprache", "de")
    if sprache == "en":
        vorlage_pfad = BASIS_PFAD / "lebenslauf_vorlage_en.txt"
        if not vorlage_pfad.exists():
            return {"ok": False, "fehler": "lebenslauf_vorlage_en.txt nicht gefunden"}
    else:
        vorlage_pfad = BASIS_PFAD / "lebenslauf_vorlage.txt"

    if not vorlage_pfad.exists():
        return {"ok": False, "fehler": f"Vorlage nicht gefunden: {vorlage_pfad}"}

    lv_vorlage = vorlage_pfad.read_text(encoding="utf-8")
    client     = anthropic_lib.Anthropic(api_key=config["api_key"])

    relevante_marker   = bestimme_relevante_marker(anpassungen)
    angepasste_vorlage = lv_vorlage

    for marker in relevante_marker:
        abschnitt = extrahiere_abschnitt(lv_vorlage, marker)
        if not abschnitt:
            continue
        neuer_inhalt = passe_abschnitt_an(marker, abschnitt, anpassungen, stelle, client)
        if neuer_inhalt:
            angepasste_vorlage = ersetze_abschnitt(angepasste_vorlage, marker, neuer_inhalt)

    with open(ziel, "w", encoding="utf-8") as f:
        f.write(f"Firma:                {firma}\n")
        f.write(f"Stelle:               {titel}\n")
        f.write(f"Score aktuell:        {s_aktuell}%\n")
        f.write(f"Score nach Anpassung: {s_danach}%\n")
        f.write(f"Erstellt am:          {datetime.now().strftime('%d.%m.%Y %H:%M')}\n")
        f.write(f"URL:                  {url}\n")
        f.write(f"\nAngepasste Abschnitte: {', '.join(relevante_marker)}\n")
        f.write("\nAnpassungshinweise:\n")
        for a in anpassungen:
            f.write(f"  → {a}\n")
        f.write("\n" + "=" * 60 + "\n\n")
        f.write(angepasste_vorlage)

    # Anschreiben-Vorlage je nach Sprache wählen
    as_ziel = ordner / "Anschreiben.txt"
    if not as_ziel.exists():
        if sprache == "en":
            as_vorlage_pfad = BASIS_PFAD / "anschreiben_vorlage_en.txt"
            if not as_vorlage_pfad.exists():
                as_vorlage_pfad = ANSCHREIBEN_VORLAGE
        else:
            as_vorlage_pfad = ANSCHREIBEN_VORLAGE

        if not as_vorlage_pfad.exists():
            print(f"  ⚠️  anschreiben_vorlage.txt nicht gefunden – Anschreiben übersprungen")
        else:
            as_vorlage = as_vorlage_pfad.read_text(encoding="utf-8")
            generiere_anschreiben(stelle, lv_vorlage, as_vorlage, config, client, ordner)

    return {"ok": True, "pfad": str(ziel)}


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    print("\n" + "=" * 60)
    print("  ANPASSER  –  Schritt 5: Lebenslauf anpassen")
    print("=" * 60)

    config = lade_config()
    if not config["api_key"]:
        print("❌ Kein API-Key in config.txt")
        sys.exit(1)

    if not VORLAGE_PFAD.exists():
        print(f"❌ Lebenslauf-Vorlage nicht gefunden: {VORLAGE_PFAD}")
        print(f"   Bitte lebenslauf_vorlage.txt in {BASIS_PFAD} ablegen.")
        sys.exit(1)

    stellen  = lade_json(STELLEN_JSON, [])
    client   = anthropic_lib.Anthropic(api_key=config["api_key"])

    if not stellen:
        print("ℹ️  stellen.json ist leer – zuerst scanner.py ausführen.")
        return

    # Alle Stellen mit Score >= MIN_SCORE und Anpassungshinweisen
    kandidaten = [
        s for s in stellen
        if get_score(s) >= MIN_SCORE
        and s.get("bewertung", {}).get("lebenslauf_anpassungen")
    ]

    print(f"  {len(kandidaten)} Stellen mit Score ≥ {MIN_SCORE}%")

    if not kandidaten:
        print(f"  ℹ️  Keine Stellen zum Anpassen.")
        return

    erstellt      = 0
    uebersprungen = 0

    for stelle in kandidaten:
        firma       = stelle["firma"]
        titel       = stelle["titel"]
        b           = stelle["bewertung"]
        s_aktuell   = b.get("score_aktuell", b.get("score", 0))
        s_danach    = b.get("score_nach_anpassung", s_aktuell)
        anpassungen = b.get("lebenslauf_anpassungen", [])

        print(f"\n  {'─'*50}")
        print(f"  {firma}: {titel[:55]}")
        print(f"  Score: {s_aktuell}% → {s_danach}% nach Anpassung")

        # Zielordner
        ordner = BEWERBUNGEN_DIR / sicherer_pfadname(firma) / sicherer_pfadname(titel)
        ordner.mkdir(parents=True, exist_ok=True)
        ziel = ordner / "Lebenslauf.txt"

        if ziel.exists():
            print(f"  ⏭️  Bereits vorhanden – übersprungen")
            uebersprungen += 1
            continue

        # Richtige Lebenslauf-Vorlage je nach Sprache wählen
        sprache = b.get("sprache", "de")
        if sprache == "en":
            lv_vorlage_pfad = BASIS_PFAD / "lebenslauf_vorlage_en.txt"
            if not lv_vorlage_pfad.exists():
                print(f"  ⚠️  lebenslauf_vorlage_en.txt nicht gefunden – bitte uebersetzer.py ausführen")
                print(f"  ⏭️  Übersprungen")
                uebersprungen += 1
                continue
            print(f"  🌍 Englische Vorlage wird verwendet")
        else:
            lv_vorlage_pfad = BASIS_PFAD / "lebenslauf_vorlage.txt"
            print(f"  🇩🇪 Deutsche Vorlage wird verwendet")
        lv_vorlage = lv_vorlage_pfad.read_text(encoding="utf-8")

        # Relevante Marker bestimmen
        relevante_marker = bestimme_relevante_marker(anpassungen)
        print(f"  📌 Relevante Abschnitte: {', '.join(relevante_marker)}")

        # Vorlage schrittweise anpassen
        angepasste_vorlage = lv_vorlage
        for marker in relevante_marker:
            abschnitt = extrahiere_abschnitt(lv_vorlage, marker)
            if not abschnitt:
                print(f"  ⚠️  Marker '{marker}' nicht in Vorlage gefunden – überspringe")
                continue

            print(f"  🤖 Passe Abschnitt {marker} an...")
            neuer_inhalt = passe_abschnitt_an(
                marker, abschnitt, anpassungen, stelle, client
            )
            if neuer_inhalt:
                angepasste_vorlage = ersetze_abschnitt(
                    angepasste_vorlage, marker, neuer_inhalt
                )
                print(f"  ✅ {marker} angepasst")
            else:
                print(f"  ⚠️  {marker} konnte nicht angepasst werden – Original behalten")

        # Datei schreiben
        with open(ziel, "w", encoding="utf-8") as f:
            f.write(f"Firma:                {firma}\n")
            f.write(f"Stelle:               {titel}\n")
            f.write(f"Score aktuell:        {s_aktuell}%\n")
            f.write(f"Score nach Anpassung: {s_danach}%\n")
            f.write(f"Erstellt am:          {datetime.now().strftime('%d.%m.%Y %H:%M')}\n")
            f.write(f"URL:                  {stelle['url']}\n")
            f.write(f"\nAngepasste Abschnitte: {', '.join(relevante_marker)}\n")
            f.write("\nAnpassungshinweise:\n")
            for a in anpassungen:
                f.write(f"  → {a}\n")
            f.write("\n" + "=" * 60 + "\n\n")
            f.write(angepasste_vorlage)

        print(f"  ✅ Lebenslauf.txt gespeichert: {ziel}")

        # Anschreiben-Vorlage je nach Sprache wählen und Anschreiben generieren
        as_ziel = ordner / "Anschreiben.txt"
        if as_ziel.exists():
            print(f"  ⏭️  Anschreiben.txt bereits vorhanden – übersprungen")
        else:
            if sprache == "en":
                as_vorlage_pfad = BASIS_PFAD / "anschreiben_vorlage_en.txt"
                if not as_vorlage_pfad.exists():
                    as_vorlage_pfad = ANSCHREIBEN_VORLAGE
            else:
                as_vorlage_pfad = ANSCHREIBEN_VORLAGE

            if not as_vorlage_pfad.exists():
                print(f"  ⚠️  anschreiben_vorlage.txt nicht gefunden – Anschreiben übersprungen")
            else:
                as_vorlage = as_vorlage_pfad.read_text(encoding="utf-8")
                generiere_anschreiben(stelle, lv_vorlage, as_vorlage, config, client, ordner)

        erstellt += 1

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Erstellt:     {erstellt}")
    print(f"  Übersprungen: {uebersprungen}")
    print(f"  Ordner:       {BEWERBUNGEN_DIR}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
