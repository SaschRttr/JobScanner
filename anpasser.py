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

sys.path.insert(0, str(Path(__file__).parent))
import db
from utils import (lade_config, sicherer_pfadname,
                   extrahiere_abschnitt, ersetze_abschnitt)


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD           = Path(__file__).parent
VORLAGE_PFAD         = BASIS_PFAD / "lebenslauf_vorlage.txt"
ANSCHREIBEN_VORLAGE  = BASIS_PFAD / "anschreiben_vorlage.txt"
BEWERBUNGEN_DIR      = BASIS_PFAD / "bewerbungen"

# claude-sonnet-4-20250514 wurde am 15.06.2026 abgeschaltet (HTTP 404) –
# dadurch schlugen Anschreiben-Generierung und Lebenslauf-Anpassung still fehl.
KI_MODELL  = "claude-sonnet-5"
MIN_SCORE  = 70


# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def get_score(s: dict) -> int:
    # Profil-Score ("lohnt sich die Bewerbung?") mit Fallback auf den Lebenslauf-Score
    b = s.get("bewertung") or {}
    profil = b.get("score_nach_anpassung")
    if isinstance(profil, (int, float)):
        return profil
    return b.get("score_aktuell", b.get("score", 0))


def _antwort_text(antwort) -> str:
    """Extrahiert den Text-Block aus einer API-Antwort (überspringt thinking-Blöcke)."""
    return next((b.text for b in antwort.content if b.type == "text"), "").strip()


_GELEAKTER_MARKER = re.compile(r"^\s*=+\s*AKTUELLER\s+ABSCHNITT.*?=+\s*\n+", re.IGNORECASE)


def entferne_geleakten_marker(text: str) -> str:
    """
    Entfernt eine geleakte Prompt-Marker-Zeile (z.B. '=== AKTUELLER ABSCHNITT (angepasst) ==='),
    falls die KI sie trotz Anweisung an den Anfang der Antwort gesetzt hat. So wird eine
    ansonsten valide Anpassung nicht wegen einer einzelnen kosmetischen Zeile verworfen.
    """
    return _GELEAKTER_MARKER.sub("", text, count=1).strip()


_RUECKFRAGE_PHRASEN = (
    "ich benötige", "ich brauche", "um diesen abschnitt",
    "bevor ich", "ich kann diesen abschnitt nicht",
)


def ist_valide_abschnitt(text: str) -> bool:
    """
    Prüft ob eine KI-Antwort wie ein echter Lebenslauf-Abschnitt aussieht,
    statt einer Rückfrage oder einem Meta-Kommentar (z.B. wenn die KI laut
    Prompt-Regel keine Fakten erfinden will und stattdessen nachfragt).

    Das Wort "abschnitt" (case-insensitive) ist der zuverlässigste Indikator:
    die KI referenziert damit den Prompt/sich selbst ("dieser Abschnitt",
    "der Original-Abschnitt", "=== AKTUELLER ABSCHNITT ===" o.ä.) – in einem
    echten Lebenslauf-Abschnitt kommt dieses Wort nie vor.
    """
    text_lower = text.lower()
    if "?" in text or "abschnitt" in text_lower or "===" in text:
        return False
    return not any(text_lower.startswith(p) for p in _RUECKFRAGE_PHRASEN)


