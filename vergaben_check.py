"""
vergaben_check.py  –  Schritt 1c: Erreichbarkeits-Prüfung
===========================================================
Prüft per HTTP ob bekannte aktive Stellen noch aufrufbar sind.

Geprüfte Jobs:
  • status in (1, 2, 3, 4, 5, 6) AND geloescht_am IS NULL AND nicht nicht_passend
  • --alle prüft zusätzlich alle nicht gelöschten Stellen unabhängig vom Status

Ergebnis pro URL:
  HTTP 200 + kein Closed-Marker  → kein Urteil (Stelle aktiv)
  HTTP 200 + Closed-Marker       → status=9 / vergaben
  HTTP 200 + Domain-Wechsel      → status=9 / vergaben
  HTTP 200 + Redirect auf Root   → status=9 / vergaben
  HTTP 404 / 410 / 0             → status=9 / vergaben (nach 2 Bestätigungen)
  HTTP 403 / 429                 → einmal retry nach 8s
  Verbindungsfehler / Timeout    → status=9 / vergaben (nach 2 Bestätigungen, wie 404/410)

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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent))
from utils import jetzt
from browser import USER_AGENT
from status_def import status_fuer_stufe

BASIS_PFAD          = Path(__file__).parent
STELLEN_JSON        = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON       = BASIS_PFAD / "bekannte_stellen.json"
MANUELL_VERGEBEN_TXT = BASIS_PFAD / "manuell_vergeben.txt"


def lade_manuell_vergeben() -> list[str]:
    """URLs aus manuell_vergeben.txt, eine pro Zeile, '#'-Kommentare ignoriert."""
    if not MANUELL_VERGEBEN_TXT.exists():
        return []
    urls = []
    for zeile in MANUELL_VERGEBEN_TXT.read_text(encoding="utf-8").splitlines():
        z = zeile.strip()
        if z and not z.startswith("#"):
            urls.append(z)
    return urls

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

# Diese Marker sind nur innerhalb des sichtbar gerenderten Seitenanfangs
# aussagekräftig. Viele SPA-Portale liefern weiter hinten im HTML ein
# vollständiges i18n-Übersetzungswörterbuch mit, das exakt dieselben Phrasen
# (z.B. "no longer available") als generische Fehlertext-Bausteine enthält –
# und zwar auf JEDER Seite, egal ob der Job aktiv oder vergeben ist. Deshalb
# NICHT auf den vollen (großen) Body anwenden, sonst Massen an False Positives.
_CLOSED_MARKERS_BEREICH = 8192

# Strukturelle Marker (z.B. eine nur im "not found"-Zustand gerenderte CSS-Klasse)
# sind dagegen unabhängig von der Position im Body aussagekräftig, da sie vom
# Framework nur bedingt gerendert werden statt Teil eines statischen
# Übersetzungs-Bundles zu sein. Diese dürfen im ganzen (großen) Body gesucht werden.
_CLOSED_MARKERS_STRUKTURELL = [
    "page-not-found-wrapper",  # Apple Jobs SPA: HTTP 200, aber serverseitig als 404 gerendert
]

# URL-Segmente, die Portale beim Redirect auf eine "Job nicht gefunden"-Seite anhängen
# (z.B. dvinci-hr: /de/jobPublication/notFound/<id>)
_NOTFOUND_URL_MARKERS = ["notfound", "not-found", "job-not-found"]

# Manche SPA-Job-Portale (z.B. jobs.apple.com) rendern die "not found"-Meldung
# erst weit hinten im HTML (Übersetzungs-Bundles, eingebettete State-JSONs
# etc. kommen zuerst). Ein zu kleiner Lesepuffer sieht die Meldung nie - daher
# wird trotzdem ein großer Puffer gelesen, nur eben nicht für die generischen
# Marker oben ausgewertet.
_MAX_BODY_BYTES = 2_000_000


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
            body = resp.read(_MAX_BODY_BYTES).decode("utf-8", errors="ignore").lower()
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

    final_path_lower = final_path.lower()
    if any(marker in final_path_lower for marker in _NOTFOUND_URL_MARKERS):
        return 0  # Redirect auf "nicht gefunden"-URL → vergaben

    for marker in _CLOSED_MARKERS:
        if marker in body[:_CLOSED_MARKERS_BEREICH]:
            return 0

    for marker in _CLOSED_MARKERS_STRUKTURELL:
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
    parser.add_argument("--url",   default=None, help="Nur diese URL prüfen")
    parser.add_argument("--firma", default=None, help="Nur diese Firma prüfen")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  VERGABEN CHECK  –  Schritt 1c: Erreichbarkeit prüfen")
    if args.url:
        print(f"  Filter: nur {args.url[:60]}")
    if args.firma:
        print(f"  Filter: nur Firma '{args.firma}'")
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

    def status_bei_vergabe(url: str) -> int:
        return status_fuer_stufe(bewerb_stufen_map.get(url, ""))

    ts = jetzt()

    # Manuelle Overrides (manuell_vergeben.txt) sofort verarbeiten - hier hat
    # ein Mensch schon geprüft, daher ohne die übliche Zwei-Läufe-Bestätigung.
    manuell_markiert = 0
    for url in lade_manuell_vergeben():
        eintrag = bekannte.get(url)
        if not eintrag or eintrag.get("geloescht_am"):
            continue
        neuer_status = status_bei_vergabe(url)
        bekannte[url]["status"]              = neuer_status
        bekannte[url]["geloescht_am"]        = ts
        bekannte[url]["vergaben_bestaetigt"] = True
        bekannte[url]["pruef_vormerken"]     = None
        idx = stellen_index.get(url)
        if idx is not None:
            stellen[idx]["geloescht_am"]  = ts
            stellen[idx]["vergabe_status"] = neuer_status
        upsert_stelle({
            "url":            url,
            "status":         neuer_status,
            "geloescht_am":   ts,
            "pruef_vormerken": None,
        })
        manuell_markiert += 1
        print(f"  📝 Manuell vergeben: {url[:70]}")

    if manuell_markiert:
        print(f"  {manuell_markiert} URL(s) aus manuell_vergeben.txt markiert.")

    # Kandidaten bestimmen
    if args.url:
        kandidaten = [args.url] if args.url in bekannte else []
        if not kandidaten:
            print(f"  ⚠️  URL nicht in der Datenbank gefunden")
            return
    elif args.alle:
        kandidaten = [
            url for url, eintrag in bekannte.items()
            if not eintrag.get("geloescht_am")
        ]
    else:
        kandidaten = [
            url for url, eintrag in bekannte.items()
            if eintrag.get("status") in (1, 2, 3, 4, 5, 6)
            and not eintrag.get("geloescht_am")
            and not eintrag.get("nicht_passend")
        ]

    if args.firma:
        def _firma_von(url: str) -> str:
            idx = stellen_index.get(url)
            return stellen[idx].get("firma", "") if idx is not None else ""
        kandidaten = [url for url in kandidaten if _firma_von(url) == args.firma]

    if not kandidaten:
        exportiere_stellen_json(STELLEN_JSON)
        exportiere_bekannte_json(BEKANNTE_JSON)
        print(f"  ℹ️  Keine aktiven Stellen zu prüfen.")
        return

    print(f"  {len(kandidaten)} URL(s) zu prüfen...")

    vergaben    = 0
    vorgemerkt  = 0
    noch_aktiv  = 0
    unklar      = 0

    for url in kandidaten:
        idx    = stellen_index.get(url)
        titel  = stellen[idx].get("titel", url[:60]) if idx is not None else url[:60]
        status = bekannte.get(url, {}).get("status", 1)

        print(f"\n  {'─'*54}")
        print(f"  {titel[:60]}")
        print(f"  Status {status} | {url[:70]}")

        code = pruefe_url(url)

        if code in (404, 410, 0) or code is None:
            # Timeouts/Verbindungsfehler werden wie 404/410 behandelt: bleiben sie
            # über zwei aufeinanderfolgende Läufe bestehen, ist die Seite mit
            # ziemlicher Sicherheit dauerhaft weg (nicht nur ein kurzer Netzwerk-Hänger).
            code_label = "Timeout/Verbindungsfehler" if code is None else f"HTTP {code}"
            bereits_vorgemerkt = bekannte[url].get("pruef_vormerken")
            if not bereits_vorgemerkt:
                # Erster fehlgeschlagener Check → nur vormerken, noch nicht endgültig markieren
                upsert_stelle({"url": url, "pruef_vormerken": ts})
                bekannte[url]["pruef_vormerken"] = ts
                vorgemerkt += 1
                print(f"  ⏳ Vorgemerkt (1. fehlgeschlagener Check, {code_label}) – Bestätigung beim nächsten Lauf")
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
                print(f"  {label.get(neuer_status, '🗑️  Vergaben')} ({code_label}, 2. Bestätigung)")

        elif code == 200:
            # Erreichbar → ggf. Vormerken zurücksetzen
            if bekannte[url].get("pruef_vormerken"):
                upsert_stelle({"url": url, "pruef_vormerken": None})
                bekannte[url]["pruef_vormerken"] = None
                print(f"  ✅ Noch aktiv (HTTP 200) – Vormerken zurückgesetzt")
            else:
                print(f"  ✅ Noch aktiv (HTTP 200)")
            noch_aktiv += 1

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
