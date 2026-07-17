"""
bewertung_variance_test.py  –  Streuungs-Analyse der KI-Bewertung
===================================================================
Bewertet die Top-5-Stellen (nach aktuellem Score) je N-mal erneut mit
identischem Prompt/Input und schreibt alle Durchläufe inkl. Begründungen
in eine JSON-Datei. Dient dazu, die Streuung der Modell-Antworten zu
verstehen (Prompt-Tuning), OHNE die DB oder stellen.json zu verändern.

Nutzung:
  python bewertung_variance_test.py [--n 5] [--out variance_ergebnisse.json]
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    import anthropic as anthropic_lib
except ImportError:
    print("anthropic nicht installiert: pip install anthropic")
    sys.exit(1)

from utils import lade_config, effektiver_score
from bewertung import bewerte_stelle, LEBENSLAUF_PFAD, KI_MODELL

BASIS_PFAD = Path(__file__).parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=5, help="Anzahl Durchläufe pro Stelle")
    parser.add_argument("--out", default="variance_ergebnisse.json", help="Ausgabedatei")
    args = parser.parse_args()

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

    lebenslauf = LEBENSLAUF_PFAD.read_text(encoding="utf-8")
    client = anthropic_lib.Anthropic(api_key=config["api_key"])

    sys.path.insert(0, str(BASIS_PFAD))
    from db import lade_alle_stellen

    stellen = lade_alle_stellen()
    bewertete = [s for s in stellen if s.get("bewertung") and s.get("stellentext")]
    if not bewertete:
        print("ℹ️  Keine bewerteten Stellen mit Stellentext gefunden.")
        return

    top5 = sorted(bewertete, key=lambda s: effektiver_score(s["bewertung"]), reverse=True)[:5]

    print(f"\n{'='*60}")
    print(f"  VARIANZ-TEST  –  Top 5 Stellen je {args.n}x bewerten (Modell: {KI_MODELL})")
    print(f"{'='*60}")

    ergebnisse = []

    for stelle in top5:
        firma = stelle["firma"]
        titel = stelle["titel"]
        stellentext = stelle["stellentext"]

        print(f"\n  {'─'*50}")
        print(f"  {firma}: {titel[:60]}")

        durchlaeufe = []
        for i in range(1, args.n + 1):
            print(f"  🤖 Durchlauf {i}/{args.n}...")
            bewertung = bewerte_stelle(stellentext, lebenslauf, config["prompt"], client)
            if bewertung is None:
                print(f"     ⚠️  Fehlgeschlagen")
                durchlaeufe.append({"durchlauf": i, "fehler": True})
                continue

            score = bewertung.get("score")
            potenzial = bewertung.get("score_potenzial")
            profil = bewertung.get("score_nach_anpassung")
            print(f"     ⭐ Lebenslauf: {score}%  →  Optimierbar: {potenzial}%  →  Profil: {profil}%  |  {bewertung.get('empfehlung','?')}")

            durchlaeufe.append({
                "durchlauf":            i,
                "score":                score,
                "score_potenzial":      potenzial,
                "score_nach_anpassung": profil,
                "effektiver_score":     effektiver_score(bewertung),
                "empfehlung":           bewertung.get("empfehlung"),
                "score_begruendung":    bewertung.get("score_begruendung"),
                "staerken":             bewertung.get("staerken"),
                "luecken":              bewertung.get("luecken"),
                "punkteabzug":          bewertung.get("punkteabzug"),
                "schliessbare_luecken": bewertung.get("schliessbare_luecken"),
                "profil_hinweise":      bewertung.get("profil_hinweise"),
            })

        scores = [d["score"] for d in durchlaeufe if isinstance(d.get("score"), (int, float))]
        streuung = {
            "min":     min(scores) if scores else None,
            "max":     max(scores) if scores else None,
            "spanne":  (max(scores) - min(scores)) if scores else None,
        }
        if streuung["spanne"]:
            print(f"  📊 Score-Spanne über {len(scores)} Durchläufe: {streuung['min']}–{streuung['max']} (Δ{streuung['spanne']})")

        ergebnisse.append({
            "firma":       firma,
            "titel":       titel,
            "url":         stelle["url"],
            "streuung":    streuung,
            "durchlaeufe": durchlaeufe,
        })

    out_pfad = BASIS_PFAD / args.out
    out_pfad.write_text(
        json.dumps({
            "modell":       KI_MODELL,
            "n_durchlaeufe": args.n,
            "erstellt_am":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            "stellen":      ergebnisse,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n{'='*60}")
    print(f"  FERTIG  →  {out_pfad}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
