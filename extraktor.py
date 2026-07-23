"""
extraktor.py  –  Job-Scanner (Schritt 2)
=========================================
Extrahiert aus dem Rohtext den relevanten Stellentext.
Keine Bewertung – das macht bewertung.py.

Status-Übergänge:
  2 (Rohtext vorhanden) → 3 (Stellentext extrahiert)

Ablauf pro Stelle:
  1. Domain in strukturen.json bekannt? → Direkt mit Markern ausschneiden
  2. Unbekannt?                         → KI extrahiert + lernt Struktur

Nutzung:
  python extraktor.py
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import anthropic as anthropic_lib
except ImportError:
    print("anthropic nicht installiert: pip install anthropic")
    sys.exit(1)

from utils import lade_config, lade_json, speichere_json, jetzt, domain, berechne_standort, standort_aus_url


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD      = Path(__file__).parent
STELLEN_JSON    = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON   = BASIS_PFAD / "bekannte_stellen.json"
STRUKTUREN_JSON = BASIS_PFAD / "strukturen.json"
SCAN_STATUS_JSON = BASIS_PFAD / "scan_status.json"

# Erkennungssignatur für Einträge, die dieser Check selbst gesetzt hat –
# damit spätere Läufe nur ihre eigenen "ok"-Rücksetzungen vornehmen und
# keine von scanner.py gesetzten Fehlermeldungen überschreiben.
_LINK_MUSTER_FEHLER_MARKER = "Link-Muster evtl. falsch"

KI_MODELL = "claude-haiku-4-5-20251001"


# =============================================================================
# EXTRAKTION
# =============================================================================

_STANDORT_RE = re.compile(
    r'(?:standort|arbeitsort|location|arbeitsplatz)\s*[:\|]\s*([A-ZÄÖÜ][a-zäöüA-ZÄÖÜ\s\-]+?)(?:\n|,|<|\|)',
    re.IGNORECASE
)


def ki_extrahiere_standort(text: str, dom: str, client) -> str:
    """Extrahiert nur den Standort aus dem Stellentext – günstiger Fallback."""
    if not text:
        return ""

    # Schnell-Check per Regex — spart KI-Kosten für häufige Muster wie "Standort: Böblingen"
    m = _STANDORT_RE.search(text[:1500])
    if m:
        gefunden = m.group(1).strip()
        if 2 < len(gefunden) < 60:
            return gefunden

    # Anfang + Ende des Texts prüfen: Ort steht oft am Schluss (nach den Aufgaben)
    text_anfang = text[:3000]
    text_ende   = text[-2000:] if len(text) > 3000 else ""
    text_snippet = text_anfang + ("\n...\n" + text_ende if text_ende else "")

    prompt = f"""Aus dem folgenden Stellentext von '{dom}': Welche Stadt/Ort ist der Arbeitsort dieser Stelle?
Es handelt sich um eine Stellenanzeige eines deutschsprachigen oder europäischen Unternehmens – der Arbeitsort
liegt also so gut wie sicher in Deutschland, Österreich oder der Schweiz. Nimm bei mehrdeutigen Ortsnamen
(z.B. "Owen") IMMER den deutschen/DACH-Ort an, nicht eine gleichnamige Stadt in einem anderen Land
(z.B. NICHT "Owen Sound" in Kanada). Rate keinen Ort ins Blaue – wenn der Text keinen Hinweis liefert,
antworte UNBEKANNT statt zu raten.
Antworte NUR mit dem Ort oder der Adresse (z.B. "Böblingen" oder "Herrenberger Str. 130, 71034 Böblingen").
Wenn wirklich keine Stadt erkennbar ist, antworte exakt: UNBEKANNT

