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
  0 = nicht mehr gefunden (Stelle vergeben/offline)
  1 = gefunden (nur Link)
  2 = Rohtext gespeichert

Nutzung:
  python scanner.py

Voraussetzungen:
  pip install playwright playwright-stealth anthropic
  playwright install chromium
"""

import json
import sys
import urllib.request
import platform
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

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


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD      = Path(__file__).parent
STELLEN_JSON    = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON   = BASIS_PFAD / "bekannte_stellen.json"
STRUKTUREN_JSON = BASIS_PFAD / "strukturen.json"
CONFIG_PFAD     = Path(__file__).parent / "config.txt"


# =============================================================================
# CONFIG EINLESEN
# =============================================================================

def lade_config() -> dict:
    if not CONFIG_PFAD.exists():
        print(f"❌ config.txt nicht gefunden: {CONFIG_PFAD}")
        sys.exit(1)

    result = {
        "api_key": "",
        "llm_bewertung": True,
        "suchbegriffe": [],
        "ausschlussbegriffe": [],
        "verbotene_standorte": [],
        "firmen": [],
        "prompt": "",
    }

    zeilen = CONFIG_PFAD.read_text(encoding="utf-8").splitlines()
    aktiver_abschnitt = None
    puffer = []

    for zeile in zeilen:
        z = zeile.strip()

        # Schließendes Tag
        if z.startswith("[\\") and z.endswith("]"):
            abschnitt = z[2:-1].lower()
            if abschnitt == "prompt":
                result["prompt"] = "\n".join(puffer).strip()
            aktiver_abschnitt = None
            puffer = []
            continue

        # Öffnendes Tag
        if z.startswith("[") and z.endswith("]") and not z.startswith("[\\"):
            aktiver_abschnitt = z[1:-1].lower()
            puffer = []
            continue

        # Außerhalb aller Abschnitte
        if aktiver_abschnitt is None:
            if z.startswith("#") or not z:
                continue
            if z.upper().startswith("API_KEY"):
                result["api_key"] = z.split("=", 1)[1].strip()
            elif z.upper().startswith("LLM_BEWERTUNG"):
                result["llm_bewertung"] = z.split("=", 1)[1].strip().lower() == "true"
            continue

        # Prompt-Abschnitt: alles übernehmen
        if aktiver_abschnitt == "prompt":
            puffer.append(zeile)
            continue

        # Alle anderen Abschnitte: Kommentare und Leerzeilen überspringen
        if z.startswith("#") or not z:
            continue

        if aktiver_abschnitt == "suchbegriffe":
            result["suchbegriffe"].append(z.lower())
        elif aktiver_abschnitt == "ausschlussbegriffe":
            result["ausschlussbegriffe"].append(z.lower())
        elif aktiver_abschnitt == "verbotene_standorte":
            result["verbotene_standorte"].append(z.lower())
        elif aktiver_abschnitt == "firmen":
            if "|" in z:
                teile = z.split("|", 1)
                name = teile[0].strip()
                url  = teile[1].strip()
                if name and url:
                    result["firmen"].append({"name": name, "url": url})

    print(f"📄 Config: {len(result['suchbegriffe'])} Suchbegriffe, "
          f"{len(result['ausschlussbegriffe'])} Ausschlussbegriffe, "
          f"{len(result['firmen'])} Firmen")
    return result


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

API_FIRMEN = [
    {
        "name": "Thales",
        "url": "https://careers.thalesgroup.com/widgets",
        "seiten": 5,
        "seiten_parameter": "from",
        "seiten_schrittweite": 10,
        "payload": {
            "lang": "en_global",
            "deviceType": "desktop",
            "country": "global",
            "pageName": "search-results",
            "ddoKey": "refineSearch",
            "sortBy": "",
            "subsearch": "",
            "jobs": True,
            "counts": True,
            "all_fields": ["category", "country", "state", "city", "type"],
            "size": 10,
            "clearAll": False,
            "jdsource": "facets",
            "isSliderEnable": False,
            "pageId": "page18",
            "siteType": "external",
            "keywords": "ditzingen",
            "global": True,
            "selected_fields": {},
            "locationData": {}
        },
        "antwort_pfad": ["refineSearch", "data", "jobs"],
        "feld_titel": "title",
        "feld_id": "jobId",
        "feld_standort": "city",
        "url_vorlage": "https://careers.thalesgroup.com/global/en/job/{id}/{titel}",
    },
    {
        "name": "TE Connectivity",
        "url": "https://careers.te.com/services/recruiting/v1/jobs",
        "seiten": 10,
        "seiten_parameter": "pageNumber",
        "seiten_schrittweite": 1,
        "payload": {
            "locale": "en_US",
            "pageNumber": 0,
            "sortBy": "",
            "keywords": "",
            "location": "",
            "facetFilters": {"filter7": ["Germany"]},
            "brand": "",
            "skills": [],
            "categoryId": 0,
            "alertId": "",
            "rcmCandidateId": ""
        },
        "antwort_pfad": ["jobSearchResult"],
        "antwort_unterebene": "response",
        "feld_titel": "unifiedStandardTitle",
        "feld_id": "id",
        "feld_standort": "jobLocationShort",
        "url_vorlage": "https://careers.te.com/job/{url_titel}/{id}-de_DE/",
        "feld_url_titel": "urlTitle",
    },
    {
    "name": "Trumpf",
    "url": "https://trumpf.wd3.myworkdayjobs.com/wday/cxs/trumpf/TRUMPF_Graduates_and_Professionals/jobs",
    "methode": "POST",
    "seiten": 10,
    "seiten_parameter": "offset",
    "seiten_start": 0,
    "seiten_schrittweite": 20,
    "payload": {
        "appliedFacets": {
            "locations": ["e3c66ea9d86601d7662bca38bb3ee008"]
        },
        "limit": 20,
        "offset": 0,
        "searchText": ""
    },
    "antwort_pfad": ["jobPostings"],
    "feld_titel": "title",
    "feld_id": "externalPath",
    "feld_standort": "locationsText",
    "url_vorlage": "https://trumpf.wd3.myworkdayjobs.com/TRUMPF_Graduates_and_Professionals{id}",
    },
    {
    "name": "Hitachi Rail",
    "url": "https://www.hitachirail.com/umbraco/api/workdayrebuild/getjobs",
    "methode": "GET",
    "seiten": 5,
    "seiten_parameter": "page",
    "seiten_start": 1,
    "seiten_schrittweite": 1,
    "payload": {
        "ItemsPerPage": 20,
        "sortByField": "primaryJobPostingDate",
        "sortDirection": "DESC",
        "page": 1,
        "City": "Stuttgart",
    },
    "antwort_pfad": ["items"],
    "feld_titel": "jobPostingTitle",
    "feld_standort": "primaryJobPostingLocation",
    "feld_id": "url",
    "url_vorlage": "https://www.hitachirail.com{id}",
},
{
    "name": "eta plus",
    "url": "https://jobs.b-ite.com/api/v1/postings/search",
    "methode": "POST",
    "seiten": 1,
    "seiten_parameter": "_dummy",
    "seiten_schrittweite": 1,
    "headers": {
        "bite-jobsapi-client": "v5-20260319-54d54ae",
    },
    "payload": {
        "key": "73b9185d464269b0f922b6fa609bf1f8fa299d96",
        "channel": 0,
        "locale": "de",
        "sort": {"by": "title", "order": "asc"},
        "origin": "https://www.eta-uv.de/de/unternehmen/karriere/stellenangebote",
        "page": {"offset": 0},
    },
    "antwort_pfad": ["jobPostings"],
    "feld_titel": "title",
    "feld_standort": "jobSite",
    "feld_id": "url",
    "url_vorlage": "{id}",
},
{
    "name": "Knorr-Bremse",
    "url": "https://production.api.recruiting-solutions.org/search",
    "methode": "POST",
    "seiten": 5,
    "seiten_parameter": "_dummy",
    "seiten_schrittweite": 1,
    "headers": {
        "customerid": "kb-prod",
        "internal": "false"
    },
    "payload": {
        "count": True,
        "facets": [],
        "filter": "datePosted lt 2026-04-09T08:39:00.794Z and (addresses/any(jt: jt/city eq 'Schwieberdingen')) and language eq 'de_DE'",
        "search": "*",
        "searchFields": "jobId,title,description,addresses/city,addresses/country,addresses/name,legalEntity",
        "skip": 0,
        "top": 20
    },
    "antwort_pfad": ["value"],
    "feld_titel": "title",
    "feld_standort": "addresses/city",
    "feld_id": "link",
    "url_vorlage": "{id}"
}
]


def scanne_api_firma(api_config: dict, bekannte_urls: set, config: dict) -> list[dict]:
    """Scannt eine Firma über deren JSON-API (kein Playwright nötig)."""
    name = api_config["name"]
    print(f"\n{'='*60}")
    print(f"  Scanne: {name} (API)")
    print(f"{'='*60}")

    stellen = []
    gesehen = set()

    for seite in range(api_config["seiten"]):
        seiten_start = api_config.get("seiten_start", 0)
        seiten_wert  = seiten_start + seite * api_config["seiten_schrittweite"]

        try:
            if api_config.get("methode", "POST").upper() == "GET":
                params = dict(api_config["payload"])
                params[api_config["seiten_parameter"]] = seiten_wert
                query = "&".join(f"{k}={v}" for k, v in params.items())
                url_mit_seite = f"{api_config['url']}?{query}"
                #print(url_mit_seite)  # <- hier
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
            titel = job.get(api_config["feld_titel"], "")

            standort_roh = job.get(api_config.get("feld_standort", ""), "")
            standort = " ".join(standort_roh) if isinstance(standort_roh, list) else standort_roh

            job_id = str(job.get(api_config.get("feld_id", ""), ""))
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

            volltext = f"{titel} {standort}"

            if titel in gesehen:
                continue
            gesehen.add(titel)

            treffer = text_matched(titel, config["suchbegriffe"])
            if not treffer:
                treffer = text_matched(volltext, config["suchbegriffe"])

            if (treffer
                    and not ist_ausgeschlossen(titel, config["ausschlussbegriffe"])
                    and not standort_verboten(volltext, config["verbotene_standorte"])):
                ist_neu = url not in bekannte_urls
                stellen.append({
                    "firma": name,
                    "titel": titel,
                    "url": url,
                    "treffer": treffer,
                    "neu": ist_neu,
                })
                neu_label = "🆕 " if ist_neu else "   "
                print(f"  ✅ {neu_label}{titel}")
                print(f"     Treffer: {', '.join(treffer)}")

    if not stellen:
        print(f"  ℹ️  Keine passenden Stellen bei {name}")

    return stellen


# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def jetzt() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


def text_matched(text: str, begriffe: list) -> list:
    t = text.lower()
    treffer = []
    for b in begriffe:
        if "+" in b:
            if all(teil in t for teil in b.split("+")):
                treffer.append(b)
        else:
            if b in t:
                treffer.append(b)
    return treffer


def ist_ausgeschlossen(text: str, begriffe: list) -> bool:
    t = text.lower()
    for b in begriffe:
        if "+" in b:
            if all(teil in t for teil in b.split("+")):
                return True
        else:
            if b in t:
                return True
    return False


def standort_verboten(text: str, verbotene: list) -> bool:
    t = text.lower()
    return any(v in t for v in verbotene)


def ist_job_link(href: str) -> bool:
    return any(m in href for m in JOB_LINK_MUSTER)


# =============================================================================
# KI: JOB-LINK-MUSTER LERNEN
# =============================================================================

def ki_lerne_muster(domain_name: str, beispiel_links: list, api_key: str) -> str | None:
    if not api_key or anthropic_lib is None:
        return None

    links_text = "\n".join(beispiel_links[:30])
    prompt = f"""Hier sind bis zu 30 Links von der Domain '{domain_name}':

