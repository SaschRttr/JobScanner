"""
rebuild_bekannte.py  –  Baut bekannte_stellen.json sauber neu auf
==================================================================
Logik:
  - Quelle: DB (stellen + bewertungen + bewerbungsstatus)
  - Basis-Status: geloescht_am→0, bewertung→4/5, stellentext→3, rohtext→2, sonst→1
  - bewerbungsstatus DB:  stufe=absage → 8
                          stufe+geloescht → 7  (beworben, Stelle weg)
                          stufe+aktiv → 6  (beworben, Stelle noch da)
  - nicht_passend: gesetzt bei Config-Regel (Titel)
                   gesetzt bei status=0 (vergaben, nie beworben)

Ausführen:
  python rebuild_bekannte.py
"""

import json
import sqlite3
from pathlib import Path

BASIS       = Path(__file__).parent
BEKANNTE    = BASIS / "bekannte_stellen.json"
CONFIG_PFAD = BASIS / "config.txt"
DB_PFAD     = BASIS / "jobscanner.db"


def lade_config():
    result = {"ausschlussbegriffe": [], "verbotene_standorte": []}
    if not CONFIG_PFAD.exists():
        return result
    aktiv = None
    for zeile in CONFIG_PFAD.read_text(encoding="utf-8").splitlines():
        z = zeile.strip()
        if z.startswith("[\\"):
            aktiv = None
        elif z == "[ausschlussbegriffe]":
            aktiv = "ausschluss"
        elif z == "[verbotene_standorte]":
            aktiv = "standorte"
        elif aktiv == "ausschluss" and z and not z.startswith("#"):
            result["ausschlussbegriffe"].append(z.lower())
        elif aktiv == "standorte" and z and not z.startswith("#"):
            result["verbotene_standorte"].append(z.lower())
    return result


def lade_db() -> tuple[list[dict], dict]:
    """Gibt (stellen_liste, {url: stufe}) aus der DB zurück."""
    if not DB_PFAD.exists():
        return [], {}
    con = sqlite3.connect(DB_PFAD)
    con.row_factory = sqlite3.Row

    stellen_rows = con.execute("""
        SELECT s.url, s.firma, s.titel, s.gefunden_am, s.geloescht_am,
               s.rohtext, s.stellentext,
               b.score
        FROM stellen s
        LEFT JOIN bewertungen b ON b.url = s.url
    """).fetchall()

    bew_rows = con.execute(
        "SELECT url, stufe FROM bewerbungsstatus WHERE stufe != '' AND stufe IS NOT NULL"
    ).fetchall()
    con.close()

    stellen = []
    for r in stellen_rows:
        stellen.append({
            "url":          r["url"],
            "titel":        r["titel"],
            "gefunden_am":  r["gefunden_am"],
            "geloescht_am": r["geloescht_am"],
            "rohtext":      r["rohtext"],
            "stellentext":  r["stellentext"],
            "score":        r["score"],
        })

    bew_status = {row[0]: row[1] for row in bew_rows}
    return stellen, bew_status


def status_berechnen(s: dict, stufe: str) -> int:
    if s.get("geloescht_am"):
        base = 0
    elif s.get("score") is not None:
        base = 4 if s["score"] >= 70 else 5
    elif s.get("stellentext"):
        base = 3
    elif s.get("rohtext"):
        base = 2
    else:
        base = 1

    if not stufe:
        return base
    if stufe == "absage":
        return 8
    return 7 if base == 0 else 6


def config_schliesst_aus(titel: str, ausschlussbegriffe: list) -> bool:
    t = titel.lower()
    return any(b in t for b in ausschlussbegriffe)


def main():
    config              = lade_config()
    stellen, bew_status = lade_db()

    bekannte_neu = {}
    stats = {"status_fix": 0, "np_config": 0, "np_geloescht": 0, "s6": 0, "s7": 0, "s8": 0}

    # alten Stand für Status-Diff-Zählung
    bekannte_alt = {}
    if BEKANNTE.exists():
        try:
            bekannte_alt = json.loads(BEKANNTE.read_text(encoding="utf-8"))
        except Exception:
            pass

    for s in stellen:
        url = s.get("url")
        if not url:
            continue

        stufe  = bew_status.get(url, "")
        status = status_berechnen(s, stufe)

        eintrag = {
            "status":       status,
            "gefunden_am":  s.get("gefunden_am", ""),
            "geloescht_am": s.get("geloescht_am"),
        }

        if status == 6: stats["s6"] += 1
        if status == 7: stats["s7"] += 1
        if status == 8: stats["s8"] += 1

        if config_schliesst_aus(s.get("titel", ""), config["ausschlussbegriffe"]):
            eintrag["nicht_passend"] = True
            stats["np_config"] += 1
        elif status == 0:
            eintrag["nicht_passend"] = True
            stats["np_geloescht"] += 1

        if bekannte_alt.get(url, {}).get("status") != status:
            stats["status_fix"] += 1

        bekannte_neu[url] = eintrag

    BEKANNTE.write_text(
        json.dumps(bekannte_neu, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"bekannte_stellen.json neu aufgebaut: {len(bekannte_neu)} Eintraege")
    print(f"   Status-Korrekturen:             {stats['status_fix']}")
    print(f"   nicht_passend (Config):         {stats['np_config']}")
    print(f"   nicht_passend (status=0):       {stats['np_geloescht']}")
    print(f"   Bewerbungen:  {stats['s6']} offen  {stats['s7']} weg  {stats['s8']} Absage")
    print()
    print("Weiter mit: python report.py --keine-mail")


if __name__ == "__main__":
    main()
