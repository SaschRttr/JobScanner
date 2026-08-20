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
import re
import sys
import urllib.parse
from pathlib import Path

BASIS_PFAD   = Path(__file__).parent
STELLEN_JSON = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON = BASIS_PFAD / "bekannte_stellen.json"

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright nicht installiert: pip install playwright && playwright install chromium")
    sys.exit(1)

from utils import klick_cookie_banner, standort_ausnahme_urls, speichere_json
from browser import (
    MIN_ROHTEXT_LAENGE, lade_pdf_text,
    starte_browser, neuer_context, neue_seite,
)

STANDORT_AUSNAHME_JSON = BASIS_PFAD / "standort_ausnahme.json"


def _entferne_standort_ausnahme(url: str) -> None:
    """Räumt den Standort-Ausnahme-Eintrag auf, den /stelle-einfuegen beim
    manuellen Hinzufügen gesetzt hat, wenn die Stelle wieder verworfen wird."""
    urls = standort_ausnahme_urls()
    if url in urls:
        urls.discard(url)
        speichere_json(STANDORT_AUSNAHME_JSON, sorted(urls))


# =============================================================================
# ROHTEXT LADEN
# =============================================================================

# Domain-spezifische Wartezeiten (ms) für SPAs die lange zum Rendern brauchen
_WARTE_MS: dict[str, int] = {
    "jobs.keysight.com":         8000,
    "jobs.infineon.com":         6000,
    "careers.te.com":            5000,
    "wd3.myworkdayjobs.com":     6000,
    "liebherr.com":              1500,
}
_WARTE_MS_DEFAULT = 4000

# Domains, die serverseitig gerendert werden, aber Tracking (usercentrics o.ä.)
# haben, das networkidle nie erreichen lässt. Für sie: direkt domcontentloaded,
# kein Cookie-Banner-Loop (der Text steht ohnehin im DOM). Spart ~20 s pro Seite.
_SCHNELL_LADEN_DOMAINS = ("liebherr.com",)


def _warte_fuer(url: str) -> int:
    for domain_teil, ms in _WARTE_MS.items():
        if domain_teil in url:
            return ms
    return _WARTE_MS_DEFAULT


# Domains, deren Job-Detailseiten nur eine generische Überschrift ("Job
# Details") statt des echten Jobtitels zeigen. _extrahiere_titel() prüft
# Kandidaten zwar gegen die URL, fällt aber ohne Treffer auf den ersten
# (ungeprüften) Kandidaten zurück – bei hitachirail.com enthält die URL nur
# jobId+Standort statt eines Titel-Slugs, daher kann der Abgleich nie greifen
# und der generische h1/title überschreibt den korrekten, per API gelieferten
# Titel. Für diese Domains wird der gescrapte Titel deshalb nie übernommen.
_KEIN_TITEL_UEBERSCHREIBEN: set[str] = {
    "hitachirail.com",
}


# Domain-spezifische CSS-Selektoren für den Arbeitsort, wenn er als eigenes
# Meta-Element im DOM steht statt im Fließtext. Bei Keysight (Phenom-basierte
# Karriereseite) steht der Ort z.B. in einem eigenen <li id="header-locations">
# neben dem Titel – nicht im h1 und ohne "Standort:"-Label, deshalb reißt
# Regex/KI-Extraktion aus dem Rohtext hier oft ins Leere oder rät falsch.
_ORT_SELEKTOR: dict[str, str] = {
    "jobs.keysight.com": "#header-locations",
}


def _extrahiere_ort(page, url: str) -> str | None:
    """Liest den Arbeitsort aus einem bekannten domain-spezifischen DOM-Element."""
    selektor = next((sel for teil, sel in _ORT_SELEKTOR.items() if teil in url), None)
    if not selektor:
        return None
    try:
        text = page.locator(selektor).first.inner_text(timeout=3000).strip()
        return text or None
    except Exception:
        return None