{links_text}

Welches URL-Teilmuster kennzeichnet Job-Detail-Links (also Links zu einzelnen Stellenanzeigen)?
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
# COOKIE-BANNER
# =============================================================================

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


# =============================================================================
# STELLENBÖRSE SCANNEN
# =============================================================================

def scanne_boerse(page, firma: dict, strukturen: dict, config: dict) -> list[dict]:
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
        return []

    page.wait_for_timeout(3000)
    klick_cookie_banner(page)

    print("  📜 Scrolle...")
    for _ in range(8):
        page.evaluate("window.scrollBy(0, 1200)")
        page.wait_for_timeout(2500)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(2000)

    alle_links = page.evaluate("""() =>
        [...document.querySelectorAll('a[href]')].map(a => ({
            href: a.href,
            text: (a.innerText || '').trim()
        }))
    """)
    print(f"  🔗 {len(alle_links)} Links gesamt")

    # Job-Link-Muster bestimmen
    muster = strukturen.get(dom, {}).get("link_muster")

    if muster:
        print(f"  ✅ Bekanntes Muster: '{muster}'")
        kandidaten = [l for l in alle_links if muster in l["href"]]
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
                kandidaten = [l for l in alle_links if muster in l["href"]]
            else:
                print(f"  ⚠️  Kein Muster gefunden – überspringe {name}")
                return []

    print(f"  📋 {len(kandidaten)} Kandidaten")

    gefunden = []
    gesehen_urls = set()
    gesehen_titel = set()

    for link in kandidaten:
        href = link["href"]
        titel_roh = link["text"]

        zeilen = [z.strip() for z in titel_roh.split("\n") if len(z.strip()) >= MIN_TITEL_LAENGE]
        titel = zeilen[0] if zeilen else titel_roh.strip()

        if not titel or len(titel) < MIN_TITEL_LAENGE:
            continue
        if href in gesehen_urls or titel in gesehen_titel:
            continue
        gesehen_urls.add(href)
        gesehen_titel.add(titel)

        volltext = titel_roh.lower()

        treffer = text_matched(titel, config["suchbegriffe"])
        if not treffer:
            treffer = text_matched(volltext, config["suchbegriffe"])

        if not treffer:
            continue
        if ist_ausgeschlossen(titel, config["ausschlussbegriffe"]):
            continue
        if standort_verboten(volltext, config["verbotene_standorte"]):
            continue

        gefunden.append({"firma": name, "titel": titel, "url": href, "treffer": treffer})
        print(f"  ✅ {titel[:70]}")
        print(f"     Treffer: {', '.join(treffer)}")

    if not gefunden:
        print(f"  ℹ️  Keine passenden Stellen.")
        for k in kandidaten[:10]:
            t = k["text"].split("\n")[0].strip()
            print(f"     - {t[:80]}")

    return gefunden

