"""
scanner.py  –  Schritt 1a: Job-URLs entdecken
===============================================
Scannt alle konfigurierten Firmen (API + Playwright), findet passende
Job-URLs und schreibt sie mit status=1 in die DB.

Kein Rohtext-Laden — das übernimmt rohtext_holen.py.
Ausnahme: API-Firmen mit feld_rohtext bekommen Rohtext direkt mitgeliefert,
aber nur wenn len(rohtext) >= MIN_ROHTEXT_LAENGE (sonst None → rohtext_holen lädt nach).

Status-Übergänge:
  neu gefunden              → status=1
  API mit vollständigem RT  → status=2
  bekannt + status=0/9      → reaktiviert auf status=1/2/3/4/5

Nutzung:
  python scanner.py                  # alle Firmen
  python scanner.py --firma "Name"   # nur eine Firma (Filter)
"""

import argparse
import base64
import json
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from urllib.parse import urlparse

from utils import (
    lade_config, lade_json, speichere_json, jetzt, domain,
    berechne_standort, standort_ablehnungsgrund, ablehnungsgrund,
    text_matched, klick_cookie_banner, normalisiere_ort, effektiver_score,
    ist_ausgeschlossen, AUSLAND_MARKER, standort_ignoriert_urls,
)
from bewertung import status_fuer_score
from browser import (
    USER_AGENT, MIN_ROHTEXT_LAENGE,
    starte_browser, neuer_context, neue_seite,
)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright nicht installiert:")
    print("  pip install playwright && playwright install chromium")
    sys.exit(1)

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
SCAN_STATUS_JSON = BASIS_PFAD / "scan_status.json"
KEIN_TREFFER_JSON = BASIS_PFAD / "kein_treffer.json"
VORSCHAU_JSON   = BASIS_PFAD / "vorschau_kandidaten.json"

# Pro Firma: {"ok": bool, "fehler": str|None, "zeitpunkt": "YYYY-MM-DD HH:MM"}
# Wird am Ende von main() nach SCAN_STATUS_JSON geschrieben, report.py zeigt es an.
SCAN_STATUS: dict = {}

# Pro Firma: Liste von {"titel": str, "url": str} für Stellen, die keinen
# Suchbegriff-Treffer hatten (anders als Ausschlussbegriffe/Standort landen die
# nirgends in stellen.json - hier bleibt zumindest sichtbar, was der Whitelist-
# Filter verwirft, damit man [suchbegriffe] bei Bedarf gezielt erweitern kann).
# Wird am Ende von main() nach KEIN_TREFFER_JSON geschrieben, report.py zeigt es an.
KEIN_TREFFER: dict = {}


def status_merken(name: str, ok: bool, fehler: str | None = None):
    SCAN_STATUS[name] = {"ok": ok, "fehler": fehler, "zeitpunkt": jetzt()}


def kein_treffer_merken(name: str, titel: str, url: str):
    KEIN_TREFFER.setdefault(name, []).append({"titel": titel, "url": url})


class SessionGesperrtFehler(Exception):
    """HTTP 403/429 auf der Börsen-Seite – die WAF hat die Session geflaggt.
    Eine frische Session (neuer Context = neue Cookies, wie ein Inkognito-
    Fenster) wird i.d.R. sofort wieder durchgelassen."""
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"HTTP {status} – Session vom Server gesperrt")

MIN_TITEL_LAENGE = 10

# Findet die Link-Heuristik weniger als so viele Job-Links, wird zusätzlich die
# KI um ein Muster gebeten (eine Karriereseite listet praktisch immer mehr
# Stellen; wenige Treffer heißen meist, dass die Heuristik nur beiläufige Links
# erwischt und die echten Stellen ein anderes Muster haben).
MIN_HEURISTIK_LINKS = 5

JOB_LINK_MUSTER = [
    "/job/", "/jobs/", "/job-", "/offer/", "/offer-redirect/",
    "/details/", "/jobboerse/", "/job-detail/", "/stelle/", "/stellen/",
    "/stellenangebot", "/stellenausschreibung", "/vacancy/", "/vacancies/",
    "/karriere/lesen/", "/FolderDetail/", "ac=jobad", "jobId=",
    "/R0", "251563-", "ashbyhq.com/sereact/", "dvinci-hr.com/de/jobs/",
    "zsw-bw-jobs.de/job-", "/careers/job/", "/career/job/",
    "/j/karriere/offene-stellen/", "/j/careers/job-vacancies/",
    "/emploi",  # frz. "Stelle" (z.B. xs-groupe.com: /emploi-xs/<slug>/)
]

_FORM_MUSTER = [r'-de-f\d+', r'/apply/', r'/bewerben$', r'/application/']

# rexx systems (z.B. Würth Elektronik, jobs.we-online.com): Job-Detailseiten
# enden auf "-<sprache>-j<nummer>.html" (z.B. "...-eng-j5226.html"), die
# Bewerbungsformulare auf "-f<nummer>" (s. _FORM_MUSTER). Substring-Muster
# reichen hier nicht, weil "-j" allein zu breit wäre – daher als Regex.
_JOB_LINK_REGEX = [re.compile(r'-j\d+\.html', re.IGNORECASE)]

# Externe Bewerberportale (ATS), die Firmen-Karriereseiten für die eigentlichen
# Stellen einbinden. Solche Links liegen auf einer anderen Root-Domain als die
# Börsen-Seite und würden sonst vom Domain-Filter verworfen; ihr Pfad enthält
# oft auch kein Standard-Job-Muster (z.B. mhm.jobs: /<id>-<slug>/job.html).
_ATS_HOSTS = {"mhm.jobs"}

_BUTTON_TEXTE = {
    "jetzt bewerben", "bewerben", "drucken", "drucken / weiterempfehlen",
    "zurück", "zurück zur übersicht", "zur initiativbewerbung",
    "zum jobalert", "mehr erfahren", "details", "apply now", "print",
}


# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def root_domain(url: str) -> str:
    teile = urlparse(url).netloc.replace("www.", "").split(".")
    return ".".join(teile[-2:]) if len(teile) >= 2 else teile[0]


def ist_ats_host(href: str) -> bool:
    return root_domain(href) in _ATS_HOSTS


def ist_job_link(href: str) -> bool:
    return (any(m in href for m in JOB_LINK_MUSTER)
            or any(r.search(href) for r in _JOB_LINK_REGEX))


def ist_bewerbungslink(href: str) -> bool:
    return any(re.search(m, href) for m in _FORM_MUSTER)


# Job-Schlüsselwörter, deren "nacktes" letztes Pfadsegment eine Übersichts-/
# Sektionsseite kennzeichnet (kein Slug dahinter = kein Stellen-Detail). Solche
# Links erfüllen zwar ist_job_link(), sind aber keine Stellen und dürfen die
# Heuristik-Schwelle nicht fälschlich erfüllen (sonst wird die KI nie gefragt).
_JOB_SEKTION_WORTE = {
    "jobs", "job", "stellen", "stellenangebot", "stellenangebote",
    "stellenausschreibung", "stellenausschreibungen", "karriere", "career",
    "careers", "emploi", "emplois", "vacancy", "vacancies", "offene-stellen",
}


def _ist_uebersichts_link(href: str, url_boerse: str) -> bool:
    """True, wenn href die Börsen-Seite selbst oder eine reine Job-Übersichts-
    seite ist (letztes Pfadsegment = Job-Schlüsselwort, kein Detail-Slug)."""
    def norm(u: str):
        pr = urlparse(u)
        return pr.netloc.replace("www.", ""), pr.path.rstrip("/")
    if norm(href) == norm(url_boerse):
        return True
    segmente = [s for s in urlparse(href).path.split("/") if s]
    if not segmente:
        return True  # Domain-Wurzel
    return segmente[-1].lower() in _JOB_SEKTION_WORTE


def _echte_job_links(kandidaten: list, url_boerse: str) -> list:
    """Reduziert Heuristik-Kandidaten auf plausible Stellen-Detail-Links: dedupe
    nach normalisierter href, ohne Seite-selbst und ohne reine Übersichtsseiten.
    Verlässlichere Zählbasis für die Schwelle, ab der die KI ein Muster lernt."""
    gesehen = set()
    echte = []
    for l in kandidaten:
        href = l.get("href", "")
        norm = href.split("#")[0].rstrip("/")
        if not norm or norm in gesehen:
            continue
        if _ist_uebersichts_link(href, url_boerse):
            continue
        gesehen.add(norm)
        echte.append(l)
    return echte


# -----------------------------------------------------------------------------
# Generischer JSON-API-Fallback
# -----------------------------------------------------------------------------
# Moderne Karriere-Seiten rendern Stellen oft NICHT als <a href> im DOM, sondern
# laden sie beim Seitenaufruf per XHR/fetch (JSON/GraphQL) nach – teils über ein
# firmeneigenes Gateway, das intern auf ein ATS (z.B. Workday) zeigt. Für solche
# Seiten wertet der Scanner die abgefangenen JSON-Antworten aus, statt am leeren
# DOM zu scheitern. Bewusst generisch (Feldnamen breit), kein Firmen-Sonderfall.
_API_TITEL_FELDER = ("title", "jobtitle", "name", "positiontitle", "jobposting",
                     "displayname", "titel", "bezeichnung", "postingtitle")
_API_URL_FELDER   = ("externalapplyurl", "externalurl", "applyurl", "joburl",
                     "url", "externalpath", "canonicalurl", "detailurl", "link",
                     "jobdetailurl", "absoluteurl", "href", "permalink", "path",
                     "apply_job_url", "wp_url", "job_url", "apply_url", "slug")
