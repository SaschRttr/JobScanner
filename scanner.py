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

import argparse
import json
import re
import sys
import io
import urllib.request
import urllib.error
import platform
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import urllib.parse

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
CONFIG_PFAD       = Path(__file__).parent / "config.txt"
API_FIRMEN_PFAD   = Path(__file__).parent / "api_firmen.json"


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
        "api_firmen": [],
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
            elif abschnitt == "api_firmen":
                try:
                    result["api_firmen"] = json.loads("\n".join(puffer))
                except Exception as e:
                    print(f"❌ Fehler beim Parsen von [api_firmen]: {e}")
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

        # Prompt- und api_firmen-Abschnitt: alles übernehmen
        if aktiver_abschnitt in ("prompt", "api_firmen"):
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

            volltext = f"{titel} {standort}"

            if titel in gesehen:
                continue
            gesehen.add(titel)

            treffer = text_matched(titel, config["suchbegriffe"])
            if not treffer:
                treffer = text_matched(volltext, config["suchbegriffe"])

            if treffer:
                if (ist_ausgeschlossen(titel, config["ausschlussbegriffe"])
                        or standort_verboten(volltext, config["verbotene_standorte"])):
                    ausgeschlossen.append({"firma": name, "titel": titel, "url": url, "treffer": treffer})
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

                    stellen.append({
                        "firma": name,
                        "titel": titel,
                        "url": url,
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
                return []

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
                kandidaten.append({"href": l["href"], "text": titel})
        if kandidaten:
            print(f"  📄 PDF-Fallback: {len(kandidaten)} PDF-Stelle(n) gefunden")

    print(f"  📋 {len(kandidaten)} Kandidaten")

    gefunden = []
    ausgeschlossen = []
    gesehen_urls = set()
    gesehen_titel = set()

    for link in kandidaten:
        href = link["href"]
        titel_roh = link["text"]

        zeilen = [z.strip() for z in titel_roh.split("\n") if len(z.strip()) >= MIN_TITEL_LAENGE]
        titel = zeilen[0] if zeilen else titel_roh.strip()

        # Fallback: Titel aus URL-Slug wenn Link-Text wie ein Button aussieht
        if not titel or len(titel) < MIN_TITEL_LAENGE or titel.lower() in _BUTTON_TEXTE:
            titel = titel_aus_slug(href)

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
        if (ist_ausgeschlossen(titel, config["ausschlussbegriffe"])
                or standort_verboten(volltext, config["verbotene_standorte"])):
            ausgeschlossen.append({"firma": name, "titel": titel, "url": href, "treffer": treffer})
            print(f"  🚫 Nicht passend: {titel[:70]}")
            continue

        gefunden.append({"firma": name, "titel": titel, "url": href, "treffer": treffer})
        print(f"  ✅ {titel[:70]}")
        print(f"     Treffer: {', '.join(treffer)}")

    if not gefunden and not ausgeschlossen:
        print(f"  ℹ️  Keine passenden Stellen.")
        for k in kandidaten[:10]:
            t = k["text"].split("\n")[0].strip()
            print(f"     - {t[:80]}")

    return gefunden, ausgeschlossen

def scanne_workday_firma(api_config: dict, bekannte_urls: set, config: dict) -> tuple[list, list]:
    """Scannt eine Firma über die Workday JSON-API.
    Gibt (passende_stellen, ausgeschlossene_stellen) zurück."""
    name = api_config["name"]
    tenant = api_config["tenant"]
    portal = api_config["portal"]

    api_url = f"https://{tenant}.wd3.myworkdayjobs.com/wday/cxs/{tenant}/{portal}/jobs"
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
            locale = api_config.get("locale")
            if locale:
                url = f"{basis_url}/{locale}/{portal}{external_path}"
            else:
                url = basis_url + external_path

            if titel in gesehen:
                continue
            gesehen.add(titel)

            volltext = f"{titel} {standort}"
            treffer = text_matched(titel, config["suchbegriffe"])
            if not treffer:
                treffer = text_matched(volltext, config["suchbegriffe"])

            if treffer:
                if (ist_ausgeschlossen(titel, config["ausschlussbegriffe"])
                        or standort_verboten(volltext, config["verbotene_standorte"])):
                    ausgeschlossen.append({"firma": name, "titel": titel, "url": url, "treffer": treffer})
                    print(f"  🚫 Nicht passend: {titel[:70]}")
                else:
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
                bekannte[url]["status"] = 1
                bekannte[url]["geloescht_am"] = None
                bekannte[url]["nicht_passend"] = False
                if idx is not None:
                    stellen[idx]["geloescht_am"] = None
                    stellen[idx]["nicht_passend"] = False
                print(f"  ♻️  Reaktiviert (neu bewerten): {t['titel'][:60]}")
            if idx is None:
                stellen.append({
                    "firma": t["firma"], "titel": t["titel"], "url": url,
                    "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                    "neu": False, "rohtext": None, "stellentext": None, "bewertung": None,
                    "nicht_passend": False,
                })
                stellen_index[url] = len(stellen) - 1
                print(f"  🔄 Wiederhergestellt: {t['titel'][:60]}")
        elif url not in bekannte:
            rohtext = t.get("rohtext")
            bekannte[url] = {"status": 2 if rohtext else 1, "gefunden_am": ts, "geloescht_am": None}
            stellen.append({
                "firma": t["firma"], "titel": t["titel"], "url": url,
                "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                "neu": True, "rohtext": rohtext, "stellentext": None, "bewertung": None,
            })
            stellen_index[url] = len(stellen) - 1
            print(f"  🆕 Neu: {t['titel'][:60]}")
        elif idx is None:
            # In bekannte vorhanden aber fehlt in stellen.json → wiederherstellen
            rohtext = t.get("rohtext")
            status = bekannte[url]["status"]
            stellen.append({
                "firma": t["firma"], "titel": t["titel"], "url": url,
                "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                "neu": False, "rohtext": rohtext, "stellentext": None, "bewertung": None,
            })
            stellen_index[url] = len(stellen) - 1
            if rohtext and status < 2:
                bekannte[url]["status"] = 2
            print(f"  🔧 Wiederhergestellt (fehlte in stellen.json): {t['titel'][:60]}")

    def reaktiviere_oder_neu_playwright(t, rohtext, ts):
        url = t["url"]
        gesehen_urls.add(url)
        idx = stellen_index.get(url)
        if url in bekannte and bekannte[url]["status"] == 0:
            if idx is not None and stellen[idx].get("bewertung"):
                bekannte[url]["status"] = 4
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
                if idx is not None and rohtext:
                    stellen[idx]["rohtext"] = rohtext
                print(f"  ♻️  Reaktiviert (neu bewerten): {t['titel'][:60]}")
            if idx is None:
                stellen.append({
                    "firma": t["firma"], "titel": t["titel"], "url": url,
                    "treffer": t["treffer"], "gefunden_am": ts, "geloescht_am": None,
                    "neu": False, "rohtext": rohtext, "stellentext": None, "bewertung": None,
                    "nicht_passend": False,
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

    def markiere_nicht_passend(t, ts):
        url = t["url"]
        gesehen_urls.add(url)  # verhindert Auto-Vergaben durch den "nicht mehr gefunden"-Loop
        idx = stellen_index.get(url)
        if url not in bekannte:
            bekannte[url] = {"status": 1, "gefunden_am": ts, "geloescht_am": None, "nicht_passend": True}
            stellen.append({
                "firma": t["firma"], "titel": t["titel"], "url": url,
                "treffer": t.get("treffer", []), "gefunden_am": ts, "geloescht_am": None,
                "neu": False, "rohtext": None, "stellentext": None, "bewertung": None,
                "nicht_passend": True,
            })
            stellen_index[url] = len(stellen) - 1
            print(f"  🚫 Nicht passend (neu erfasst): {t['titel'][:60]}")
        else:
            bekannte[url]["nicht_passend"] = True
            bekannte[url]["geloescht_am"] = None
            if idx is not None:
                stellen[idx]["nicht_passend"] = True
                stellen[idx]["geloescht_am"] = None
            print(f"  🚫 Nicht passend: {t['titel'][:60]}")

    # API-Firmen zuerst scannen (kein Playwright nötig)
    for api_firma in api_firmen:
        try:
            if api_firma.get("typ") == "workday":
                treffer_liste, ausgeschlossen_liste = scanne_workday_firma(api_firma, set(bekannte.keys()), config)
            else:
                treffer_liste, ausgeschlossen_liste = scanne_api_firma(api_firma, set(bekannte.keys()), config)
            for t in ausgeschlossen_liste:
                if t["url"] not in gesehen_urls:
                    markiere_nicht_passend(t, jetzt())
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
                treffer_liste, ausgeschlossen_liste = scanne_boerse(page, firma, strukturen, config)
            except Exception as e:
                print(f"\n❌ Fehler bei {firma['name']}: {e}")
                continue

            for t in ausgeschlossen_liste:
                if t["url"] not in gesehen_urls:
                    markiere_nicht_passend(t, jetzt())

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
            if not url or not url.startswith("http"):
                continue
            if bekannte.get(url, {}).get("status") == 1 and not stelle.get("rohtext"):
                print(f"  📄 Lade Rohtext (API): {stelle['titel'][:60]}...")
                rohtext = lade_rohtext(page, url)
                if rohtext:
                    stelle["rohtext"] = rohtext
                    bekannte[url]["status"] = 2
                    print(f"  ✅ Rohtext geladen")

    # Nicht mehr gefundene Stellen → HTTP-Check vor Vergaben-Markierung
    # Nur wenn die Firma auch wirklich gescannt wurde (Schutz gegen auskommentierte Firmen)
    ts = jetzt()
    deaktiviert = 0
    gescannte_domains = {domain(f["url"]) for f in config["firmen"]}
    for f in api_firmen:
        if f.get("typ") == "workday":
            gescannte_domains.add(f"{f['tenant']}.wd3.myworkdayjobs.com")
        elif "url" in f:
            gescannte_domains.add(domain(f["url"]))

    kandidaten_vergaben = []
    for url, eintrag in bekannte.items():
        if url not in gesehen_urls and eintrag["status"] != 0:
            if any(d in url for d in gescannte_domains):
                kandidaten_vergaben.append(url)

    if kandidaten_vergaben and not nur_firma:
        print(f"\n  🔍 Prüfe {len(kandidaten_vergaben)} nicht mehr gesehene Stelle(n) per HTTP...")
        ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        for url in kandidaten_vergaben:
            eintrag = bekannte[url]
            erreichbar = None
            for methode in ("HEAD", "GET"):
                try:
                    req = urllib.request.Request(url, method=methode,
                        headers={"User-Agent": ua})
                    with urllib.request.urlopen(req, timeout=10):
                        erreichbar = True
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 405 and methode == "HEAD":
                        continue
                    if e.code in (404, 410):
                        erreichbar = False
                    break
                except Exception:
                    break  # Timeout/DNS → unbekannt, nicht markieren
            if erreichbar is False:
                eintrag["status"] = 0
                eintrag["geloescht_am"] = ts
                idx = stellen_index.get(url)
                if idx is not None:
                    stellen[idx]["geloescht_am"] = ts
                deaktiviert += 1
                print(f"  🗑️  Vergeben (404/410): {url[:80]}")
            elif erreichbar:
                print(f"  ✅ Noch aktiv (Scan hat's übersehen): {url[:80]}")
            # erreichbar is None → kein Urteil, Status bleibt
    elif kandidaten_vergaben and nur_firma:
        # Beim Einzelfirmen-Test kein HTTP-Check, direkt markieren
        for url in kandidaten_vergaben:
            eintrag = bekannte[url]
            eintrag["status"] = 0
            eintrag["geloescht_am"] = ts
            idx = stellen_index.get(url)
            if idx is not None:
                stellen[idx]["geloescht_am"] = ts
            deaktiviert += 1

    # Manuell eingefügte Stellen: direkte HTTP-Prüfung (Domain nicht in config)
    # Wird beim Einzelfirmen-Test übersprungen
    if not nur_firma:
        print("\n  🔍 Prüfe manuell eingefügte Stellen auf Verfügbarkeit...")
        ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        for url, eintrag in list(bekannte.items()):
            if eintrag["status"] == 0 or url in gesehen_urls:
                continue
            if any(d in url for d in gescannte_domains):
                continue
            # URL gehört zu keiner gescannten Domain → manuell eingefügt
            erreichbar = None
            for methode in ("HEAD", "GET"):
                try:
                    req = urllib.request.Request(url, method=methode,
                        headers={"User-Agent": ua})
                    with urllib.request.urlopen(req, timeout=10):
                        erreichbar = True
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 405 and methode == "HEAD":
                        continue  # HEAD nicht erlaubt → GET versuchen
                    if e.code in (404, 410):
                        erreichbar = False
                    break
                except Exception:
                    break  # Timeout, DNS-Fehler → unbekannt, nicht markieren
            if erreichbar is False:
                eintrag["status"] = 0
                eintrag["geloescht_am"] = ts
                idx = stellen_index.get(url)
                if idx is not None:
                    stellen[idx]["geloescht_am"] = ts
                deaktiviert += 1
                print(f"  🗑️  Vergeben (404/410): {url[:80]}")
            elif erreichbar:
                print(f"  ✅ Noch aktiv: {url[:80]}")

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