def pruefe_auf_erfindungen(lebenslauf_gesamt: str, angepasst: str, client) -> list:
    """
    Lässt die KI in einem separaten Aufruf gegenprüfen, ob der angepasste
    Abschnitt Fähigkeiten/Tools/Erfahrungen nennt, die NIRGENDWO im GESAMTEN
    Lebenslauf vorkommen oder daraus ableitbar sind (nicht nur im gerade
    bearbeiteten Abschnitt – eine Fähigkeit, die z.B. nur bei einer anderen
    Stelle dokumentiert ist, aber legitim in "Fähigkeiten" auftauchen soll,
    darf nicht als Erfindung gelten). Reine Keyword-Heuristiken (z.B. auf neue
    Akronyme) übersehen Erfindungen ohne Akronym (z.B. "Python-based
    instrument control") und lösen bei harmlosen Wörtern wie "CV" Fehlalarm
    aus – ein zweiter, gezielter KI-Check ist robuster.
    Gibt eine Liste erkannter Erfindungen zurück (leer = nichts Neues erfunden).
    Bei API-Fehlern wird [] zurückgegeben (blockiert die Anpassung nicht).
    """
    prompt = f"""Vergleiche LEBENSLAUF (gesamter Lebenslauf einer Person) und
ANGEPASST (ein neu formulierter Abschnitt aus diesem Lebenslauf) unten. Liste
jede konkrete Fähigkeit, Technologie, jedes Tool, Protokoll oder jede
Erfahrung im ANGEPASSTEN Text auf, die NIRGENDWO im LEBENSLAUF vorkommt und
auch nicht eindeutig daraus ableitbar ist.

Antworte NUR als JSON-Array von kurzen Strings (leeres Array [] falls nichts
Neues erfunden wurde), ohne Markdown, ohne Kommentar.

=== LEBENSLAUF ===
{lebenslauf_gesamt}

=== ANGEPASST ===
{angepasst}"""

    try:
        antwort = client.messages.create(
            model=KI_MODELL,
            max_tokens=512,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )
        text = re.sub(r"```json|```", "", _antwort_text(antwort)).strip()
        start, ende = text.find("["), text.rfind("]") + 1
        if start != -1 and ende > start:
            text = text[start:ende]
        erfindungen = json.loads(text)
        return erfindungen if isinstance(erfindungen, list) else []
    except Exception as e:
        print(f"  ⚠️  Verifikations-Check fehlgeschlagen (wird ignoriert): {e}")
        return []


