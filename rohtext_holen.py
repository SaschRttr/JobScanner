"""
rohtext_holen.py  –  Schritt 1b: Rohtext für einzelne Stellenseiten laden
===========================================================================
Lädt den vollständigen Seitentext (Rohtext) für alle Stellen, die noch
keinen oder nur einen zu kurzen Rohtext haben – via Playwright.

Abgedeckte Fälle:
  • Status 1, kein Rohtext           → neu laden
  • Status 1 oder 2, Rohtext < MIN   → neu laden (API-Jobs speichern oft nur
                                       "Standort: XYZ" als Platzhalter)
  • Status 3+, rohtext vorhanden     → überspringen

Nutzung:
  python rohtext_holen.py             # alle offenen Stellen
  python rohtext_holen.py --url URL   # nur eine bestimmte Stelle
  python rohtext_holen.py --force     # auch Status 3/4/5 neu laden (Reparatur)

Status-Übergänge:
  1 → 2  (Rohtext geladen)
  2 bleibt 2 (kurzer Rohtext ersetzt)
"""

import argparse
import sys
from pathlib import Path

BASIS_PFAD   = Path(__file__).parent
STELLEN_JSON = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON = BASIS_PFAD / "bekannte_stellen.json"

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright nicht installiert: pip install playwright && playwright install chromium")
    sys.exit(1)

from utils import klick_cookie_banner
from browser import (
    MIN_ROHTEXT_LAENGE, lade_pdf_text,
    starte_browser, neuer_context, neue_seite,
)


# =============================================================================
# ROHTEXT LADEN
# =============================================================================

# Domain-spezifische Wartezeiten (ms) für SPAs die lange zum Rendern brauchen
_WARTE_MS: dict[str, int] = {
    "jobs.keysight.com":         8000,
    "jobs.infineon.com":         6000,
    "careers.te.com":            5000,
    "wd3.myworkdayjobs.com":     6000,
}
_WARTE_MS_DEFAULT = 4000


def _warte_fuer(url: str) -> int:
    for domain_teil, ms in _WARTE_MS.items():
        if domain_teil in url:
            return ms
    return _WARTE_MS_DEFAULT


def _url_anpassen(url: str) -> str:
    """Domain-spezifische URL-Umschreibungen für bessere Inhalte."""
    # Bertrandt onlyfy: Volltext-URL statt Detail-URL
    if "onlyfy.jobs" in url:
        job_id = url.rstrip("/").split("/")[-1]
        return f"https://bertrandtgroup.onlyfy.jobs/job/show/{job_id}/full?lang=de&mode=candidate"
    return url


def _bereinige_rohtext(rohtext: str) -> str:
    """Reduziert aufeinanderfolgende Leerzeilen auf maximal 2."""
    zeilen = rohtext.splitlines()
    ergebnis = []
    leer = 0
    for z in zeilen:
        if z.strip() == "":
            leer += 1
            if leer <= 2:
                ergebnis.append("")
        else:
            leer = 0
            ergebnis.append(z)
    return "\n".join(ergebnis)


def _extrahiere_titel(page) -> str | None:
    """Liest den echten Jobtitel von der geladenen Seite."""
    try:
        h1 = page.locator("h1").first.inner_text(timeout=3000).strip()
        if h1 and len(h1) > 5:
            return h1
    except Exception:
        pass
    try:
        titel = page.title().strip()
        for trenner in [" | ", " - ", " – ", " — ", " :: "]:
            if trenner in titel:
                titel = titel.split(trenner)[0].strip()
                break
        if titel and len(titel) > 5:
            return titel
    except Exception:
        pass
    return None