=== TEXT ===
{text_snippet}"""
    try:
        antwort = client.messages.create(
            model=KI_MODELL, max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        standort = antwort.content[0].text.strip().strip('"').strip("'")
        # Ablehnen wenn KI einen Erklärungssatz zurückgibt statt Ort/Adresse
        _SATZ_START = re.compile(
            r'^(?:der|die|das|kein|keine|leider|es |ich |im text|der text|der stellentext|in dem)',
            re.IGNORECASE
        )
        if standort.upper() == "UNBEKANNT" or _SATZ_START.match(standort) or len(standort) >= 100:
            return ""
        return standort
    except Exception:
        return ""


def extrahiere_mit_markern(rohtext: str, start_marker: str, ende_marker: str) -> tuple[str, bool]:
    """Schneidet rohtext[start_marker:ende_marker] aus. Gibt zusätzlich zurück,
    ob der start_marker tatsächlich gefunden wurde – wenn nicht, ist das
    Ergebnis der komplette unbereinigte rohtext (Cookie-Banner, Navigation,
    Footer inklusive), was der Aufrufer als Fehlschlag behandeln muss, statt
    ihn fälschlich als sauber extrahierten (nur zufällig langen) Stellentext
    zu übernehmen."""
    start = rohtext.find(start_marker)
    if start == -1:
        return rohtext, False
    ende = rohtext.find(ende_marker, start)
    if ende == -1:
        return rohtext[start:], True
    return rohtext[start:ende], True


def ki_extrahiere_und_lerne(rohtext: str, dom: str, client) -> tuple[str, dict | None]:
    rohtext_gekuerzt = rohtext[:8000]

    prompt = f"""Du bekommst den rohen Seitentext einer Stellenanzeige von '{dom}'.

Deine Aufgaben:
1. Extrahiere NUR den relevanten Stellentext (Aufgaben, Anforderungen, ggf. Angebot).
   Lass weg: Navigation, Footer, Cookie-Hinweise, "Ähnliche Stellen", Login-Bereiche.
2. Identifiziere einen eindeutigen START-Marker (erste Zeile des relevanten Teils)
   und einen ENDE-Marker (erste Zeile NACH dem relevanten Teil).
   START-Marker darf KEIN Jobtitel sein – wähle eine strukturelle Zeile wie "Deine Aufgaben", "Responsibilities", "Über uns" o.ä.
3. Extrahiere den Arbeitsstandort (Stadt/Ort) der Stelle. Nur die Stadt, kein Land.
   Der Arbeitsort liegt so gut wie sicher in Deutschland, Österreich oder der Schweiz. Nimm bei
   mehrdeutigen Ortsnamen (z.B. "Owen") IMMER den deutschen/DACH-Ort an, nicht eine gleichnamige
   Stadt in einem anderen Land (z.B. NICHT "Owen Sound" in Kanada). Rate keinen Ort ins Blaue –
   wenn der Text keinen Hinweis liefert, lass "arbeitsort" leer statt zu raten.

Antworte NUR als JSON ohne Markdown:
{{
  "stellentext": "der extrahierte Text hier",
  "arbeitsort": "Böblingen",
  "start_marker": "strukturelle Zeile (kein Jobtitel)",
  "ende_marker": "exakter Text der ersten nicht mehr relevanten Zeile"
}}

