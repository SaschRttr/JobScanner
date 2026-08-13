"""
export_ablehnungen.py  –  Export abgelehnter Stellen für Prompt-Verbesserung
============================================================================
Erzeugt eine JSON-Datei mit allen Stellen, die einen Ablehnungsgrund haben –
sowohl manuell eingetragene (``nicht_beworben_grund``) als auch automatisch
erkannte (``nicht_passend_grund``: Ausschlussbegriff/Standort).

Pro Stelle werden nur die für die Prompt-Verbesserung relevanten Felder
ausgegeben: Joblink, Stellenbeschreibung (extrahierter Stellentext) und Grund.

Aufruf:
    python export_ablehnungen.py                 # -> ablehnungen.json
    python export_ablehnungen.py -o datei.json   # eigener Ausgabepfad
    python export_ablehnungen.py --nur-manuell    # nur manuelle Gründe
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import verbindung

STANDARD_AUSGABE = Path(__file__).parent / "ablehnungen.json"


def sammle_ablehnungen(nur_manuell: bool = False) -> list[dict]:
    """Liest alle Stellen mit einem Ablehnungsgrund aus der DB.

    Grund-Priorität: der manuell eingetragene ``nicht_beworben_grund`` gewinnt
    über den automatischen ``nicht_passend_grund`` – die manuelle Einschätzung
    ist das verlässlichere Signal für die Prompt-Verbesserung.
    """
    with verbindung() as con:
        zeilen = con.execute(
            """
            SELECT s.url,
                   s.titel,
                   s.firma,
                   s.stellentext,
                   s.nicht_passend_grund,
                   b.nicht_beworben_grund
            FROM stellen s
            LEFT JOIN bewerbungsstatus b ON b.url = s.url
            """
        ).fetchall()

    ergebnis = []
    for r in zeilen:
        manuell = (r["nicht_beworben_grund"] or "").strip()
        auto = (r["nicht_passend_grund"] or "").strip()

        grund = manuell if nur_manuell else (manuell or auto)
        if not grund:
            continue

        ergebnis.append({
            "url": r["url"],
            "titel": r["titel"] or "",
            "firma": r["firma"] or "",
            "stellentext": (r["stellentext"] or "").strip(),
            "grund": grund,
            "grund_quelle": "manuell" if manuell else "automatisch",
        })

    return ergebnis


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Exportiert abgelehnte Stellen (Link, Stellentext, Grund) als JSON.")
    parser.add_argument("-o", "--ausgabe", type=Path, default=STANDARD_AUSGABE,
                        help=f"Zielpfad der JSON-Datei (Standard: {STANDARD_AUSGABE.name})")
    parser.add_argument("--nur-manuell", action="store_true",
                        help="Nur manuell eingetragene Gründe (nicht_beworben_grund) exportieren.")
    args = parser.parse_args()

    daten = sammle_ablehnungen(nur_manuell=args.nur_manuell)
    args.ausgabe.write_text(
        json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")

    manuell = sum(1 for d in daten if d["grund_quelle"] == "manuell")
    auto = len(daten) - manuell
    print(f"OK: {len(daten)} Ablehnung(en) exportiert nach {args.ausgabe}")
    print(f"    davon {manuell} manuell, {auto} automatisch")


if __name__ == "__main__":
    main()
