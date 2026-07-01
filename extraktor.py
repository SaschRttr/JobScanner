"""
extraktor.py  –  Job-Scanner (Schritt 2)
=========================================
Extrahiert aus dem Rohtext den relevanten Stellentext.
Keine Bewertung – das macht bewertung.py.

Status-Übergänge:
  2 (Rohtext vorhanden) → 3 (Stellentext extrahiert)

Ablauf pro Stelle:
  1. Domain in strukturen.json bekannt? → Direkt mit Markern ausschneiden
  2. Unbekannt?                         → KI extrahiert + lernt Struktur

Nutzung:
  python extraktor.py
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import anthropic as anthropic_lib
except ImportError:
    print("anthropic nicht installiert: pip install anthropic")
    sys.exit(1)

from utils import lade_config, lade_json, speichere_json, jetzt, domain, berechne_standort


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD      = Path(__file__).parent
STELLEN_JSON    = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON   = BASIS_PFAD / "bekannte_stellen.json"
STRUKTUREN_JSON = BASIS_PFAD / "strukturen.json"

KI_MODELL = "claude-haiku-4-5-20251001"


# =============================================================================
# EXTRAKTION
# =============================================================================

_STANDORT_RE = re.compile(
    r'(?:standort|arbeitsort|location|arbeitsplatz)\s*[:\|]\s*([A-ZÄÖÜ][a-zäöüA-ZÄÖÜ\s\-]+?)(?:\n|,|<|\|)',
    re.IGNORECASE
)


def ki_extrahiere_standort(text: str, dom: str, client) -> str:
    """Extrahiert nur den Standort aus dem Stellentext – günstiger Fallback."""
    if not text:
        return ""

    # Schnell-Check per Regex — spart KI-Kosten für häufige Muster wie "Standort: Böblingen"
    m = _STANDORT_RE.search(text[:1500])
    if m:
        gefunden = m.group(1).strip()
        if 2 < len(gefunden) < 60:
            return gefunden

    # Anfang + Ende des Texts prüfen: Ort steht oft am Schluss (nach den Aufgaben)
    text_anfang = text[:3000]
    text_ende   = text[-2000:] if len(text) > 3000 else ""
    text_snippet = text_anfang + ("\n...\n" + text_ende if text_ende else "")

    prompt = f"""Aus dem folgenden Stellentext von '{dom}': Welche Stadt/Ort ist der Arbeitsort dieser Stelle?
Es handelt sich um eine Stellenanzeige eines deutschsprachigen oder europäischen Unternehmens.
Antworte NUR mit dem Ort oder der Adresse (z.B. "Böblingen" oder "Herrenberger Str. 130, 71034 Böblingen").
Wenn wirklich keine Stadt erkennbar ist, antworte exakt: UNBEKANNT

=== TEXT ===
{text_snippet}"""
    try:
        antwort = client.messages.create(
            model=KI_MODELL, max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        standort = antwort.content[0].text.strip().strip('"').strip("'")
        # Ablehnen wenn KI einen Erklärungssatz zurückgibt statt Ort/Adresse
        _SATZ_START = re.compile(
            r'^(?:der|die|das|kein|keine|leider|es |ich |im text|der text|der stellentext|in dem)',
            re.IGNORECASE
        )
        if standort.upper() == "UNBEKANNT" or _SATZ_START.match(standort) or len(standort) >= 100:
            return ""
        return standort
    except Exception:
        return ""


def extrahiere_mit_markern(rohtext: str, start_marker: str, ende_marker: str) -> str:
    start = rohtext.find(start_marker)
    if start == -1:
        return rohtext
    ende = rohtext.find(ende_marker, start)
    if ende == -1:
        return rohtext[start:]
    return rohtext[start:ende]


def ki_extrahiere_und_lerne(rohtext: str, dom: str, client) -> tuple[str, dict | None]:
    rohtext_gekuerzt = rohtext[:8000]

    prompt = f"""Du bekommst den rohen Seitentext einer Stellenanzeige von '{dom}'.

Deine Aufgaben:
1. Extrahiere NUR den relevanten Stellentext (Aufgaben, Anforderungen, ggf. Angebot).
   Lass weg: Navigation, Footer, Cookie-Hinweise, "Ähnliche Stellen", Login-Bereiche.