_API_ORT_FELDER   = ("primarylocation", "locationstext", "location", "city",
                     "arbeitsort", "ort", "standort", "locationcountry",
                     "locationname", "workplace")


def _finde_job_liste(obj, tiefe: int = 0) -> list:
    """Sucht rekursiv die größte Liste aus Dicts, die wie Job-Einträge aussehen –
    jedes Dict hat sowohl ein Titel- als auch ein URL-artiges Feld."""
    if tiefe > 8:
        return []
    beste: list = []
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if len(dicts) >= 2:
            def _hat(d, felder):
                return any(k.lower() in felder for k in d)
            treffer = [d for d in dicts
                       if _hat(d, _API_TITEL_FELDER) and _hat(d, _API_URL_FELDER)]
            if len(treffer) >= max(2, len(dicts) // 2):
                beste = treffer
        for x in obj:
            kand = _finde_job_liste(x, tiefe + 1)
            if len(kand) > len(beste):
                beste = kand
    elif isinstance(obj, dict):
        for v in obj.values():
            kand = _finde_job_liste(v, tiefe + 1)
            if len(kand) > len(beste):
                beste = kand
    return beste


def _stellen_aus_api_json(bodies: list, basis_url: str) -> list:
    """Baut aus abgefangenen JSON-Antworten Job-Kandidaten [{href, text,
    arbeitsort, api}]. Nimmt die erste Antwort, die eine plausible Job-Liste
    enthält. Relative Pfade werden gegen basis_url aufgelöst."""
    for body in bodies:
        liste = _finde_job_liste(body)
        if not liste:
            continue
        kand = []
        gesehen = set()
        for d in liste:
            low = {k.lower(): v for k, v in d.items()}
            titel = next((str(low[f]).strip() for f in _API_TITEL_FELDER
                          if isinstance(low.get(f), str) and low.get(f).strip()), "")
            href = next((low[f].strip() for f in _API_URL_FELDER
                         if isinstance(low.get(f), str) and low.get(f).strip()), "")
            if not titel or not href:
                continue
            if href.startswith("/"):
                href = urllib.parse.urljoin(basis_url, href)
            if not href.startswith("http"):
                continue
            if href in gesehen:
                continue
            gesehen.add(href)
            ort = next((low[f].strip() for f in _API_ORT_FELDER
                        if isinstance(low.get(f), str) and low.get(f).strip()), "")
            kand.append({"href": href, "text": titel, "arbeitsort": ort, "api": True})
        if kand:
            return kand
    return []


# Pagination-Parameter, die in Job-REST-APIs vorkommen (Seite bzw. Größe) sowie
# Felder für die Gesamtzahl. Für die vollständige, saubere API-Auswertung.
_API_SEITE_PARAMS = ("page", "pagenumber", "pageno", "offset", "from", "start")
_API_SIZE_PARAMS  = ("size", "pagesize", "limit", "rows", "per_page")
_API_TOTAL_FELDER = ("total", "totalcount", "totalresults", "totalhits",
                     "count", "numfound", "totaljobs")


def _api_total(body) -> int | None:
    """Liest eine Gesamt-Trefferzahl aus einer API-Antwort (flach, oberste Ebene)."""
    if isinstance(body, dict):
        for k, v in body.items():
            if k.lower() in _API_TOTAL_FELDER and isinstance(v, int):
                return v
    return None


def _finde_job_api(captures: list, basis_url: str):
    """Sucht die erste abgefangene Antwort mit einer Job-Liste.
    Gibt (kandidaten, quelle) zurück – quelle = {url, method, post_data, body}."""
    for cap in captures:
        kand = _stellen_aus_api_json([cap.get("body")], basis_url)
        if kand:
            return kand, cap
    return [], None


def _paginiere_api(page, quelle: dict, basis_url: str) -> list:
    """Holt eine erkannte GET-JSON-API vollständig ab: der Request wird im Seiten-
    Origin (page.evaluate → fetch, erreicht die API trotz CORS) mit hochgezähltem
    Seiten-/Offset-Parameter und großem size erneut abgerufen. Gibt die komplette,
    deduplizierte Kandidatenliste zurück. Bei POST/GraphQL oder ohne erkennbare
    Pagination-Params: nur die bereits abgefangene erste Antwort."""
    einzeln = _stellen_aus_api_json([quelle.get("body")], basis_url)
    if (quelle.get("method") or "GET").upper() != "GET":
        return einzeln  # POST/GraphQL: kein generisches Replay

    pr = urlparse(quelle["url"])
    params = urllib.parse.parse_qs(pr.query, keep_blank_values=True)
    low = {k.lower(): k for k in params}
    seite_key = next((low[p] for p in _API_SEITE_PARAMS if p in low), None)
    if not seite_key:
        return einzeln  # keine Pagination erkennbar → eine Antwort reicht

    size_key = next((low[p] for p in _API_SIZE_PARAMS if p in low), None)
    offset_modus = seite_key.lower() in ("offset", "from", "start")
    SIZE = 100
    if size_key:
        params[size_key] = [str(SIZE)]

    def _url(wert: int) -> str:
        p = {k: v[:] for k, v in params.items()}
        p[seite_key] = [str(wert)]
        return urllib.parse.urlunparse(pr._replace(query=urllib.parse.urlencode(p, doseq=True)))

    MAX_API_SEITEN = 30
    alle: list = []
    gesehen: set = set()
    total = None
    wert = 0 if offset_modus else 1
    for _ in range(MAX_API_SEITEN):
        try:
            data = page.evaluate(
                "async (u) => { try { const r = await fetch(u, {headers:{'Accept':'application/json'}});"
                " if(!r.ok) return null; return await r.json(); } catch(e){ return null; } }",
                _url(wert))
        except Exception:
            break
        if not data:
            break
        if total is None:
            total = _api_total(data)
        roh = _finde_job_liste(data)
        if not roh:
            break
        for k in _stellen_aus_api_json([data], basis_url):
            if k["href"] not in gesehen:
                gesehen.add(k["href"])
                alle.append(k)
        if total is not None and len(alle) >= total:
            break
        if total is None and len(roh) < SIZE:
            break  # letzte (Teil-)Seite bei unbekannter Gesamtzahl
        wert += len(roh) if offset_modus else 1
    return alle if alle else einzeln


_UUID_MUSTER = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)


def hat_stabile_offer_id(href: str) -> bool:
    """True wenn der 'offerApiId'-Query-Parameter Base64-dekodiert eine UUID ergibt.
    Manche Job-Boersen (z.B. Neura Robotics) zeigen pro Stelle zwei offer-redirect-
    Links: einen mit stabiler UUID (dauerhaft) und einen mit einem verschluesselten,
    vermutlich sitzungsgebundenen Token (laeuft irgendwann ab). Ohne Bevorzugung
    wuerde der Dedupe zufaellig den ablaufenden Link behalten koennen."""
    werte = urllib.parse.parse_qs(urlparse(href).query).get("offerApiId")
    if not werte:
        return False
    try:
        dekodiert = base64.b64decode(werte[0] + "=" * (-len(werte[0]) % 4)).decode("utf-8")
    except Exception:
        return False
    return bool(_UUID_MUSTER.match(dekodiert))


def standort_aus_linktext(zeilen_roh: list, titel: str, config: dict) -> str:
    """Sucht in den Link-Text-Zeilen diejenige, die ein bekannter Ort ist
    (Treffer in White- oder Blacklist). Die frühere Heuristik 'letzte Zeile
    >= MIN_TITEL_LAENGE' hat kurze Ortsnamen wie 'Metzingen' (9 Zeichen)
    verworfen und stattdessen den Firmennamen als Standort geliefert, was
    Stellen fälschlich als 'Außerhalb Umkreis' aussortiert hat.
    Kein Ortstreffer -> '' (Standort wird später aus dem Rohtext bestimmt)."""
    for z in zeilen_roh:
        if z == titel or len(z) < 3:
            continue
        t = normalisiere_ort(z)
        # Rückwärts-Containment (t in o) nur ab 5 Zeichen, sonst matchen
        # Kürzel wie 'AI' auf Whitelist-Orte wie 'W_ai_blingen'.
        if any(o == t or o in t or (len(t) >= 5 and t in o) for o in config["erlaubte_standorte"]):
            return z
        if any(v in t for v in config["verbotene_standorte"]):
            return z
    return ""


# Domains, deren gelistete Job-Links nur Weiterleitungen sind: der Server
# antwortet mit HTTP 301 auf die echte /offer/<slug>/<uuid>-URL. Die echte URL
# speichern, damit bestehende DB-Einträge wiedererkannt statt dupliziert werden
# (und Report/Bewerbung den dauerhaften Link bekommen).
_REDIRECT_AUFLOESEN_DOMAINS = {"jobs.advantest-career.de"}


