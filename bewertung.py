"""
bewertung.py  –  Job-Scanner (Schritt 3)
=========================================
Bewertet alle Stellen mit sauberem Stellentext per KI.
Prompt kommt aus config.txt.

Status-Übergänge:
  3 (Stellentext extrahiert) → 4 (KI-Bewertung vorhanden)

Nutzung:
  python bewertung.py
"""

import json
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

BASIS_PFAD      = Path(__file__).parent
STELLEN_JSON  = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON = BASIS_PFAD / "bekannte_stellen.json"
LEBENSLAUF_PFAD = BASIS_PFAD / "lebenslauf.txt"
CONFIG_PFAD   = Path(__file__).parent / "config.txt"

KI_MODELL = "claude-haiku-4-5-20251001"


# =============================================================================
# CONFIG
# =============================================================================

def lade_config() -> dict:
    if not CONFIG_PFAD.exists():
        print(f"❌ config.txt nicht gefunden: {CONFIG_PFAD}")
        sys.exit(1)

    result = {"api_key": "", "prompt": "", "verbotene_standorte": []}
    zeilen = CONFIG_PFAD.read_text(encoding="utf-8").splitlines()
    aktiver_abschnitt = None
    puffer = []

    for zeile in zeilen:
        z = zeile.strip()

        if z.startswith("[\\") and z.endswith("]"):
            abschnitt = z[2:-1].lower()
            if abschnitt == "prompt":
                result["prompt"] = "\n".join(puffer).strip()
            aktiver_abschnitt = None
            puffer = []
            continue

        if z.startswith("[") and z.endswith("]") and not z.startswith("[\\"):
            aktiver_abschnitt = z[1:-1].lower()
            puffer = []
            continue

        if aktiver_abschnitt is None:
            if z.startswith("#") or not z:
                continue
            if z.upper().startswith("API_KEY"):
                result["api_key"] = z.split("=", 1)[1].strip()
            continue

        if aktiver_abschnitt == "prompt":
            puffer.append(zeile)
        elif aktiver_abschnitt == "verbotene_standorte":
            if z and not z.startswith("#"):
                result["verbotene_standorte"].append(z.lower())

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


# =============================================================================
# KI-BEWERTUNG
# =============================================================================