def _url_anpassen(url: str) -> str:
    """Domain-spezifische URL-Umschreibungen für bessere Inhalte."""
    # onlyfy-Widget-Job → Volltext-Detailseite. Wichtig: den KORREKTEN Firmen-
    # Subdomain beibehalten (früher hart bertrandtgroup → 404 bei anderen Firmen
    # wie Dürr Dental) und die Job-ID ohne Query-Parameter nehmen.
    if "onlyfy.jobs" in url:
        pfad = url.split("?")[0]
        m = re.search(r'/job/([a-z0-9]{16,})', pfad)
        host_m = re.match(r'(https?://[^/]+)', url)
        if m and host_m:
            return f"{host_m.group(1)}/job/show/{m.group(1)}/full?lang=de&mode=candidate"

    # Query-Parameter mit Wert "apply" entfernen: sie öffnen bei vielen ATS
    # (z.B. Phenom ?tcsource=apply) direkt das Bewerbungsformular statt der
    # Stellenbeschreibung. Generisch, plattformunabhängig.
    pr = urllib.parse.urlparse(url)
    if pr.query and "apply" in pr.query.lower():
        params = [(k, v) for k, v in urllib.parse.parse_qsl(pr.query, keep_blank_values=True)
                  if v.strip().lower() != "apply"]
        url = urllib.parse.urlunparse(pr._replace(query=urllib.parse.urlencode(params)))

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


def _slug_woerter(url: str) -> set[str]:
    """Wortmenge aus dem letzten URL-Pfadsegment (z.B. Job-Slug), als
    Vergleichsbasis um Titel-Kandidaten zu validieren."""
    pfad = url.split("?")[0].rstrip("/")
    segment = pfad.rsplit("/", 1)[-1]
    return {w for w in re.split(r"[^a-zA-Z0-9]+", segment.lower()) if len(w) >= 4}


def _extrahiere_titel(page, url: str = "") -> str | None:
    """Liest den echten Jobtitel von der geladenen Seite.
    Manche SPAs (z.B. Workday) zeigen im <h1> nur eine generische
    Seitenüberschrift statt des Jobtitels – deshalb wird jeder Kandidat
    gegen den URL-Slug geprüft (der i.d.R. den Jobtitel enthält) und nur
    bei Übereinstimmung übernommen."""
    slug = _slug_woerter(url) if url else set()

    def _passt_zur_url(kandidat: str) -> bool:
        if not slug:
            return True
        woerter = {w for w in re.split(r"[^a-zA-Z0-9]+", kandidat.lower()) if len(w) >= 4}
        return bool(woerter & slug)

    kandidaten = []
    try:
        h1 = page.locator("h1").first.inner_text(timeout=3000).strip()
        if h1 and len(h1) > 5:
            kandidaten.append(h1)
    except Exception:
        pass
    try:
        titel = page.title().strip()
        for trenner in [" | ", " - ", " – ", " — ", " :: "]:
            if trenner in titel:
                titel = titel.split(trenner)[0].strip()
                break
        if titel and len(titel) > 5:
            kandidaten.append(titel)
    except Exception:
        pass

    for k in kandidaten:
        if _passt_zur_url(k):
            return k
    return kandidaten[0] if kandidaten else None


