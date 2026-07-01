"""
scanner.py  –  Job-Scanner (Schritt 1)
=======================================
Lädt Stellenbörsen per Playwright, findet Job-Links, lädt Rohtexte
und speichert alles als JSON.

Ausgabe-Dateien (im selben Ordner wie dieses Skript):
  stellen.json          – alle gefundenen Stellen mit Rohtext
  bekannte_stellen.json – Status-Tracker pro URL
  strukturen.json       – gelernte Job-Link-Muster pro Domain

Status-Codes:
  0 = nicht mehr gefunden (Stelle vergeben/offline, kein expliziter Check)
  1 = gefunden (nur Link)
  2 = Rohtext gespeichert
  3 = Stellentext extrahiert
  4 = KI-Bewertung ≥ 70 % (bewerben empfohlen) – wird jeden Scan auf Verfügbarkeit geprüft
  5 = KI-Bewertung < 70 % (nicht bewerben / geringer Match)
  6 = beworben, Stelle noch offen/erreichbar
  7 = beworben + nicht mehr erreichbar (vergaben)
  8 = beworben + Absage erhalten + vergaben
  9 = vergaben, nie beworben (explizit geprüft)

Nutzung:
  python scanner.py

Voraussetzungen:
  pip install playwright playwright-stealth anthropic
  playwright install chromium
"""

import argparse
import json
import re
import sys
import io
import urllib.request
import urllib.error
import platform
from pathlib import Path
from urllib.parse import urlparse
import urllib.parse

from utils import (
    lade_config, lade_json, speichere_json, jetzt, domain,
    berechne_standort, standort_ablehnungsgrund,
    text_matched, ist_ausgeschlossen,
    klick_cookie_banner,
)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright nicht installiert:")
    print("  pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    from playwright_stealth import Stealth
except ImportError:
    print("playwright-stealth nicht installiert:")
    print("  pip install playwright-stealth")
    sys.exit(1)

try:
    import anthropic as anthropic_lib
except ImportError:
    anthropic_lib = None

try:
    import pypdf
    PYPDF_OK = True
except ImportError:
    PYPDF_OK = False


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD        = Path(__file__).parent
STELLEN_JSON      = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON     = BASIS_PFAD / "bekannte_stellen.json"
STRUKTUREN_JSON   = BASIS_PFAD / "strukturen.json"
API_FIRMEN_PFAD   = Path(__file__).parent / "api_firmen.json"


# =============================================================================
# BEKANNTE URL-MUSTER FÜR JOB-DETAIL-LINKS (Heuristik)
# =============================================================================

JOB_LINK_MUSTER = [
    "/job/", "/jobs/", "/job-", "/offer/", "/offer-redirect/",
    "/details/", "/jobboerse/", "/job-detail/", "/stelle/", "/stellen/",
    "/stellenangebot", "/stellenausschreibung", "/vacancy/", "/vacancies/",
    "/karriere/lesen/", "/FolderDetail/", "ac=jobad", "jobId=",
    "/R0", "251563-", "ashbyhq.com/sereact/", "dvinci-hr.com/de/jobs/",
    "zsw-bw-jobs.de/job-", "/careers/job/", "/career/job/",
]

MIN_TITEL_LAENGE = 10


# =============================================================================
# API-FIRMEN KONFIGURATION
# =============================================================================
# TODO: Strategie entwickeln um API-Endpunkte automatisch zu erkennen,
#       damit Anwender nur die URL der Stellenbörse angeben muss.
#       Aktuell müssen API-Firmen manuell konfiguriert werden.

def lade_api_firmen(config: dict) -> list:
    """Lädt API-Firmen aus config.txt ([api_firmen]-Abschnitt) oder api_firmen.json als Fallback."""
    if config.get("api_firmen"):
        return config["api_firmen"]
    if API_FIRMEN_PFAD.exists():
        try:
            return json.loads(API_FIRMEN_PFAD.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"❌ Fehler beim Laden von api_firmen.json: {e}")
    return []


def scanne_api_firma(api_config: dict, bekannte_urls: set, config: dict) -> tuple[list, list]:
    """Scannt eine Firma über deren JSON-API (kein Playwright nötig).
    Gibt (passende_stellen, ausgeschlossene_stellen) zurück."""
    name = api_config["name"]
    print(f"\n{'='*60}")
    print(f"  Scanne: {name} (API)")
    print(f"{'='*60}")

    stellen = []
    ausgeschlossen = []
    gesehen = set()

    for seite in range(api_config["seiten"]):
        seiten_start = api_config.get("seiten_start", 0)
        seiten_wert  = seiten_start + seite * api_config["seiten_schrittweite"]

        try:
            if api_config.get("methode", "POST").upper() == "GET":
                params = dict(api_config["payload"])
                params[api_config["seiten_parameter"]] = seiten_wert
                query = urllib.parse.urlencode(params)
                url_mit_seite = f"{api_config['url']}?{query}"
                req = urllib.request.Request(
                    url_mit_seite,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    },
                    method="GET"
                )
            else:
                payload = dict(api_config["payload"])
                payload[api_config["seiten_parameter"]] = seiten_wert
                extra_headers = api_config.get("headers", {})
                req = urllib.request.Request(
                    api_config["url"],
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "*/*",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        **extra_headers,
                    },
                    method="POST"
                )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  ❌ API-Fehler Seite {seite+1}: {e}")
            break

        jobs = data
        for schluessel in api_config["antwort_pfad"]:
            jobs = jobs.get(schluessel, {}) if isinstance(jobs, dict) else {}
        if not jobs:
            print(f"  ℹ️  Keine weiteren Jobs auf Seite {seite+1}")
            break

        unterebene = api_config.get("antwort_unterebene")
        if unterebene:
            jobs = [j.get(unterebene, {}) for j in jobs]

        print(f"  📋 Seite {seite+1}: {len(jobs)} Jobs gefunden")

        for job in jobs:
            titel = _get_nested(job, api_config["feld_titel"])

            standort_roh = _get_nested(job, api_config.get("feld_standort", ""))
            standort = " ".join(standort_roh) if isinstance(standort_roh, list) else standort_roh

            job_id = str(_get_nested(job, api_config.get("feld_id", "")))
            url_vorlage = api_config["url_vorlage"]
            titel_fuer_url = titel.lower().replace(" ", "-")
            if api_config.get("feld_url_titel"):
                titel_fuer_url = job.get(api_config["feld_url_titel"], titel_fuer_url)
            if url_vorlage == "{id}":
                url = job_id
            else:
                url = (url_vorlage
                    .replace("{id}", job_id)
                    .replace("{titel}", titel_fuer_url)
                    .replace("{url_titel}", titel_fuer_url))

            if not url or not url.startswith("http"):
                print(f"  ⚠️  Leere/ungültige URL für '{titel[:50]}' – übersprungen (id={job_id!r})")
                continue

            if titel in gesehen:
                continue
            gesehen.add(titel)

            treffer = text_matched(titel, config["suchbegriffe"])

            if treffer:
                _np_grund = ""
                if ist_ausgeschlossen(titel, config["ausschlussbegriffe"]):
                    for _b in config["ausschlussbegriffe"]:
                        if (all(_t in titel.lower() for _t in _b.split("+")) if "+" in _b else _b in titel.lower()):
                            _np_grund = f"Ausschlussbegriff: '{_b}'"
                            break
                if not _np_grund:
                    _np_grund = standort_ablehnungsgrund(standort, config["erlaubte_standorte"], config["verbotene_standorte"])
                if _np_grund:
                    ausgeschlossen.append({"firma": name, "titel": titel, "url": url,
                                           "treffer": treffer, "nicht_passend_grund": _np_grund})
                    print(f"  🚫 Nicht passend: {titel[:70]}")
                else:
                    ist_neu = url not in bekannte_urls

                    # Rohtext direkt aus API-Feldern zusammensetzen (verhindert 403 beim Playwright-Laden)
                    rohtext = None
                    feld_rohtext = api_config.get("feld_rohtext")
                    if feld_rohtext:
                        teile = [str(_get_nested(job, f)).strip()
                                 for f in (feld_rohtext if isinstance(feld_rohtext, list) else [feld_rohtext])]
                        rohtext = "\n\n".join(t for t in teile if t and t != "None") or None
                    if standort and rohtext:
                        rohtext = f"Standort: {standort}\n\n{rohtext}"
                    elif standort:
                        rohtext = f"Standort: {standort}"

                    stellen.append({
                        "firma": name,
                        "titel": titel,
                        "url": url,
                        "arbeitsort": standort,
                        "standort": berechne_standort(standort, config["erlaubte_standorte"], config["verbotene_standorte"]),
                        "treffer": treffer,
                        "neu": ist_neu,
                        "rohtext": rohtext,
                    })
                    neu_label = "🆕 " if ist_neu else "   "
                    print(f"  ✅ {neu_label}{titel}")
                    print(f"     Treffer: {', '.join(treffer)}")

    if not stellen and not ausgeschlossen:
        print(f"  ℹ️  Keine passenden Stellen bei {name}")

    return stellen, ausgeschlossen


# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def root_domain(url: str) -> str:
    """Gibt die Root-Domain zurück (z.B. 'helmut-fischer.com' für 'stellenangebote.helmut-fischer.com')."""
    teile = urlparse(url).netloc.replace("www.", "").split(".")
    return ".".join(teile[-2:]) if len(teile) >= 2 else teile[0]


def ist_job_link(href: str) -> bool:
    return any(m in href for m in JOB_LINK_MUSTER)


# Bewerbungsformular-URLs ausschließen (z.B. rexx-systems -de-f\d+, generische Apply-Pfade)
_FORM_MUSTER = [r'-de-f\d+', r'/apply/', r'/bewerben$', r'/application/']

def ist_bewerbungslink(href: str) -> bool:
    return any(re.search(m, href) for m in _FORM_MUSTER)


_BUTTON_TEXTE = {
    "jetzt bewerben", "bewerben", "drucken", "drucken / weiterempfehlen",
    "zurück", "zurück zur übersicht", "zur initiativbewerbung",
    "zum jobalert", "mehr erfahren", "details", "apply now", "print",
}

def titel_aus_slug(href: str) -> str:
    """Extrahiert einen lesbaren Titel aus dem URL-Slug als Fallback."""
    pfad = urlparse(href).path
    slug = pfad.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.[a-z]+$", "", slug)            # .html entfernen
    slug = re.sub(r"-[a-z]{0,3}-[jf]?\d+$", "", slug)  # -de-j1860 entfernen
    slug = re.sub(r"-\d+$", "", slug)                # trailing -1234 entfernen
    return slug.replace("-", " ").strip()


def _get_nested(obj: dict, pfad: str, standard="") -> any:
    """Liest einen verschachtelten Wert per Punkt-Notation: 'data.title' → obj['data']['title']."""
    for schluessel in pfad.split("."):
        if not isinstance(obj, dict):
            return standard
        obj = obj.get(schluessel, standard)
    return obj if obj is not None else standard


# =============================================================================
# KI: JOB-LINK-MUSTER LERNEN
# =============================================================================

def ki_lerne_muster(domain_name: str, beispiel_links: list, api_key: str) -> str | None:
    if not api_key or anthropic_lib is None:
        return None

    links_text = "\n".join(beispiel_links[:30])
    prompt = f"""Hier sind bis zu 30 Links von der Domain '{domain_name}':

{links_text}

Analysiere die Links und finde das kürzeste gemeinsame URL-Teilstück,
das NUR in Job-Detail-Links vorkommt (nicht in Navigation, Login, etc.).

Wichtige Regeln:
- Gib einen echten Teilstring zurück, der wörtlich in den URLs vorkommt
- KEINE Platzhalter wie {{id}}, :id oder [slug]
- Zahlen in URLs sind OK – nimm den stabilen Präfix davor (z.B. "/Vacancies/" statt "/Vacancies/1593/")
- Möglichst kurz, aber eindeutig

Antworte NUR als JSON ohne Markdown:
{{"muster": "/das/muster/"}}

Wenn kein eindeutiges Muster erkennbar ist: {{"muster": null}}"""

    try:
        client = anthropic_lib.Anthropic(api_key=api_key)
        antwort = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        text = antwort.content[0].text.strip()
        text = text.removeprefix("```json").removesuffix("```").strip()
        ergebnis = json.loads(text)
        return ergebnis.get("muster")
    except Exception as e:
        print(f"  ⚠️  KI-Mustererkennung fehlgeschlagen: {e}")
        return None


# =============================================================================
# STELLENBÖRSE SCANNEN
# =============================================================================