2. Identifiziere einen eindeutigen START-Marker (erste Zeile des relevanten Teils)
   und einen ENDE-Marker (erste Zeile NACH dem relevanten Teil).
   START-Marker darf KEIN Jobtitel sein – wähle eine strukturelle Zeile wie "Deine Aufgaben", "Responsibilities", "Über uns" o.ä.
3. Extrahiere den Arbeitsstandort (Stadt/Ort) der Stelle. Nur die Stadt, kein Land.

Antworte NUR als JSON ohne Markdown:
{{
  "stellentext": "der extrahierte Text hier",
  "arbeitsort": "Böblingen",
  "start_marker": "strukturelle Zeile (kein Jobtitel)",
  "ende_marker": "exakter Text der ersten nicht mehr relevanten Zeile"
}}

=== ROHTEXT ===
{rohtext_gekuerzt}"""

    try:
        antwort = client.messages.create(
            model=KI_MODELL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = antwort.content[0].text.strip()
        text = text.removeprefix("```json").removesuffix("```").strip()
        # Sonderzeichen im stellentext können JSON brechen – nur Marker extrahieren
        try:
            ergebnis = json.loads(text)
        except json.JSONDecodeError:
            start = re.search(r'"start_marker"\s*:\s*"(.*?)"(?=\s*,|\s*})', text, re.DOTALL)
            ende  = re.search(r'"ende_marker"\s*:\s*"(.*?)"(?=\s*,|\s*})', text, re.DOTALL)
            sttext = re.search(r'"stellentext"\s*:\s*"(.*?)"(?=\s*,\s*"(?:start|ende))', text, re.DOTALL)
            ergebnis = {
                "stellentext":  sttext.group(1).replace("\\n", "\n") if sttext else rohtext_gekuerzt[:5000],
                "start_marker": start.group(1) if start else None,
                "ende_marker":  ende.group(1)  if ende  else None,
            }

        stellentext = ergebnis.get("stellentext", rohtext_gekuerzt)
        start    = ergebnis.get("start_marker")
        ende     = ergebnis.get("ende_marker")
        standort = ergebnis.get("arbeitsort") or ""

        _JOBTITEL_MUSTER = re.compile(
            r'\(m/w/d\)|\(m/f/d\)|\(w/m/d\)|\(m/f/x\)|\(human\)|\(m/f\)|\(all genders\)',
            re.IGNORECASE
        )

        struktur = None
        if start and ende and len(start) > 5 and len(ende) > 5:
            if not _JOBTITEL_MUSTER.search(start):
                struktur = {
                    "start_marker": start,
                    "ende_marker":  ende,
                    "gelernt_am":   jetzt(),
                }

        return stellentext, standort, struktur

    except Exception as e:
        print(f"  ⚠️  KI-Extraktion fehlgeschlagen: {e}")
        return rohtext[:5000], "", None


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=None, help="Nur diese URL verarbeiten")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  EXTRAKTOR  –  Schritt 2: Stellentext extrahieren")
    if args.url:
        print(f"  Filter: nur {args.url[:60]}")
    print("=" * 60)

    config         = lade_config()
    erlaubte_orte  = config["erlaubte_standorte"]
    verbotene_orte = config["verbotene_standorte"]
    if not config["api_key"]:
        print("❌ Kein API-Key in config.txt")
        sys.exit(1)

    client      = anthropic_lib.Anthropic(api_key=config["api_key"])
    sys.path.insert(0, str(BASIS_PFAD))
    from db import lade_alle_stellen, upsert_stelle, exportiere_stellen_json, exportiere_bekannte_json, erstelle_schema
    erstelle_schema()
    stellen:    list = lade_alle_stellen()
    strukturen: dict = lade_json(STRUKTUREN_JSON, {})

    if not stellen:
        print("ℹ️  Keine Stellen in DB – zuerst scanner.py ausführen.")
        return

    zu_bearbeiten = [
        (i, s) for i, s in enumerate(stellen)
        if s.get("status") in (1, 2)
        and s.get("rohtext")
        and not s.get("nicht_passend")
        and (args.url is None or s["url"] == args.url)
    ]

    print(f"  {len(zu_bearbeiten)} Stellen zu bearbeiten (Status 2)")

    extrahiert = 0

    for idx, stelle in zu_bearbeiten:
        url     = stelle["url"]
        titel   = stelle["titel"]
        firma   = stelle["firma"]
        rohtext = stelle["rohtext"]
        dom     = domain(url)

        print(f"\n  {'─'*50}")
        print(f"  {firma}: {titel[:60]}")

        dom_struktur = strukturen.get(dom, {})
        start = dom_struktur.get("start_marker")
        ende  = dom_struktur.get("ende_marker")

        arbeitsort = stelle.get("arbeitsort") or ""

        if start and ende:
            print(f"  ✂️  Bekannte Marker – extrahiere direkt...")
            stellentext = extrahiere_mit_markern(rohtext, start, ende)
            print(f"  ✅ {len(stellentext)} Zeichen extrahiert")
            if len(stellentext) < 100:
                print(f"  ⚠️  Marker-Ergebnis zu kurz – KI extrahiert als Fallback...")
                strukturen.pop(dom, None)
                stellentext, arbeitsort_neu, neue_struktur = ki_extrahiere_und_lerne(rohtext, dom, client)
                if arbeitsort_neu and not arbeitsort:
                    arbeitsort = arbeitsort_neu
                print(f"  ✅ {len(stellentext)} Zeichen (KI-Fallback) extrahiert")
                if arbeitsort:
                    print(f"  📍 Arbeitsort: {arbeitsort}")
                if neue_struktur:
                    strukturen[dom] = neue_struktur
                    print(f"  💾 Struktur für '{dom}' aktualisiert")
            elif not arbeitsort:
                arbeitsort = ki_extrahiere_standort(stellentext, dom, client)
                if not arbeitsort:
                    arbeitsort = ki_extrahiere_standort(rohtext, dom, client)
                if arbeitsort:
                    print(f"  📍 Arbeitsort: {arbeitsort}")
        else:
            print(f"  🤖 Unbekannte Struktur – KI extrahiert...")
            stellentext, arbeitsort_neu, neue_struktur = ki_extrahiere_und_lerne(rohtext, dom, client)
            if arbeitsort_neu:
                arbeitsort = arbeitsort_neu
            print(f"  ✅ {len(stellentext)} Zeichen extrahiert")
            if arbeitsort:
                print(f"  📍 Arbeitsort: {arbeitsort}")
            if neue_struktur:
                strukturen.setdefault(dom, {}).update(neue_struktur)
                print(f"  💾 Struktur für '{dom}' gelernt")

        stellen[idx]["arbeitsort"] = arbeitsort or ""
        stellen[idx]["standort"] = berechne_standort(arbeitsort, erlaubte_orte, verbotene_orte)
        if not arbeitsort:
            print(f"  ⚠️  Kein Standort erkannt – muss manuell nachgetragen werden: {firma} – {titel[:60]} ({url})")

        if stellentext and len(stellentext) > 100:
            stellen[idx]["stellentext"] = stellentext
            stellen[idx]["status"] = 3
            extrahiert += 1
            upsert_stelle({"url": url, "stellentext": stellentext,
                           "arbeitsort": stellen[idx]["arbeitsort"],
                           "standort":   stellen[idx]["standort"],
                           "status": 3})
        elif rohtext and len(rohtext) > 100:
            print(f"  ⚠️  Extraktion fehlgeschlagen – verwende Rohtext als Fallback")
            stellen[idx]["stellentext"] = rohtext[:8000]
            stellen[idx]["status"] = 3
            extrahiert += 1
            upsert_stelle({"url": url, "stellentext": rohtext[:8000],
                           "arbeitsort": stellen[idx]["arbeitsort"],
                           "standort":   stellen[idx]["standort"],
                           "status": 3})
        else:
            print(f"  ⚠️  Rohtext zu kurz oder leer – Status auf 1 zurückgesetzt (scanner.py lädt neu)")
            stellen[idx]["rohtext"] = None
            stellen[idx]["status"] = 1
            upsert_stelle({"url": url, "rohtext": None, "status": 1})

        # Zwischenspeichern nach jeder Stelle
        exportiere_stellen_json(STELLEN_JSON)
        exportiere_bekannte_json(BEKANNTE_JSON)
        speichere_json(STRUKTUREN_JSON, strukturen)

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Texte extrahiert:       {extrahiert}")
    print(f"  Weiter mit:             python bewertung.py")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