def lade_rohtext_playwright(page, url: str) -> str | None:
    """
    Lädt eine einzelne Stellenseite via Playwright und gibt den Rohtext zurück.
    Gibt None zurück wenn die Seite nicht geladen werden konnte.
    """
    if url.lower().endswith(".pdf"):
        return lade_pdf_text(url)

    lade_url = _url_anpassen(url)
    warte_ms  = _warte_fuer(lade_url)

    try:
        antwort = None
        try:
            antwort = page.goto(lade_url, wait_until="networkidle", timeout=45000)
        except Exception:
            antwort = page.goto(lade_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)

        if antwort and antwort.status in (403, 404, 410):
            print(f"  ❌ HTTP {antwort.status} – nicht erreichbar")
            return None

        page.wait_for_timeout(warte_ms)
        klick_cookie_banner(page)
        page.wait_for_timeout(2000)

        rohtext = page.inner_text("body")

        # Zu kurzer Inhalt → nochmal warten (SPA noch nicht fertig)
        if not rohtext or len(rohtext.strip()) < 300:
            page.wait_for_timeout(6000)
            rohtext = page.inner_text("body")

        if not rohtext or len(rohtext.strip()) < 100:
            return None

        return _bereinige_rohtext(rohtext)

    except Exception as e:
        print(f"  ❌ Playwright-Fehler: {e}")
        return None


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Rohtext für einzelne Stellenseiten laden")
    parser.add_argument("--url",   default=None, help="Nur diese URL verarbeiten")
    parser.add_argument("--firma", default=None, help="Nur diese Firma verarbeiten")
    parser.add_argument("--force", action="store_true",
                        help="Auch Status 3/4/5 neu laden (Reparatur-Modus)")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  ROHTEXT HOLEN  –  Schritt 1b: Rohtext laden")
    if args.url:
        print(f"  Filter: nur {args.url[:60]}")
    if args.firma:
        print(f"  Filter: nur Firma '{args.firma}'")
    if args.force:
        print("  Modus: FORCE (alle Stellen, auch bereits extrahierte)")
    print("=" * 60)

    sys.path.insert(0, str(BASIS_PFAD))
    from db import lade_alle_stellen, upsert_stelle, exportiere_stellen_json, \
                   exportiere_bekannte_json, erstelle_schema
    erstelle_schema()
    stellen = lade_alle_stellen()

    if args.force:
        ziel_status = (1, 2, 3, 4, 5)
    else:
        ziel_status = (1, 2)

    zu_laden = []
    for i, s in enumerate(stellen):
        url = s.get("url") or ""
        if not url.startswith("http"):
            continue
        if args.url and url != args.url:
            continue
        if args.firma and s.get("firma") != args.firma:
            continue
        if s.get("nicht_passend"):
            continue
        if s.get("status") not in ziel_status:
            continue

        rohtext = s.get("rohtext") or ""
        # Laden wenn: kein Rohtext, oder Rohtext unter Mindestlänge
        if len(rohtext.strip()) < MIN_ROHTEXT_LAENGE:
            zu_laden.append((i, s))

    if not zu_laden:
        print(f"  ℹ️  Alle {len(stellen)} Stellen haben ausreichend Rohtext.")
        return

    kurz = sum(1 for _, s in zu_laden if s.get("rohtext") and len(s["rohtext"].strip()) > 0)
    leer = len(zu_laden) - kurz
    print(f"  {len(zu_laden)} Stelle(n) zu laden  "
          f"({leer} ohne Rohtext, {kurz} mit zu kurzem Rohtext < {MIN_ROHTEXT_LAENGE} Z.)")

    geladen    = 0
    zu_kurz    = 0
    fehler     = 0

    with sync_playwright() as p:
        browser = starte_browser(p)
        context = neuer_context(browser, extra_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Dest": "document",
            "Upgrade-Insecure-Requests": "1",
        })
        page = neue_seite(context)

        for idx, stelle in zu_laden:
            url    = stelle["url"]
            firma  = stelle.get("firma", "?")
            titel  = stelle.get("titel", "?")
            status = stelle.get("status", 1)
            alter_rohtext = (stelle.get("rohtext") or "").strip()

            print(f"\n  {'─'*54}")
            print(f"  {firma}: {titel[:55]}")
            print(f"  Status {status} | Aktuell: {len(alter_rohtext)} Z. | {url[:60]}")

            rohtext = lade_rohtext_playwright(page, url)

            if rohtext and len(rohtext.strip()) >= MIN_ROHTEXT_LAENGE:
                # Jobtitel direkt von der Seite lesen (genauer als Link-Text)
                seiten_titel = _extrahiere_titel(page)
                if seiten_titel:
                    stellen[idx]["titel"] = seiten_titel
                    print(f"  🏷️  Titel: {seiten_titel[:70]}")

                stellen[idx]["rohtext"] = rohtext
                neuer_status = 2 if status <= 2 else status
                stellen[idx]["status"] = neuer_status

                geladen += 1
                print(f"  ✅ {len(rohtext)} Zeichen geladen (Status → {neuer_status})")

                upsert_stelle({
                    "url":    url,
                    "rohtext": rohtext,
                    "titel":  stellen[idx]["titel"],
                    "status": neuer_status,
                })

            elif rohtext:
                # Geladen aber zu kurz (z.B. Login-Wall, leere SPA)
                zu_kurz += 1
                print(f"  ⚠️  Nur {len(rohtext.strip())} Zeichen geladen – zu kurz, wird übersprungen")
                if not alter_rohtext:
                    upsert_stelle({"url": url, "nicht_ladbar": True})
            else:
                # Kompletter Fehler
                fehler += 1
                print(f"  ❌ Kein Inhalt geladen")
                if not alter_rohtext:
                    upsert_stelle({"url": url, "nicht_ladbar": True})

        browser.close()

    # JSON-Spiegel einmal am Ende aktualisieren (die DB ist pro Stelle schon
    # aktuell; der Export der großen stellen.json nach jeder Stelle war unnötig
    # teuer, v.a. auf SD-Karte).
    exportiere_stellen_json(STELLEN_JSON)
    exportiere_bekannte_json(BEKANNTE_JSON)

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Geladen:           {geladen}")
    print(f"  Zu kurz / gesperrt:{zu_kurz}")
    print(f"  Fehler:            {fehler}")
    print(f"  Weiter mit:        python vergaben_check.py  (optional)")
    print(f"                     python extraktor.py")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