def loese_offer_redirect_auf(href: str, cache: dict) -> str:
    if href in cache:
        return cache[href]
    ergebnis = href
    try:
        req = urllib.request.Request(href, headers={"User-Agent": USER_AGENT}, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            final = resp.url
        if "/offer-redirect/" not in final:
            ergebnis = final.split("?")[0]
    except Exception:
        pass  # Auflösung fehlgeschlagen → Original-Link behalten
    cache[href] = ergebnis
    return ergebnis


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
        # KI hängt trotz Anweisung manchmal Text nach dem JSON an -> nur erstes JSON-Objekt parsen
        ergebnis, _ = json.JSONDecoder().raw_decode(text)
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
    gesamt_jobs_gesehen = 0
    api_fehler = False
    struktur_fehler = False

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
            status_merken(name, False, f"API-Fehler: {e}")
            api_fehler = True
            break

        jobs = data
        pfad_ok = True
        for schluessel in api_config["antwort_pfad"]:
            if isinstance(jobs, dict) and schluessel in jobs:
                jobs = jobs[schluessel]
            else:
                pfad_ok = False
                jobs = []
                break
        if not pfad_ok and gesamt_jobs_gesehen == 0:
            # Nur als Fehler werten, wenn noch nie Jobs gefunden wurden – manche APIs
            # (z.B. TE Connectivity) liefern nach der letzten Seite nur noch
            # {"totalJobs": n} ohne die Job-Liste, das ist normales Paginierungsende.
            struktur_fehler = True
        if not jobs:
            print(f"  ℹ️  Keine weiteren Jobs auf Seite {seite+1}")
            break

        unterebene = api_config.get("antwort_unterebene")
        if unterebene:
            jobs = [j.get(unterebene, {}) for j in jobs]

        gesamt_jobs_gesehen += len(jobs)
        print(f"  📋 Seite {seite+1}: {len(jobs)} Jobs gefunden")

        _gesehen_vor_seite = len(gesehen)

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

            if titel in gesehen:
                continue
            gesehen.add(titel)

            treffer = text_matched(titel, config["suchbegriffe"])

            if not treffer:
                kein_treffer_merken(name, titel, url)
                continue

            if treffer:
                _np_grund = ablehnungsgrund(titel, standort, config)
                if _np_grund:
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
                        # else: rohtext bleibt None → rohtext_holen.py lädt nach

                    if standort and rohtext:
                        rohtext = f"Standort: {standort}\n\n{rohtext}"

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

        if len(gesehen) == _gesehen_vor_seite:
            print(f"  ℹ️  Seite {seite+1} lieferte keine neuen Jobs – Paginierung offenbar am Ende, breche ab")
            break

    if not stellen and not ausgeschlossen:
        print(f"  ℹ️  Keine passenden Stellen bei {name}")

    if not api_fehler:
        if struktur_fehler:
            print(f"  ⚠️  Antwortstruktur unerwartet (Pfad {api_config['antwort_pfad']} nicht gefunden) – Request/Payload evtl. defekt")
            status_merken(name, False, f"Antwortstruktur unerwartet (Pfad {api_config['antwort_pfad']} nicht gefunden)")
        elif gesamt_jobs_gesehen == 0:
            print(f"  ℹ️  0 Jobs von der API erhalten (aktuell keine offenen Stellen laut Filter)")
            status_merken(name, True)
        else:
            status_merken(name, True)

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
    gesamt_zeilen_gesehen = 0
    api_fehler   = False
    struktur_fehler = False

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
            status_merken(name, False, f"Fehler: {e}")
            api_fehler = True
            break

        if "html" not in data:
            struktur_fehler = True

        if seite == 1:
            max_seite = int(data.get("maxPage", 1))
            print(f"  📋 {data.get('amount', '?')} Jobs gesamt, {max_seite} Seite(n)")

        zeilen = re.findall(r'<tr\b[^>]*>(.*?)</tr>', data.get("html", ""), re.DOTALL)
        gesamt_zeilen_gesehen += len(zeilen)
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
                kein_treffer_merken(name, titel, url)
                continue

            _np_grund = ablehnungsgrund(titel, standort, config)
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

    if not api_fehler:
        if struktur_fehler:
            print(f"  ⚠️  Antwortstruktur unerwartet ('html'-Feld fehlt) – Request/Payload evtl. defekt")
            status_merken(name, False, "Antwortstruktur unerwartet ('html'-Feld fehlt)")
        elif gesamt_zeilen_gesehen == 0:
            print(f"  ℹ️  0 Zeilen von der API erhalten (aktuell keine offenen Stellen laut Filter)")
            status_merken(name, True)
        else:
            status_merken(name, True)

    return stellen, ausgeschlossen


def scanne_html_tabelle_firma(api_config: dict, bekannte_urls: set, config: dict) -> tuple[list, list]:
    """Scannt eine serverseitig gerenderte HTML-Tabelle (eine <tr> je Stelle, Titel-Link
    in der ersten Zelle, Land/Standort in den folgenden Zellen) ohne Playwright.
    Für Erbe Elektromedizin: die weltweite Job-Übersicht auf de.erbegroup.com liefert Land
    und Standort direkt als eigene Spalten statt im Linktext – die generische Link-Heuristik
    des Playwright-Scanners (die Standort aus dem Linktext rät) findet sie deshalb nicht,
    und auf recruiting.ultipro.com (US-Bewerberportal, vorher konfiguriert) tauchen die
    deutschen Stellen gar nicht erst auf."""
    import html as _html
    name      = api_config["name"]
    url       = api_config["url"]
    basis_url = api_config.get("basis_url", "").rstrip("/")
    zeilen_muster = api_config.get("zeilen_muster", r'<tr class="job-row[^>]*>(.*?)</tr>')

    print(f"\n{'='*60}")
    print(f"  Scanne: {name} (HTML-Tabelle)")
    print(f"{'='*60}")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            seite_html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ❌ Fehler: {e}")
        status_merken(name, False, f"Fehler: {e}")
        return [], []

    stellen = []
    ausgeschlossen = []
    gesehen = set()

    zeilen = re.findall(zeilen_muster, seite_html, re.DOTALL)
    print(f"  📋 {len(zeilen)} Zeilen")

    for zeile in zeilen:
        link_m = re.search(r'<a[^>]*class="job-title"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', zeile, re.DOTALL)
        if not link_m:
            continue
        href    = link_m.group(1)
        titel   = _html.unescape(re.sub(r'<[^>]+>', '', link_m.group(2)).strip())
        url_job = href if href.startswith("http") else f"{basis_url}{href}"

        if url_job in gesehen:
            continue
        gesehen.add(url_job)

        tds      = re.findall(r'<td\b[^>]*>(.*?)</td>', zeile, re.DOTALL)
        orte     = [_html.unescape(re.sub(r'<[^>]+>', '', t).strip()) for t in tds[1:]]
        standort = ", ".join(o for o in orte if o)

        treffer = text_matched(titel, config["suchbegriffe"])
        if not treffer:
            kein_treffer_merken(name, titel, url_job)
            continue

        _np_grund = ablehnungsgrund(titel, standort, config)
        if _np_grund:
            ausgeschlossen.append({"firma": name, "titel": titel, "url": url_job,
                                   "treffer": treffer, "nicht_passend_grund": _np_grund})
            print(f"  🚫 Nicht passend: {titel[:70]}")
            continue

        stellen.append({
            "firma": name, "titel": titel, "url": url_job,
            "arbeitsort": standort,
            "standort": berechne_standort(standort, config["erlaubte_standorte"], config["verbotene_standorte"]),
            "treffer": treffer,
            "neu": url_job not in bekannte_urls, "rohtext": None,
        })
        print(f"  ✅ {titel[:70]}")
        if standort:
            print(f"     📍 {standort}")
        print(f"     Treffer: {', '.join(treffer)}")

    if not stellen and not ausgeschlossen:
        print(f"  ℹ️  Keine passenden Stellen bei {name}")
        if not zeilen:
            status_merken(name, False, "0 Zeilen gefunden (Struktur evtl. geändert)")
        else:
            status_merken(name, True)
    else:
        status_merken(name, True)

    return stellen, ausgeschlossen


def scanne_workday_firma(api_config: dict, bekannte_urls: set, config: dict) -> tuple[list, list]:
    name    = api_config["name"]
    tenant  = api_config["tenant"]
    portal  = api_config["portal"]
    wd      = api_config.get("wd_version", "wd3")

    api_url   = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{portal}/jobs"
    basis_url = f"https://{tenant}.{wd}.myworkdayjobs.com"

    print(f"\n{'='*60}")
    print(f"  Scanne: {name} (Workday API)")
    print(f"{'='*60}")

    stellen = []
    ausgeschlossen = []
    gesehen = set()
    limit = api_config["payload"].get("limit", 20)
    gesamt_jobs_gesehen = 0
    api_fehler = False
    struktur_fehler = False

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
            status_merken(name, False, f"API-Fehler: {e}")
            api_fehler = True
            break

        if "jobPostings" not in data:
            struktur_fehler = True

        total = data.get("total", 0)
        jobs  = data.get("jobPostings", [])

        if not jobs:
            print(f"  ℹ️  Keine weiteren Jobs auf Seite {seite+1}")
            break

        gesamt_jobs_gesehen += len(jobs)

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

            treffer  = text_matched(titel, config["suchbegriffe"])

            if not treffer:
                kein_treffer_merken(name, titel, url)
                continue

            if treffer:
                _np_grund = ablehnungsgrund(titel, standort, config)
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
                        "rohtext": None,
                    })
                    neu_label = "🆕 " if ist_neu else "   "
                    print(f"  ✅ {neu_label}{titel}")
                    print(f"     {standort} | Treffer: {', '.join(treffer)}")

        if payload["offset"] + len(jobs) >= total:
            break

    if not stellen and not ausgeschlossen:
        print(f"  ℹ️  Keine passenden Stellen bei {name}")

    if not api_fehler:
        if struktur_fehler:
            print(f"  ⚠️  Antwortstruktur unerwartet ('jobPostings'-Feld fehlt) – Request/Payload evtl. defekt")
            status_merken(name, False, "Antwortstruktur unerwartet ('jobPostings'-Feld fehlt)")
        elif gesamt_jobs_gesehen == 0:
            print(f"  ℹ️  0 Jobs von der API erhalten (aktuell keine offenen Stellen laut Filter)")
            status_merken(name, True)
        else:
            status_merken(name, True)

    return stellen, ausgeschlossen