def scanne_boerse(page, firma: dict, strukturen: dict, config: dict) -> tuple[list, list]:
    """Scannt eine Stellenbörse per Playwright.
    Gibt (passende_stellen, ausgeschlossene_stellen) zurück."""
    name = firma["name"]
    url_boerse = firma["url"]
    dom = domain(url_boerse)

    print(f"\n{'='*60}")
    print(f"  Scanne: {name}")
    print(f"{'='*60}")

    try:
        page.goto(url_boerse, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"  ❌ Seite nicht erreichbar: {e}")
        return [], []

    page.wait_for_timeout(3000)
    klick_cookie_banner(page)

    # Oracle Recruiting Cloud (z.B. Nokia) lädt Jobs per AJAX nach DOM-Load
    if any(d in url_boerse for d in ("nokia.com", "oraclecloud.com")):
        print("  ⏳ Oracle CX – warte auf Netzwerk-Idle...")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

    print("  📜 Scrolle...")
    for _ in range(8):
        page.evaluate("window.scrollBy(0, 1200)")
        page.wait_for_timeout(2500)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(2000)

    alle_links = page.evaluate("""() =>
        [...document.querySelectorAll('a[href]')].map(a => {
            let text = (a.innerText || a.getAttribute('aria-label') || '').trim();
            if (text.length < 10) {
                const parent = a.closest('li, article, [role="listitem"]');
                if (parent) {
                    const h = parent.querySelector('h1,h2,h3,h4,[class*="title"],[class*="name"]');
                    if (h) text = (h.innerText || '').trim();
                    if (text.length < 10)
                        text = [...parent.childNodes]
                            .map(n => (n.textContent || '').trim())
                            .find(t => t.length >= 10) || text;
                }
            }
            return { href: a.href, text };
        })
    """)
    print(f"  🔗 {len(alle_links)} Links gesamt")

    # Job-Link-Muster bestimmen
    muster = strukturen.get(dom, {}).get("link_muster")

    def muster_trifft(href: str, m: str) -> bool:
        try:
            return bool(re.search(m, href))
        except re.error:
            return m in href

    if muster:
        print(f"  ✅ Bekanntes Muster: '{muster}'")
        kandidaten = [l for l in alle_links if muster_trifft(l["href"], muster)]
    else:
        kandidaten = [l for l in alle_links if ist_job_link(l["href"])]
        if kandidaten:
            print(f"  ✅ Heuristik: {len(kandidaten)} Job-Links erkannt")
        else:
            print(f"  🤖 Unbekannte Domain – frage KI...")
            alle_hrefs = list({l["href"] for l in alle_links if len(l["href"]) > 30})
            muster = ki_lerne_muster(dom, alle_hrefs, config["api_key"])
            if muster:
                print(f"  ✅ KI-Muster gelernt: '{muster}'")
                strukturen.setdefault(dom, {})["link_muster"] = muster
                strukturen.setdefault(dom, {})["gelernt_am"] = jetzt()
                kandidaten = [l for l in alle_links if muster_trifft(l["href"], muster)]
            else:
                print(f"  ⚠️  Kein Muster gefunden – überspringe {name}")
                return [], []

    # Domain-Filter: nur Links der eigenen Root-Domain übernehmen
    rd = root_domain(url_boerse)
    vor_filter = len(kandidaten)
    kandidaten = [l for l in kandidaten if root_domain(l["href"]) == rd]
    if len(kandidaten) < vor_filter:
        print(f"  🔒 Domain-Filter: {vor_filter - len(kandidaten)} Fremd-Links entfernt (nur '{rd}' erlaubt)")

    # Bewerbungsformular-Links rausfiltern
    kandidaten = [l for l in kandidaten if not ist_bewerbungslink(l["href"])]

    # Fallback: PDF-Links mit jobrelevanten Begriffen im Pfad
    if not kandidaten:
        pdf_begriffe = ("stellenausschreibung", "ausschreibung", "karriere",
                        "job", "stelle", "position", "bewerbung", "wp-content/uploads")
        for l in alle_links:
            href_lower = l["href"].lower()
            if href_lower.endswith(".pdf") and any(b in href_lower for b in pdf_begriffe):
                dateiname = l["href"].rstrip("/").split("/")[-1]
                titel = dateiname[:-4].replace("-", " ").replace("_", " ")
                titel = titel[:1].upper() + titel[1:] if titel else dateiname
                kandidaten.append({"href": l["href"], "text": titel, "is_pdf": True})
        if kandidaten:
            print(f"  📄 PDF-Fallback: {len(kandidaten)} PDF-Stelle(n) gefunden")

    print(f"  📋 {len(kandidaten)} Kandidaten")

    gefunden = []
    ausgeschlossen = []
    gesehen_urls = set()
    gesehen_titel = set()

    for link in kandidaten:
        href = link["href"].split("#")[0].rstrip("/") or link["href"]
        titel_roh = link["text"]

        ist_pdf_link = link.get("is_pdf") or href.lower().endswith(".pdf")

        zeilen = [z.strip() for z in titel_roh.split("\n") if len(z.strip()) >= MIN_TITEL_LAENGE]
        titel = zeilen[0] if zeilen else titel_roh.strip()

        # Bei PDF-Links immer Dateinamen als Titel verwenden (Linktext ist oft "Mehr erfahren")
        if ist_pdf_link:
            dateiname = href.rstrip("/").split("/")[-1]
            titel = dateiname[:-4].replace("-", " ").replace("_", " ").strip()
            titel = titel[:1].upper() + titel[1:] if titel else dateiname
        elif not titel or len(titel) < MIN_TITEL_LAENGE or titel.lower() in _BUTTON_TEXTE:
            titel = titel_aus_slug(href)

        if not titel or len(titel) < MIN_TITEL_LAENGE:
            continue
        if href in gesehen_urls or titel in gesehen_titel:
            continue
        gesehen_urls.add(href)
        gesehen_titel.add(titel)

        # Standort aus Link-Text extrahieren: letzte Zeile nach dem Titel (z.B. "Böblingen")
        standort_aus_text = zeilen[-1] if len(zeilen) >= 2 and zeilen[-1] != titel else ""

        treffer = text_matched(titel, config["suchbegriffe"])

        # PDF-Stellen: Dateiname enthält selten Suchbegriffe → immer einschließen
        if not treffer and not ist_pdf_link:
            continue
        if not treffer:
            treffer = ["pdf"]
        # Standort nur prüfen wenn bekannt (leer = durchlassen)
        _np_grund = ""
        if ist_ausgeschlossen(titel, config["ausschlussbegriffe"]):
            for _b in config["ausschlussbegriffe"]:
                if (all(_t in titel.lower() for _t in _b.split("+")) if "+" in _b else _b in titel.lower()):
                    _np_grund = f"Ausschlussbegriff: '{_b}'"
                    break
        if not _np_grund:
            _np_grund = standort_ablehnungsgrund(standort_aus_text, config["erlaubte_standorte"], config["verbotene_standorte"])
        if _np_grund:
            ausgeschlossen.append({"firma": name, "titel": titel, "url": href,
                                   "treffer": treffer, "nicht_passend_grund": _np_grund})
            print(f"  🚫 Nicht passend: {titel[:70]}")
            continue

        gefunden.append({"firma": name, "titel": titel, "url": href,
                         "treffer": treffer, "arbeitsort": standort_aus_text,
                         "standort": berechne_standort(standort_aus_text, config["erlaubte_standorte"], config["verbotene_standorte"])})
        print(f"  ✅ {titel[:70]}")
        print(f"     Treffer: {', '.join(treffer)}")

    if not gefunden and not ausgeschlossen:
        print(f"  ℹ️  Keine passenden Stellen.")
        for k in kandidaten[:10]:
            t = k["text"].split("\n")[0].strip()
            print(f"     - {t[:80]}")

    return gefunden, ausgeschlossen