def scanne_workday_firma(api_config: dict, bekannte_urls: set, config: dict) -> list[dict]:
    """Scannt eine Firma über die Workday JSON-API."""
    name = api_config["name"]
    tenant = api_config["tenant"]
    portal = api_config["portal"]

    api_url = f"https://{tenant}.wd3.myworkdayjobs.com/wday/cxs/{tenant}/{portal}/jobs"
    basis_url = f"https://{tenant}.wd3.myworkdayjobs.com"

    print(f"\n{'='*60}")
    print(f"  Scanne: {name} (Workday API)")
    print(f"{'='*60}")

    stellen = []
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

        print(f"  📋 Seite {seite+1}: {len(jobs)} Jobs ({payload['offset']+1}–{payload['offset']+len(jobs)} von {total})")

        for job in jobs:
            titel = job.get("title", "")
            standort = job.get("locationsText", "")
            external_path = job.get("externalPath", "")
            url = basis_url + external_path

            if titel in gesehen:
                continue
            gesehen.add(titel)

            volltext = f"{titel} {standort}"
            treffer = text_matched(titel, config["suchbegriffe"])
            if not treffer:
                treffer = text_matched(volltext, config["suchbegriffe"])

            if (treffer
                    and not ist_ausgeschlossen(titel, config["ausschlussbegriffe"])
                    and not standort_verboten(volltext, config["verbotene_standorte"])):
                ist_neu = url not in bekannte_urls
                stellen.append({
                    "firma": name,
                    "titel": titel,
                    "url": url,
                    "treffer": treffer,
                    "neu": ist_neu,
                })
                neu_label = "🆕 " if ist_neu else "   "
                print(f"  ✅ {neu_label}{titel}")
                print(f"     {standort} | Treffer: {', '.join(treffer)}")

        # Abbruch wenn alle Jobs geladen
        if payload["offset"] + len(jobs) >= total:
            break

    if not stellen:
        print(f"  ℹ️  Keine passenden Stellen bei {name}")

    return stellen