# =============================================================================
# PLAYWRIGHT SCANNER
# =============================================================================

# Gemeinsame Paginierungs-Erkennung (Light- + Shadow-DOM), sprach- und
# framework-unabhängig über die Seitenzahlen. Statt "Geschwister unter demselben
# Eltern-Knoten" (scheitert, wenn jede Zahl einzeln in <li>/<span> gewrappt ist,
# z.B. Rheinmetall) wird von einer Zahl aus der nächste Vorfahr gesucht, der >=3
# nummerierte a/button enthält – das ist die Paginierungsleiste. Die >=3-Schwelle
# schützt vor einzelnen Jahreszahlen o.Ä. in Job-Karten.
_PAGINATION_HELPER_JS = r"""
    function _klasse(el) {
        return (typeof el.className === 'string')
            ? el.className : ((el.className && el.className.baseVal) || '');
    }
    function _sammleNums() {
        const nums = [];
        function walk(root) {
            root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot); });
            root.querySelectorAll('a,button').forEach(el => {
                if (/^\d{1,4}$/.test((el.innerText || '').trim())) nums.push(el);
            });
        }
        walk(document);
        return nums;
    }
    function _container(nums) {
        function anzahl(anc) { let c = 0; for (const x of nums) if (anc.contains(x)) c++; return c; }
        let cur = nums[0] && nums[0].parentElement, tiefe = 0;
        while (cur && tiefe < 6) { if (anzahl(cur) >= 3) return cur; cur = cur.parentElement; tiefe++; }
        return null;
    }
    function _paginationItems() {
        const nums = _sammleNums();
        if (nums.length < 3) return [];
        const c = _container(nums);
        if (!c) return [];
        return nums.filter(el => c.contains(el))
                   .map(el => [parseInt(el.innerText.trim(), 10), el]);
    }
"""

# Klickt die "nächste Seite" einer nummerierten Paginierung per JS – für Fälle,
# in denen der "Weiter"-Button für Playwright nicht klickbar ist (z.B. Sitecore-
# patternlib-Controls in einem Shadow-Root, Actionability-Timeout). Aktive Seite
# aus aria-current oder Klasse active/current/selected, dann active+1 klicken.
_SHADOW_NEXT_JS = "() => {" + _PAGINATION_HELPER_JS + r"""
    const items = _paginationItems();
    if (!items.length) return false;
    let active = null;
    for (const [n, el] of items)
        if (el.getAttribute('aria-current')
            || /(^|\s)(active|current|selected)(\s|$)/i.test(_klasse(el))) { active = n; break; }
    if (active === null) return false;
    const naechste = items.find(([n]) => n === active + 1);
    if (naechste) { naechste[1].click(); return true; }
    return false;
}"""


def _klick_naechste_seite_shadow(page) -> bool:
    try:
        return bool(page.evaluate(_SHADOW_NEXT_JS))
    except Exception:
        return False


# Liefert die höchste Seitenzahl aus der Paginierungsleiste, oder 0. Damit wissen
# wir bei der URL-Paginierung, bis zu welcher Seite es sich zu blättern lohnt.
_MAX_SEITE_JS = "() => {" + _PAGINATION_HELPER_JS + r"""
    const items = _paginationItems();
    return items.length ? Math.max(...items.map(([n]) => n)) : 0;
}"""

# Gängige Query-Parameter für die Seitennummer (in Reihenfolge des Ausprobierens).
_URL_SEITEN_PARAM = ("page", "p", "pageNumber", "seite", "pg")
_URL_PAGINATION_CAP = 100  # harte Obergrenze an Seiten (Laufzeit-Schutz)

# Auf der Trefferliste genannte Gesamtzahl ("69 Ergebnisse"), um unvollständige
# Paginierung zu erkennen. Bewusst breit (mehrsprachig), Zahl darf Tausender-
# Punkt/Komma enthalten (z.B. "1.234 Treffer").
_TREFFER_RE = re.compile(
    r'([\d.,]{1,7})\s*(?:ergebnis|treffer|stellen|result|jobs?\b|vacan|position|offene)',
    re.IGNORECASE)


def _sxa_signatur(url_boerse: str) -> str:
    """32-stellige Hex-Signatur, die Sitecore SXA seinen Query-Parametern
    voranstellt (z.B. '<sig>term=', '<sig>filter='). Der zugehörige Seiten-
    Parameter ist dann '<sig>page'. Leerer String, wenn keine Signatur da ist."""
    for teil in urlparse(url_boerse).query.split("&"):
        m = re.match(r'^([0-9a-f]{32})', teil.split("=", 1)[0])
        if m:
            return m.group(1)
    return ""


def _gesamt_treffer(page) -> int:
    """Liest die auf der Seite genannte Gesamt-Trefferzahl aus (für die
    Abdeckungs-Warnung), oder 0 wenn keine gefunden wird."""
    try:
        txt = page.inner_text("body")[:20000]
    except Exception:
        return 0
    zahlen = []
    for m in _TREFFER_RE.finditer(txt):
        try:
            zahlen.append(int(m.group(1).replace(".", "").replace(",", "")))
        except ValueError:
            pass
    # größte plausible Zahl (die Trefferzahl steht meist prominent, andere Zahlen
    # sind kleiner); unrealistisch große Werte ignorieren.
    zahlen = [z for z in zahlen if 0 < z < 100000]
    return max(zahlen) if zahlen else 0


