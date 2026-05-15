"""
utils.py  –  Gemeinsame Hilfsfunktionen für den Job-Scanner
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

CONFIG_PFAD = Path(__file__).parent / "config.txt"


# =============================================================================
# DATUM / URL
# =============================================================================

def jetzt() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


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
    t = text[:3000].lower()
    return any(v in t for v in verbotene)


def berechne_standort(arbeitsort: str, verbotene: list) -> str:
    """Gibt 'ok', 'verboten' oder '' zurück basierend auf der Verbotsliste."""
    if not arbeitsort:
        return ""
    if standort_verboten(arbeitsort, verbotene):
        return "verboten"
    return "ok"


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
        "api_key":             "",
        "llm_bewertung":       True,
        "email_aktiv":         False,
        "email_absender":      "",
        "email_passwort":      "",
        "email_empfaenger":    "",
        "raspi_ip":            "",
        "google_maps_key":     "",
        "fahrzeit_startpunkt": "",
        "suchbegriffe":        [],
        "ausschlussbegriffe":  [],
        "verbotene_standorte": [],
        "firmen":              [],
        "firmen_reihenfolge":  [],
        "api_firmen":          [],
        "firma_adressen":      {},
        "prompt":              "",
        "steckbrief_prompt":   "",
        "anschreiben_prompt":  "",
    }

    zeilen = CONFIG_PFAD.read_text(encoding="utf-8").splitlines()
    aktiver_abschnitt = None
    puffer = []

    for zeile in zeilen:
        z = zeile.strip()

        if z.startswith("[\\") and z.endswith("]"):
            abschnitt = z[2:-1].lower()
            if abschnitt == "prompt":
                result["prompt"] = "\n".join(puffer).strip()
            elif abschnitt == "steckbrief_prompt":
                result["steckbrief_prompt"] = "\n".join(puffer).strip()
            elif abschnitt == "anschreiben_prompt":
                result["anschreiben_prompt"] = "\n".join(puffer).strip()
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

        if aktiver_abschnitt in ("prompt", "steckbrief_prompt", "anschreiben_prompt", "api_firmen"):
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
            elif z.upper().startswith("RASPI_IP"):
                result["raspi_ip"] = z.split("=", 1)[1].strip()
            elif z.upper().startswith("GOOGLE_MAPS_KEY"):
                result["google_maps_key"] = z.split("=", 1)[1].strip()
            elif z.upper().startswith("FAHRZEIT_STARTPUNKT"):
                result["fahrzeit_startpunkt"] = z.split("=", 1)[1].strip()
        elif aktiver_abschnitt == "suchbegriffe":
            result["suchbegriffe"].append(z.lower())
        elif aktiver_abschnitt == "ausschlussbegriffe":
            result["ausschlussbegriffe"].append(z.lower())
        elif aktiver_abschnitt == "verbotene_standorte":
            result["verbotene_standorte"].append(z.lower())
        elif aktiver_abschnitt == "firmen":
            if "|" in z:
                teile = z.split("|", 1)
                name = teile[0].strip()
                url = teile[1].strip()
                if name and url:
                    result["firmen"].append({"name": name, "url": url})
                    result["firmen_reihenfolge"].append(name)
        elif aktiver_abschnitt == "firma_anschreiben":
            if "|" in z:
                teile = [t.strip() for t in z.split("|")]
                if len(teile) >= 6:
                    result["firma_adressen"][teile[0]] = f"{teile[3]}, {teile[4]} {teile[5]}"

    return result