=== ROHTEXT ===
{rohtext_gekuerzt}"""

    try:
        antwort = client.messages.create(
            model=KI_MODELL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = antwort.content[0].text.strip()
        text = text.removeprefix("```json").removesuffix("```").strip()
        # Sonderzeichen im stellentext können JSON brechen – nur Marker extrahieren
        try:
            ergebnis = json.loads(text)
        except json.JSONDecodeError:
            start = re.search(r'"start_marker"\s*:\s*"(.*?)"(?=\s*,|\s*})', text, re.DOTALL)
            ende  = re.search(r'"ende_marker"\s*:\s*"(.*?)"(?=\s*,|\s*})', text, re.DOTALL)
            sttext = re.search(r'"stellentext"\s*:\s*"(.*?)"(?=\s*,\s*"(?:start|ende))', text, re.DOTALL)
            ergebnis = {
                "stellentext":  sttext.group(1).replace("\\n", "\n") if sttext else rohtext_gekuerzt[:5000],
                "start_marker": start.group(1) if start else None,
                "ende_marker":  ende.group(1)  if ende  else None,
            }

        stellentext = ergebnis.get("stellentext", rohtext_gekuerzt)
        start    = ergebnis.get("start_marker")
        ende     = ergebnis.get("ende_marker")
        standort = ergebnis.get("arbeitsort") or ""

        _JOBTITEL_MUSTER = re.compile(
            r'\(m/w/d\)|\(m/f/d\)|\(w/m/d\)|\(m/f/x\)|\(human\)|\(m/f\)|\(all genders\)',
            re.IGNORECASE
        )

        struktur = None
        if start and ende and len(start) > 5 and len(ende) > 5:
            if not _JOBTITEL_MUSTER.search(start):
                struktur = {
                    "start_marker": start,
                    "ende_marker":  ende,
                    "gelernt_am":   jetzt(),
                }

        return stellentext, standort, struktur

    except Exception as e:
        print(f"  ⚠️  KI-Extraktion fehlgeschlagen: {e}")
        return rohtext[:5000], "", None


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=None, help="Nur diese URL verarbeiten")
    parser.add_argument("--firma", default=None, help="Nur diese Firma verarbeiten")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  EXTRAKTOR  –  Schritt 2: Stellentext extrahieren")
    if args.url:
        print(f"  Filter: nur {args.url[:60]}")
    if args.firma:
        print(f"  Filter: nur Firma '{args.firma}'")
    print("=" * 60)

    config         = lade_config()
    erlaubte_orte  = config["erlaubte_standorte"]
    verbotene_orte = config["verbotene_standorte"]
    if not config["api_key"]:
        print("❌ Kein API-Key in config.txt")
        sys.exit(1)

    client      = anthropic_lib.Anthropic(api_key=config["api_key"])
    sys.path.insert(0, str(BASIS_PFAD))
    from db import lade_alle_stellen, upsert_stelle, exportiere_stellen_json, exportiere_bekannte_json, erstelle_schema
    erstelle_schema()
    stellen:    list = lade_alle_stellen()
    strukturen: dict = lade_json(STRUKTUREN_JSON, {})

    if not stellen:
        print("ℹ️  Keine Stellen in DB – zuerst scanner.py ausführen.")
        return

    zu_bearbeiten = [
        (i, s) for i, s in enumerate(stellen)
        if s.get("status") in (1, 2)
        and s.get("rohtext")
        and not s.get("nicht_passend")
        and (args.url is None or s["url"] == args.url)
        and (args.firma is None or s.get("firma") == args.firma)
    ]

    print(f"  {len(zu_bearbeiten)} Stellen zu bearbeiten (Status 2)")

    extrahiert = 0
    # Pro Firma: wie viele Stellen lieferten trotz vorhandenem Link keinen
    # brauchbaren Inhalt (Rohtext leer/zu kurz)? Wenn ausnahmslos alle
    # verarbeiteten Stellen einer Firma daran scheitern, deutet das auf ein
    # falsches Link-Muster hin (z.B. Kategorie- statt Job-Links wie bei
    # Advantest) – das meldet die bisherige "0 Treffer"-Prüfung in scanner.py
    # nicht, weil dort ja Links gefunden wurden.
    firma_extraktion_stats: dict = {}

    for idx, stelle in zu_bearbeiten:
        url     = stelle["url"]
        titel   = stelle["titel"]
        firma   = stelle["firma"]
        rohtext = stelle["rohtext"]
        dom     = domain(url)

        print(f"\n  {'─'*50}")
        print(f"  {firma}: {titel[:60]}")

        dom_struktur = strukturen.get(dom, {})
        start = dom_struktur.get("start_marker")
        ende  = dom_struktur.get("ende_marker")

        arbeitsort = stelle.get("arbeitsort") or ""

        if start and ende:
            print(f"  ✂️  Bekannte Marker – extrahiere direkt...")
            stellentext, marker_gefunden = extrahiere_mit_markern(rohtext, start, ende)
            if marker_gefunden:
                print(f"  ✅ {len(stellentext)} Zeichen extrahiert")
            if not marker_gefunden or len(stellentext) < 100:
                grund = "Start-Marker nicht gefunden" if not marker_gefunden else "Marker-Ergebnis zu kurz"
                print(f"  ⚠️  {grund} – KI extrahiert als Fallback...")
                strukturen.pop(dom, None)
                stellentext, arbeitsort_neu, neue_struktur = ki_extrahiere_und_lerne(rohtext, dom, client)
                if arbeitsort_neu and not arbeitsort:
                    arbeitsort = arbeitsort_neu
                print(f"  ✅ {len(stellentext)} Zeichen (KI-Fallback) extrahiert")
                if arbeitsort:
                    print(f"  📍 Arbeitsort: {arbeitsort}")
                if neue_struktur:
                    strukturen[dom] = neue_struktur
                    print(f"  💾 Struktur für '{dom}' aktualisiert")
            elif not arbeitsort:
                arbeitsort = ki_extrahiere_standort(stellentext, dom, client)
                if not arbeitsort:
                    arbeitsort = ki_extrahiere_standort(rohtext, dom, client)
                if arbeitsort:
                    print(f"  📍 Arbeitsort: {arbeitsort}")
        else:
            print(f"  🤖 Unbekannte Struktur – KI extrahiert...")
            stellentext, arbeitsort_neu, neue_struktur = ki_extrahiere_und_lerne(rohtext, dom, client)
            if arbeitsort_neu:
                arbeitsort = arbeitsort_neu
            print(f"  ✅ {len(stellentext)} Zeichen extrahiert")
            if arbeitsort:
                print(f"  📍 Arbeitsort: {arbeitsort}")
            if neue_struktur:
                strukturen.setdefault(dom, {}).update(neue_struktur)
                print(f"  💾 Struktur für '{dom}' gelernt")

        if not arbeitsort:
            arbeitsort = standort_aus_url(url)
            if arbeitsort:
                print(f"  📍 Arbeitsort (aus URL): {arbeitsort}")

        stellen[idx]["arbeitsort"] = arbeitsort or ""
        stellen[idx]["standort"] = berechne_standort(arbeitsort, erlaubte_orte, verbotene_orte)
        if not arbeitsort:
            print(f"  ⚠️  Kein Standort erkannt – muss manuell nachgetragen werden: {firma} – {titel[:60]} ({url})")
        else:
            try:
                from report import aktualisiere_fahrzeit_fuer_stelle
                aktualisiere_fahrzeit_fuer_stelle(url, firma, arbeitsort, config)
            except Exception as e:
                print(f"  ⚠️  Fahrzeit-Berechnung fehlgeschlagen: {e}")

        firma_stat = firma_extraktion_stats.setdefault(firma, {"verarbeitet": 0, "fehlgeschlagen": 0})
        firma_stat["verarbeitet"] += 1

        if stellentext and len(stellentext) > 100:
            stellen[idx]["stellentext"] = stellentext
            stellen[idx]["status"] = 3
            extrahiert += 1
            upsert_stelle({"url": url, "stellentext": stellentext,
                           "arbeitsort": stellen[idx]["arbeitsort"],
                           "standort":   stellen[idx]["standort"],
                           "status": 3})
        elif rohtext and len(rohtext) > 100:
            print(f"  ⚠️  Extraktion fehlgeschlagen – verwende Rohtext als Fallback")
            stellen[idx]["stellentext"] = rohtext[:8000]
            stellen[idx]["status"] = 3
            extrahiert += 1
            upsert_stelle({"url": url, "stellentext": rohtext[:8000],
                           "arbeitsort": stellen[idx]["arbeitsort"],
                           "standort":   stellen[idx]["standort"],
                           "status": 3})
        else:
            firma_stat["fehlgeschlagen"] += 1
            print(f"  ⚠️  Rohtext zu kurz oder leer – Status auf 1 zurückgesetzt (scanner.py lädt neu)")
            stellen[idx]["rohtext"] = None
            stellen[idx]["status"] = 1
            upsert_stelle({"url": url, "rohtext": None, "status": 1})

        # Gelernte Struktur sofort sichern (kleine Datei, wertvoller Lernfortschritt);
        # die großen JSON-Spiegel erst am Ende – die DB ist pro Stelle schon aktuell.
        speichere_json(STRUKTUREN_JSON, strukturen)

    if zu_bearbeiten:
        exportiere_stellen_json(STELLEN_JSON)
        exportiere_bekannte_json(BEKANNTE_JSON)

    if firma_extraktion_stats:
        scan_status = lade_json(SCAN_STATUS_JSON, {})
        for name, stat in firma_extraktion_stats.items():
            bisher = scan_status.get(name, {})
            war_link_muster_fehler = _LINK_MUSTER_FEHLER_MARKER in (bisher.get("fehler") or "")
            if stat["fehlgeschlagen"] == stat["verarbeitet"]:
                scan_status[name] = {
                    "ok": False,
                    "fehler": (f"{stat['fehlgeschlagen']}/{stat['verarbeitet']} Job-Links liefern keinen "
                               f"Inhalt (Rohtext leer/zu kurz) – {_LINK_MUSTER_FEHLER_MARKER}"),
                    "zeitpunkt": jetzt(),
                }
                print(f"  ⚠️  {name}: alle verarbeiteten Links ohne Inhalt – als Scan-Problem vermerkt")
            elif war_link_muster_fehler:
                # War beim letzten Lauf komplett fehlgeschlagen, diesmal kam wieder
                # Inhalt durch – eigene Markierung zurücksetzen (Fehler von scanner.py
                # bleiben unangetastet, die betreffen einen anderen Lauf/Schritt).
                scan_status[name] = {"ok": True, "fehler": None, "zeitpunkt": jetzt()}
        speichere_json(SCAN_STATUS_JSON, scan_status)

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Texte extrahiert:       {extrahiert}")
    print(f"  Weiter mit:             python bewertung.py")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