def _paginiere_per_url(page, url_boerse: str, alle_links: list,
                       gesehene_hrefs: set, links_js: str) -> int:
    """Blättert über einen URL-Query-Parameter (?page=N) statt per Klick.

    Für Portale, deren nummerierte Paginierung nur JS-Klick-Handler ohne echtes
    href hat und deshalb im Headless-Browser nicht nachlädt (z.B. Rheinmetall /
    Sitecore SXA: ?page=N liefert serverseitig die nächsten Treffer). Läuft nur
    als Fallback, wenn die Klick-Paginierung nichts gebracht hat, und nur wenn
    eine nummerierte Paginierungsleiste erkannt wird. Portal- und sprach-
    unabhängig: bevorzugt bei Sitecore SXA den signatur-präfixierten Parameter,
    sonst gängige Namen, und übernimmt den, dessen Seite 2 neue Job-Links liefert.
    Blättert bis zur letzten Seite (zwei leere Seiten = Ende) und warnt, wenn
    weniger Stellen erfasst wurden als die Seite als Gesamtzahl nennt (manche
    Portale liefern über den URL-Parameter nicht zuverlässig alle Treffer).
    Gibt die Zahl zusätzlich geladener Seiten zurück."""
    max_seite = 0
    try:
        max_seite = int(page.evaluate(_MAX_SEITE_JS) or 0)
    except Exception:
        pass
    if max_seite < 2:
        return 0
    gesamt = _gesamt_treffer(page)  # vor dem Wegnavigieren von Seite 1 lesen
    trenn = "&" if "?" in url_boerse else "?"

    def lade(param: str, n: int) -> list:
        try:
            page.goto(f"{url_boerse}{trenn}{param}={n}",
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
            return page.evaluate(links_js)
        except Exception:
            return []

    # Parameter bestimmen: Seite 2 muss neue, nach Job aussehende Links liefern.
    # Bei SXA zuerst den korrekten '<sig>page'-Parameter probieren.
    sig = _sxa_signatur(url_boerse)
    kandidaten_param = ([f"{sig}page"] if sig else []) + list(_URL_SEITEN_PARAM)
    param = None
    seite2_links: list = []
    for kandidat in kandidaten_param:
        links = lade(kandidat, 2)
        if any(l["href"] not in gesehene_hrefs and ist_job_link(l["href"]) for l in links):
            param, seite2_links = kandidat, links
            break
    if not param:
        return 0

    print(f"  🔢 URL-Paginierung erkannt (?{param}=N, bis Seite {max_seite})")
    zusatz = 0
    leer_folge = 0
    aktuelle = seite2_links
    n = 2
    while n <= min(max_seite, _URL_PAGINATION_CAP):
        # Nicht beim ersten überlappenden (0-neuen) Seite abbrechen – manche
        # Portale (Rheinmetall) liefern überlappende Seiten; erst zwei leere
        # Seiten hintereinander gelten als Listenende.
        if not any(ist_job_link(l["href"]) for l in aktuelle):
            leer_folge += 1
            if leer_folge >= 2:
                break
        else:
            leer_folge = 0
        neue_hrefs = {l["href"] for l in aktuelle} - gesehene_hrefs
        if neue_hrefs:
            alle_links.extend(l for l in aktuelle if l["href"] in neue_hrefs)
            gesehene_hrefs |= neue_hrefs
            zusatz += 1
        n += 1
        if n <= min(max_seite, _URL_PAGINATION_CAP):
            aktuelle = lade(param, n)
    if zusatz:
        print(f"  🔢 URL-Paginierung: +{zusatz} Seite(n), {len(alle_links)} Links gesamt")
    if gesamt:
        erfasst = len({l["href"] for l in alle_links if ist_job_link(l["href"])})
        if erfasst < gesamt * 0.95:
            print(f"  ⚠️  Nur {erfasst} von {gesamt} Stellen erfasst – das Portal "
                  f"liefert über die URL-Paginierung nicht alle Treffer. Für "
                  f"vollständige Ergebnisse enger filtern (Stadt/Bereich).")
    return zusatz


# Firmen-Karriereseiten binden ihre Stellen oft über ein iframe-Widget eines
# externen ATS ein (z.B. onlyfy bei Dürr Dental / Bertrandt). Der eigentliche
# Stellen-Content liegt dann im iframe, nicht auf der Wrapper-Seite. Wird ein
# solches Widget erkannt, scannt scanne_boerse direkt dessen URL. Erweiterbar.
_ATS_WIDGET_MUSTER = ("onlyfy.jobs/candidate/widget",)


def _finde_ats_widget(page) -> str:
    """URL eines eingebetteten ATS-Widget-iframes, oder '' wenn keins da ist."""
    for f in page.frames:
        u = f.url or ""
        if any(m in u for m in _ATS_WIDGET_MUSTER):
            return u
    return ""


def _onlyfy_alle_url(widget_url: str) -> str:
    """Schreibt eine onlyfy-Widget-URL auf den ajax_list-Endpoint um, der ALLE
    Stellen auf einmal liefert (display_length hoch) – spart das wiederholte
    Klicken auf 'WEITERE ANZEIGEN' (onlyfy zeigt sonst nur 10 pro Seite)."""
    m = re.search(r'(https?://[^/]+)/candidate/widget/([a-z0-9]+)', widget_url)
    if not m:
        return widget_url
    host, cfg = m.group(1), m.group(2)
    return (f"{host}/candidate/job/ajax_list?widgetConfig={cfg}"
            f"&display_length=200&page=1&sort=matching&sort_dir=DESC&search=")


def scanne_boerse(page, firma: dict, strukturen: dict, config: dict) -> tuple[list, list]:
    name       = firma["name"]
    url_boerse = firma["url"]
    dom        = domain(url_boerse)

    print(f"\n{'='*60}")
    print(f"  Scanne: {name}")
    print(f"{'='*60}")

    # b-ite-ATS (z.B. Vincorion, eta plus) lädt die Stellen per API-Call an
    # jobs.b-ite.com – nicht im DOM. Die Antwort abfangen und daraus später
    # direkt die Kandidaten bauen (statt DOM-Links zu scrapen).
    bite_jobs: list = []
    def _b_ite_capture(resp):
        try:
            if "b-ite.com" in resp.url and "postings/search" in resp.url:
                jp = (resp.json() or {}).get("jobPostings") or []
                if jp:
                    bite_jobs[:] = jp
        except Exception:
            pass
    page.on("response", _b_ite_capture)

    # Generischer JSON-Capture: JSON-Antworten von XHR/fetch mitschneiden – inkl.
    # Request (URL/Methode/Body), damit eine erkannte Job-API später vollständig
    # nachpaginiert werden kann. Deckt SPAs ab, die Stellen per API/GraphQL laden
    # (z.B. Zeiss-Gateway → Workday, Capgemini job-search).
    api_captures: list = []
    def _json_capture(resp):
        try:
            if len(api_captures) >= 60:
                return
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            if "json" not in (resp.headers.get("content-type", "") or "").lower():
                return
            body = resp.json()
            if isinstance(body, (dict, list)):
                try:
                    post_data = resp.request.post_data
                except Exception:
                    post_data = None
                api_captures.append({
                    "url":       resp.url,
                    "method":    resp.request.method,
                    "post_data": post_data,
                    "body":      body,
                })
        except Exception:
            pass
    page.on("response", _json_capture)

    try:
        antwort = page.goto(url_boerse, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"  ❌ Seite nicht erreichbar: {e}")
        status_merken(name, False, f"Seite nicht erreichbar: {e}")
        return [], []

    if antwort and antwort.status in (403, 429):
        raise SessionGesperrtFehler(antwort.status)

    page.wait_for_timeout(3000)
    klick_cookie_banner(page)

    # Eingebettetes ATS-Widget (iframe) automatisch erkennen und direkt dessen
    # Stellen scannen – die Wrapper-Seite selbst listet keine Stellen (z.B. onlyfy).
    # Greift auch, wenn direkt die Widget-URL eingegeben wurde (dann alle Stellen
    # über den ajax_list-Endpoint statt nur die ersten 10).
    widget_url = url_boerse if any(m in url_boerse for m in _ATS_WIDGET_MUSTER) \
        else _finde_ats_widget(page)
    if widget_url:
        scan_url = _onlyfy_alle_url(widget_url)
        if scan_url != url_boerse:
            print(f"  🔎 onlyfy-Widget – scanne alle Stellen direkt: {scan_url[:75]}")
            try:
                page.goto(scan_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                klick_cookie_banner(page)
            except Exception as e:
                print(f"  ⚠️  Widget-URL nicht ladbar, bleibe auf Wrapper-Seite: {e}")
                scan_url = url_boerse
        url_boerse = scan_url
        dom = domain(scan_url)

    if any(d in url_boerse for d in ("nokia.com", "oraclecloud.com", "ultipro.com")):
        print("  ⏳ Oracle CX / UltiPro – warte auf Netzwerk-Idle...")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

    # Bis zum Seitenende scrollen statt fixer Schrittzahl: Infinite-Scroll-
    # Listen (z.B. Neura Robotics, ~100 Stellen alphabetisch) laden sonst nur
    # die ersten ~30 Einträge und alles ab "H" wird nie gesehen. Abbruch, wenn
    # die Link-Anzahl mehrere Runden stabil bleibt.
    print("  📜 Scrolle...")
    MAX_SCROLL_RUNDEN = 40
    letzte_anzahl = -1
    stabil = 0
    for _ in range(MAX_SCROLL_RUNDEN):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)
        anzahl = page.evaluate("document.querySelectorAll('a[href]').length")
        if anzahl == letzte_anzahl:
            stabil += 1
            if stabil >= 3:
                break
        else:
            stabil = 0
            letzte_anzahl = anzahl
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(2000)

    # Link-Sammler, der auch Shadow-DOM durchdringt: moderne Web-Components
    # (z.B. Sitecore-patternlib, manche Salesforce-/Custom-Portale) kapseln ihre
    # Job-Links in Shadow-Roots, die ein normales document.querySelectorAll der
    # Hauptseite nicht sieht. Rekursiv über alle Shadow-Roots gehen.
    _LINKS_JS = """() => {
        const out = [];
        const titelVon = (a) => {
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
            return text;
        };
        const besuche = (root) => {
            let anchors = [];
            try { anchors = root.querySelectorAll('a[href]'); } catch (e) {}
            for (const a of anchors) out.push({ href: a.href, text: titelVon(a) });
            let alle = [];
            try { alle = root.querySelectorAll('*'); } catch (e) {}
            for (const el of alle) if (el.shadowRoot) besuche(el.shadowRoot);
        };
        besuche(document);
        return out;
    }"""
    alle_links = page.evaluate(_LINKS_JS)

    # Zusätzlich Links aus allen iframes einsammeln: eingebettete ATS-Widgets
    # rendern die Stellen oft in einem (auch fremd-domänigen) Frame, den das
    # querySelectorAll der Hauptseite nicht erreicht. Playwright kann in jeden
    # Frame hineinsehen. Bereits bekannte hrefs werden später ohnehin dedupliziert.
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        try:
            frame_links = [l for l in frame.evaluate(_LINKS_JS) if l.get("href")]
        except Exception:
            continue
        if frame_links:
            alle_links.extend(frame_links)
            print(f"  🖼️  +{len(frame_links)} Links aus iframe: {frame.url[:60]}")

    print(f"  🔗 {len(alle_links)} Links gesamt (Seite 1)")

    # Seiten-Paginierung folgen (statt nur Infinite-Scroll): manche Portale
    # (z.B. umantis, festool-group.com) zeigen nur ~10 Treffer pro Seite und
    # brauchen einen Klick auf "nächste Seite" statt Scrollen.
    # Neben aria-label auch das Standard-rel="next" sowie Klassen-Marker
    # abdecken: manche Portale (z.B. Rheinmetall, Sitecore SXA) haben einen
    # "nächste"-Button ganz ohne aria-label – nur Klasse "anchor-next" bzw. ein
    # eigenes Token "next". [class~='next'] matcht nur das ganze Token "next"
    # (nicht "context" o.Ä.); Playwright klickt real (kümmert sich um Sichtbar-
    # keit/Scroll), was zuverlässiger ist als ein synthetischer JS-Klick.
    _NEXT_SELECTOR = (
        "a[rel='next']:not([aria-disabled='true']), "
        "button[rel='next']:not([disabled]), "
        "a[aria-label*='nächste' i]:not([aria-disabled='true']), "
        "a[aria-label*='next' i]:not([aria-disabled='true']), "
        "button[aria-label*='nächste' i]:not([disabled]), "
        "button[aria-label*='next' i]:not([disabled]), "
        "a[class~='next']:not([aria-disabled='true']):not(.disabled), "
        "button[class~='next']:not([disabled]):not(.disabled), "
        "a[class*='anchor-next']:not([aria-disabled='true']):not(.disabled), "
        "button[class*='anchor-next']:not([disabled]):not(.disabled)"
    )
    MAX_SEITEN = 15
    gesehene_hrefs = {l["href"] for l in alle_links}
    seite = 1
    while seite < MAX_SEITEN:
        try:
            next_el = page.query_selector(_NEXT_SELECTOR)
            if next_el and next_el.is_visible() and next_el.is_enabled():
                next_el.click()
            elif not _klick_naechste_seite_shadow(page):
                # Weder Standard-"nächste"-Button noch Shadow-DOM-Paginierung
                # (z.B. Sitecore-patternlib, deren Controls in einem Shadow-Root
                # liegen und nicht klickbar sind – daher JS-Klick) → Ende.
                break
            page.wait_for_timeout(2500)
            for _ in range(3):
                page.evaluate("window.scrollBy(0, 1200)")
                page.wait_for_timeout(800)
        except Exception:
            break

        neue_links = page.evaluate(_LINKS_JS)
        neue_hrefs = {l["href"] for l in neue_links} - gesehene_hrefs
        if not neue_hrefs:
            break  # keine neuen Links -> Klick hat nichts bewirkt, Schleife beenden
        alle_links.extend(l for l in neue_links if l["href"] in neue_hrefs)
        gesehene_hrefs |= neue_hrefs
        seite += 1
        print(f"  🔗 Seite {seite}: {len(neue_hrefs)} neue Links ({len(alle_links)} gesamt)")

    # Hat die Klick-Paginierung keine weitere Seite gebracht, als Fallback über
    # einen URL-Query-Parameter blättern (nummerierte Leisten ohne echtes href,
    # deren JS-Klick im Headless nicht nachlädt – z.B. Rheinmetall).
    if seite == 1:
        _paginiere_per_url(page, url_boerse, alle_links, gesehene_hrefs, _LINKS_JS)

    print(f"  🔗 {len(alle_links)} Links gesamt")

    muster = strukturen.get(dom, {}).get("link_muster")

    def muster_trifft(href: str, m: str) -> bool:
        # Von der KI gelernte Muster sind als wörtlicher Teilstring gedacht,
        # können aber zufällig Regex-Sonderzeichen enthalten (z.B. "?" vor
        # einem Query-String wie "OpportunityDetail?opportunityId="). Als
        # Regex interpretiert würde das die Bedeutung verändern und nie
        # matchen – daher zuerst als literaler Substring prüfen.
        if m in href:
            return True
        try:
            return bool(re.search(m, href))
        except re.error:
            return False

    aus_api = False  # True, wenn Kandidaten aus einer abgefangenen JSON-API stammen
    # API-first: zuerst prüfen, ob die Seite Stellen per JSON-API geliefert hat.
    # Wenn ja, diese sauberen, vollständig paginierten Daten bevorzugen – DOM-
    # Scraping erwischt bei SPAs oft nur zufällige Teilmengen (flaky). Nur die
    # b-ite-Sondererkennung hat Vorrang.
    api_kandidaten: list = []
    if not bite_jobs:
        _api_kand, _api_quelle = _finde_job_api(api_captures, url_boerse)
        if _api_kand and _api_quelle:
            api_kandidaten = _paginiere_api(page, _api_quelle, url_boerse)

    if bite_jobs:
        # b-ite-API erkannt: Stellen direkt aus der Antwort (Titel + echte Job-URL),
        # unabhängig von DOM/Heuristik. jobSite (Standort) ist oft leer.
        kandidaten = [{"href": j.get("url", ""),
                       "text": j.get("title", ""),
                       "arbeitsort": j.get("jobSite") or ""}
                      for j in bite_jobs if j.get("url") and j.get("title")]
        print(f"  🔎 b-ite-API erkannt – {len(kandidaten)} Stellen direkt aus der API")
    elif len(api_kandidaten) >= MIN_HEURISTIK_LINKS:
        # Solide Job-API erkannt → saubere, vollständige Daten bevorzugen (vor
        # DOM-Muster/Heuristik). z.B. Zeiss (GraphQL→Workday), Capgemini (job-search).
        kandidaten = api_kandidaten
        aus_api = True
        print(f"  🛰️  JSON-API erkannt – {len(kandidaten)} Stellen (voll paginiert)")
    elif muster:
        print(f"  ✅ Bekanntes Muster: '{muster}'")
        kandidaten = [l for l in alle_links if muster_trifft(l["href"], muster)]
    elif api_kandidaten:
        # API vorhanden, aber wenige Treffer – trotzdem nutzen (sauberer als
        # flakiges DOM-Scraping), statt an der Heuristik zu scheitern.
        kandidaten = api_kandidaten
        aus_api = True
        print(f"  🛰️  JSON-API erkannt – {len(kandidaten)} Stellen aus abgefangener API-Antwort")
    else:
        kandidaten = [l for l in alle_links if ist_job_link(l["href"]) or ist_ats_host(l["href"])]
        schon_ki_geprueft = bool(strukturen.get(dom, {}).get("ki_geprueft"))

        # Schwelle NICHT gegen rohe Treffer prüfen: Navigations-/Übersichtslinks
        # (z.B. /de/jobs/, /en/jobs/) erfüllen ist_job_link(), sind aber keine
        # Stellen. Würden sie mitzählen, gilt die Heuristik fälschlich als
        # erfolgreich und die KI (die das echte Muster lernen könnte) wird nie
        # gefragt. Daher nur echte Detail-Links als Zählbasis.
        echte = _echte_job_links(kandidaten, url_boerse)

        if len(echte) >= MIN_HEURISTIK_LINKS or (echte and schon_ki_geprueft):
            print(f"  ✅ Heuristik: {len(echte)} Job-Links erkannt")
        elif schon_ki_geprueft:
            # Domain wurde schon einmal ergebnislos der KI vorgelegt → nicht erneut
            # fragen (spart Tokens); nichts gefunden heißt hier wirklich nichts.
            print(f"  ⚠️  Kein Muster gefunden – überspringe {name}")
            status_merken(name, False, "Kein Job-Link-Muster erkannt")
            return [], []
        else:
            # Heuristik findet verdächtig wenig (oder nichts) → KI um ein Muster
            # bitten und nur das STRIKT bessere Ergebnis übernehmen. Das fängt
            # Seiten ab, auf denen ein paar beiläufige Links die Heuristik "erfüllen",
            # die echten Stellen aber ein anderes Muster haben. Der >-Vergleich
            # (gegen die ECHTEN Detail-Links) schützt eine funktionierende
            # Heuristik, die 0.6-Grenze vor zu breiten (Navigation mitfangenden)
            # KI-Mustern.
            print(f"  🤖 Heuristik nur {len(echte)} echte Job-Link(s) – frage KI...")
            alle_hrefs = list({l["href"] for l in alle_links if len(l["href"]) > 30})
            ki_muster = ki_lerne_muster(dom, alle_hrefs, config["api_key"])
            ki_kandidaten = ([l for l in alle_links if muster_trifft(l["href"], ki_muster)]
                             if ki_muster else [])
            zu_breit = len(ki_kandidaten) > 0.6 * max(len(alle_links), 1)
            if ki_muster and len(ki_kandidaten) > len(echte) and not zu_breit:
                print(f"  ✅ KI-Muster gelernt: '{ki_muster}' ({len(ki_kandidaten)} Links)")
                strukturen.setdefault(dom, {})["link_muster"] = ki_muster
                strukturen.setdefault(dom, {})["gelernt_am"] = jetzt()
                kandidaten = ki_kandidaten
            else:
                # Kein besseres KI-Muster. Domain merken (nur wenn die Seite geladen
                # war, sonst könnte ein transienter Fehler die KI dauerhaft aussperren),
                # damit wir nicht bei jedem Lauf erneut die KI bemühen.
                if len(alle_links) >= 20:
                    strukturen.setdefault(dom, {})["ki_geprueft"] = jetzt()
                if echte:
                    # Nur die echten Detail-Links weiterverwenden (Übersichts-/
                    # Selbst-Links raus), nicht die rohe Müll-Liste.
                    kandidaten = echte
                    print(f"  ✅ Heuristik: {len(echte)} Job-Links (KI kein besseres Muster)")
                else:
                    print(f"  ⚠️  Kein Muster gefunden – überspringe {name}")
                    status_merken(name, False, "Kein Job-Link-Muster erkannt")
                    return [], []

    # API-Kandidaten überspringen den Domain-/Bewerbungslink-Filter: ihre URLs
    # zeigen bewusst auf das dahinterliegende ATS (z.B. *.myworkdayjobs.com) und
    # sind bereits echte Job-Einträge aus der API, keine beiläufigen DOM-Links.
    if not aus_api:
        rd = root_domain(url_boerse)
        vor_filter = len(kandidaten)
        kandidaten = [l for l in kandidaten
                      if root_domain(l["href"]) == rd or ist_ats_host(l["href"])]
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

    if dom in _REDIRECT_AUFLOESEN_DOMAINS:
        _redirect_cache: dict = {}
        for l in kandidaten:
            if "/offer-redirect/" in l["href"]:
                l["href"] = loese_offer_redirect_auf(l["href"], _redirect_cache)
        aufgeloest = sum(1 for v in _redirect_cache.values() if "/offer-redirect/" not in v)
        if _redirect_cache:
            print(f"  ↪️  {aufgeloest}/{len(_redirect_cache)} Redirect-Links auf echte Job-URLs aufgelöst")

    print(f"  📋 {len(kandidaten)} Kandidaten")
    for _href in sorted({l["href"] for l in kandidaten}):
        print(f"     🔗 {_href}")

    # Stabile offer-redirect-Links (Base64 → UUID) zuerst, damit der Dedupe
    # unten nicht zufällig den ablaufenden Token-Link statt der dauerhaften
    # UUID-Variante behält (Python-Sort ist stabil, Reihenfolge sonst unverändert).
    kandidaten = sorted(kandidaten, key=lambda l: not hat_stabile_offer_id(l["href"]))

    gefunden = []
    ausgeschlossen = []
    gesehen_urls = set()
    gesehen_titel = set()
    roh = 0               # echte Stellen (eindeutiger Titel) VOR allen Inhalts-Filtern
    ohne_suchbegriff = 0  # davon wegen fehlendem Suchbegriff verworfen

    for link in kandidaten:
        href = link["href"].split("#")[0].rstrip("/") or link["href"]
        titel_roh = link["text"]

        ist_pdf_link = link.get("is_pdf") or href.lower().endswith(".pdf")

        zeilen_roh = [z.strip() for z in titel_roh.split("\n") if z.strip()]
        zeilen = [z for z in zeilen_roh if len(z) >= MIN_TITEL_LAENGE]
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
        roh += 1

        # Bei API-Kandidaten den Standort direkt aus der API übernehmen (z.B.
        # primaryLocation), sonst wie bisher aus dem Linktext ableiten.
        standort_aus_text = link.get("arbeitsort") or standort_aus_linktext(zeilen_roh, titel, config)

        treffer = text_matched(titel, config["suchbegriffe"])

        if not treffer and not ist_pdf_link:
            kein_treffer_merken(name, titel, href)
            ohne_suchbegriff += 1
            continue
        if not treffer:
            treffer = ["pdf"]

        _np_grund = ablehnungsgrund(titel, standort_aus_text, config)

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

    # Debug-Aufschlüsselung: was findet der Scanner roh, und wie stark filtern
    # Suchbegriffe bzw. Ausschluss/Standort? (roh = echte Stellen vor allen Filtern)
    print(f"  📊 [{name}] roh gefunden: {roh}  |  ohne Suchbegriff: {ohne_suchbegriff}"
          f"  |  ausgeschlossen (Ausschluss/Standort): {len(ausgeschlossen)}"
          f"  |  passend: {len(gefunden)}")

    if not gefunden and not ausgeschlossen:
        print(f"  ℹ️  Keine passenden Stellen.")
        for k in kandidaten[:10]:
            t = k["text"].split("\n")[0].strip()
            print(f"     - {t[:80]}")

    if not kandidaten:
        print(f"  ⚠️  0 Job-Kandidaten auf der Seite gefunden – Struktur evtl. geändert")
        status_merken(name, False, "0 Job-Links auf der Seite gefunden (Struktur evtl. geändert)")
    else:
        status_merken(name, True)
    return gefunden, ausgeschlossen


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

    ignoriert = standort_ignoriert_urls()
    zu_entfernen = []
    gruende = {}
    for stelle in stellen:
        # Übernommene out-of-area-Stellen (Standort-Ausnahme) sowie provisorische
        # Vorschau-Stellen dauerhaft durchlassen – sie sollen ja gerade außerhalb
        # des Umkreises bleiben.
        if stelle.get("url") in ignoriert:
            continue
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


def bereinige_ausschlussbegriffe(stellen: list, bekannte: dict, begriffe: list) -> int:
    """Entfernt bereits gespeicherte Stellen, deren Titel einen Ausschlussbegriff
    enthält. Fängt Fälle ab, in denen ein Begriff erst nachträglich zur
    config.txt hinzugefügt wurde, nachdem die Stelle schon gefunden war.
    Markiert den bekannte-Eintrag als nicht_passend (statt löschen), damit die
    Stelle im selben Scan-Lauf nicht neu hinzugefügt wird.
    Gibt die Anzahl entfernter Stellen zurück."""
    if not begriffe:
        return 0

    zu_entfernen = []
    gruende = {}
    for stelle in stellen:
        titel = stelle.get("titel") or ""
        if not titel or not ist_ausgeschlossen(titel, begriffe):
            continue
        for b in begriffe:
            t = titel.lower()
            if (all(teil in t for teil in b.split("+")) if "+" in b else b in t):
                zu_entfernen.append(stelle)
                gruende[stelle.get("url")] = f"Ausschlussbegriff: '{b}'"
                break

    if zu_entfernen:
        print(f"\n🧹 {len(zu_entfernen)} Stelle(n) wegen Ausschlussbegriff entfernt:")
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

def main_vorschau(nur_firma: str | None = None, direkt_url: str | None = None,
                  direkt_name: str | None = None):
    """Breiter Scan (ganz Deutschland).

    Findet Stellen wie der normale Scan, aber:
    - Standort-Regel = "ganz Deutschland" (Whitelist aus, nur Ausland raus),
    - schreibt NICHT in die DB, sondern nach VORSCHAU_JSON,
    - keine KI, keine Pipeline. Bereits bekannte URLs werden ausgefiltert.
    Die Vorschau-Datei wird bei jedem Lauf komplett neu geschrieben.

    Mit ``direkt_url`` wird genau diese (frei eingegebene) Karriere-URL per
    Playwright gescannt – NICHT die config-URL der Firma. Das ist der Normalfall
    für die breite Suche, weil config-URLs oft Standort-Parameter (z.B. nur
    Stuttgart) enthalten, die "ganz Deutschland" aushebeln würden.
    """
    print("\n" + "=" * 60)
    print("  SCANNER  –  Breiter Vorschau-Scan (ganz Deutschland)")
    if direkt_url:
        print(f"  Direkt-URL: {direkt_url[:70]}")
    elif nur_firma:
        print(f"  Filter: nur '{nur_firma}'")
    print("=" * 60)

    config = lade_config()
    # "Ganz Deutschland": Whitelist ignorieren, nur Ausland als verboten werten.
    config["erlaubte_standorte"] = []
    config["verbotene_standorte"] = list(AUSLAND_MARKER)

    if direkt_url:
        # Freie URL direkt scannen (Playwright), config-Firmen ignorieren.
        api_firmen = []
        config["firmen"] = [{"name": (direkt_name or "Manuell").strip() or "Manuell",
                             "url": direkt_url}]
    else:
        api_firmen = lade_api_firmen(config)
        if nur_firma:
            api_firmen       = [f for f in api_firmen        if f["name"].lower() == nur_firma.lower()]
            config["firmen"] = [f for f in config["firmen"]  if f["name"].strip().lower() == nur_firma.lower()]

    strukturen: dict = lade_json(STRUKTUREN_JSON, {})

    sys.path.insert(0, str(BASIS_PFAD))
    from db import lade_bekannte_dict
    bekannte_urls = set(lade_bekannte_dict().keys())

    kandidaten: list = []
    gesehen: set = set()
    ts = jetzt()

    def sammle(treffer_liste: list):
        for t in treffer_liste:
            url = t.get("url")
            if not url or url in gesehen or url in bekannte_urls:
                continue
            gesehen.add(url)
            kandidaten.append({
                "firma":       t.get("firma", ""),
                "titel":       t.get("titel", ""),
                "url":         url,
                "arbeitsort":  t.get("arbeitsort", ""),
                "treffer":     t.get("treffer", []),
                "gefunden_am": ts,
            })

    # API-Firmen
    for api_firma in api_firmen:
        try:
            if api_firma.get("typ") == "workday":
                treffer_liste, _ = scanne_workday_firma(api_firma, bekannte_urls, config)
            elif api_firma.get("typ") == "hr4you":
                treffer_liste, _ = scanne_hr4you_firma(api_firma, bekannte_urls, config)
            elif api_firma.get("typ") == "html_tabelle":
                treffer_liste, _ = scanne_html_tabelle_firma(api_firma, bekannte_urls, config)
            else:
                treffer_liste, _ = scanne_api_firma(api_firma, bekannte_urls, config)
        except Exception as e:
            print(f"\n❌ API-Fehler bei {api_firma['name']}: {e}")
            continue
        sammle(treffer_liste)

    # Playwright-Firmen
    if config["firmen"]:
        with sync_playwright() as p:
            browser = starte_browser(p)
            context = neuer_context(browser)
            for firma in config["firmen"]:
                page = neue_seite(context)
                try:
                    treffer_liste, _ = scanne_boerse(page, firma, strukturen, config)
                except SessionGesperrtFehler as e:
                    print(f"  🔄 HTTP {e.status} – frische Session, zweiter Versuch...")
                    page.close()
                    context.close()
                    context = neuer_context(browser)
                    page = neue_seite(context)
                    try:
                        treffer_liste, _ = scanne_boerse(page, firma, strukturen, config)
                    except Exception as e2:
                        print(f"\n❌ Fehler bei {firma['name']}: {e2}")
                        page.close()
                        continue
                except Exception as e:
                    print(f"\n❌ Fehler bei {firma['name']}: {e}")
                    page.close()
                    continue
                page.close()
                sammle(treffer_liste)
            browser.close()

    # Fahrzeit für Kandidaten mit bekanntem Ort direkt mitberechnen – bei der
    # breiten Suche ist die Entfernung das zentrale Entscheidungskriterium. Wird
    # direkt im Kandidaten gespeichert (kein DB-Cache, die Stelle ist noch nicht
    # in der DB). Kandidaten ohne Ort (Ort erst nach Extraktion bekannt, z.B.
    # Liebherr) bleiben ohne Fahrzeit.
    api_key    = config.get("google_maps_key", "")
    startpunkt = config.get("fahrzeit_startpunkt", "")
    firma_adressen = config.get("firma_adressen", {})
    mit_ort = [k for k in kandidaten
               if firma_adressen.get(k.get("firma", "")) or k.get("arbeitsort")]
    if mit_ort and api_key and startpunkt and api_key != "DEIN_GOOGLE_MAPS_API_KEY":
        try:
            from report import hole_fahrzeit_daten
            print(f"  🚗 Berechne Fahrzeit für {len(mit_ort)} Kandidat(en)...")
            for k in mit_ort:
                ziel = firma_adressen.get(k.get("firma", "")) or k.get("arbeitsort") or ""
                fz = hole_fahrzeit_daten(ziel, api_key, startpunkt)
                if fz:
                    k["fahrzeit"] = fz
        except Exception as e:
            print(f"  ⚠️  Fahrzeit-Berechnung übersprungen: {e}")

    speichere_json(VORSCHAU_JSON, kandidaten)

    print(f"\n{'='*60}")
    print(f"  FERTIG (Vorschau)")
    print(f"  Kandidaten (neu, DE): {len(kandidaten)}")
    print(f"  Geschrieben nach:     {VORSCHAU_JSON.name}")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Job-URLs entdecken (Schritt 1a)")
    parser.add_argument("--firma", default=None, help="Nur diese Firma scannen (Name)")
    parser.add_argument("--vorschau", action="store_true",
                        help="Breiter Vorschau-Scan (ganz Deutschland), schreibt nur "
                             "vorschau_kandidaten.json statt in die DB.")
    parser.add_argument("--vorschau-url", default=None,
                        help="Freie Karriere-URL für den breiten Vorschau-Scan "
                             "(statt der config-URL einer Firma).")
    parser.add_argument("--vorschau-name", default=None,
                        help="Firmenname für die Direkt-URL des breiten Vorschau-Scans.")
    args = parser.parse_args()
    nur_firma = args.firma.strip() if args.firma else None

    if args.vorschau:
        main_vorschau(nur_firma, args.vorschau_url, args.vorschau_name)
        return

    print("\n" + "=" * 60)
    print("  SCANNER  –  Schritt 1a: Job-URLs entdecken")
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

    bereinige_verbotene_standorte(stellen, bekannte, config["erlaubte_standorte"], config["verbotene_standorte"])
    bereinige_ausschlussbegriffe(stellen, bekannte, config["ausschlussbegriffe"])

    stellen_index: dict = {s["url"]: i for i, s in enumerate(stellen)}
    gesehen_urls:  set  = set()
    ts = jetzt()

    # ------------------------------------------------------------------
    # Hilfsfunktionen für DB-Zustand
    # ------------------------------------------------------------------

    def reaktiviere_oder_neu(t: dict, rohtext=None) -> bool:
        """Gibt True zurück wenn die Stelle neu angelegt wurde (für den Zähler)."""
        url = t["url"]
        gesehen_urls.add(url)
        idx = stellen_index.get(url)

        if idx is not None and t.get("arbeitsort") and not stellen[idx].get("arbeitsort"):
            stellen[idx]["arbeitsort"] = t["arbeitsort"]
            stellen[idx]["standort"] = berechne_standort(t["arbeitsort"], config["erlaubte_standorte"], config["verbotene_standorte"])

        if url in bekannte and bekannte[url]["status"] in (0, 9):
            # Stelle wurde als vergeben markiert, ist aber wieder in der Börse
            # gelistet (Wiederausschreibung) → reaktivieren. vergaben_bestaetigt
            # zurücksetzen, sonst markiert die Reparatur-Regel in
            # repariere_inkonsistente_status() sie sofort wieder als vergeben.
            bekannte[url]["vergaben_bestaetigt"] = False
            if idx is not None and stellen[idx].get("bewertung"):
                bekannte[url]["status"] = status_fuer_score(effektiver_score(stellen[idx]["bewertung"] or {}))
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
            return True

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
        return False

    # Übernommene (Standort-Ausnahme) und provisorische Vorschau-Stellen einmal
    # laden – sie dürfen wegen des Standorts NICHT wieder versteckt werden.
    standort_ignoriert = standort_ignoriert_urls()

    def markiere_nicht_passend(t: dict):
        url   = t["url"]
        gesehen_urls.add(url)
        idx   = stellen_index.get(url)
        grund = t.get("nicht_passend_grund", "")

        # Bewusst behaltene out-of-area-Stellen nicht erneut wegen des Standorts
        # als "nicht passend" markieren (der Nutzer kennt die Entfernung). Andere
        # Ausschlussgründe (z.B. Ausschlussbegriff) greifen weiterhin.
        if url in standort_ignoriert and (
                grund.startswith("Außerhalb Umkreis") or grund.startswith("Verbotener Standort")):
            return

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
            elif api_firma.get("typ") == "html_tabelle":
                treffer_liste, ausgeschlossen_liste = scanne_html_tabelle_firma(
                    api_firma, set(bekannte.keys()), config)
            else:
                treffer_liste, ausgeschlossen_liste = scanne_api_firma(
                    api_firma, set(bekannte.keys()), config)
        except Exception as e:
            print(f"\n❌ API-Fehler bei {api_firma['name']}: {e}")
            status_merken(api_firma["name"], False, str(e))
            continue

        # Erfolg/0-Jobs-Warnung wird bereits innerhalb der jeweiligen
        # scanne_*_firma-Funktion gesetzt (genauer als ein pauschales "True" hier).

        for t in ausgeschlossen_liste:
            if t["url"] not in gesehen_urls:
                markiere_nicht_passend(t)

        for t in treffer_liste:
            if t["url"] in gesehen_urls:
                continue
            rohtext = t.get("rohtext")
            if reaktiviere_oder_neu(t, rohtext):
                gesamt_neu += 1

    # ------------------------------------------------------------------
    # Playwright-Firmen (nur Link-Entdeckung, KEIN Rohtext laden)
    # ------------------------------------------------------------------
    with sync_playwright() as p:
        browser = starte_browser(p)
        context = neuer_context(browser)

        def scanne_mit_session_retry(firma: dict) -> tuple[list, list]:
            # WAF-Sperren (HTTP 403/429, z.B. jobs.advantest-career.de) hängen an
            # der Session: dieselben Cookies bleiben gesperrt, eine frische Session
            # kommt sofort wieder rein (wie ein neues Inkognito-Fenster). Daher bei
            # SessionGesperrtFehler den kompletten Context wegwerfen und einmal
            # mit neuen Cookies nachfassen.
            nonlocal context
            page = neue_seite(context)
            try:
                return scanne_boerse(page, firma, strukturen, config)
            except SessionGesperrtFehler as e:
                print(f"  🔄 HTTP {e.status} – frische Session, zweiter Versuch...")
                page.close()
                context.close()
                context = neuer_context(browser)
                page = neue_seite(context)
                return scanne_boerse(page, firma, strukturen, config)
            finally:
                page.close()

        for firma in config["firmen"]:
            # Pro Firma eine frische Seite: eine fehlgeschlagene Navigation (z.B.
            # ERR_CONNECTION_RESET) kann die Seite in einem Zustand hängen lassen,
            # in dem jede weitere Navigation mit "interrupted by another
            # navigation" abbricht – das würde sonst den kompletten Rest der
            # Firmenliste mitreißen.
            try:
                treffer_liste, ausgeschlossen_liste = scanne_mit_session_retry(firma)
            except Exception as e:
                print(f"\n❌ Fehler bei {firma['name']}: {e}")
                status_merken(firma["name"], False, str(e))
                continue

            for t in ausgeschlossen_liste:
                if t["url"] not in gesehen_urls:
                    markiere_nicht_passend(t)

            for t in treffer_liste:
                if t["url"] in gesehen_urls:
                    continue
                if reaktiviere_oder_neu(t):
                    gesamt_neu += 1

        browser.close()

    # ------------------------------------------------------------------
    # Strukturen speichern
    # ------------------------------------------------------------------
    speichere_json(STRUKTUREN_JSON, strukturen)

    # Mit vorhandenem Status mergen statt überschreiben, sonst gehen bei
    # --firma-gefilterten Läufen (z.B. Einzeltest) die Status aller anderen
    # Firmen verloren.
    gesamt_status = lade_json(SCAN_STATUS_JSON, {})
    gesamt_status.update(SCAN_STATUS)
    speichere_json(SCAN_STATUS_JSON, gesamt_status)

    gesamt_kein_treffer = lade_json(KEIN_TREFFER_JSON, {})
    gesamt_kein_treffer.update(KEIN_TREFFER)
    speichere_json(KEIN_TREFFER_JSON, gesamt_kein_treffer)

    # Zweiter Bereinigungslauf: erfasst Stellen, deren standort-Feld erst im
    # aktuellen Scan nachgetragen wurde und beim ersten Lauf noch fehlte.
    bereinige_verbotene_standorte(stellen, bekannte, config["erlaubte_standorte"], config["verbotene_standorte"])
    bereinige_ausschlussbegriffe(stellen, bekannte, config["ausschlussbegriffe"])

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
            s.get("arbeitsort", ""), config["erlaubte_standorte"], config["verbotene_standorte"])
        upsert_stelle({
            **s,
            "standort":            standort_wert,
            "status":              b.get("status", s.get("status", 1)),
            "nicht_passend":       b.get("nicht_passend", s.get("nicht_passend", False)),
            "geloescht_am":        b.get("geloescht_am") if b.get("geloescht_am") is not None else s.get("geloescht_am"),
            "vergaben_bestaetigt": b.get("vergaben_bestaetigt", False),
        })

    # URLs die in bekannte, aber nicht mehr in stellen sind (z.B. durch
    # bereinige_verbotene_standorte entfernt) → Status trotzdem in DB persistieren,
    # sonst geht die Änderung beim Prozessende verloren und wiederholt sich jeden Lauf.
    stellen_urls = {s["url"] for s in stellen}
    for url, b in bekannte.items():
        if url not in stellen_urls:
            upsert_stelle({"url": url, "firma": "", "titel": "",
                           "status": b.get("status", 1),
                           "nicht_passend": b.get("nicht_passend", False),
                           "nicht_passend_grund": b.get("nicht_passend_grund", ""),
                           "geloescht_am": b.get("geloescht_am"),
                           "vergaben_bestaetigt": b.get("vergaben_bestaetigt", False)})

    exportiere_stellen_json(STELLEN_JSON)
    exportiere_bekannte_json(BEKANNTE_JSON)

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Neue Stellen:   {gesamt_neu}")
    print(f"  Gesamt in DB:   {len(stellen)}")
    print(f"  Weiter mit:     python rohtext_holen.py")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