# =============================================================================
# ROHTEXT LADEN
# =============================================================================

def lade_rohtext(page, url: str) -> str | None:
    try:
        # Bertrandt onlyfy: spezielle URL für Volltext
        if "onlyfy.jobs" in url:
            job_id = url.rstrip("/").split("/")[-1]
            url = f"https://bertrandtgroup.onlyfy.jobs/job/show/{job_id}/full?lang=de&mode=candidate"
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)
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
# JSON-HILFSFUNKTIONEN
# =============================================================================

def lade_json(pfad: Path, standard):
    if pfad.exists():
        try:
            return json.loads(pfad.read_text(encoding="utf-8"))
        except Exception:
            pass
    return standard


def speichere_json(pfad: Path, daten):
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    print("\n" + "=" * 60)
    print("  JOB-SCANNER  –  Schritt 1: Scannen & Rohtext laden")
    print("=" * 60)

    config = lade_config()
    BASIS_PFAD.mkdir(parents=True, exist_ok=True)

    bekannte:   dict = lade_json(BEKANNTE_JSON, {})
    stellen:    list = lade_json(STELLEN_JSON, [])
    strukturen: dict = lade_json(STRUKTUREN_JSON, {})
    print(f"  📂 Stellen geladen: {len(stellen)}")
    stellen_index = {s["url"]: i for i, s in enumerate(stellen)}
    gesehen_urls: set = set()

    def reaktiviere_oder_neu_api(t, ts):
        url = t["url"]
        gesehen_urls.add(url)
        idx = stellen_index.get(url)
        if url in bekannte and bekannte[url]["status"] == 0:
            if idx is not None and stellen[idx].get("bewertung"):
                bekannte[url]["status"] = 4
                bekannte[url]["geloescht_am"] = None
                print(f"  ♻️  Reaktiviert (Bewertung vorhanden): {t['titel'][:60]}")
            elif idx is not None and stellen[idx].get("stellentext"):
                bekannte[url]["status"] = 3
                bekannte[url]["geloescht_am"] = None
                print(f"  ♻️  Reaktiviert (Stellentext vorhanden): {t['titel'][:60]}")
            else:
                bekannte[url]["status"] = 1
                bekannte[url]["geloescht_am"] = None
                print(f"  ♻️  Reaktiviert (neu bewerten): {t['titel'][:60]}")
            if idx is None:
                stellen.append({
                    "firma": t["firma"], "titel": t["titel"], "url": url,
                    "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                    "neu": False, "rohtext": None, "stellentext": None, "bewertung": None,
                })
                stellen_index[url] = len(stellen) - 1
                print(f"  🔄 Wiederhergestellt: {t['titel'][:60]}")
        elif url not in bekannte:
            bekannte[url] = {"status": 1, "gefunden_am": ts, "geloescht_am": None}
            stellen.append({
                "firma": t["firma"], "titel": t["titel"], "url": url,
                "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                "neu": True, "rohtext": None, "stellentext": None, "bewertung": None,
            })
            stellen_index[url] = len(stellen) - 1
            print(f"  🆕 Neu: {t['titel'][:60]}")

    def reaktiviere_oder_neu_playwright(t, rohtext, ts):
        url = t["url"]
        gesehen_urls.add(url)
        idx = stellen_index.get(url)
        if url in bekannte and bekannte[url]["status"] == 0:
            if idx is not None and stellen[idx].get("bewertung"):
                bekannte[url]["status"] = 4
                bekannte[url]["geloescht_am"] = None
                print(f"  ♻️  Reaktiviert (Bewertung vorhanden): {t['titel'][:60]}")
            elif idx is not None and stellen[idx].get("stellentext"):
                bekannte[url]["status"] = 3
                bekannte[url]["geloescht_am"] = None
                print(f"  ♻️  Reaktiviert (Stellentext vorhanden): {t['titel'][:60]}")
            else:
                bekannte[url]["status"] = 2 if rohtext else 1
                bekannte[url]["geloescht_am"] = None
                if idx is not None and rohtext:
                    stellen[idx]["rohtext"] = rohtext
                print(f"  ♻️  Reaktiviert (neu bewerten): {t['titel'][:60]}")
            if idx is None:
                stellen.append({
                    "firma": t["firma"], "titel": t["titel"], "url": url,
                    "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                    "neu": False, "rohtext": rohtext, "stellentext": None, "bewertung": None,
                })
                stellen_index[url] = len(stellen) - 1
                print(f"  🔄 Wiederhergestellt: {t['titel'][:60]}")
        elif url not in bekannte:
            bekannte[url] = {
                "status": 2 if rohtext else 1,
                "gefunden_am": ts, "geloescht_am": None,
            }
            stellen.append({
                "firma": t["firma"], "titel": t["titel"], "url": url,
                "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                "neu": True, "rohtext": rohtext, "stellentext": None, "bewertung": None,
            })
            stellen_index[url] = len(stellen) - 1
            print(f"  🆕 Neu: {t['titel'][:60]}")
        else:
            if rohtext and bekannte[url]["status"] < 2:
                bekannte[url]["status"] = 2
                if idx is not None:
                    stellen[idx]["rohtext"] = rohtext
                print(f"  📥 Rohtext ergänzt: {t['titel'][:60]}")

    # API-Firmen zuerst scannen (kein Playwright nötig)
    for api_firma in API_FIRMEN:
        try:
            if api_firma.get("typ") == "workday":
                treffer_liste = scanne_workday_firma(api_firma, set(bekannte.keys()), config)
            else:
                treffer_liste = scanne_api_firma(api_firma, set(bekannte.keys()), config)
            for t in treffer_liste:
                if t["url"] in gesehen_urls:
                    continue
                reaktiviere_oder_neu_api(t, jetzt())
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
                treffer_liste = scanne_boerse(page, firma, strukturen, config)
            except Exception as e:
                print(f"\n❌ Fehler bei {firma['name']}: {e}")
                continue

            for t in treffer_liste:
                url = t["url"]
                if url in gesehen_urls:
                    continue
                if url in bekannte and bekannte[url]["status"] >= 2:
                    gesehen_urls.add(url)
                    print(f"  ⏭️  Rohtext bereits vorhanden: {t['titel'][:60]}")
                    continue
                print(f"  📄 Lade Rohtext: {t['titel'][:60]}...")
                rohtext = lade_rohtext(page, url)
                reaktiviere_oder_neu_playwright(t, rohtext, jetzt())

        # Rohtext für API-Firmen nachladen (Status 1, kein Rohtext)
        for stelle in stellen:
            url = stelle["url"]
            if bekannte.get(url, {}).get("status") == 1 and not stelle.get("rohtext"):
                print(f"  📄 Lade Rohtext (API): {stelle['titel'][:60]}...")
                rohtext = lade_rohtext(page, url)
                if rohtext:
                    stelle["rohtext"] = rohtext
                    bekannte[url]["status"] = 2
                    print(f"  ✅ Rohtext geladen")

    # Nicht mehr gefundene Stellen → Status 0
    # Nur wenn die Firma auch wirklich gescannt wurde (Schutz gegen auskommentierte Firmen)
    ts = jetzt()
    deaktiviert = 0
    gescannte_domains = {domain(f["url"]) for f in config["firmen"]}
    for f in API_FIRMEN:
        if f.get("typ") == "workday":
            gescannte_domains.add(f"{f['tenant']}.wd3.myworkdayjobs.com")
        elif "url" in f:
            gescannte_domains.add(domain(f["url"]))
    for url, eintrag in bekannte.items():
        if url not in gesehen_urls and eintrag["status"] != 0:
            if any(d in url for d in gescannte_domains):
                eintrag["status"] = 0
                eintrag["geloescht_am"] = ts
                idx = stellen_index.get(url)
                if idx is not None:
                    stellen[idx]["geloescht_am"] = ts
                deaktiviert += 1

    speichere_json(STRUKTUREN_JSON, strukturen)
    speichere_json(BEKANNTE_JSON, bekannte)
    print(f"  💾 Stellen vor Speichern: {len(stellen)}")
    speichere_json(STELLEN_JSON, stellen)

    # Datenbank aktualisieren
    try:
        from db import erstelle_schema, upsert_stelle
        erstelle_schema()
        for s in stellen:
            upsert_stelle({**s, "status": bekannte.get(s["url"], {}).get("status", 1)})
        print(f"  🗄️  Datenbank aktualisiert")
    except Exception as e:
        print(f"  ⚠️  Datenbank-Fehler (nicht kritisch): {e}")

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Stellen gefunden (aktiv):  {len(gesehen_urls)}")
    print(f"  Als vergeben markiert:     {deaktiviert}")
    print(f"  stellen.json:              {STELLEN_JSON}")
    print(f"  bekannte_stellen.json:     {BEKANNTE_JSON}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()