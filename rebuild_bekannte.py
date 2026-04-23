"""
rebuild_bekannte.py  –  Baut bekannte_stellen.json sauber neu auf
==================================================================
Logik:
  - Status wird aus stellen.json-Daten abgeleitet (bewertung=4, stellentext=3, rohtext=2, sonst=1)
  - geloescht_am → status 0
  - nicht_passend: bleibt wenn bekannte es hatte (manuell/scanner gesetzt)
                   wird neu gesetzt wenn Config-Regel greift (Titel/Standort-Feld)
                   wird ENTFERNT wenn stellen.json es hat aber bekannte nicht + keine Config-Regel
                   (= alte Fehlmarkierungen durch Text-Check)

Ausführen:
  python rebuild_bekannte.py
"""

import json
from pathlib import Path

BASIS       = Path(__file__).parent
STELLEN     = BASIS / "stellen.json"
BEKANNTE    = BASIS / "bekannte_stellen.json"
CONFIG_PFAD = BASIS / "config.txt"


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


def status_aus_daten(s: dict) -> int:
    if s.get("geloescht_am"):
        return 0
    if s.get("bewertung"):
        return 4
    if s.get("stellentext"):
        return 3
    if s.get("rohtext"):
        return 2
    return 1


def config_schliesst_aus(s: dict, ausschlussbegriffe: list, verbotene_standorte: list) -> bool:
    titel    = s.get("titel", "").lower()
    standort = (s.get("standort") or "").lower()
    ausgeschlossen = any(t in titel for t in ausschlussbegriffe)
    # Standort nur prüfen wenn bekannt — leer = durchlassen
    standort_weg   = bool(standort) and standort != "ok" and any(v in standort for v in verbotene_standorte)
    return ausgeschlossen or standort_weg


def main():
    stellen  = json.loads(STELLEN.read_text(encoding="utf-8"))
    bekannte_alt = {}
    if BEKANNTE.exists():
        try:
            bekannte_alt = json.loads(BEKANNTE.read_text(encoding="utf-8"))
        except Exception:
            pass
    config = lade_config()

    bekannte_neu = {}
    stats = {"status_fix": 0, "np_behalten": 0, "np_config": 0, "np_geloescht": 0, "np_cleared": 0}

    for s in stellen:
        url = s.get("url")
        if not url:
            continue

        alt = bekannte_alt.get(url, {})
        status = status_aus_daten(s)

        eintrag = {
            "status":      status,
            "gefunden_am": alt.get("gefunden_am") or s.get("gefunden_am", ""),
            "geloescht_am": s.get("geloescht_am"),
        }

        # nicht_passend bestimmen
        np_alt_bekannte = alt.get("nicht_passend", False)
        np_alt_stellen  = s.get("nicht_passend", False)
        np_config       = config_schliesst_aus(s, config["ausschlussbegriffe"], config["verbotene_standorte"])

        if np_config:
            # Config-Regel greift (Titel/Standort-Feld) → setzen
            eintrag["nicht_passend"] = True
            stats["np_config"] += 1
        elif status == 0:
            # Nicht mehr gelistet → nicht_passend
            eintrag["nicht_passend"] = True
            stats["np_geloescht"] += 1
        else:
            # Alles andere: False — löscht auch False Positives aus alter bereinigung (Text-Check)
            if np_alt_bekannte or np_alt_stellen:
                stats["np_cleared"] += 1
        # sonst: nicht_passend gar nicht setzen (default False)

        if alt.get("status") != status:
            stats["status_fix"] += 1

        bekannte_neu[url] = eintrag

    BEKANNTE.write_text(
        json.dumps(bekannte_neu, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"✅ bekannte_stellen.json neu aufgebaut: {len(bekannte_neu)} Einträge")
    print(f"   Status-Korrekturen:          {stats['status_fix']}")
    print(f"   nicht_passend behalten:      {stats['np_behalten']}  (war in bekannte gesetzt)")
    print(f"   nicht_passend neu (Config):  {stats['np_config']}   (Titel/Standort trifft Regel)")
    print(f"   nicht_passend (status=0):    {stats['np_geloescht']}")
    print(f"   Fehlmarkierungen bereinigt:  {stats['np_cleared']}  (stellen hatte True, bekannte nicht → gelöscht)")
    print()
    print("Weiter mit: python report.py --keine-mail")


if __name__ == "__main__":
    main()
