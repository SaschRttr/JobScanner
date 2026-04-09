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

import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    import anthropic as anthropic_lib
except ImportError:
    print("anthropic nicht installiert: pip install anthropic")
    sys.exit(1)


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD      = Path(__file__).parent
STELLEN_JSON    = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON   = BASIS_PFAD / "bekannte_stellen.json"
STRUKTUREN_JSON = BASIS_PFAD / "strukturen.json"
CONFIG_PFAD     = Path(__file__).parent / "config.txt"

KI_MODELL = "claude-haiku-4-5-20251001"


# =============================================================================
# CONFIG
# =============================================================================

def lade_config() -> dict:
    if not CONFIG_PFAD.exists():
        print(f"❌ config.txt nicht gefunden: {CONFIG_PFAD}")
        sys.exit(1)
    result = {"api_key": ""}
    for zeile in CONFIG_PFAD.read_text(encoding="utf-8").splitlines():
        z = zeile.strip()
        if z.upper().startswith("API_KEY"):
            result["api_key"] = z.split("=", 1)[1].strip()
    return result


# =============================================================================
# JSON-HILFSFUNKTIONEN
# =============================================================================

def lade_json(pfad: Path, standard):
    if pfad.exists():
        try:
            return json.loads(pfad.read_text(encoding="utf-8"))
        except Exception:
            pass
    return standard


def speichere_json(pfad: Path, daten):
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")


def jetzt() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


# =============================================================================
# EXTRAKTION
# =============================================================================

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

Antworte NUR als JSON ohne Markdown:
{{
  "stellentext": "der extrahierte Text hier",
  "start_marker": "exakter Text der ersten relevanten Zeile",
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
            import re
            start = re.search(r'"start_marker"\s*:\s*"(.*?)"(?=\s*,|\s*})', text, re.DOTALL)
            ende  = re.search(r'"ende_marker"\s*:\s*"(.*?)"(?=\s*,|\s*})', text, re.DOTALL)
            sttext = re.search(r'"stellentext"\s*:\s*"(.*?)"(?=\s*,\s*"(?:start|ende))', text, re.DOTALL)
            ergebnis = {
                "stellentext":  sttext.group(1).replace("\\n", "\n") if sttext else rohtext_gekuerzt[:5000],
                "start_marker": start.group(1) if start else None,
                "ende_marker":  ende.group(1)  if ende  else None,
            }

        stellentext = ergebnis.get("stellentext", rohtext_gekuerzt)
        start = ergebnis.get("start_marker")
        ende  = ergebnis.get("ende_marker")

        struktur = None
        if start and ende and len(start) > 5 and len(ende) > 5:
            struktur = {
                "start_marker": start,
                "ende_marker":  ende,
                "gelernt_am":   jetzt(),
            }

        return stellentext, struktur

    except Exception as e:
        print(f"  ⚠️  KI-Extraktion fehlgeschlagen: {e}")
        return rohtext[:5000], None


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    print("\n" + "=" * 60)
    print("  EXTRAKTOR  –  Schritt 2: Stellentext extrahieren")
    print("=" * 60)

    config = lade_config()
    if not config["api_key"]:
        print("❌ Kein API-Key in config.txt")
        sys.exit(1)

    client      = anthropic_lib.Anthropic(api_key=config["api_key"])
    stellen:    list = lade_json(STELLEN_JSON, [])
    bekannte:   dict = lade_json(BEKANNTE_JSON, {})
    strukturen: dict = lade_json(STRUKTUREN_JSON, {})

    if not stellen:
        print("ℹ️  stellen.json ist leer – zuerst scanner.py ausführen.")
        return

    zu_bearbeiten = [
        (i, s) for i, s in enumerate(stellen)
        if bekannte.get(s["url"], {}).get("status") == 2
        and s.get("rohtext")
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

        if start and ende:
            print(f"  ✂️  Bekannte Marker – extrahiere direkt...")
            stellentext = extrahiere_mit_markern(rohtext, start, ende)
            print(f"  ✅ {len(stellentext)} Zeichen extrahiert")
        else:
            print(f"  🤖 Unbekannte Struktur – KI extrahiert...")
            stellentext, neue_struktur = ki_extrahiere_und_lerne(rohtext, dom, client)
            print(f"  ✅ {len(stellentext)} Zeichen extrahiert")
            if neue_struktur:
                strukturen.setdefault(dom, {}).update(neue_struktur)
                print(f"  💾 Struktur für '{dom}' gelernt")

        if stellentext and len(stellentext) > 100:
            stellen[idx]["stellentext"] = stellentext
            bekannte[url]["status"] = 3
            extrahiert += 1
        else:
            print(f"  ⚠️  Extraktion fehlgeschlagen – Status bleibt 2")

        # Zwischenspeichern nach jeder Stelle
        speichere_json(STELLEN_JSON, stellen)
        speichere_json(BEKANNTE_JSON, bekannte)
        speichere_json(STRUKTUREN_JSON, strukturen)

        # Datenbank aktualisieren (nur wenn Extraktion erfolgreich)
        if bekannte[url]["status"] == 3:
            try:
                from db import upsert_stelle
                upsert_stelle({"url": url, "stellentext": stellentext, "status": 3})
            except Exception as e:
                print(f"  ⚠️  Datenbank-Fehler (nicht kritisch): {e}")

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Texte extrahiert: {extrahiert}")
    print(f"  Weiter mit:       python bewertung.py")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
