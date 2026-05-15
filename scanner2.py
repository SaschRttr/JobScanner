"""
scanner2.py  –  Schritt 1a: Job-URLs entdecken
===============================================
Scannt alle konfigurierten Firmen (API + Playwright), findet passende
Job-URLs und schreibt sie mit status=1 in die DB.

Kein Rohtext-Laden — das übernimmt rohtext_holen2.py.
Ausnahme: API-Firmen mit feld_rohtext bekommen Rohtext direkt mitgeliefert,
aber nur wenn len(rohtext) >= MIN_ROHTEXT_LAENGE (sonst None → rohtext_holen2 lädt nach).

Status-Übergänge:
  neu gefunden              → status=1
  API mit vollständigem RT  → status=2
  bekannt + status=0/9      → reaktiviert auf status=1/2/3/4/5

Nutzung:
  python scanner2.py                  # alle Firmen
  python scanner2.py --firma "Name"   # nur eine Firma (Filter)
"""

import argparse
import json
import re
import sys
import platform
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from urllib.parse import urlparse

from utils import (
    lade_config, lade_json, speichere_json, jetzt, domain,
    berechne_standort, standort_verboten, text_matched, ist_ausgeschlossen,
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
    STEALTH_OK = True
except ImportError:
    STEALTH_OK = False

try:
    import anthropic as anthropic_lib
except ImportError:
    anthropic_lib = None


# =============================================================================
# PFADE & KONSTANTEN
# =============================================================================

BASIS_PFAD      = Path(__file__).parent
STELLEN_JSON    = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON   = BASIS_PFAD / "bekannte_stellen.json"
STRUKTUREN_JSON = BASIS_PFAD / "strukturen.json"

MIN_ROHTEXT_LAENGE = 500   # API-Rohtext unter dieser Länge gilt als unvollständig
MIN_TITEL_LAENGE   = 10

JOB_LINK_MUSTER = [
    "/job/", "/jobs/", "/job-", "/offer/", "/offer-redirect/",
    "/details/", "/jobboerse/", "/job-detail/", "/stelle/", "/stellen/",
    "/stellenangebot", "/stellenausschreibung", "/vacancy/", "/vacancies/",
    "/karriere/lesen/", "/FolderDetail/", "ac=jobad", "jobId=",
    "/R0", "251563-", "ashbyhq.com/sereact/", "dvinci-hr.com/de/jobs/",
    "zsw-bw-jobs.de/job-", "/careers/job/", "/career/job/",
]

_FORM_MUSTER = [r'-de-f\d+', r'/apply/', r'/bewerben$', r'/application/']

_BUTTON_TEXTE = {
    "jetzt bewerben", "bewerben", "drucken", "drucken / weiterempfehlen",
    "zurück", "zurück zur übersicht", "zur initiativbewerbung",
    "zum jobalert", "mehr erfahren", "details", "apply now", "print",
}

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


# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def root_domain(url: str) -> str:
    teile = urlparse(url).netloc.replace("www.", "").split(".")
    return ".".join(teile[-2:]) if len(teile) >= 2 else teile[0]


def ist_job_link(href: str) -> bool:
    return any(m in href for m in JOB_LINK_MUSTER)


def ist_bewerbungslink(href: str) -> bool:
    return any(re.search(m, href) for m in _FORM_MUSTER)


def titel_aus_slug(href: str) -> str:
    pfad = urlparse(href).path
    slug = pfad.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.[a-z]+$", "", slug)
    slug = re.sub(r"-[a-z]{0,3}-[jf]?\d+$", "", slug)
    slug = re.sub(r"-\d+$", "", slug)
    return slug.replace("-", " ").strip()


def _get_nested(obj: dict, pfad: str, standard="") -> any:
    for schluessel in pfad.split("."):
        if not isinstance(obj, dict):
            return standard
        obj = obj.get(schluessel, standard)
    return obj if obj is not None else standard


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
# API-SCANNER
# =============================================================================

def lade_api_firmen(config: dict) -> list:
    if config.get("api_firmen"):
        return config["api_firmen"]
    api_pfad = BASIS_PFAD / "api_firmen.json"
    if api_pfad.exists():
        try:
            return json.loads(api_pfad.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"❌ Fehler beim Laden von api_firmen.json: {e}")
    return []


def scanne_api_firma(api_config: dict, bekannte_urls: set, config: dict) -> tuple[list, list]:
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
                        "User-Agent": USER_AGENT,
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
                        "User-Agent": USER_AGENT,
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
                print(f"  ⚠️  Leere/ungültige URL für '{titel[:50]}' – übersprungen")
                continue

            volltext = f"{titel} {standort}"

            if titel in gesehen:
                continue
            gesehen.add(titel)

            treffer = text_matched(titel, config["suchbegriffe"])

            if treffer:
                if (ist_ausgeschlossen(titel, config["ausschlussbegriffe"])
                        or standort_verboten(volltext, config["verbotene_standorte"])):
                    _np_grund = ""
                    for _b in config["ausschlussbegriffe"]:
                        if (all(_t in titel.lower() for _t in _b.split("+")) if "+" in _b else _b in titel.lower()):
                            _np_grund = f"Ausschlussbegriff: '{_b}'"
                            break
                    if not _np_grund and standort:
                        _np_grund = f"Verbotener Standort: {standort}"
                    ausgeschlossen.append({"firma": name, "titel": titel, "url": url,
                                           "treffer": treffer, "nicht_passend_grund": _np_grund})
                    print(f"  🚫 Nicht passend: {titel[:70]}")
                else:
                    ist_neu = url not in bekannte_urls

                    # Rohtext aus API nur übernehmen wenn lang genug
                    rohtext = None
                    feld_rohtext = api_config.get("feld_rohtext")
                    if feld_rohtext:
                        teile = [str(_get_nested(job, f)).strip()
                                 for f in (feld_rohtext if isinstance(feld_rohtext, list) else [feld_rohtext])]
                        rohtext_roh = "\n\n".join(t for t in teile if t and t != "None") or None
                        if rohtext_roh and len(rohtext_roh.strip()) >= MIN_ROHTEXT_LAENGE:
                            rohtext = rohtext_roh
                        # else: rohtext bleibt None → rohtext_holen2.py lädt nach

                    if standort and rohtext:
                        rohtext = f"Standort: {standort}\n\n{rohtext}"
                    elif standort and not rohtext:
                        pass  # kein 35-Zeichen-Platzhalter mehr

                    stellen.append({
                        "firma": name,
                        "titel": titel,
                        "url": url,
                        "arbeitsort": standort,
                        "standort": berechne_standort(standort, config["verbotene_standorte"]),
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


def scanne_hr4you_firma(api_config: dict, bekannte_urls: set, config: dict) -> tuple[list, list]:
    import html as _html
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
                    "User-Agent": USER_AGENT,
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

            volltext = f"{titel} {standort}"
            if (ist_ausgeschlossen(titel, config["ausschlussbegriffe"])
                    or standort_verboten(volltext, config["verbotene_standorte"])):
                _np_grund = ""
                for _b in config["ausschlussbegriffe"]:
                    if (all(_t in titel.lower() for _t in _b.split("+")) if "+" in _b else _b in titel.lower()):
                        _np_grund = f"Ausschlussbegriff: '{_b}'"
                        break
                if not _np_grund and standort:
                    _np_grund = f"Verbotener Standort: {standort}"
                ausgeschlossen.append({"firma": name, "titel": titel, "url": url,
                                       "treffer": treffer, "nicht_passend_grund": _np_grund})
                print(f"  🚫 Nicht passend: {titel[:70]}")
            else:
                ist_neu = url not in bekannte_urls
                stellen.append({
                    "firma": name, "titel": titel, "url": url,
                    "arbeitsort": standort,
                    "standort": berechne_standort(standort, config["verbotene_standorte"]),
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
    name    = api_config["name"]
    tenant  = api_config["tenant"]
    portal  = api_config["portal"]

    api_url   = f"https://{tenant}.wd3.myworkdayjobs.com/wday/cxs/{tenant}/{portal}/jobs"
    basis_url = f"https://{tenant}.wd3.myworkdayjobs.com"

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
                    "User-Agent": USER_AGENT,
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  ❌ API-Fehler Seite {seite+1}: {e}")
            break

        total = data.get("total", 0)
        jobs  = data.get("jobPostings", [])

        if not jobs:
            print(f"  ℹ️  Keine weiteren Jobs auf Seite {seite+1}")
            break

        print(f"  📋 Seite {seite+1}: {len(jobs)} Jobs ({payload['offset']+1}–{payload['offset']+len(jobs)} von {total})")

        for job in jobs:
            titel          = job.get("title", "")
            standort       = job.get("locationsText", "")
            external_path  = job.get("externalPath", "")
            locale         = api_config.get("locale")
            if locale:
                url = f"{basis_url}/{locale}/{portal}{external_path}"
            else:
                url = basis_url + external_path

            if titel in gesehen:
                continue
            gesehen.add(titel)

            volltext = f"{titel} {standort}"
            treffer  = text_matched(titel, config["suchbegriffe"])

            if treffer:
                if (ist_ausgeschlossen(titel, config["ausschlussbegriffe"])
                        or standort_verboten(volltext, config["verbotene_standorte"])):
                    _np_grund = ""
                    for _b in config["ausschlussbegriffe"]:
                        if (all(_t in titel.lower() for _t in _b.split("+")) if "+" in _b else _b in titel.lower()):
                            _np_grund = f"Ausschlussbegriff: '{_b}'"
                            break
                    if not _np_grund and standort:
                        _np_grund = f"Verbotener Standort: {standort}"
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
                        "standort": berechne_standort(standort, config["verbotene_standorte"]),
                        "treffer": treffer,
                        "neu": ist_neu,
                        "rohtext": None,
                    })
                    neu_label = "🆕 " if ist_neu else "   "
                    print(f"  ✅ {neu_label}{titel}")
                    print(f"     {standort} | Treffer: {', '.join(treffer)}")

        if payload["offset"] + len(jobs) >= total:
            break

    if not stellen and not ausgeschlossen:
        print(f"  ℹ️  Keine passenden Stellen bei {name}")

    return stellen, ausgeschlossen


# =============================================================================
# PLAYWRIGHT SCANNER
# =============================================================================

def scanne_boerse(page, firma: dict, strukturen: dict, config: dict) -> tuple[list, list]:
    name       = firma["name"]
    url_boerse = firma["url"]
    dom        = domain(url_boerse)

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

    rd = root_domain(url_boerse)
    vor_filter = len(kandidaten)
    kandidaten = [l for l in kandidaten if root_domain(l["href"]) == rd]
    if len(kandidaten) < vor_filter:
        print(f"  🔒 Domain-Filter: {vor_filter - len(kandidaten)} Fremd-Links entfernt")

    kandidaten = [l for l in kandidaten if not ist_bewerbungslink(l["href"])]

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
        titel  = zeilen[0] if zeilen else titel_roh.strip()

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

        standort_aus_text = zeilen[-1] if len(zeilen) >= 2 and zeilen[-1] != titel else ""

        treffer = text_matched(titel, config["suchbegriffe"])

        if not treffer and not ist_pdf_link:
            continue
        if not treffer:
            treffer = ["pdf"]

        _np_grund = ""
        if ist_ausgeschlossen(titel, config["ausschlussbegriffe"]):
            for _b in config["ausschlussbegriffe"]:
                if (all(_t in titel.lower() for _t in _b.split("+")) if "+" in _b else _b in titel.lower()):
                    _np_grund = f"Ausschlussbegriff: '{_b}'"
                    break
        elif standort_aus_text and standort_verboten(standort_aus_text.lower(), config["verbotene_standorte"]):
            _np_grund = f"Verbotener Standort: {standort_aus_text}"

        if _np_grund:
            ausgeschlossen.append({"firma": name, "titel": titel, "url": href,
                                   "treffer": treffer, "nicht_passend_grund": _np_grund})
            print(f"  🚫 Nicht passend: {titel[:70]}")
            continue

        gefunden.append({"firma": name, "titel": titel, "url": href,
                         "treffer": treffer, "arbeitsort": standort_aus_text,
                         "standort": berechne_standort(standort_aus_text, config["verbotene_standorte"])})
        print(f"  ✅ {titel[:70]}")
        print(f"     Treffer: {', '.join(treffer)}")

    if not gefunden and not ausgeschlossen:
        print(f"  ℹ️  Keine passenden Stellen.")
        for k in kandidaten[:10]:
            t = k["text"].split("\n")[0].strip()
            print(f"     - {t[:80]}")

    return gefunden, ausgeschlossen


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Job-URLs entdecken (Schritt 1a)")
    parser.add_argument("--firma", default=None, help="Nur diese Firma scannen (Name)")
    args = parser.parse_args()
    nur_firma = args.firma.strip() if args.firma else None

    print("\n" + "=" * 60)
    print("  SCANNER 2  –  Schritt 1a: Job-URLs entdecken")
    if nur_firma:
        print(f"  Filter: nur '{nur_firma}'")
    print("=" * 60)

    config = lade_config()
    api_firmen = lade_api_firmen(config)

    if nur_firma:
        api_firmen        = [f for f in api_firmen        if f["name"].lower() == nur_firma.lower()]
        config["firmen"]  = [f for f in config["firmen"]  if f["name"].strip().lower() == nur_firma.lower()]

    sys.path.insert(0, str(BASIS_PFAD))
    from db import (erstelle_schema, lade_alle_stellen, lade_bekannte_dict,
                    upsert_stelle, exportiere_stellen_json, exportiere_bekannte_json)
    erstelle_schema()

    bekannte:   dict = lade_bekannte_dict()
    stellen:    list = lade_alle_stellen()
    strukturen: dict = lade_json(STRUKTUREN_JSON, {})

    print(f"  📂 Stellen geladen: {len(stellen)}")

    stellen_index: dict = {s["url"]: i for i, s in enumerate(stellen)}
    gesehen_urls:  set  = set()
    ts = jetzt()

    # ------------------------------------------------------------------
    # Hilfsfunktionen für DB-Zustand
    # ------------------------------------------------------------------

    def reaktiviere_oder_neu(t: dict, rohtext=None):
        url = t["url"]
        gesehen_urls.add(url)
        idx = stellen_index.get(url)

        if idx is not None and t.get("arbeitsort") and not stellen[idx].get("arbeitsort"):
            stellen[idx]["arbeitsort"] = t["arbeitsort"]
            stellen[idx]["standort"] = berechne_standort(t["arbeitsort"], config["verbotene_standorte"])

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
                neuer_s = 2 if rohtext else 1
                bekannte[url]["status"] = neuer_s
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

        elif url not in bekannte:
            neuer_s = 2 if rohtext else 1
            bekannte[url] = {"status": neuer_s, "gefunden_am": ts, "geloescht_am": None}
            stellen.append({
                "firma": t["firma"], "titel": t["titel"], "url": url,
                "arbeitsort": t.get("arbeitsort", ""),
                "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                "neu": True, "rohtext": rohtext, "stellentext": None, "bewertung": None,
            })
            stellen_index[url] = len(stellen) - 1
            print(f"  🆕 Neu: {t['titel'][:60]}")

        elif idx is None:
            neuer_s = 2 if rohtext else 1
            stellen.append({
                "firma": t["firma"], "titel": t["titel"], "url": url,
                "arbeitsort": t.get("arbeitsort", ""),
                "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                "neu": False, "rohtext": rohtext, "stellentext": None, "bewertung": None,
            })
            stellen_index[url] = len(stellen) - 1
            bekannte[url]["status"] = neuer_s if bekannte[url]["status"] < 2 else bekannte[url]["status"]
            print(f"  🔧 Wiederhergestellt: {t['titel'][:60]}")

        else:
            if rohtext and not stellen[idx].get("rohtext"):
                stellen[idx]["rohtext"] = rohtext
                print(f"  📥 Rohtext ergänzt: {t['titel'][:60]}")
                if bekannte[url]["status"] < 2:
                    bekannte[url]["status"] = 2

    def markiere_nicht_passend(t: dict):
        url   = t["url"]
        gesehen_urls.add(url)
        idx   = stellen_index.get(url)
        grund = t.get("nicht_passend_grund", "")

        if url not in bekannte:
            bekannte[url] = {"status": 1, "gefunden_am": ts, "geloescht_am": None,
                             "nicht_passend": True, "nicht_passend_grund": grund}
            stellen.append({
                "firma": t["firma"], "titel": t["titel"], "url": url,
                "treffer": t.get("treffer", []), "gefunden_am": ts, "geloescht_am": None,
                "neu": False, "rohtext": None, "stellentext": None, "bewertung": None,
                "nicht_passend": True, "nicht_passend_grund": grund,
            })
            stellen_index[url] = len(stellen) - 1
            print(f"  🚫 Nicht passend (neu): {t['titel'][:60]}")
        else:
            bekannte[url]["nicht_passend"] = True
            bekannte[url]["nicht_passend_grund"] = grund
            bekannte[url]["geloescht_am"] = None
            if idx is not None:
                stellen[idx]["nicht_passend"] = True
                stellen[idx]["nicht_passend_grund"] = grund
                stellen[idx]["geloescht_am"] = None
            print(f"  🚫 Nicht passend: {t['titel'][:60]}")

    # ------------------------------------------------------------------
    # API-Firmen (kein Playwright)
    # ------------------------------------------------------------------
    gesamt_neu = 0

    for api_firma in api_firmen:
        try:
            if api_firma.get("typ") == "workday":
                treffer_liste, ausgeschlossen_liste = scanne_workday_firma(
                    api_firma, set(bekannte.keys()), config)
            elif api_firma.get("typ") == "hr4you":
                treffer_liste, ausgeschlossen_liste = scanne_hr4you_firma(
                    api_firma, set(bekannte.keys()), config)
            else:
                treffer_liste, ausgeschlossen_liste = scanne_api_firma(
                    api_firma, set(bekannte.keys()), config)
        except Exception as e:
            print(f"\n❌ API-Fehler bei {api_firma['name']}: {e}")
            continue

        for t in ausgeschlossen_liste:
            if t["url"] not in gesehen_urls:
                markiere_nicht_passend(t)

        for t in treffer_liste:
            if t["url"] in gesehen_urls:
                continue
            rohtext = t.get("rohtext")
            reaktiviere_oder_neu(t, rohtext)
            if t.get("neu"):
                gesamt_neu += 1

    # ------------------------------------------------------------------
    # Playwright-Firmen (nur Link-Entdeckung, KEIN Rohtext laden)
    # ------------------------------------------------------------------
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
            },
        )
        page = context.new_page()
        if STEALTH_OK:
            try:
                Stealth().apply_stealth_sync(page)
            except Exception:
                pass
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        for firma in config["firmen"]:
            try:
                treffer_liste, ausgeschlossen_liste = scanne_boerse(page, firma, strukturen, config)
            except Exception as e:
                print(f"\n❌ Fehler bei {firma['name']}: {e}")
                continue

            for t in ausgeschlossen_liste:
                if t["url"] not in gesehen_urls:
                    markiere_nicht_passend(t)

            for t in treffer_liste:
                if t["url"] in gesehen_urls:
                    continue
                reaktiviere_oder_neu(t)
                if t.get("neu"):
                    gesamt_neu += 1

        browser.close()

    # ------------------------------------------------------------------
    # Strukturen speichern
    # ------------------------------------------------------------------
    speichere_json(STRUKTUREN_JSON, strukturen)

    # ------------------------------------------------------------------
    # Duplikate entfernen
    # ------------------------------------------------------------------
    _seen: set = set()
    stellen = [s for s in stellen if s["url"] not in _seen and not _seen.add(s["url"])]

    # ------------------------------------------------------------------
    # Alles in DB schreiben
    # ------------------------------------------------------------------
    print(f"\n  💾 Schreibe {len(stellen)} Stellen in DB...")
    for s in stellen:
        b = bekannte.get(s["url"], {})
        standort_wert = s.get("standort") or berechne_standort(
            s.get("arbeitsort", ""), config["verbotene_standorte"])
        upsert_stelle({
            **s,
            "standort":            standort_wert,
            "status":              b.get("status", s.get("status", 1)),
            "nicht_passend":       b.get("nicht_passend", s.get("nicht_passend", False)),
            "geloescht_am":        b.get("geloescht_am") if b.get("geloescht_am") is not None else s.get("geloescht_am"),
            "vergaben_bestaetigt": b.get("vergaben_bestaetigt", False),
        })

    exportiere_stellen_json(STELLEN_JSON)
    exportiere_bekannte_json(BEKANNTE_JSON)

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Neue Stellen:   {gesamt_neu}")
    print(f"  Gesamt in DB:   {len(stellen)}")
    print(f"  Weiter mit:     python rohtext_holen2.py")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