def bewerte_stelle(stellentext: str, lebenslauf: str, prompt_vorlage: str, client) -> dict | None:
    prompt = prompt_vorlage.replace("{stellentext}", stellentext[:5000])
    prompt = prompt.replace("{lebenslauf}", lebenslauf)

    try:
        antwort = client.messages.create(
            model=KI_MODELL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = antwort.content[0].text.strip()
        text = text.removeprefix("```json").removesuffix("```").strip()
        # Nur JSON-Block extrahieren falls extra Text vorhanden
        start = text.find("{")
        ende  = text.rfind("}") + 1
        if start != -1 and ende > start:
            text = text[start:ende]
        ergebnis = json.loads(text)
        # score_aktuell als Hauptscore setzen (Rückwärtskompatibilität)
        if "score_aktuell" in ergebnis and "score" not in ergebnis:
            ergebnis["score"] = ergebnis["score_aktuell"]
        ergebnis["empfehlung"] = "bewerben" if ergebnis.get("score", 0) >= 70 else "nicht bewerben"
        # Sprache des Stellentexts erkennen
        if "sprache" not in ergebnis:
            ergebnis["sprache"] = "de"
        return ergebnis
    except json.JSONDecodeError as e:
        print(f"  ⚠️  JSON-Parse-Fehler: {e}")
        return None
    except Exception as e:
        print(f"  ❌ API-Fehler: {e}")
        return None


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    print("\n" + "=" * 60)
    print("  BEWERTUNG  –  Schritt 3: KI-Bewertung")
    print("=" * 60)

    config = lade_config()

    if not config["api_key"]:
        print("❌ Kein API-Key in config.txt")
        sys.exit(1)

    if not config["prompt"]:
        print("❌ Kein [prompt] in config.txt")
        sys.exit(1)

    if not LEBENSLAUF_PFAD.exists():
        print(f"❌ Lebenslauf nicht gefunden: {LEBENSLAUF_PFAD}")
        sys.exit(1)

    lebenslauf  = LEBENSLAUF_PFAD.read_text(encoding="utf-8")
    client      = anthropic_lib.Anthropic(api_key=config["api_key"])
    stellen:  list = lade_json(STELLEN_JSON, [])
    bekannte: dict = lade_json(BEKANNTE_JSON, {})

    if not stellen:
        print("ℹ️  stellen.json ist leer – zuerst scanner.py ausführen.")
        return

    verbotene = config["verbotene_standorte"]

    def standort_ok(s: dict) -> bool:
        standort = (s.get("standort") or "").lower()
        if not standort or standort == "ok":
            return True
        return not any(v in standort for v in verbotene)

    # Stellen mit verbotenem Standort explizit als nicht_passend markieren
    zu_markieren = [
        (i, s) for i, s in enumerate(stellen)
        if bekannte.get(s["url"], {}).get("status") == 3
        and s.get("stellentext")
        and not standort_ok(s)
        and not s.get("nicht_passend")
    ]
    if zu_markieren:
        print(f"  {len(zu_markieren)} Stellen wegen verbotenem Standort → nicht_passend:")
        for idx, stelle in zu_markieren:
            stellen[idx]["nicht_passend"] = True
            bekannte[stelle["url"]]["nicht_passend"] = True
            print(f"    🚫 {stelle['firma']}: {stelle['titel'][:60]} ({stelle.get('standort','')})")
        speichere_json(STELLEN_JSON, stellen)
        speichere_json(BEKANNTE_JSON, bekannte)

    zu_bearbeiten = [
        (i, s) for i, s in enumerate(stellen)
        if bekannte.get(s["url"], {}).get("status") == 3
        and s.get("stellentext")
        and standort_ok(s)
        and not s.get("nicht_passend")
    ]

    print(f"  {len(zu_bearbeiten)} Stellen zu bewerten (Status 3)")

    bewertet = 0

    for idx, stelle in zu_bearbeiten:
        url         = stelle["url"]
        titel       = stelle["titel"]
        firma       = stelle["firma"]
        stellentext = stelle["stellentext"]

        print(f"\n  {'─'*50}")
        print(f"  {firma}: {titel[:60]}")
        print(f"  🤖 Bewerte...")

        bewertung = bewerte_stelle(stellentext, lebenslauf, config["prompt"], client)

        if bewertung:
            stellen[idx]["bewertung"] = bewertung
            bekannte[url]["status"]   = 4
            score = bewertung.get("score", 0)
            empf  = bewertung.get("empfehlung", "?")
            print(f"  ⭐ Score: {score}%  |  {empf.upper()}")
            bewertet += 1
        else:
            print(f"  ⚠️  Bewertung fehlgeschlagen")

        # Zwischenspeichern nach jeder Stelle
        speichere_json(STELLEN_JSON, stellen)
        speichere_json(BEKANNTE_JSON, bekannte)

        # Datenbank aktualisieren
        try:
            from db import upsert_stelle, upsert_bewertung
            upsert_stelle({"url": url, "status": 4})
            if bewertung:
                upsert_bewertung(url, bewertung)
        except Exception as e:
            print(f"  ⚠️  Datenbank-Fehler (nicht kritisch): {e}")

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Stellen bewertet: {bewertet}")
    print(f"{'='*60}\n")

    # Top 5 ausgeben
    bewertete = [s for s in stellen if s.get("bewertung")]
    if bewertete:
        top = sorted(bewertete, key=lambda s: s["bewertung"].get("score", 0), reverse=True)[:5]
        print("  🏆 TOP 5:")
        for s in top:
            score = s["bewertung"].get("score", 0)
            empf  = s["bewertung"].get("empfehlung", "?")
            print(f"     {score:3d}%  [{empf[:5]}]  {s['firma']}: {s['titel'][:50]}")
        print()


if __name__ == "__main__":
    main()