_MARKER_KEYWORDS = {
    "KOMPETENZPROFIL":   ["profil", "summary", "einleitung", "zusammenfassung",
                          "kompetenz", "usp", "stärken"],
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


def bestimme_relevante_marker(anpassungen: list) -> list:
    """
    Leitet aus den Anpassungshinweisen ab welche Abschnitte geändert werden müssen.
    Gibt eine Liste von Marker-Namen zurück.
    """
    relevante = set()
    anpassungen_lower = " ".join(anpassungen).lower()

    for marker, schlagworte in _MARKER_KEYWORDS.items():
        if any(s in anpassungen_lower for s in schlagworte):
            relevante.add(marker)

    # Fallback: wenn keine Zuordnung → Kompetenzprofil und Fähigkeiten
    if not relevante:
        relevante = {"KOMPETENZPROFIL", "FAEHIGKEITEN"}

    return list(relevante)


def filtere_anpassungen_fuer_marker(marker: str, anpassungen: list) -> list:
    """
    Wählt aus allen Anpassungshinweisen nur die aus, die inhaltlich zu diesem
    Marker passen (gleiche Schlagwörter wie bestimme_relevante_marker). Ohne
    diesen Filter sieht die KI bei JEDEM Abschnitt ALLE Hinweise gleichzeitig
    und kann z.B. einen für FAEHIGKEITEN gedachten Hinweis (z.B. "ISO 26262 in
    Fähigkeiten zusammenfassen") fälschlich in eine STELLE_X_AUFGABEN-Anpassung
    einweben, obwohl die Erfahrung dort laut Lebenslauf gar nicht stattfand
    (Cross-Contamination zwischen Abschnitten/Stellen).
    Fallback: alle Hinweise, falls keiner zum Marker passt (sollte kaum
    vorkommen, da der Marker überhaupt nur wegen mind. einem passenden Hinweis
    gewählt wird).
    """
    schlagworte = _MARKER_KEYWORDS.get(marker)
    if not schlagworte:
        return anpassungen

    gefiltert = [a for a in anpassungen if any(s in a.lower() for s in schlagworte)]
    return gefiltert or anpassungen


# =============================================================================
# KI: ABSCHNITT ANPASSEN
# =============================================================================

def passe_abschnitt_an(
    abschnitt_name: str,
    abschnitt_inhalt: str,
    anpassungen: list,
    stelle: dict,
    client,
    sprache: str = "de",
) -> str | None:
    """Lässt die KI einen einzelnen Abschnitt anpassen."""
    anpassungen_text = "\n".join(f"- {a}" for a in anpassungen)
    sprache_name = "Englisch" if sprache == "en" else "Deutsch"

    prompt = f"""Du bist ein professioneller Bewerbungsberater.

Passe diesen Abschnitt des Lebenslaufs für die folgende Stelle an:

Firma:    {stelle['firma']}
Stelle:   {stelle['titel']}
Abschnitt: {abschnitt_name}

Anpassungshinweise:
{anpassungen_text}

Regeln:
- Verändere NUR was die Anpassungshinweise für diesen Abschnitt ({abschnitt_name}) verlangen
- Falls ein Anpassungshinweis erkennbar einen ANDEREN Abschnitt oder eine andere
  Stelle betrifft (z.B. eine Erfahrung die laut Hinweis nur bei einem anderen
  Arbeitgeber/einer anderen Position stattfand), ignoriere ihn hier – wende ihn
  NICHT auf diesen Abschnitt an
- Erfinde KEINE neuen Fakten oder Erfahrungen
- Nenne eine konkrete Technologie/Methode/Tool/Tätigkeit NUR, wenn sie bereits
  im Original-Abschnitt unten vorkommt oder direkt daraus ableitbar ist – auch
  wenn ein Anpassungshinweis sie ohne Einschränkung vorschlägt. Anpassungshinweise
  können ungenau oder spekulativ sein und sind KEIN Beleg für tatsächliche Erfahrung
- Falls du eine Anpassung ohne Fakten zu erfinden nicht sauber umsetzen kannst,
  gib GENAU den Original-Abschnitt unverändert zurück
- Stelle KEINE Rückfragen, schreibe KEINE Meta-Kommentare, KEINE Erklärungen
  außerhalb des eigentlichen Abschnitt-Inhalts
- Antworte ausschließlich auf {sprache_name} – der Original-Abschnitt ist auf {sprache_name}
- Behalte Formatierung (Bullet-Zeichen, Einrückung) exakt bei
- Gib NUR den angepassten Abschnitt zurück, ohne Marker-Zeilen, ohne Kommentar

=== AKTUELLER ABSCHNITT ===
{abschnitt_inhalt}"""

    try:
        antwort = client.messages.create(
            model=KI_MODELL,
            max_tokens=2048,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )
        text = entferne_geleakten_marker(_antwort_text(antwort))
        return text or None
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

_MONTHS_EN = {
    1: "January", 2: "February", 3: "March",    4: "April",
    5: "May",     6: "June",     7: "July",      8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

_DUMMY_FIRMA = {
    "firmenname":      "[Firmenname]",
    "abteilung":       "[Abteilung]",
    "ansprechpartner": "[Ansprechpartner]",
    "strasse":         "[Straße]",
    "plz":             "[PLZ]",
    "ort":             "[Ort]",
}


def lade_firma_anschreiben(firma_name: str, config: dict) -> dict:
    """
    Sucht die Adressdaten aus config["firma_anschreiben"] (Abschnitt
    [firma_anschreiben] der config.txt) per Teilstring-Match (case-insensitive).
    Gibt Dummy-Werte zurück wenn kein Eintrag passt.
    """
    firma_lower = firma_name.lower()
    for name, daten in config.get("firma_anschreiben", {}).items():
        eintrag_lower = name.lower()
        if eintrag_lower in firma_lower or firma_lower in eintrag_lower:
            return daten
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
    sprache: str = "de",
) -> str | None:
    """
    Erzeugt Anschreiben.txt im Zielordner.
    - EMPFAENGER, DATUM, BETREFF werden direkt befüllt (kein KI)
    - Alle Absätze in einem einzigen KI-Aufruf via config["anschreiben_prompt"]
    - Marker-Zeilen werden aus der gespeicherten Datei entfernt
    Gibt None zurück wenn erfolgreich, sonst eine Fehlermeldung.
    """
    prompt_key = "anschreiben_prompt_en" if sprache == "en" else "anschreiben_prompt"
    if not config.get(prompt_key):
        # Fallback to German prompt if English one is missing
        prompt_key = "anschreiben_prompt"
    if not config.get(prompt_key):
        print(f"  ⚠️  Kein [anschreiben_prompt] in config.txt – Anschreiben übersprungen")
        return "Kein [anschreiben_prompt] in config.txt"

    firma_d = lade_firma_anschreiben(stelle.get("firma", ""), config)

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
    if sprache == "en":
        datum = f"Stuttgart, {_MONTHS_EN[heute.month]} {heute.day}, {heute.year}"
    else:
        datum = f"Stuttgart, {heute.day}. {_MONATE[heute.month]} {heute.year}"

    # --- BETREFF direkt befüllen ---
    betreff = stelle.get("titel", "")

    # --- Aufgaben-Pool aus Lebenslauf-Vorlage ---
    aufgaben_pool = extrahiere_aufgaben_pool(lebenslauf_vorlage)
    if not aufgaben_pool:
        print(f"  ⚠️  Kein Aufgaben-Pool in lebenslauf_vorlage – Anschreiben übersprungen")
        return "Kein Aufgaben-Pool in der Lebenslauf-Vorlage gefunden"

    # --- KI-Prompt aus config befüllen ---
    b = stelle.get("bewertung") or {}
    staerken_text      = "\n".join(f"- {s}" for s in b.get("staerken", []))
    score_begruendung  = b.get("score_begruendung", "")

    prompt = config[prompt_key]
    prompt = prompt.replace("{firma}",             stelle.get("firma", ""))
    prompt = prompt.replace("{titel}",             stelle.get("titel", ""))
    prompt = prompt.replace("{staerken}",          staerken_text or "(keine Angaben)")
    prompt = prompt.replace("{score_begruendung}", score_begruendung or "(keine Angaben)")
    prompt = prompt.replace("{aufgaben_pool}",     aufgaben_pool)

    # --- KI-Aufruf (1 Retry bei kaputtem JSON oder Rückfrage/Meta-Text) ---
    felder = ("ANREDE", "ABSATZ_1", "ABSATZ_2_INTRO", "ABSATZ_2_BULLETS", "ABSATZ_3", "ABSATZ_4")
    inhalt = None
    letzter_fehler = ""
    for versuch in (1, 2):
        try:
            antwort = client.messages.create(
                model=KI_MODELL,
                max_tokens=2048,
                thinking={"type": "disabled"},
                messages=[{"role": "user", "content": prompt}],
            )
            text = re.sub(r"```json|```", "", _antwort_text(antwort)).strip()
            # Nur das JSON-Objekt parsen, falls die KI Text drumherum schreibt
            start, ende = text.find("{"), text.rfind("}") + 1
            if start != -1 and ende > start:
                text = text[start:ende]
            geparst = json.loads(text)

            ungueltige_felder = [
                f for f in felder if not ist_valide_abschnitt(geparst.get(f, ""))
            ]
            if ungueltige_felder:
                letzter_fehler = f"KI-Antwort wirkte wie Rückfrage/Meta-Text in: {', '.join(ungueltige_felder)}"
                print(f"  ⚠️  {letzter_fehler} (Versuch {versuch}/2)")
                continue

            inhalt = geparst
            break
        except json.JSONDecodeError as e:
            letzter_fehler = f"KI-Antwort war kein gültiges JSON: {e}"
            print(f"  ⚠️  {letzter_fehler} (Versuch {versuch}/2)")
        except Exception as e:
            letzter_fehler = f"API-Fehler: {e}"
            print(f"  ❌ API-Fehler Anschreiben: {e}")
            break
    if inhalt is None:
        return letzter_fehler or "Anschreiben-Generierung fehlgeschlagen"

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
    return None


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

    stellen = db.lade_alle_stellen()
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

    relevante_marker      = bestimme_relevante_marker(anpassungen)
    angepasste_vorlage    = lv_vorlage
    nicht_angepasste_marker = []

    for marker in relevante_marker:
        abschnitt = extrahiere_abschnitt(lv_vorlage, marker)
        if not abschnitt:
            continue
        marker_anpassungen = filtere_anpassungen_fuer_marker(marker, anpassungen)
        neuer_inhalt = passe_abschnitt_an(marker, abschnitt, marker_anpassungen, stelle, client, sprache=sprache)
        erfundene = []
        if neuer_inhalt and ist_valide_abschnitt(neuer_inhalt):
            erfundene = pruefe_auf_erfindungen(lv_vorlage, neuer_inhalt, client)
        if neuer_inhalt and ist_valide_abschnitt(neuer_inhalt) and not erfundene:
            angepasste_vorlage = ersetze_abschnitt(angepasste_vorlage, marker, neuer_inhalt)
        else:
            if erfundene:
                print(f"  ⚠️  {marker}: KI-Antwort enthält unbelegte Angaben {erfundene} – Original behalten")
            elif neuer_inhalt:
                print(f"  ⚠️  {marker}: KI-Antwort wirkte wie Rückfrage/Meta-Text – Original behalten")
            nicht_angepasste_marker.append(marker)

    with open(ziel, "w", encoding="utf-8") as f:
        f.write(f"Firma:                {firma}\n")
        f.write(f"Stelle:               {titel}\n")
        f.write(f"Score aktuell:        {s_aktuell}%\n")
        f.write(f"Score nach Anpassung: {s_danach}%\n")
        if nicht_angepasste_marker:
            f.write(f"Nicht angepasst (Original behalten): {', '.join(nicht_angepasste_marker)}\n")
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
    anschreiben_fehler = None
    if not as_ziel.exists():
        if sprache == "en":
            as_vorlage_pfad = BASIS_PFAD / "anschreiben_vorlage_en.txt"
            if not as_vorlage_pfad.exists():
                as_vorlage_pfad = ANSCHREIBEN_VORLAGE
        else:
            as_vorlage_pfad = ANSCHREIBEN_VORLAGE

        if not as_vorlage_pfad.exists():
            print(f"  ⚠️  anschreiben_vorlage.txt nicht gefunden – Anschreiben übersprungen")
            anschreiben_fehler = "anschreiben_vorlage.txt nicht gefunden"
        else:
            as_vorlage = as_vorlage_pfad.read_text(encoding="utf-8")
            anschreiben_fehler = generiere_anschreiben(
                stelle, lv_vorlage, as_vorlage, config, client, ordner, sprache=sprache)

    return {"ok": True, "pfad": str(ziel), "anschreiben_fehler": anschreiben_fehler, "sprache": sprache}


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

    stellen  = db.lade_alle_stellen()
    client   = anthropic_lib.Anthropic(api_key=config["api_key"])

    if not stellen:
        print("ℹ️  Keine Stellen in der Datenbank – zuerst scanner.py ausführen.")
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
        nicht_angepasste_marker = []
        for marker in relevante_marker:
            abschnitt = extrahiere_abschnitt(lv_vorlage, marker)
            if not abschnitt:
                print(f"  ⚠️  Marker '{marker}' nicht in Vorlage gefunden – überspringe")
                continue

            print(f"  🤖 Passe Abschnitt {marker} an...")
            marker_anpassungen = filtere_anpassungen_fuer_marker(marker, anpassungen)
            neuer_inhalt = passe_abschnitt_an(
                marker, abschnitt, marker_anpassungen, stelle, client, sprache=sprache
            )
            erfundene = []
            if neuer_inhalt and ist_valide_abschnitt(neuer_inhalt):
                erfundene = pruefe_auf_erfindungen(lv_vorlage, neuer_inhalt, client)
            if neuer_inhalt and ist_valide_abschnitt(neuer_inhalt) and not erfundene:
                angepasste_vorlage = ersetze_abschnitt(
                    angepasste_vorlage, marker, neuer_inhalt
                )
                print(f"  ✅ {marker} angepasst")
            else:
                if erfundene:
                    print(f"  ⚠️  {marker}: KI-Antwort enthält unbelegte Angaben {erfundene} – Original behalten")
                elif neuer_inhalt:
                    print(f"  ⚠️  {marker}: KI-Antwort wirkte wie Rückfrage/Meta-Text – Original behalten")
                else:
                    print(f"  ⚠️  {marker} konnte nicht angepasst werden – Original behalten")
                nicht_angepasste_marker.append(marker)

        # Datei schreiben
        with open(ziel, "w", encoding="utf-8") as f:
            f.write(f"Firma:                {firma}\n")
            f.write(f"Stelle:               {titel}\n")
            f.write(f"Score aktuell:        {s_aktuell}%\n")
            f.write(f"Score nach Anpassung: {s_danach}%\n")
            if nicht_angepasste_marker:
                f.write(f"Nicht angepasst (Original behalten): {', '.join(nicht_angepasste_marker)}\n")
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
                generiere_anschreiben(stelle, lv_vorlage, as_vorlage, config, client, ordner, sprache=sprache)

        erstellt += 1

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Erstellt:     {erstellt}")
    print(f"  Übersprungen: {uebersprungen}")
    print(f"  Ordner:       {BEWERBUNGEN_DIR}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