def lade_rohtext_playwright(page, url: str) -> tuple[str | None, int | None]:
    """
    Lädt eine einzelne Stellenseite via Playwright.
    Gibt (rohtext, http_status) zurück; rohtext ist None wenn die Seite
    nicht geladen werden konnte. Der Status erlaubt dem Aufrufer, bei
    403/429 (WAF-Session-Sperre) mit frischem Context neu zu versuchen.
    """
    if url.lower().endswith(".pdf"):
        return lade_pdf_text(url), None

    lade_url = _url_anpassen(url)
    warte_ms  = _warte_fuer(lade_url)

    schnell = any(d in lade_url for d in _SCHNELL_LADEN_DOMAINS)

    try:
        antwort = None
        if schnell:
            # SSR-Seiten mit Tracking (z.B. Liebherr/usercentrics): networkidle wird
            # NIE erreicht und der Cookie-Banner blockiert den body-Text nicht (der
            # Stellentext ist serverseitig schon im DOM). Direkt domcontentloaded,
            # kurze Wartezeit, KEIN Cookie-Banner-Loop → spart ~20 s pro Seite.
            antwort = page.goto(lade_url, wait_until="domcontentloaded", timeout=30000)
        else:
            try:
                # Kurzes networkidle-Timeout: tracking-lastige Seiten erreichen
                # networkidle NIE und würden sonst die vollen Sekunden warten (sieht
                # eingefroren aus). Nach 12 s auf domcontentloaded zurückfallen.
                antwort = page.goto(lade_url, wait_until="networkidle", timeout=12000)
            except Exception:
                antwort = page.goto(lade_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(4000)

        status = antwort.status if antwort else None

        if status in (403, 404, 410, 429):
            print(f"  ❌ HTTP {status} – nicht erreichbar")
            return None, status

        page.wait_for_timeout(warte_ms)
        if not schnell:
            klick_cookie_banner(page)
        page.wait_for_timeout(2000)

        # onlyfy-Wrapper mit ?jh=<jobid>: der Hash IST die onlyfy-Job-ID. Der
        # richtige onlyfy-Subdomain steckt im eingebetteten Widget-iframe – von
        # dort holen und direkt die Volltext-Detailseite laden (sonst bekäme man
        # nur die Wrapper-Hülle ohne Stellentext). Generisch für jede onlyfy-Firma.
        if "jh=" in lade_url and "onlyfy.jobs" not in lade_url:
            jh = urllib.parse.parse_qs(urllib.parse.urlparse(lade_url).query).get("jh", [""])[0]
            onlyfy_host = ""
            for f in page.frames:
                if "onlyfy.jobs" in (f.url or ""):
                    mo = re.match(r'(https?://[^/]+)', f.url)
                    onlyfy_host = mo.group(1) if mo else ""
                    break
            if jh and onlyfy_host:
                detail = f"{onlyfy_host}/job/show/{jh}/full?lang=de&mode=candidate"
                print(f"  🔎 onlyfy-Wrapper (?jh=) → Detailseite: {detail[:70]}")
                try:
                    antwort = page.goto(detail, wait_until="domcontentloaded", timeout=40000)
                    status = antwort.status if antwort else status
                    page.wait_for_timeout(2500)
                except Exception as e:
                    print(f"  ⚠️  onlyfy-Detailseite nicht ladbar: {e}")

        rohtext = page.inner_text("body")

        # Zu kurzer Inhalt → nochmal warten (SPA noch nicht fertig)
        if not rohtext or len(rohtext.strip()) < 300:
            page.wait_for_timeout(6000)
            rohtext = page.inner_text("body")

        # jobware.net rendert den eigentlichen Stellentext in einem
        # separaten <iframe src="/jobsearch/embed/job/...">, nicht im
        # Haupt-Dokument – body-Text ist dort nur die Seiten-Hülle
        # (Cookie-Banner, Titel, "Ähnliche Anzeigen").
        if "jobware.net" in lade_url:
            for frame in page.frames:
                if "embed/job" in frame.url:
                    try:
                        iframe_text = frame.locator("body").inner_text()
                        if iframe_text and len(iframe_text.strip()) > 200:
                            rohtext = iframe_text
                    except Exception:
                        pass
                    break

        if not rohtext or len(rohtext.strip()) < 100:
            return None, status

        return _bereinige_rohtext(rohtext), status

    except Exception as e:
        print(f"  ❌ Playwright-Fehler: {e}")
        return None, None


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
                   exportiere_bekannte_json, erstelle_schema, loesche_stelle
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

    # Diese Navigation-Header (Sec-Fetch-*, Upgrade-Insecure-Requests) gelten
    # in Playwright context-weit für JEDEN Request – auch für die internen
    # XHR/Fetch-Aufrufe, mit denen SPAs (z.B. Workday) Jobdaten nachladen.
    # Ein echter Browser schickt dort andere Sec-Fetch-Werte als bei der
    # Seiten-Navigation; erzwingt man sie überall, liefern SPA-Backends wie
    # Workday leeren Content zurück. Deshalb nur für die WAF-Domain(s)
    # aktivieren, für die sie ursprünglich gedacht waren.
    _WAF_DOMAINS = ("advantest-career.de",)
    _EXTRA_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Upgrade-Insecure-Requests": "1",
    }

    def _braucht_waf_header(url: str) -> bool:
        return any(d in url for d in _WAF_DOMAINS)

    with sync_playwright() as p:
        browser = starte_browser(p)
        aktueller_waf_modus = None
        context = None
        page = None

        for idx, stelle in zu_laden:
            url    = stelle["url"]
            firma  = stelle.get("firma", "?")
            titel  = stelle.get("titel", "?")
            status = stelle.get("status", 1)
            alter_rohtext = (stelle.get("rohtext") or "").strip()

            print(f"\n  {'─'*54}")
            print(f"  {firma}: {titel[:55]}")
            print(f"  Status {status} | Aktuell: {len(alter_rohtext)} Z. | {url[:60]}")

            waf_modus = _braucht_waf_header(url)
            if context is None or waf_modus != aktueller_waf_modus:
                if page:
                    page.close()
                if context:
                    context.close()
                context = neuer_context(browser, extra_headers=_EXTRA_HEADERS if waf_modus else None)
                page = neue_seite(context)
                aktueller_waf_modus = waf_modus

            rohtext, http_status = lade_rohtext_playwright(page, url)

            if rohtext is None and http_status in (403, 429):
                # WAF-Session-Sperre (z.B. jobs.advantest-career.de): dieselben
                # Cookies bleiben gesperrt, eine frische Session kommt sofort
                # wieder rein (wie ein neues Inkognito-Fenster) → Context
                # wegwerfen und einmal mit neuen Cookies nachfassen.
                print(f"  🔄 HTTP {http_status} – frische Session, zweiter Versuch...")
                page.close()
                context.close()
                context = neuer_context(browser, extra_headers=_EXTRA_HEADERS if waf_modus else None)
                page = neue_seite(context)
                rohtext, http_status = lade_rohtext_playwright(page, url)

            if rohtext and len(rohtext.strip()) >= MIN_ROHTEXT_LAENGE:
                # Jobtitel direkt von der Seite lesen (genauer als Link-Text) –
                # außer bei Domains, wo das bekanntermaßen nur eine generische
                # Überschrift liefert (siehe _KEIN_TITEL_UEBERSCHREIBEN oben).
                if not any(d in url for d in _KEIN_TITEL_UEBERSCHREIBEN):
                    seiten_titel = _extrahiere_titel(page, url)
                    if seiten_titel:
                        stellen[idx]["titel"] = seiten_titel
                        print(f"  🏷️  Titel: {seiten_titel[:70]}")

                stellen[idx]["rohtext"] = rohtext
                neuer_status = 2 if status <= 2 else status
                stellen[idx]["status"] = neuer_status

                geladen += 1
                print(f"  ✅ {len(rohtext)} Zeichen geladen (Status → {neuer_status})")

                update_felder = {
                    "url":    url,
                    "rohtext": rohtext,
                    "titel":  stellen[idx]["titel"],
                    "status": neuer_status,
                    "nicht_ladbar": False,
                    "manuell_neu": False,
                }

                seiten_ort = _extrahiere_ort(page, url)
                if seiten_ort:
                    stellen[idx]["arbeitsort"] = seiten_ort
                    update_felder["arbeitsort"] = seiten_ort
                    print(f"  📍 Ort: {seiten_ort}")

                upsert_stelle(update_felder)

            elif rohtext:
                # Geladen aber zu kurz (z.B. Login-Wall, leere SPA)
                zu_kurz += 1
                print(f"  ⚠️  Nur {len(rohtext.strip())} Zeichen geladen – zu kurz, wird übersprungen")
                if not alter_rohtext:
                    if stelle.get("manuell_neu"):
                        # Manuell hinzugefügte Stelle, die noch nie erfolgreich geladen
                        # wurde – kein Stub-Eintrag, der als leere Karteileiche im
                        # Report hängen bleibt, sondern direkt wieder verwerfen.
                        loesche_stelle(url)
                        _entferne_standort_ausnahme(url)
                        print(f"  🗑️  Manuell hinzugefügte Stelle verworfen (nicht ladbar)")
                    else:
                        upsert_stelle({"url": url, "nicht_ladbar": True})
            else:
                # Kompletter Fehler
                fehler += 1
                print(f"  ❌ Kein Inhalt geladen")
                if not alter_rohtext:
                    if stelle.get("manuell_neu"):
                        loesche_stelle(url)
                        _entferne_standort_ausnahme(url)
                        print(f"  🗑️  Manuell hinzugefügte Stelle verworfen (nicht ladbar)")
                    else:
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
