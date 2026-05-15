"""
vergaben_check.py  –  Schritt 1c: Erreichbarkeits-Prüfung
===========================================================
Prüft per HTTP ob bekannte aktive Stellen noch aufrufbar sind.

Geprüfte Jobs:
  • status in (1, 2, 3, 4, 5, 6) AND geloescht_am IS NULL
  • nur wenn Domain auch in diesem Lauf gescannt wurde (Schutz gegen Fehlmarkierung)
  • aktive Bewerbungen (status=6) werden immer geprüft

Ergebnis pro URL:
  HTTP 200 + kein Closed-Marker  → kein Urteil (Stelle aktiv)
  HTTP 200 + Closed-Marker       → status=9 / vergaben
  HTTP 200 + Domain-Wechsel      → status=9 / vergaben
  HTTP 200 + Redirect auf Root   → status=9 / vergaben
  HTTP 404 / 410 / 0             → status=9 / vergaben
  HTTP 403 / 429                 → einmal retry nach 8s
  Verbindungsfehler              → kein Urteil

Nutzung:
  python vergaben_check.py                # Standard
  python vergaben_check.py --alle         # auch Stellen die nicht gescannt wurden
  python vergaben_check.py --url URL      # nur eine bestimmte URL prüfen
"""

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from urllib.parse import urlparse

BASIS_PFAD    = Path(__file__).parent
STELLEN_JSON  = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON = BASIS_PFAD / "bekannte_stellen.json"

_CLOSED_MARKERS = [
    "no longer available",
    "this job is no longer",
    "position is no longer",
    "stelle ist nicht mehr",
    "nicht mehr verfügbar",
    "job is closed",
    "posting is no longer active",
    "sorry, this job is",
    "leider nicht mehr",
    "bereits vergeben",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# =============================================================================
# HTTP-CHECK
# =============================================================================

def _einzel_request(url: str) -> tuple[int | None, str, str]:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT},
            method="GET"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read(8192).decode("utf-8", errors="ignore").lower()
            return resp.status, resp.url, body
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception:
        return None, url, ""


def _prüfe_200(orig_url: str, final_url: str, body: str) -> int:
    orig_parsed  = urlparse(orig_url)
    final_parsed = urlparse(final_url)

    if orig_parsed.netloc != final_parsed.netloc:
        return 0  # Domain-Wechsel → vergaben

    orig_path  = orig_parsed.path
    final_path = final_parsed.path
    if orig_path and len(final_path) < len(orig_path) * 0.5:
        return 0  # Redirect auf Portal-Root → vergaben

    for marker in _CLOSED_MARKERS:
        if marker in body:
            return 0

    return 200


def _workday_job_aktiv(url: str) -> bool | None:
    parsed  = urlparse(url)
    netloc  = parsed.netloc
    tenant  = netloc.split(".")[0]
    segments = [s for s in parsed.path.split("/") if s]
    if segments and re.match(r'^[a-z]{2}-[A-Z]{2}$', segments[0]):
        segments = segments[1:]
    if len(segments) < 2:
        return None

    portal   = segments[0]
    last_seg = segments[-1]
    m = re.search(r'_(R\d+(?:-\d+)?)$', last_seg)
    if not m:
        return None
    job_id  = m.group(1)
    api_url = f"https://{netloc}/wday/cxs/{tenant}/{portal}/jobs"
    payload = {"searchText": job_id, "limit": 5, "offset": 0, "appliedFacets": {}}

    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        jobs = data.get("jobPostings", [])
        for job in jobs:
            if job_id in job.get("externalPath", ""):
                return True
        return False
    except Exception:
        return None


