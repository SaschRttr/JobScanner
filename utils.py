"""
utils.py  –  Gemeinsame Hilfsfunktionen für den Job-Scanner
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote

CONFIG_PFAD = Path(__file__).parent / "config.txt"
WHITELIST_PFAD = Path(__file__).parent / "whitelist_standorte.txt"


# =============================================================================
# DATUM / URL
# =============================================================================

def jetzt() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


def normalisiere_url(url: str) -> str:
    """Normalisiert eine URL für den Duplikat-Vergleich: dekodiert Prozent-Encoding
    (auch mehrfach kodiert, z.B. %2528 -> %28 -> '(') und entfernt trailing slash."""
    if not url:
        return url
    dekodiert = url
    for _ in range(3):
        neu = unquote(dekodiert)
        if neu == dekodiert:
            break
        dekodiert = neu
    return dekodiert.rstrip("/")


def sicherer_pfadname(text: str, max_len: int = 50) -> str:
    """Macht aus einem Titel/Firmennamen einen dateisystem-sicheren Ordnernamen."""
    bereinigt = re.sub(r'[^\w\s\-]', '', text).strip()
    bereinigt = re.sub(r'\s+', '_', bereinigt)
    return bereinigt[:max_len]


# =============================================================================
# JSON
# =============================================================================

def lade_json(pfad: Path, standard):
    if pfad.exists():
        try:
            return json.loads(pfad.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return standard


def speichere_json(pfad: Path, daten):
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")


# =============================================================================
# SUCHLOGIK
# =============================================================================

_UMLAUT_ERSATZ = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def normalisiere_ort(text: str) -> str:
    """Lowercase + Umlaute als Digraph (ü->ue, ö->oe, ä->ae, ß->ss), damit z.B.
    'Gehenbühl' und 'Gehenbuehl' beim Standort-Abgleich als gleich gelten."""
    t = text.lower()
    for umlaut, digraph in _UMLAUT_ERSATZ.items():
        t = t.replace(umlaut, digraph)
    return t


def text_matched(text: str, begriffe: list) -> list:
    t = text.lower()
    treffer = []
    for b in begriffe:
        if "+" in b:
            if all(teil in t for teil in b.split("+")):
                treffer.append(b)
        else:
            if b.lower() in t:
                treffer.append(b)
    return treffer


def ist_ausgeschlossen(titel: str, begriffe: list) -> bool:
    return bool(text_matched(titel, begriffe))


def standort_verboten(text: str, verbotene: list) -> bool:
    t = normalisiere_ort(text[:3000])
    return any(v in t for v in verbotene)


def standort_erlaubt(text: str, erlaubte: list) -> bool:
    """True wenn keine Whitelist konfiguriert ist, oder ein Whitelist-Ort im Text vorkommt.
    Vergleich in beide Richtungen, da manche Whitelist-Einträge amtliche Zusätze
    haben (z.B. 'Wendlingen am Neckar'), während erkannte Orte oft nur den
    Kurznamen enthalten (z.B. 'Wendlingen')."""
    if not erlaubte:
        return True
    t = normalisiere_ort(text[:3000])
    return any(o in t or t in o for o in erlaubte)


def berechne_standort(arbeitsort: str, erlaubte: list, verbotene: list) -> str:
    """Gibt 'ok', 'verboten' oder '' zurück. Reihenfolge: erst Whitelist (Umkreis), dann Blacklist."""
    if not arbeitsort:
        return ""
    if not standort_erlaubt(arbeitsort, erlaubte):
        return "verboten"
    if standort_verboten(arbeitsort, verbotene):
        return "verboten"
    return "ok"


def standort_ablehnungsgrund(arbeitsort: str, erlaubte: list, verbotene: list) -> str:
    """Liefert einen menschenlesbaren Ablehnungsgrund, oder '' wenn der Standort ok bzw. unbekannt ist."""
    if not arbeitsort:
        return ""
    if not standort_erlaubt(arbeitsort, erlaubte):
        return f"Außerhalb Umkreis: {arbeitsort}"
    if standort_verboten(arbeitsort, verbotene):
        return f"Verbotener Standort: {arbeitsort}"
    return ""


def ablehnungsgrund(titel: str, arbeitsort: str, config: dict) -> str:
    """Prüft Titel gegen Ausschlussbegriffe und Arbeitsort gegen White-/Blacklist.
    Gibt den Grund als Text zurück, oder '' wenn die Stelle passt."""
    if ist_ausgeschlossen(titel, config["ausschlussbegriffe"]):
        for b in config["ausschlussbegriffe"]:
            t = titel.lower()
            if (all(teil in t for teil in b.split("+")) if "+" in b else b in t):
                return f"Ausschlussbegriff: '{b}'"
    return standort_ablehnungsgrund(arbeitsort, config["erlaubte_standorte"], config["verbotene_standorte"])


# =============================================================================
# MARKER-ABSCHNITTE (---NAME--- ... ---/NAME---)
# =============================================================================

def extrahiere_abschnitt(text: str, marker: str) -> str | None:
    """Gibt den Inhalt zwischen ---MARKER--- und ---/MARKER--- zurück."""
    start = text.find(f"---{marker}---")
    ende  = text.find(f"---/{marker}---")
    if start == -1 or ende == -1:
        return None
    inhalt_start = text.find("\n", start) + 1
    return text[inhalt_start:ende].strip()


def ersetze_abschnitt(text: str, marker: str, neuer_inhalt: str) -> str:
    """Ersetzt den Inhalt zwischen ---MARKER--- und ---/MARKER---.
    Marker nicht gefunden → Text unverändert zurück."""
    start_marker = f"---{marker}---"
    ende_marker  = f"---/{marker}---"
    start = text.find(start_marker)
    ende  = text.find(ende_marker)
    if start == -1 or ende == -1:
        return text
    inhalt_start = text.find("\n", start) + 1
    return text[:inhalt_start] + neuer_inhalt + "\n" + text[ende:]


# =============================================================================
# COOKIE-BANNER (Playwright)
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


def klick_cookie_banner(page) -> bool:
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
# ZENTRALE CONFIG
# =============================================================================

def lade_config() -> dict:
    if not CONFIG_PFAD.exists():
        print(f"❌ config.txt nicht gefunden: {CONFIG_PFAD}")
        sys.exit(1)

    result = {
        "api_key":                "",
        "llm_bewertung":          True,
        "email_aktiv":            False,
        "email_absender":         "",
        "email_passwort":         "",
        "email_empfaenger":       "",
        "google_maps_key":        "",
        "fahrzeit_startpunkt":    "",
        "suchbegriffe":           [],
        "ausschlussbegriffe":     [],
        "verbotene_standorte":    [],
        "erlaubte_standorte":     [],
        "firmen":                 [],
        "api_firmen":             [],
        "firma_adressen":         {},
        "firma_anschreiben":      {},
        "firma_domains":          {},
        "prompt":                 "",
        "steckbrief_prompt":      "",
        "anschreiben_prompt":     "",
        "anschreiben_prompt_en":  "",
    }

    zeilen = CONFIG_PFAD.read_text(encoding="utf-8").splitlines()
    aktiver_abschnitt = None
    puffer = []

    for zeile in zeilen:
        z = zeile.strip()

        if z.startswith("[\\") and z.endswith("]"):
            abschnitt = z[2:-1].lower()
            if abschnitt in ("prompt", "steckbrief_prompt", "anschreiben_prompt", "anschreiben_prompt_en"):
                result[abschnitt] = "\n".join(puffer).strip()
            elif abschnitt == "api_firmen":
                try:
                    result["api_firmen"] = json.loads("\n".join(puffer))
                except Exception as e:
                    print(f"❌ Fehler beim Parsen von [api_firmen]: {e}")
            aktiver_abschnitt = None
            puffer = []
            continue

        if z.startswith("[") and z.endswith("]") and not z.startswith("[\\"):
            aktiver_abschnitt = z[1:-1].lower()
            puffer = []
            continue

        if aktiver_abschnitt in ("prompt", "steckbrief_prompt", "anschreiben_prompt",
                                 "anschreiben_prompt_en", "api_firmen"):
            puffer.append(zeile)
            continue

        if z.startswith("#") or not z:
            continue

        if aktiver_abschnitt is None:
            if z.upper().startswith("API_KEY"):
                result["api_key"] = z.split("=", 1)[1].strip()
            elif z.upper().startswith("LLM_BEWERTUNG"):
                result["llm_bewertung"] = z.split("=", 1)[1].strip().lower() == "true"
            elif z.upper().startswith("EMAIL_AKTIV"):
                result["email_aktiv"] = z.split("=", 1)[1].strip().lower() == "true"
            elif z.upper().startswith("EMAIL_ABSENDER"):
                result["email_absender"] = z.split("=", 1)[1].strip()
            elif z.upper().startswith("EMAIL_PASSWORT"):
                result["email_passwort"] = z.split("=", 1)[1].strip()
            elif z.upper().startswith("EMAIL_EMPFAENGER"):
                result["email_empfaenger"] = z.split("=", 1)[1].strip()
            elif z.upper().startswith("GOOGLE_MAPS_KEY"):
                result["google_maps_key"] = z.split("=", 1)[1].strip()
            elif z.upper().startswith("FAHRZEIT_STARTPUNKT"):
                result["fahrzeit_startpunkt"] = z.split("=", 1)[1].strip()
        elif aktiver_abschnitt == "suchbegriffe":
            result["suchbegriffe"].append(z.lower())
        elif aktiver_abschnitt == "ausschlussbegriffe":
            result["ausschlussbegriffe"].append(z.lower())
        elif aktiver_abschnitt == "verbotene_standorte":
            result["verbotene_standorte"].append(normalisiere_ort(z))
        elif aktiver_abschnitt == "firmen":
            if "|" in z:
                teile = z.split("|", 1)
                name = teile[0].strip()
                url = teile[1].strip()
                if name and url:
                    result["firmen"].append({"name": name, "url": url})
        elif aktiver_abschnitt == "firma_anschreiben":
            if "|" in z:
                teile = [t.strip() for t in z.split("|")]
                if len(teile) >= 6:
                    result["firma_adressen"][teile[0]] = f"{teile[3]}, {teile[4]} {teile[5]}"
                    result["firma_anschreiben"][teile[0]] = {
                        "firmenname":      teile[0],
                        "abteilung":       teile[1],
                        "ansprechpartner": teile[2],
                        "strasse":         teile[3],
                        "plz":             teile[4],
                        "ort":             teile[5],
                    }
        elif aktiver_abschnitt == "firma_domains":
            if "=" in z:
                firma, _, dom = z.partition("=")
                if firma.strip() and dom.strip():
                    result["firma_domains"][firma.strip()] = dom.strip().lower()

    if WHITELIST_PFAD.exists():
        for zeile in WHITELIST_PFAD.read_text(encoding="utf-8").splitlines():
            z = zeile.strip()
            if z and not z.startswith("#"):
                result["erlaubte_standorte"].append(normalisiere_ort(z))

    return result