def scanne_hr4you_firma(api_config: dict, bekannte_urls: set, config: dict) -> tuple[list, list]:
    """Scannt eine Firma über die HR4YOU API.
    Die API gibt pro Seite ein html-Feld zurück, das Job-Links als <a>-Tags enthält.
    Gibt (passende_stellen, ausgeschlossene_stellen) zurück."""
    name      = api_config["name"]
    basis_url = api_config["basis_url"].rstrip("/")
    api_url   = api_config["url"]
    params_basis = api_config.get("params", {})

    print(f"\n{'='*60}")
    print(f"  Scanne: {name} (HR4YOU)")
    print(f"{'='*60}")

    stellen      = []
    ausgeschlossen = []
    gesehen      = set()
    seite        = 1
    max_seite    = 1

    while seite <= max_seite:
        params = {**params_basis, "page": seite}
        url_mit_seite = f"{api_url}?{urllib.parse.urlencode(params)}"

        try:
            req = urllib.request.Request(
                url_mit_seite,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  ❌ Fehler Seite {seite}: {e}")
            break

        if seite == 1:
            max_seite = int(data.get("maxPage", 1))
            print(f"  📋 {data.get('amount', '?')} Jobs gesamt, {max_seite} Seite(n)")

        zeilen = re.findall(r'<tr\b[^>]*>(.*?)</tr>', data.get("html", ""), re.DOTALL)
        print(f"  📋 Seite {seite}/{max_seite}: {len(zeilen)} Zeilen")

        for zeile in zeilen:
            link_m = re.search(r'onclick="window\.open\(\'([^\']+)\'[^"]*"[^>]*>(.*?)</a>', zeile, re.DOTALL)
            if not link_m:
                continue
            import html as _html
            raw_url = link_m.group(1)
            id_m = re.search(r'/job/view/(\d+)', raw_url)
            url  = f"{basis_url}/job/view/{id_m.group(1)}" if id_m else raw_url.split('?')[0]
            titel = _html.unescape(re.sub(r'<[^>]+>', '', link_m.group(2)).strip())

            tds      = re.findall(r'<td\b[^>]*>(.*?)</td>', zeile, re.DOTALL)
            standort = _html.unescape(re.sub(r'<[^>]+>', '', tds[2]).strip()) if len(tds) >= 3 else ""

            if url in gesehen:
                continue
            gesehen.add(url)

            treffer = text_matched(titel, config["suchbegriffe"])
            if not treffer:
                continue

            _np_grund = ""
            if ist_ausgeschlossen(titel, config["ausschlussbegriffe"]):
                for _b in config["ausschlussbegriffe"]:
                    if (all(_t in titel.lower() for _t in _b.split("+")) if "+" in _b else _b in titel.lower()):
                        _np_grund = f"Ausschlussbegriff: '{_b}'"
                        break
            if not _np_grund:
                _np_grund = standort_ablehnungsgrund(standort, config["erlaubte_standorte"], config["verbotene_standorte"])
            if _np_grund:
                ausgeschlossen.append({"firma": name, "titel": titel, "url": url,
                                       "treffer": treffer, "nicht_passend_grund": _np_grund})
                print(f"  🚫 Nicht passend: {titel[:70]}")
            else:
                ist_neu = url not in bekannte_urls
                stellen.append({
                    "firma": name, "titel": titel, "url": url,
                    "arbeitsort": standort,
                    "standort": berechne_standort(standort, config["erlaubte_standorte"], config["verbotene_standorte"]),
                    "treffer": treffer,
                    "neu": ist_neu, "rohtext": None,
                })
                neu_label = "🆕 " if ist_neu else "   "
                print(f"  ✅ {neu_label}{titel}")
                if standort:
                    print(f"     📍 {standort}")
                print(f"     Treffer: {', '.join(treffer)}")

        seite += 1

    if not stellen and not ausgeschlossen:
        print(f"  ℹ️  Keine passenden Stellen bei {name}")

    return stellen, ausgeschlossen


def scanne_workday_firma(api_config: dict, bekannte_urls: set, config: dict) -> tuple[list, list]:
    """Scannt eine Firma über die Workday JSON-API.
    Gibt (passende_stellen, ausgeschlossene_stellen) zurück."""
    name = api_config["name"]
    tenant = api_config["tenant"]
    portal = api_config["portal"]

    wd_ver = api_config.get("wd_version", "wd3")
    api_url = f"https://{tenant}.{wd_ver}.myworkdayjobs.com/wday/cxs/{tenant}/{portal}/jobs"
    basis_url = f"https://{tenant}.{wd_ver}.myworkdayjobs.com"

    print(f"\n{'='*60}")
    print(f"  Scanne: {name} (Workday API)")
    print(f"{'='*60}")

    stellen = []
    ausgeschlossen = []
    gesehen = set()
    limit = api_config["payload"].get("limit", 20)

    for seite in range(api_config["seiten"]):
        payload = dict(api_config["payload"])
        payload["offset"] = seite * limit

        try:
            req = urllib.request.Request(
                api_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  ❌ API-Fehler Seite {seite+1}: {e}")
            break

        total = data.get("total", 0)
        jobs = data.get("jobPostings", [])

        if not jobs:
            print(f"  ℹ️  Keine weiteren Jobs auf Seite {seite+1}")
            break

        if seite > 0 and payload["offset"] >= total:
            break

        print(f"  📋 Seite {seite+1}: {len(jobs)} Jobs ({payload['offset']+1}–{payload['offset']+len(jobs)} von {total})")

        for job in jobs:
            titel = job.get("title", "")
            standort = job.get("locationsText", "")
            external_path = job.get("externalPath", "")
            locale = api_config.get("locale")
            if locale:
                url = f"{basis_url}/{locale}/{portal}{external_path}"
            else:
                url = basis_url + external_path

            job_key = external_path.rstrip("/").split("/")[-1] if external_path else titel
            if job_key in gesehen:
                continue
            gesehen.add(job_key)

            treffer = text_matched(titel, config["suchbegriffe"])

            if treffer:
                _np_grund = ""
                if ist_ausgeschlossen(titel, config["ausschlussbegriffe"]):
                    for _b in config["ausschlussbegriffe"]:
                        if (all(_t in titel.lower() for _t in _b.split("+")) if "+" in _b else _b in titel.lower()):
                            _np_grund = f"Ausschlussbegriff: '{_b}'"
                            break
                if not _np_grund:
                    _np_grund = standort_ablehnungsgrund(standort, config["erlaubte_standorte"], config["verbotene_standorte"])
                if _np_grund:
                    ausgeschlossen.append({"firma": name, "titel": titel, "url": url,
                                           "treffer": treffer, "nicht_passend_grund": _np_grund})
                    print(f"  🚫 Nicht passend: {titel[:70]}")
                else:
                    ist_neu = url not in bekannte_urls
                    stellen.append({
                        "firma": name,
                        "titel": titel,
                        "url": url,
                        "arbeitsort": standort,
                        "standort": berechne_standort(standort, config["erlaubte_standorte"], config["verbotene_standorte"]),
                        "treffer": treffer,
                        "neu": ist_neu,
                    })
                    neu_label = "🆕 " if ist_neu else "   "
                    print(f"  ✅ {neu_label}{titel}")
                    print(f"     {standort} | Treffer: {', '.join(treffer)}")

        # Abbruch wenn alle Jobs geladen
        if payload["offset"] + len(jobs) >= total:
            break

    if not stellen and not ausgeschlossen:
        print(f"  ℹ️  Keine passenden Stellen bei {name}")

    return stellen, ausgeschlossen

# =============================================================================
# ROHTEXT LADEN
# =============================================================================

def lade_rohtext(page, url: str) -> str | None:
    if url.lower().endswith(".pdf"):
        if not PYPDF_OK:
            print(f"  ⚠️  pypdf nicht installiert – PDF übersprungen: {url[:60]}")
            return None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            reader = pypdf.PdfReader(io.BytesIO(data))
            seiten = [p.extract_text() or "" for p in reader.pages]
            return "\n".join(seiten)
        except Exception as e:
            print(f"  ❌ PDF-Fehler ({url[:60]}): {e}")
            return None

    try:
        # Bertrandt onlyfy: spezielle URL für Volltext
        if "onlyfy.jobs" in url:
            job_id = url.rstrip("/").split("/")[-1]
            url = f"https://bertrandtgroup.onlyfy.jobs/job/show/{job_id}/full?lang=de&mode=candidate"

        # Keysight SPA braucht länger bis JSON-Inhalt gerendert ist
        warte_ms = 8000 if "jobs.keysight.com" in url else 4000
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(warte_ms)
        klick_cookie_banner(page)
        page.wait_for_timeout(2000)

        rohtext = page.inner_text("body")

        zeilen = rohtext.splitlines()
        bereinigt = []
        leer = 0
        for z in zeilen:
            if z.strip() == "":
                leer += 1
                if leer <= 2:
                    bereinigt.append("")
            else:
                leer = 0
                bereinigt.append(z)

        return "\n".join(bereinigt)

    except Exception as e:
        print(f"  ❌ Rohtext-Fehler ({url[:60]}): {e}")
        return None


# =============================================================================
# BEREINIGUNG: VERBOTENE STANDORTE AUS BESTAND ENTFERNEN
# =============================================================================

def bereinige_verbotene_standorte(stellen: list, bekannte: dict, erlaubte: list, verbotene: list) -> int:
    """Entfernt bereits gespeicherte Stellen, deren Arbeitsort außerhalb der Whitelist liegt
    oder auf der Blacklist steht. Markiert den bekannte-Eintrag als nicht_passend (statt löschen),
    damit die Stelle im selben Scan-Lauf nicht neu hinzugefügt wird.
    Gibt die Anzahl entfernter Stellen zurück."""
    if not erlaubte and not verbotene:
        return 0

    zu_entfernen = []
    gruende = {}
    for stelle in stellen:
        arbeitsort = stelle.get("arbeitsort") or ""
        if not arbeitsort:
            # Kein Arbeitsort bekannt → kein Filter (sicher durchlassen)
            continue
        grund = standort_ablehnungsgrund(arbeitsort, erlaubte, verbotene)
        if grund:
            zu_entfernen.append(stelle)
            gruende[stelle.get("url")] = grund

    if zu_entfernen:
        print(f"\n🧹 {len(zu_entfernen)} Stelle(n) wegen Standort entfernt:")
        for s in zu_entfernen:
            print(f"   🗑️  {s.get('firma', '?')} – {s.get('titel', '?')}")
            url = s.get("url")
            grund = gruende.get(url, "")
            if url:
                if url in bekannte:
                    bekannte[url]["nicht_passend"] = True
                    bekannte[url]["nicht_passend_grund"] = grund
                else:
                    bekannte[url] = {"status": 0, "nicht_passend": True,
                                     "nicht_passend_grund": grund, "geloescht_am": jetzt()}

        entfernte_urls = {s.get("url") for s in zu_entfernen}
        stellen[:] = [s for s in stellen if s.get("url") not in entfernte_urls]

    return len(zu_entfernen)


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--firma", default=None, help="Nur diese Firma scannen (Name)")
    args = parser.parse_args()
    nur_firma = args.firma.strip() if args.firma else None

    print("\n" + "=" * 60)
    print("  JOB-SCANNER  –  Schritt 1: Scannen & Rohtext laden")
    if nur_firma:
        print(f"  Filter: nur '{nur_firma}'")
    print("=" * 60)

    config = lade_config()
    api_firmen = lade_api_firmen(config)
    if nur_firma:
        api_firmen      = [f for f in api_firmen      if f["name"].lower() == nur_firma.lower()]
        config["firmen"] = [f for f in config["firmen"] if f["name"].strip().lower() == nur_firma.lower()]
    BASIS_PFAD.mkdir(parents=True, exist_ok=True)

    from db import erstelle_schema, lade_alle_stellen, lade_bekannte_dict
    erstelle_schema()
    bekannte:   dict = lade_bekannte_dict()
    stellen:    list = lade_alle_stellen()
    strukturen: dict = lade_json(STRUKTUREN_JSON, {})
    print(f"  📂 Stellen geladen: {len(stellen)}")

    bereinige_verbotene_standorte(stellen, bekannte, config["erlaubte_standorte"], config["verbotene_standorte"])

    stellen_index = {s["url"]: i for i, s in enumerate(stellen)}
    gesehen_urls: set = set()

    def reaktiviere_oder_neu(t, ts, rohtext=None):
        """Verarbeitet eine gefundene Stelle – egal ob via API oder Playwright."""
        rohtext = rohtext if rohtext is not None else t.get("rohtext")
        url = t["url"]
        gesehen_urls.add(url)
        idx = stellen_index.get(url)

        # Standort nachrüsten wenn Stelle schon bekannt aber Feld fehlt
        if idx is not None and t.get("arbeitsort") and not stellen[idx].get("arbeitsort"):
            stellen[idx]["arbeitsort"] = t["arbeitsort"]
            stellen[idx]["standort"] = berechne_standort(t["arbeitsort"], config["erlaubte_standorte"], config["verbotene_standorte"])

        if url in bekannte and bekannte[url]["status"] == 0:
            if idx is not None and stellen[idx].get("bewertung"):
                score = (stellen[idx]["bewertung"] or {}).get("score", 0)
                bekannte[url]["status"] = 4 if score >= 70 else 5
                bekannte[url]["geloescht_am"] = None
                bekannte[url]["nicht_passend"] = False
                stellen[idx]["geloescht_am"] = None
                stellen[idx]["nicht_passend"] = False
                print(f"  ♻️  Reaktiviert (Bewertung vorhanden): {t['titel'][:60]}")
            elif idx is not None and stellen[idx].get("stellentext"):
                bekannte[url]["status"] = 3
                bekannte[url]["geloescht_am"] = None
                bekannte[url]["nicht_passend"] = False
                stellen[idx]["geloescht_am"] = None
                stellen[idx]["nicht_passend"] = False
                print(f"  ♻️  Reaktiviert (Stellentext vorhanden): {t['titel'][:60]}")
            else:
                bekannte[url]["status"] = 2 if rohtext else 1
                bekannte[url]["geloescht_am"] = None
                bekannte[url]["nicht_passend"] = False
                if idx is not None:
                    stellen[idx]["geloescht_am"] = None
                    stellen[idx]["nicht_passend"] = False
                    if rohtext:
                        stellen[idx]["rohtext"] = rohtext
                print(f"  ♻️  Reaktiviert (neu bewerten): {t['titel'][:60]}")
            if idx is None:
                stellen.append({
                    "firma": t["firma"], "titel": t["titel"], "url": url,
                    "arbeitsort": t.get("arbeitsort", ""),
                    "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                    "neu": False, "rohtext": rohtext, "stellentext": None, "bewertung": None,
                    "nicht_passend": False,
                })
                stellen_index[url] = len(stellen) - 1
                print(f"  🔄 Wiederhergestellt: {t['titel'][:60]}")
        elif url not in bekannte:
            bekannte[url] = {"status": 2 if rohtext else 1, "gefunden_am": ts, "geloescht_am": None}
            stellen.append({
                "firma": t["firma"], "titel": t["titel"], "url": url,
                "arbeitsort": t.get("arbeitsort", ""),
                "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                "neu": True, "rohtext": rohtext, "stellentext": None, "bewertung": None,
            })
            stellen_index[url] = len(stellen) - 1
            print(f"  🆕 Neu: {t['titel'][:60]}")
        elif idx is None:
            # In bekannte vorhanden aber fehlt in stellen → wiederherstellen
            stellen.append({
                "firma": t["firma"], "titel": t["titel"], "url": url,
                "arbeitsort": t.get("arbeitsort", ""),
                "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                "neu": False, "rohtext": rohtext, "stellentext": None, "bewertung": None,
            })
            stellen_index[url] = len(stellen) - 1
            bekannte[url]["status"] = (2 if rohtext else 1) if bekannte[url]["status"] < 2 else bekannte[url]["status"]
            print(f"  🔧 Wiederhergestellt (fehlte in stellen.json): {t['titel'][:60]}")
        else:
            if rohtext and not stellen[idx].get("rohtext"):
                stellen[idx]["rohtext"] = rohtext
                print(f"  📥 Rohtext ergänzt: {t['titel'][:60]}")
                if bekannte[url]["status"] < 2:
                    bekannte[url]["status"] = 2

    def markiere_nicht_passend(t, ts):
        url = t["url"]
        gesehen_urls.add(url)  # verhindert Auto-Vergaben durch den "nicht mehr gefunden"-Loop
        idx = stellen_index.get(url)
        grund = t.get("nicht_passend_grund", "")
        if url not in bekannte:
            bekannte[url] = {"status": 1, "gefunden_am": ts, "geloescht_am": None, "nicht_passend": True,
                             "nicht_passend_grund": grund}
            stellen.append({
                "firma": t["firma"], "titel": t["titel"], "url": url,
                "treffer": t.get("treffer", []), "gefunden_am": ts, "geloescht_am": None,
                "neu": False, "rohtext": None, "stellentext": None, "bewertung": None,
                "nicht_passend": True, "nicht_passend_grund": grund,
            })
            stellen_index[url] = len(stellen) - 1
            print(f"  🚫 Nicht passend (neu erfasst): {t['titel'][:60]}")
        else:
            bekannte[url]["nicht_passend"] = True
            bekannte[url]["nicht_passend_grund"] = grund
            bekannte[url]["geloescht_am"] = None
            if idx is not None:
                stellen[idx]["nicht_passend"] = True
                stellen[idx]["nicht_passend_grund"] = grund
                stellen[idx]["geloescht_am"] = None
            print(f"  🚫 Nicht passend: {t['titel'][:60]}")

    # Domains die erfolgreich gescannt wurden (mind. 1 Treffer oder Ausschluss)
    erfolgreich_gescannte_domains: set = set()

    # API-Firmen zuerst scannen (kein Playwright nötig)
    for api_firma in api_firmen:
        try:
            if api_firma.get("typ") == "workday":
                treffer_liste, ausgeschlossen_liste = scanne_workday_firma(api_firma, set(bekannte.keys()), config)
            elif api_firma.get("typ") == "hr4you":
                treffer_liste, ausgeschlossen_liste = scanne_hr4you_firma(api_firma, set(bekannte.keys()), config)
            else:
                treffer_liste, ausgeschlossen_liste = scanne_api_firma(api_firma, set(bekannte.keys()), config)
            if treffer_liste or ausgeschlossen_liste:
                if api_firma.get("typ") == "workday":
                    erfolgreich_gescannte_domains.add(f"{api_firma['tenant']}.wd3.myworkdayjobs.com")
                elif "url" in api_firma:
                    erfolgreich_gescannte_domains.add(domain(api_firma["url"]))
            for t in ausgeschlossen_liste:
                if t["url"] not in gesehen_urls:
                    markiere_nicht_passend(t, jetzt())
            for t in treffer_liste:
                if t["url"] in gesehen_urls:
                    continue
                reaktiviere_oder_neu(t, jetzt())
        except Exception as e:
            print(f"\n❌ API-Fehler bei {api_firma['name']}: {e}")

    # Playwright-Firmen scannen
    with sync_playwright() as p:
        if platform.system() == "Linux":
            browser = p.chromium.launch(
                headless=True,
                executable_path="/usr/bin/chromium-browser",
                args=["--no-sandbox", "--disable-gpu"]
            )
        else:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu"]
            )
        context = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="de-DE")
        page = context.new_page()
        Stealth().apply_stealth_sync(page)

        for firma in config["firmen"]:
            try:
                treffer_liste, ausgeschlossen_liste = scanne_boerse(page, firma, strukturen, config)
            except Exception as e:
                print(f"\n❌ Fehler bei {firma['name']}: {e}")
                continue

            for t in ausgeschlossen_liste:
                if t["url"] not in gesehen_urls:
                    markiere_nicht_passend(t, jetzt())

            if treffer_liste or ausgeschlossen_liste:
                erfolgreich_gescannte_domains.add(domain(firma["url"]))

            for t in treffer_liste:
                url = t["url"]
                if url in gesehen_urls:
                    continue
                idx = stellen_index.get(url)
                if url in bekannte and bekannte[url]["status"] >= 2 and idx is not None and stellen[idx].get("rohtext"):
                    gesehen_urls.add(url)
                    print(f"  ⏭️  Rohtext bereits vorhanden: {t['titel'][:60]}")
                    continue
                print(f"  📄 Lade Rohtext: {t['titel'][:60]}...")
                rohtext = lade_rohtext(page, url)
                reaktiviere_oder_neu(t, jetzt(), rohtext)

        # Rohtext für API-Firmen nachladen (Status 1, kein Rohtext)
        for stelle in stellen:
            url = stelle["url"]
            if not url or not url.startswith("http"):
                continue
            if bekannte.get(url, {}).get("status") == 1 and not stelle.get("rohtext") and not stelle.get("nicht_passend"):
                print(f"  📄 Lade Rohtext: {stelle['titel'][:60]}...")
                rohtext = lade_rohtext(page, url)
                if rohtext:
                    stelle["rohtext"] = rohtext
                    bekannte[url]["status"] = 2
                    print(f"  ✅ Rohtext geladen")

        _http_warnungen: list[str] = []  # Rate-Limit / unklare Antworten sammeln

        def _ist_vergeben(url: str) -> int | None:
            """HTTP GET. Gibt den echten Status-Code zurück.
            0  = Domain-Wechsel, Redirect auf Portal-Root, Closed-Marker oder Workday-API → vergaben.
            None = Verbindungsfehler / Timeout.
            403/429 werden einmalig mit Pause wiederholt.
            404 wird einmalig mit kurzer Pause wiederholt (Schutz gegen Rate-Limit-404).
            Workday-URLs (*.myworkdayjobs.com/*/job/*) werden per API auf Jobexistenz geprüft."""
            import time as _time

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

            def _einzel_request(u: str) -> tuple[int | None, str, str]:
                """Gibt (status, final_url, body_snippet) zurück."""
                try:
                    req = urllib.request.Request(
                        u,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                        method="GET"
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        body = resp.read(8192).decode("utf-8", errors="ignore").lower()
                        return resp.status, resp.url, body
                except urllib.error.HTTPError as e:
                    return e.code, u, ""
                except Exception:
                    return None, u, ""

            def _prüfe_200(final_url: str, body: str) -> int:
                """Prüft ob eine 200-Antwort in Wirklichkeit eine geschlossene Stelle ist (non-Workday)."""
                orig_path    = urlparse(url).path
                final_path   = urlparse(final_url).path
                orig_netloc  = urlparse(url).netloc
                final_netloc = urlparse(final_url).netloc

                if orig_netloc != final_netloc:
                    _http_warnungen.append(f"  📍 Domain-Wechsel → vergaben: {url[:70]}")
                    return 0

                if orig_path and len(final_path) < len(orig_path) * 0.5:
                    _http_warnungen.append(f"  📍 Redirect auf Portal-Root → vergaben: {url[:70]}")
                    return 0

                for marker in _CLOSED_MARKERS:
                    if marker in body:
                        _http_warnungen.append(f"  📍 Closed-Marker '{marker}' → vergaben: {url[:70]}")
                        return 0

                return 200

            def _workday_job_aktiv() -> bool | None:
                """Prüft via Workday-API ob der Job noch existiert.
                True = noch gelistet, False = nicht mehr gelistet, None = API-Fehler."""
                parsed = urlparse(url)
                netloc = parsed.netloc  # z.B. trumpf.wd3.myworkdayjobs.com
                tenant = netloc.split(".")[0]

                segments = [s for s in parsed.path.split("/") if s]
                # Locale-Präfix entfernen (Format: de-DE, en-US, ...)
                if segments and re.match(r'^[a-z]{2}-[A-Z]{2}$', segments[0]):
                    segments = segments[1:]
                if len(segments) < 2:
                    return None

                portal = segments[0]

                # Job-External-ID aus letztem Pfadsegment (Format: ..._R00035907-1)
                last_seg = segments[-1]
                m = re.search(r'_(R\d+(?:-\d+)?)$', last_seg)
                if not m:
                    return None
                job_id = m.group(1)

                api_url = f"https://{netloc}/wday/cxs/{tenant}/{portal}/jobs"
                payload = {"searchText": job_id, "limit": 5, "offset": 0, "appliedFacets": {}}
                try:
                    req = urllib.request.Request(
                        api_url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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

            code, final_url, body = _einzel_request(url)

            if code in (403, 429):
                # Rate-Limit-Signal → 8 Sekunden warten, dann nochmal
                _time.sleep(8)
                code2, final_url2, body2 = _einzel_request(url)
                if code2 is not None:
                    if code != code2:
                        _http_warnungen.append(f"  ⚠️  {code}→{code2} (Rate-Limit?) {url[:70]}")
                    code, final_url, body = code2, final_url2, body2

            elif code == 404:
                # Möglicherweise zu schnell gescannt → 3 Sekunden warten, dann nochmal
                _time.sleep(3)
                code2, final_url2, body2 = _einzel_request(url)
                if code2 is not None and code2 != 404:
                    _http_warnungen.append(f"  ⚠️  404→{code2} (Rate-Limit-404 korrigiert) {url[:70]}")
                    code, final_url, body = code2, final_url2, body2

            if code == 200:
                if ".myworkdayjobs.com" in url and "/job/" in url:
                    aktiv = _workday_job_aktiv()
                    if aktiv is False:
                        _http_warnungen.append(f"  📍 Workday-API: Job nicht mehr gelistet → vergaben: {url[:70]}")
                        code = 0
                    # aktiv is None → API-Fehler, konservativ: 200 behalten
                else:
                    code = _prüfe_200(final_url, body)

            return code

        # Vergaben-Repair: bereits vergaben-markierte Stellen per HTTP prüfen
        if not nur_firma:
            from datetime import datetime as _dt
            _jetzt_dt = _dt.now()

            def _recheck_erlaubt(eintrag: dict, idx) -> bool:
                if not eintrag.get("vergaben_bestaetigt"):
                    return True
                if idx is None:
                    return False
                geloescht = stellen[idx].get("geloescht_am")
                if not geloescht:
                    return False
                try:
                    return (_jetzt_dt - _dt.fromisoformat(str(geloescht))).days <= 14
                except Exception:
                    return False

            vergaben_repair = [
                url for url, eintrag in bekannte.items()
                if eintrag.get("status") in (0, 9)
                and stellen_index.get(url) is not None
                and stellen[stellen_index[url]].get("geloescht_am")
                and not stellen[stellen_index[url]].get("nicht_passend")
                and _recheck_erlaubt(eintrag, stellen_index.get(url))
            ]
            if vergaben_repair:
                print(f"\n  🔍 Prüfe {len(vergaben_repair)} vergaben-markierte Stelle(n) auf Erreichbarkeit (HTTP)...")
                reaktiviert = 0
                for url in vergaben_repair:
                    code = _ist_vergeben(url)
                    if code == 200:  # noch erreichbar → reaktivieren
                        idx = stellen_index[url]
                        stellen[idx]["geloescht_am"] = None
                        stellen[idx]["vergabe_status"] = None
                        stellen[idx]["nicht_passend"] = False
                        stellen[idx]["nicht_passend_grund"] = ""
                        bekannte[url]["status"] = 1
                        bekannte[url]["geloescht_am"] = None
                        bekannte[url]["vergaben_bestaetigt"] = False
                        bekannte[url]["nicht_passend"] = False
                        reaktiviert += 1
                        print(f"  ✅ Vergaben aufgehoben (HTTP 200, noch aktiv): {url[:70]}")
                    elif code in (404, 410, 0):
                        bekannte[url]["vergaben_bestaetigt"] = True
                        print(f"  ✅ Vergaben bestätigt (HTTP {code}): {url[:70]}")
                    elif code is not None:
                        print(f"  ❓ Unklarer Status HTTP {code}: {url[:70]}")
                if reaktiviert:
                    print(f"  ✅ {reaktiviert} Stelle(n) reaktiviert (war fälschlich als vergaben markiert)")

        # Nicht mehr gefundene Stellen → Playwright-Check vor Vergaben-Markierung
        # Nur für Domains die in diesem Scan mind. 1 Ergebnis hatten (Schutz gegen Ladeprobleme)
        ts = jetzt()
        deaktiviert = 0

        # Bewerbungsstatus und Score-URLs für Erreichbarkeits-Check laden
        try:
            from db import verbindung as _db_verb_schutz
            with _db_verb_schutz() as _con_schutz:
                _bewerb_stufen_map = {
                    r[0]: r[1] for r in _con_schutz.execute(
                        "SELECT url, stufe FROM bewerbungsstatus"
                    ).fetchall()
                }
                _score_urls = {r[0] for r in _con_schutz.execute(
                    "SELECT url FROM bewertungen WHERE score >= 70"
                ).fetchall()}
        except Exception:
            _bewerb_stufen_map = {}
            _score_urls = set()

        _aktive_stufen_set = {"beworben", "kennenlernen", "einladung"}
        _schutz_urls = {url for url, stufe in _bewerb_stufen_map.items() if stufe in _aktive_stufen_set}
        _pruefen_set = _schutz_urls | _score_urls

        def _status_bei_vergabe(url: str) -> int:
            stufe = _bewerb_stufen_map.get(url, "")
            if stufe in _aktive_stufen_set:
                return 7
            if stufe in ("absage", "zusage"):
                return 8
            return 9

        # Status 6: aktive Bewerbungs-URLs die im aktuellen Scan noch gefunden wurden
        for url in _schutz_urls:
            if url in gesehen_urls and bekannte.get(url, {}).get("status") not in (6, 7, 8):
                bekannte[url]["status"] = 6
                idx = stellen_index.get(url)
                titel = stellen[idx].get("titel", url)[:60] if idx is not None else url[:60]
                print(f"  🟢 Beworben + noch offen (Status 5): {titel}")

        # Verfügbarkeits-Check: nur Status-4 Stellen (Score≥70%, bewerben empfohlen)
        # sowie aktive Bewerbungen die nicht mehr in den Listings gefunden wurden.
        # Status-3 NICHT prüfen: HTTP-Check ist unzuverlässig für JS-gerenderte Portale
        # (gibt false 404 zurück obwohl Stelle noch aktiv ist)
        kandidaten_vergaben = []
        for url, eintrag in bekannte.items():
            if url not in gesehen_urls and eintrag["status"] not in (0, 5, 6, 7, 8, 9):
                if any(d in url for d in erfolgreich_gescannte_domains):
                    if eintrag["status"] == 4 or url in _pruefen_set:
                        kandidaten_vergaben.append(url)

        if kandidaten_vergaben:
            print(f"\n  🔍 Prüfe {len(kandidaten_vergaben)} nicht mehr gesehene Stelle(n) per HTTP...")
            for url in kandidaten_vergaben:
                eintrag = bekannte[url]
                code = _ist_vergeben(url)
                if code in (404, 410, 0):
                    neuer_status = _status_bei_vergabe(url)
                    eintrag["status"] = neuer_status
                    eintrag["geloescht_am"] = ts
                    eintrag["vergaben_bestaetigt"] = True
                    idx = stellen_index.get(url)
                    if idx is not None:
                        stellen[idx]["geloescht_am"] = ts
                        stellen[idx]["vergabe_status"] = neuer_status
                    deaktiviert += 1
                    _verg_label = {7: "📭 Vergeben (Bewerbung lief)", 8: "❌ Vergeben (Absage)", 9: "🗑️  Vergeben"}
                    print(f"  {_verg_label.get(neuer_status, '🗑️  Vergeben')} (HTTP {code}): {url[:75]}")
                elif code == 200:
                    if url in _schutz_urls or eintrag.get("status") == 4:
                        # Aktive Bewerbung ODER Score≥70%: URL noch erreichbar → nicht anfassen
                        print(f"  🔒 URL noch aktiv (HTTP 200), nicht mehr gelistet → Status bleibt: {url[:65]}")
                    else:
                        idx = stellen_index.get(url)
                        if idx is not None:
                            stellen[idx]["nicht_passend"] = True
                            stellen[idx]["nicht_passend_grund"] = "Nicht mehr in Stellenbörse gelistet (HTTP 200)"
                            stellen[idx]["geloescht_am"] = None
                        bekannte[url]["nicht_passend"] = True
                        print(f"  🚫 Nicht mehr gelistet (HTTP 200) → nicht_passend: {url[:65]}")
                elif code is not None:
                    print(f"  ❓ Unklarer Status HTTP {code} → kein Urteil: {url[:70]}")
                # None = Verbindungsfehler → kein Urteil, Status bleibt

        # Aktive Bewerbungen und Score≥70%-Stellen prüfen die nicht durch den
        # domain-basierten Check erfasst wurden (z.B. nicht mehr gescannte Domains)
        kandidaten_vergaben_set = set(kandidaten_vergaben)
        bewerb_zu_pruefen = [
            url for url in _pruefen_set
            if url not in gesehen_urls
            and url not in kandidaten_vergaben_set
            and bekannte.get(url, {}).get("status") not in (0, 6, 7, 8, 9)
        ]

        if bewerb_zu_pruefen:
            print(f"\n  🔍 Prüfe {len(bewerb_zu_pruefen)} URL(s) auf Erreichbarkeit (Bewerbungen, HTTP)...")
            for url in bewerb_zu_pruefen:
                code = _ist_vergeben(url)
                if code in (404, 410, 0):
                    neuer_status = _status_bei_vergabe(url)
                    if url in bekannte:
                        bekannte[url]["status"] = neuer_status
                        bekannte[url]["geloescht_am"] = ts
                        bekannte[url]["vergaben_bestaetigt"] = True
                    idx = stellen_index.get(url)
                    if idx is not None:
                        stellen[idx]["geloescht_am"] = ts
                        stellen[idx]["vergabe_status"] = neuer_status
                    deaktiviert += 1
                    _verg_label = {7: "📭 Bewerbung vergeben", 8: "❌ Absage + vergaben", 9: "🗑️  Vergaben"}
                    print(f"  {_verg_label.get(neuer_status, '🗑️')} (HTTP {code}): {url[:75]}")
                elif code == 200:
                    print(f"  🔒 Noch erreichbar (HTTP 200): {url[:75]}")
                elif code is not None:
                    print(f"  ❓ Unklarer Status HTTP {code}: {url[:75]}")

        if _http_warnungen:
            print(f"\n  ⚠️  Rate-Limit / korrigierte HTTP-Codes ({len(_http_warnungen)}):")
            for w in _http_warnungen:
                print(w)

    # Zweiter Bereinigungslauf: erfasst Stellen, deren standort-Feld erst im
    # aktuellen Scan nachgetragen wurde und beim ersten Lauf noch fehlte.
    bereinige_verbotene_standorte(stellen, bekannte, config["erlaubte_standorte"], config["verbotene_standorte"])

    speichere_json(STRUKTUREN_JSON, strukturen)

    # Duplikate entfernen (gleiche URL mehrfach)
    _seen: set = set()
    stellen = [s for s in stellen if s["url"] not in _seen and not _seen.add(s["url"])]

    print(f"  💾 Stellen vor Speichern: {len(stellen)}")

    # Alles in DB schreiben
    from db import upsert_stelle, upsert_bewertung, exportiere_stellen_json, exportiere_bekannte_json
    for s in stellen:
        b = bekannte.get(s["url"], {})
        # standort ableiten falls noch nicht gesetzt aber arbeitsort vorhanden
        standort_wert = s.get("standort") or berechne_standort(s.get("arbeitsort", ""), config["erlaubte_standorte"], config["verbotene_standorte"])
        upsert_stelle({
            **s,
            "standort":            standort_wert,
            "status":              b.get("status", s.get("status", 1)),
            "nicht_passend":       b.get("nicht_passend", s.get("nicht_passend", False)),
            "geloescht_am":        b.get("geloescht_am") if b.get("geloescht_am") is not None else s.get("geloescht_am"),
            "vergaben_bestaetigt": b.get("vergaben_bestaetigt", False),
        })
        if s.get("bewertung"):
            upsert_bewertung(s["url"], s["bewertung"])

    # URLs die in bekannte, aber nicht in stellen → Status-Update
    stellen_urls = {s["url"] for s in stellen}
    for url, b in bekannte.items():
        if url not in stellen_urls:
            upsert_stelle({"url": url, "firma": "", "titel": "",
                           "status": b.get("status", 1),
                           "nicht_passend": b.get("nicht_passend", False),
                           "nicht_passend_grund": b.get("nicht_passend_grund", ""),
                           "geloescht_am": b.get("geloescht_am"),
                           "vergaben_bestaetigt": b.get("vergaben_bestaetigt", False)})

    # JSON-Spiegel exportieren
    exportiere_stellen_json(STELLEN_JSON)
    exportiere_bekannte_json(BEKANNTE_JSON)
    print(f"  🗄️  Datenbank aktualisiert, JSON-Spiegel exportiert")

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Stellen gefunden (aktiv):  {len(gesehen_urls)}")
    print(f"  Als vergeben markiert:     {deaktiviert}")
    print(f"  stellen.json:              {STELLEN_JSON}")
    print(f"  bekannte_stellen.json:     {BEKANNTE_JSON}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()