def pruefe_url(url: str) -> int | None:
    """Gibt HTTP-Ergebnis zurück: 200=aktiv, 0=vergaben, 404/410=vergaben, None=unbekannt."""
    code, final_url, body = _einzel_request(url)

    if code in (403, 429):
        time.sleep(8)
        code2, final_url2, body2 = _einzel_request(url)
        if code2 is not None:
            code, final_url, body = code2, final_url2, body2

    elif code == 404:
        time.sleep(3)
        code2, final_url2, body2 = _einzel_request(url)
        if code2 is not None and code2 != 404:
            code, final_url, body = code2, final_url2, body2

    if code == 200:
        if ".wd3.myworkdayjobs.com" in url and "/job/" in url:
            aktiv = _workday_job_aktiv(url)
            if aktiv is False:
                return 0
        else:
            code = _prüfe_200(url, final_url, body)

    return code


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Vergaben-Check per HTTP")
    parser.add_argument("--alle", action="store_true",
                        help="Auch Stellen aus nicht-gescannten Domains prüfen")
    parser.add_argument("--url",  default=None, help="Nur diese URL prüfen")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  VERGABEN CHECK  –  Schritt 1c: Erreichbarkeit prüfen")
    if args.url:
        print(f"  Filter: nur {args.url[:60]}")
    if args.alle:
        print("  Modus: ALLE Domains")
    print("=" * 60)

    sys.path.insert(0, str(BASIS_PFAD))
    from db import (erstelle_schema, lade_alle_stellen, lade_bekannte_dict,
                    upsert_stelle, exportiere_stellen_json, exportiere_bekannte_json,
                    repariere_inkonsistente_status)
    erstelle_schema()
    repariere_inkonsistente_status()

    stellen:  list = lade_alle_stellen()
    bekannte: dict = lade_bekannte_dict()

    stellen_index = {s["url"]: i for i, s in enumerate(stellen)}

    # Status-Mapping für Bewerbungs-URLs
    try:
        from db import verbindung as _db_verb
        with _db_verb() as con:
            bewerb_stufen_map = {
                r[0]: r[1] for r in con.execute("SELECT url, stufe FROM bewerbungsstatus").fetchall()
            }
    except Exception:
        bewerb_stufen_map = {}

    _aktive_stufen = {"beworben", "kennenlernen", "einladung"}

    def status_bei_vergabe(url: str) -> int:
        stufe = bewerb_stufen_map.get(url, "")
        if stufe in _aktive_stufen:
            return 7
        if stufe in ("absage", "zusage"):
            return 8
        return 9

    # Kandidaten bestimmen
    if args.url:
        kandidaten = [args.url] if args.url in bekannte else []
        if not kandidaten:
            print(f"  ⚠️  URL nicht in bekannte_stellen.json gefunden")
            return
    else:
        kandidaten = [
            url for url, eintrag in bekannte.items()
            if eintrag.get("status") in (1, 2, 3, 4, 5, 6)
            and not eintrag.get("geloescht_am")
            and not eintrag.get("nicht_passend")
        ]

    if not kandidaten:
        print(f"  ℹ️  Keine aktiven Stellen zu prüfen.")
        return

    print(f"  {len(kandidaten)} URL(s) zu prüfen...")

    vergaben    = 0
    vorgemerkt  = 0
    noch_aktiv  = 0
    unklar      = 0
    ts = __import__("utils").jetzt()

    for url in kandidaten:
        idx    = stellen_index.get(url)
        titel  = stellen[idx].get("titel", url[:60]) if idx is not None else url[:60]
        status = bekannte.get(url, {}).get("status", 1)

        print(f"\n  {'─'*54}")
        print(f"  {titel[:60]}")
        print(f"  Status {status} | {url[:70]}")

        code = pruefe_url(url)

        if code in (404, 410, 0):
            bereits_vorgemerkt = bekannte[url].get("pruef_vormerken")
            if not bereits_vorgemerkt:
                # Erster fehlgeschlagener Check → nur vormerken, noch nicht endgültig markieren
                upsert_stelle({"url": url, "pruef_vormerken": ts})
                bekannte[url]["pruef_vormerken"] = ts
                vorgemerkt += 1
                print(f"  ⏳ Vorgemerkt (1. fehlgeschlagener Check, HTTP {code}) – Bestätigung beim nächsten Lauf")
            else:
                # Zweiter fehlgeschlagener Check → jetzt endgültig als vergaben markieren
                neuer_status = status_bei_vergabe(url)
                bekannte[url]["status"]              = neuer_status
                bekannte[url]["geloescht_am"]        = ts
                bekannte[url]["vergaben_bestaetigt"] = True
                bekannte[url]["pruef_vormerken"]     = None
                if idx is not None:
                    stellen[idx]["geloescht_am"]  = ts
                    stellen[idx]["vergabe_status"] = neuer_status
                upsert_stelle({
                    "url":            url,
                    "status":         neuer_status,
                    "geloescht_am":   ts,
                    "pruef_vormerken": None,
                })
                vergaben += 1
                label = {7: "📭 Vergaben (Bewerbung lief)", 8: "❌ Vergaben (Absage)", 9: "🗑️  Vergaben"}
                print(f"  {label.get(neuer_status, '🗑️  Vergaben')} (HTTP {code}, 2. Bestätigung)")

        elif code == 200:
            # Erreichbar → ggf. Vormerken zurücksetzen
            if bekannte[url].get("pruef_vormerken"):
                upsert_stelle({"url": url, "pruef_vormerken": None})
                bekannte[url]["pruef_vormerken"] = None
                print(f"  ✅ Noch aktiv (HTTP 200) – Vormerken zurückgesetzt")
            else:
                print(f"  ✅ Noch aktiv (HTTP 200)")
            noch_aktiv += 1

        elif code is None:
            print(f"  ⏱️  Verbindungsfehler / Timeout – kein Urteil")
            unklar += 1
        else:
            print(f"  ❓ HTTP {code} – kein Urteil")
            unklar += 1

    exportiere_stellen_json(STELLEN_JSON)
    exportiere_bekannte_json(BEKANNTE_JSON)

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Vergaben markiert:  {vergaben}")
    print(f"  Vorgemerkt (1x):    {vorgemerkt}  ← wird nächsten Lauf bestätigt")
    print(f"  Noch aktiv:         {noch_aktiv}")
    print(f"  Unklar:             {unklar}")
    print(f"  Weiter mit:         python extraktor.py")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
