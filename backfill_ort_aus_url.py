"""
backfill_ort_aus_url.py  –  Einmalige Reparatur: Ort aus URL nachtragen
=========================================================================
Für bereits extrahierte Stellen (Status 3+) mit leerem arbeitsort wird
versucht, den Ort per URL-Muster nachzutragen (siehe standort_aus_url()
in extraktor.py). Betrifft v.a. ATS wie jobs.renesas.com, die den Ort
per JS-Widget rendern statt im Seitentext.

Nutzung:
  python backfill_ort_aus_url.py
"""

import sys
from pathlib import Path

BASIS_PFAD = Path(__file__).parent
sys.path.insert(0, str(BASIS_PFAD))

from db import lade_alle_stellen, upsert_stelle, exportiere_stellen_json, exportiere_bekannte_json
from utils import lade_config, berechne_standort, standort_aus_url


def main():
    config    = lade_config()
    erlaubte  = config["erlaubte_standorte"]
    verbotene = config["verbotene_standorte"]

    stellen = lade_alle_stellen()
    fixed = 0
    for s in stellen:
        if s.get("arbeitsort"):
            continue
        ort = standort_aus_url(s.get("url") or "")
        if not ort:
            continue
        standort = berechne_standort(ort, erlaubte, verbotene)
        upsert_stelle({"url": s["url"], "arbeitsort": ort, "standort": standort})
        print(f"  📍 {s.get('firma')}: {ort} ({standort}) – {s['url'][-60:]}")
        fixed += 1

    exportiere_stellen_json(BASIS_PFAD / "stellen.json")
    exportiere_bekannte_json(BASIS_PFAD / "bekannte_stellen.json")

    print(f"\n{fixed} Stelle(n) repariert.")


if __name__ == "__main__":
    main()
