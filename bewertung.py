"""
bewertung.py  –  Job-Scanner (Schritt 3)
=========================================
Bewertet alle Stellen mit sauberem Stellentext per KI.
Prompt kommt aus config.txt.

Status-Übergänge:
  3 (Stellentext extrahiert) → 4 (KI-Bewertung ≥ 70 %, bewerben)
                             → 5 (KI-Bewertung < 70 %, nicht bewerben)

Nutzung:
  python bewertung.py
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import anthropic as anthropic_lib
except ImportError:
    print("anthropic nicht installiert: pip install anthropic")
    sys.exit(1)

from utils import lade_config, standort_verboten


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD      = Path(__file__).parent
STELLEN_JSON    = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON   = BASIS_PFAD / "bekannte_stellen.json"
LEBENSLAUF_PFAD = BASIS_PFAD / "lebenslauf.txt"

KI_MODELL = "claude-haiku-4-5-20251001"


# =============================================================================
# KI-BEWERTUNG
# =============================================================================

def _parse_json_antwort(text: str) -> dict:
    text = text.removeprefix("```json").removesuffix("```").strip()
    start = text.find("{")
    ende  = text.rfind("}") + 1
    if start != -1 and ende > start:
        text = text[start:ende]
    return json.loads(text)


def bewerte_stelle(stellentext: str, lebenslauf: str, prompt_vorlage: str, client) -> dict | None:
    prompt = prompt_vorlage.replace("{stellentext}", stellentext[:5000])
    prompt = prompt.replace("{lebenslauf}", lebenslauf)

    for versuch in range(1, 4):
        try:
            antwort = client.messages.create(
                model=KI_MODELL,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            text = antwort.content[0].text.strip()
            ergebnis = _parse_json_antwort(text)
            if "score_aktuell" in ergebnis and "score" not in ergebnis:
                ergebnis["score"] = ergebnis["score_aktuell"]
            ergebnis["empfehlung"] = "bewerben" if ergebnis.get("score", 0) >= 70 else "nicht bewerben"
            if "sprache" not in ergebnis:
                ergebnis["sprache"] = "de"
            return ergebnis
        except json.JSONDecodeError as e:
            print(f"  ⚠️  JSON-Parse-Fehler (Versuch {versuch}/3): {e}")
            if versuch == 3:
                return None
        except Exception as e:
            print(f"  ❌ API-Fehler: {e}")
            return None
    return None


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=None, help="Nur diese URL bewerten")
    parser.add_argument("--firma", default=None, help="Nur diese Firma bewerten")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  BEWERTUNG  –  Schritt 3: KI-Bewertung")
    if args.url:
        print(f"  Filter: nur {args.url[:60]}")
    if args.firma:
        print(f"  Filter: nur Firma '{args.firma}'")
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
    sys.path.insert(0, str(BASIS_PFAD))
    from db import lade_alle_stellen, upsert_stelle, upsert_bewertung, exportiere_stellen_json, exportiere_bekannte_json, erstelle_schema
    erstelle_schema()
    stellen: list = lade_alle_stellen()

    if not stellen:
        print("ℹ️  Keine Stellen in DB – zuerst scanner.py ausführen.")
        return

    verbotene = config["verbotene_standorte"]

    def standort_ok(s: dict) -> bool:
        arbeitsort = s.get("arbeitsort") or ""
        if not arbeitsort:
            return True
        # standort_verboten normalisiert Umlaute (München == Muenchen)
        return not standort_verboten(arbeitsort, verbotene)

    # Stellen mit verbotenem Standort explizit als nicht_passend markieren
    zu_markieren = [
        (i, s) for i, s in enumerate(stellen)
        if s.get("status") == 3
        and s.get("stellentext")
        and not standort_ok(s)
        and not s.get("nicht_passend")
        and (args.firma is None or s.get("firma") == args.firma)
    ]
    if zu_markieren:
        print(f"  {len(zu_markieren)} Stellen wegen verbotenem Standort → nicht_passend:")
        for idx, stelle in zu_markieren:
            stellen[idx]["nicht_passend"] = True
            np_grund = f"Verbotener Standort: {stelle.get('arbeitsort', '?')}"
            stellen[idx]["nicht_passend_grund"] = np_grund
            upsert_stelle({"url": stelle["url"], "nicht_passend": True, "nicht_passend_grund": np_grund})
            print(f"    🚫 {stelle['firma']}: {stelle['titel'][:60]} ({stelle.get('arbeitsort','')})")
        exportiere_stellen_json(STELLEN_JSON)
        exportiere_bekannte_json(BEKANNTE_JSON)

    # Status-3 Stellen bleiben bei nicht_passend=True einfach auf Status 3 stehen
    # (Status 9 ist für per HTTP bestätigte Vergaben reserviert – siehe vergaben_check.py).
    # nicht_passend hält sie schon aus allen aktiven Report-Listen raus; sobald
    # nicht_passend wieder False wird (z.B. nach Whitelist-Änderung), landen sie
    # automatisch wieder in zu_bearbeiten unten und werden neu bewertet.

    zu_bearbeiten = [
        (i, s) for i, s in enumerate(stellen)
        if s.get("status") == 3
        and s.get("stellentext")
        and standort_ok(s)
        and not s.get("nicht_passend")
        and (args.url is None or s["url"] == args.url)
        and (args.firma is None or s.get("firma") == args.firma)
    ]

    print(f"  {len(zu_bearbeiten)} Stellen zu bewerten (Status 3)")

    # Diagnose: zeige noch verbliebene status=3 Stellen die übersprungen werden
    nicht_bewertet = [
        s for s in stellen
        if s.get("status") == 3 and s not in [st for _, st in zu_bearbeiten]
    ]
    if nicht_bewertet and len(zu_bearbeiten) == 0:
        print(f"  ℹ️  {len(nicht_bewertet)} Status-3 Stellen werden übersprungen:")
        for s in nicht_bewertet:
            if not s.get("stellentext"):
                grund = "⚠️  kein Stellentext"
            else:
                grund = f"Arbeitsort: {s.get('arbeitsort', '?')}"
            print(f"     {s['firma']}: {s['titel'][:55]} → {grund}")

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
            score = bewertung.get("score", 0)
            empf  = bewertung.get("empfehlung", "?")
            neuer_status = 4 if score >= 70 else 5
            stellen[idx]["status"] = neuer_status
            print(f"  ⭐ Score: {score}%  |  {empf.upper()}  →  Status {neuer_status}")
            bewertet += 1
            upsert_stelle({"url": url, "status": neuer_status})
            upsert_bewertung(url, bewertung)
        else:
            print(f"  ⚠️  Bewertung fehlgeschlagen")

    # JSON-Spiegel einmal am Ende aktualisieren (DB ist pro Stelle schon aktuell)
    if zu_bearbeiten:
        exportiere_stellen_json(STELLEN_JSON)
        exportiere_bekannte_json(BEKANNTE_JSON)

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
