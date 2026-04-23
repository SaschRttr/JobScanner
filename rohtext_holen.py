"""
rohtext_holen.py  –  Schritt 1b: Rohtext für manuell eingetragene Stellen holen
=================================================================================
Verarbeitet alle Stellen in stellen.json die noch keinen Rohtext haben (status=1).
Nutzt Playwright (wie scanner.py) um JS-gerenderte Seiten zu lesen.

Nutzung:
  python rohtext_holen.py
"""

import argparse
import json
import platform
import sys
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright nicht installiert: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    from playwright_stealth import Stealth
    STEALTH_VERFUEGBAR = True
except ImportError:
    STEALTH_VERFUEGBAR = False

BASIS_PFAD      = Path(__file__).parent
STELLEN_JSON    = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON   = BASIS_PFAD / "bekannte_stellen.json"

COOKIE_SELEKTOREN = [
    "text=ALLES AKZEPTIEREN", "text=Alles akzeptieren",
    "text=Alle akzeptieren", "text=Alle Cookies akzeptieren",
    "text=Akzeptieren", "text=ABLEHNEN", "text=Ablehnen",
    "text=Nur notwendige", "text=Nur erforderliche",
    "text=Accept All", "text=Accept all", "text=Accept all cookies",
    "text=Accept Cookies", "text=Reject All", "text=Reject all",
    "text=I Accept", "text=Got it", "text=OK",
    "#onetrust-accept-btn-handler",
    "button[id*='cookie-accept']", "button[id*='accept-all']",
    "button[id*='onetrust-accept']",
]


def klick_cookie_banner(page):
    for sel in COOKIE_SELEKTOREN:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=400):
                btn.click()
                page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


def extrahiere_titel(page) -> str | None:
    """Versucht den echten Jobtitel aus der Seite zu lesen."""
    # 1. H1-Element
    try:
        h1 = page.locator("h1").first.inner_text(timeout=3000).strip()
        if h1 and len(h1) > 3:
            return h1
    except Exception:
        pass
    # 2. Page-Title (Browser-Tab), Trennzeichen herausschneiden
    try:
        titel = page.title().strip()
        # Häufige Muster: "Jobtitel | Firma" oder "Jobtitel – Firma"
        for trenner in [" | ", " - ", " – ", " — ", " :: "]:
            if trenner in titel:
                titel = titel.split(trenner)[0].strip()
                break
        if titel and len(titel) > 3:
            return titel
    except Exception:
        pass
    return None


def bereinige_rohtext(rohtext: str) -> str:
    """
    Versucht, den sinnvollen Stellentext aus dem rohen page.inner_text zu isolieren.
    Schneidet Navigations-/Metadaten-Müll am Anfang weg.
    """
    # Marker die auf den Start der eigentlichen Stellenbeschreibung hinweisen
    start_marker = [
        "Stellenbeschreibung:",
        "Ihre Aufgaben",
        "Deine Aufgaben",
        "Aufgaben:",
        "Was Sie erwartet",
        "Was dich erwartet",
        "Das erwartet Sie",
        "Das erwartet dich",
        "Wir suchen",
        "Wir bieten",
        "Über die Stelle",
        "Über uns",
        "Job Description",
        "Your responsibilities",
        "About the role",
    ]
    best_idx = len(rohtext)
    for marker in start_marker:
        idx = rohtext.find(marker)
        if 0 < idx < best_idx:
            best_idx = idx

    # Nur kürzen wenn Marker gefunden und nicht zu weit hinten (>80% des Textes)
    if best_idx < len(rohtext) * 0.8 and best_idx > 0:
        rohtext = rohtext[best_idx:]

    return rohtext


def hole_rohtext(page, url: str) -> str | None:
    try:
        # networkidle wartet bis keine Netzwerk-Requests mehr laufen (wichtig für SPAs)
        antwort = None
        try:
            antwort = page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:
            # Fallback: domcontentloaded wenn networkidle timeout
            antwort = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(6000)

        # 403/404/410 → sofort abbrechen, kein Retry
        if antwort and antwort.status in (403, 404, 410):
            print(f"  ❌ HTTP {antwort.status} – Seite blockiert/nicht vorhanden")
            return None

        page.wait_for_timeout(3000)
        klick_cookie_banner(page)
        page.wait_for_timeout(2000)

        rohtext = page.inner_text("body")

        # Prüfen ob Inhalt sinnvoll ist (nicht nur HTML-Gerüst oder zu kurz)
        if not rohtext or len(rohtext.strip()) < 200:
            # Nochmal warten und erneut versuchen
            page.wait_for_timeout(5000)
            rohtext = page.inner_text("body")

        zeilen    = rohtext.splitlines()
        bereinigt = []
        leer      = 0
        for z in zeilen:
            if z.strip() == "":
                leer += 1
                if leer <= 2:
                    bereinigt.append("")
            else:
                leer = 0
                bereinigt.append(z)

        ergebnis = "\n".join(bereinigt)
        print(f"  📄 Inhalt: {len(ergebnis)} Zeichen, erste 100: {repr(ergebnis[:100])}")
        return ergebnis
    except Exception as e:
        print(f"  ❌ Fehler beim Laden ({url[:70]}): {e}")
        return None


def lade_json(pfad: Path, standard):
    if pfad.exists():
        try:
            return json.loads(pfad.read_text(encoding="utf-8"))
        except Exception:
            pass
    return standard


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=None, help="Nur diese URL verarbeiten")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  ROHTEXT-HOLEN  –  Schritt 1b: Playwright-Fetch")
    if args.url:
        print(f"  Filter: nur {args.url[:60]}")
    print("=" * 60)

    stellen  = lade_json(STELLEN_JSON,  [])
    bekannte = lade_json(BEKANNTE_JSON, {})

    # Nur Stellen ohne Rohtext und status=1 (nicht bereits als nicht ladbar markiert)
    zu_laden = [
        (i, s) for i, s in enumerate(stellen)
        if not s.get("rohtext")
        and bekannte.get(s["url"], {}).get("status", 0) <= 1
        and not bekannte.get(s["url"], {}).get("nicht_ladbar")
        and (args.url is None or s["url"] == args.url)
    ]

    if not zu_laden:
        print("  ℹ️  Keine Stellen ohne Rohtext gefunden.")
        return

    print(f"  {len(zu_laden)} Stelle(n) ohne Rohtext – lade via Playwright...")

    geladen = 0

    BROWSER_ARGS = [
        "--no-sandbox",
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
    ]
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    with sync_playwright() as p:
        if platform.system() == "Linux":
            browser = p.chromium.launch(
                headless=True,
                executable_path="/usr/bin/chromium-browser",
                args=BROWSER_ARGS,
            )
        else:
            browser = p.chromium.launch(
                headless=True,
                args=BROWSER_ARGS,
            )

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="de-DE",
            user_agent=USER_AGENT,
            extra_http_headers={
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-User": "?1",
                "Sec-Fetch-Dest": "document",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        page = context.new_page()
        if STEALTH_VERFUEGBAR:
            try:
                Stealth().apply_stealth_sync(page)
            except Exception:
                pass
        # Zusätzlich: navigator.webdriver entfernen falls stealth nicht verfügbar
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        for idx, stelle in zu_laden:
            url = stelle["url"]
            print(f"\n  ▶️  {stelle.get('firma','?')}: {stelle.get('titel','?')[:60]}")
            print(f"     {url[:80]}")

            rohtext = hole_rohtext(page, url)

            if rohtext and len(rohtext) > 200:
                rohtext = bereinige_rohtext(rohtext)
                stellen[idx]["rohtext"] = rohtext

                # Jobtitel aus der Seite lesen
                titel = extrahiere_titel(page)
                if titel:
                    stellen[idx]["titel"] = titel
                    print(f"  🏷️  Titel: {titel[:70]}")

                bekannte.setdefault(url, {})["status"]     = 2
                bekannte[url]["rohtext_geholt_am"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                geladen += 1
                print(f"  ✅ {len(rohtext)} Zeichen geladen")

                # Auch DB aktualisieren
                try:
                    sys.path.insert(0, str(BASIS_PFAD))
                    from db import upsert_stelle
                    upsert_stelle({"url": url, "rohtext": rohtext, "titel": titel or stellen[idx]["titel"], "status": 2})
                except Exception as e:
                    print(f"  ⚠️  DB-Update fehlgeschlagen (nicht kritisch): {e}")

                # Zwischenspeichern
                STELLEN_JSON.write_text(
                    json.dumps(stellen, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                BEKANNTE_JSON.write_text(
                    json.dumps(bekannte, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            else:
                # URL nicht ladbar (403, Timeout, zu kurz) → nicht nochmal versuchen
                bekannte.setdefault(url, {})["nicht_ladbar"] = True
                BEKANNTE_JSON.write_text(
                    json.dumps(bekannte, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"  ⚠️  Kein verwertbarer Inhalt – als nicht ladbar markiert (wird übersprungen)")

        browser.close()

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Rohtexte geladen: {geladen} / {len(zu_laden)}")
    print(f"  Weiter mit:       python extraktor.py")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
