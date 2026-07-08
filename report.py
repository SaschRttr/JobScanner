"""
report.py  –  Job-Scanner (Schritt 4)
=======================================
Erstellt einen HTML-Report aus stellen.json und sendet optional
eine Änderungs-Mail (nur neue + weggefallene Stellen) per iCloud.

Nutzung:
  python report.py

E-Mail wird nur gesendet wenn:
  - EMAIL_AKTIV = true in config.txt
  - Es neue oder weggefallene Stellen gibt
"""
# -*- coding: utf-8 -*-
import html as _html
import json
import re
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent))

from utils import (lade_config, lade_json, ist_ausgeschlossen, text_matched,
                   standort_ablehnungsgrund, sicherer_pfadname)
from status_def import (STATUS_LABELS, STATUS_EMOJIS, INAKTIVE_STATUSWERTE,
                        UNBEWERTETE_STATUSWERTE, FILTER_STATUS_VALS)


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD      = Path(__file__).parent
STELLEN_JSON    = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON   = BASIS_PFAD / "bekannte_stellen.json"
REPORT_PFAD     = BASIS_PFAD / "report.html"
BEWERBUNGEN_DIR = BASIS_PFAD / "bewerbungen"
STATUS_JSON     = BASIS_PFAD / "status.json"
SCAN_STATUS_JSON = BASIS_PFAD / "scan_status.json"


def _scan_status_html() -> str:
    """Zeigt ganz oben im Report, ob beim letzten scanner.py-Lauf alle Firmen
    erfolgreich gescannt wurden – sonst welche Firma mit welchem Fehler
    hängengeblieben ist. Spart das Durchsuchen des Logfiles."""
    status = lade_json(SCAN_STATUS_JSON, {})
    if not status:
        return ""

    fehler = {name: info for name, info in status.items() if not info.get("ok")}
    letzter_stand = max((info.get("zeitpunkt", "") for info in status.values()), default="")

    if not fehler:
        return (
            f'<div class="summary-box" style="background:#eafaf1; border-left:4px solid #27ae60; color:#1e3a2f;">'
            f'✅ Alle {len(status)} Firmen beim letzten Scan erreichbar (Stand: {letzter_stand})'
            f'</div>\n'
        )

    zeilen = "".join(
        f'<li><b>{name}</b> – {info.get("fehler", "unbekannter Fehler")} '
        f'<span style="color:#666; font-size:0.85em;">({info.get("zeitpunkt", "")})</span></li>\n'
        for name, info in sorted(fehler.items())
    )
    return (
        f'<div class="summary-box" style="background:#fdecea; border-left:4px solid #e74c3c; color:#5c1c17;">'
        f'⚠️ {len(fehler)} von {len(status)} Firmen beim letzten Scan mit Problem (Stand: {letzter_stand}):'
        f'<ul style="margin:6px 0 0 0;">{zeilen}</ul>'
        f'</div>\n'
    )





# =============================================================================
# FAHRZEIT (Google Distance Matrix API)
# =============================================================================

def hole_fahrzeit_daten(ziel: str, api_key: str, startpunkt: str) -> dict | None:
    """Fragt Google Distance Matrix API ab (driving + transit). Kein Caching — nur reiner API-Call."""
    if not api_key or not ziel or not startpunkt or api_key == "DEIN_GOOGLE_MAPS_API_KEY":
        return None

    def _anfrage(mode: str) -> dict | None:
        params = urllib.parse.urlencode({
            "origins":      startpunkt,
            "destinations": ziel,
            "mode":         mode,
            "language":     "de",
            "key":          api_key,
        })
        api_url = f"https://maps.googleapis.com/maps/api/distancematrix/json?{params}"
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": "JobScanner/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            rows = data.get("rows", [])
            if not rows:
                return None
            el = rows[0]["elements"][0]
            if el.get("status") == "OK":
                return {
                    "min": el["duration"]["value"] // 60,
                    "km":  round(el["distance"]["value"] / 1000, 1),
                }
        except Exception:
            pass
        return None

    auto    = _anfrage("driving")
    transit = _anfrage("transit")
    if auto is None and transit is None:
        return None
    return {
        "auto_min":    auto["min"]    if auto    else None,
        "auto_km":     auto["km"]     if auto    else None,
        "transit_min": transit["min"] if transit else None,
    }


# =============================================================================
# HTML-BAUSTEINE
# =============================================================================

def stelle_zu_html(s: dict, zeige_firma: bool = False, fahrzeit: dict | None = None, geringer_match: bool = False, scanner_status: int | None = None, zu_weit: bool = False) -> str:
    import html as _html
    ist_neu       = s.get("neu", False)
    ist_geloescht = s.get("geloescht_am") is not None
    vergabe_st    = s.get("vergabe_status", 0)

    tags = "".join(f'<span class="tag">{t}</span>' for t in s.get("treffer", []))
    neu_badge = '<span class="badge badge-neu">NEU</span>' if ist_neu else ""

    np_grund = s.get("nicht_passend_grund") or ""
    np_grund_html = (
        f'<div class="np-grund-label" title="{_html.escape(np_grund)}">'
        f'🚫 <strong>Grund:</strong> {_html.escape(np_grund)}</div>'
    ) if np_grund else ""

    if scanner_status is not None:
        # Scanner-Status ist die einzige Wahrheit
        label = STATUS_LABELS.get(scanner_status, str(scanner_status))
        status_badge = f'<span class="scanner-status scanner-status-{scanner_status}" title="Status {scanner_status}">{label}</span>'
        geloescht_badge = ""
        if scanner_status in (0, 9):
            css = "stelle stelle-geloescht"
        elif ist_neu:
            css = "stelle stelle-neu"
        else:
            css = "stelle"
    else:
        status_badge = ""
        if ist_geloescht:
            css = "stelle stelle-geloescht"
        elif ist_neu:
            css = "stelle stelle-neu"
        else:
            css = "stelle"
        if vergabe_st == 6:
            geloescht_badge = '<span class="badge badge-weg">BEWERBUNG VERGABEN</span>'
        elif vergabe_st == 7:
            geloescht_badge = '<span class="badge badge-weg" style="background:#c0392b;">ABSAGE</span>'
        elif ist_geloescht:
            geloescht_badge = '<span class="badge badge-weg">VERGEBEN</span>'
        else:
            geloescht_badge = ""
    # url_attr: für HTML-Attribute (href, data-url). url_js: für JS-String-
    # Literale in onclick/onchange/onblur. Beide escapen die gescrapte URL,
    # damit Titel/URL von Fremdseiten keinen HTML/JS-Ausbruch (XSS) erlauben.
    url_attr       = _html.escape(s["url"], quote=True)
    url_js         = _html.escape(s["url"].replace("\\", "\\\\").replace("'", "\\'"), quote=True)
    firma_escaped  = _html.escape(s.get("firma") or "", quote=True)
    arbeitsort = s.get("arbeitsort") or ""
    _ort_text  = _html.escape(arbeitsort) if arbeitsort else "kein Standort"
    _ort_farbe = "#888" if arbeitsort else "#e67e22"
    standort_label = (
        f' <span class="standort-label" data-url="{url_attr}" style="cursor:pointer; color:{_ort_farbe};" '
        f'title="Klicken zum Bearbeiten" onclick="standortBearbeiten(this)">📍 {_ort_text} ✏️</span>'
    )
    firma_label    = f'<span class="firma-label"> — {firma_escaped}</span>' if zeige_firma else f'<span class="firma-label-auto"> — {firma_escaped}</span>'
    gefunden_am    = (s.get("gefunden_am") or "")[:10]
    geloescht_am   = s.get("geloescht_am") or ""
    datum_label    = f'<span style="color:#aaa; font-size:0.8em; margin-left:8px;">📅 gefunden: {gefunden_am}'
    if geloescht_am:
        if vergabe_st == 6:
            datum_label += f' &nbsp;|&nbsp; 📭 Bewerbung vergaben: {geloescht_am[:10]}'
        elif vergabe_st == 7:
            datum_label += f' &nbsp;|&nbsp; ❌ Absage: {geloescht_am[:10]}'
        else:
            datum_label += f' &nbsp;|&nbsp; 🗑️ vergeben: {geloescht_am[:10]}'
    datum_label += '</span>'

    # Fahrzeit-Info
    if fahrzeit is None:
        fahrzeit_html = ""
    elif fahrzeit.get("kein_ziel"):
        fahrzeit_html = '<div class="fahrzeit-info fahrzeit-unbekannt">📍 Adresse/Standort unbekannt</div>'
    else:
        auto_min    = fahrzeit.get("auto_min")
        auto_km     = fahrzeit.get("auto_km")
        transit_min = fahrzeit.get("transit_min")
        genau       = fahrzeit.get("genau", True)
        teile = []
        if auto_min is not None:
            km_text = f" ({auto_km} km)" if auto_km is not None else ""
            teile.append(f'🚗 {auto_min} min{km_text}')
        if transit_min is not None:
            teile.append(f'🚌 {transit_min} min')
        if teile:
            ca_prefix = "ca. " if not genau else ""
            ziel_text = fahrzeit.get("ziel", "")
            maps_url  = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(ziel_text)}"
            maps_link = f' <a href="{maps_url}" target="_blank" class="fahrzeit-maps">📌</a>'
            ziel_hinweis = f' <span class="fahrzeit-hinweis">({ziel_text})</span>' if not genau else ""
            fahrzeit_html = f'<div class="fahrzeit-info">{ca_prefix}{" &nbsp;&nbsp; ".join(teile)}{ziel_hinweis}{maps_link}</div>'
        else:
            fahrzeit_html = ""

    # KI-Bewertung
    bewertung_html = ""
    if s.get("bewertung"):
        b     = s["bewertung"]
        score    = b.get("score", 0)
        score_na = b.get("score_nach_anpassung")
        empf  = b.get("empfehlung", "?")
        farbe       = "#27ae60" if score >= 70 else "#f39c12" if score >= 40 else "#e74c3c"
        farbe_na    = "#27ae60" if (score_na or 0) >= 70 else "#f39c12" if (score_na or 0) >= 40 else "#e74c3c"
        empf_farbe  = "#27ae60" if empf == "bewerben" else "#e74c3c"
        staerken    = "".join(f"<li>{p}</li>" for p in b.get("staerken", []))
        luecken     = "".join(f"<li>{p}</li>" for p in b.get("luecken", []))
        anpassungen = "".join(f"<li>{p}</li>" for p in b.get("lebenslauf_anpassungen", []))
        begruendung = b.get("score_begruendung", "")
        score_na_html = (
            f' → <strong style="color:{farbe_na};">{score_na}%</strong>'
            f'<span style="color:#999;font-size:0.85em;"> nach Anpassung</span>'
        ) if score_na and score_na > score else ""
        bewertung_html = f"""
        <div class="bewertung">
            <strong style="color:{farbe};">Score: {score}%</strong>{score_na_html}
            &nbsp;|&nbsp;
            <strong style="color:{empf_farbe};">{empf.upper()}</strong>
            <details><summary>Details anzeigen</summary>
                <p><strong>Stärken:</strong></p><ul>{staerken}</ul>
                <p><strong>Lücken:</strong></p><ul>{luecken}</ul>
                <p><strong>Lebenslauf-Anpassungen:</strong></p><ul>{anpassungen}</ul>
                <p class="begruendung">📊 {begruendung}</p>
            </details>
        </div>"""

    # Notizen (localStorage)
    notizen_html = f"""
        <details class="notizen">
            <summary>📝 Notizen</summary>
            <div style="margin-top:6px;">
                <div style="margin-bottom:6px;">
                    <select class="stufen-select"
                        onchange="speichern('{url_js}', 'stufe', this.value)">
                        <option value="">— kein Status —</option>
                        <option value="beworben">✅ Beworben</option>
                        <option value="kennenlernen">📞 Kennenlerngespräch</option>
                        <option value="einladung">📅 Gesprächseinladung</option>
                        <option value="zusage">🎉 Zusage</option>
                        <option value="absage">❌ Absage</option>
                    </select>
                    <span class="stufen-ts"></span>
                </div>
                <textarea class="kommentar" rows="2"
                    placeholder="Notizen..."
                    onblur="speichern('{url_js}', 'kommentar', this.value)"></textarea>
                <textarea class="nicht-beworben-grund" rows="2"
                    placeholder="Nicht beworben weil..."
                    onblur="speichern('{url_js}', 'nicht_beworben_grund', this.value)"></textarea>
            </div>
        </details>"""

    # Bewerbungsunterlagen: Checkbox + Download-Links
    firma_safe = sicherer_pfadname(s["firma"])
    titel_safe = sicherer_pfadname(s["titel"])
    score      = (s.get("bewertung") or {}).get("score", 0)

    stelle_dir = BEWERBUNGEN_DIR / firma_safe / titel_safe
    lv_docx = Path(s["lebenslauf_pfad"]) if s.get("lebenslauf_pfad") else None
    as_docx = Path(s["anschreiben_pfad"]) if s.get("anschreiben_pfad") else None
    # Fallback: falls Pfade nicht in stellen.json, neueste Datei im Ordner suchen
    if lv_docx is None or not lv_docx.exists():
        treffer = sorted(stelle_dir.glob("Lebenslauf*.docx")) if stelle_dir.exists() else []
        lv_docx = treffer[-1] if treffer else None
    if as_docx is None or not as_docx.exists():
        treffer = sorted(stelle_dir.glob("Anschreiben*.docx")) if stelle_dir.exists() else []
        as_docx = treffer[-1] if treffer else None

    if lv_docx or as_docx:
        # Mindestens eine DOCX vorhanden → Download-Link(s) immer anzeigen,
        # unabhängig vom aktuellen Score (der kann sich durch Neubewertung
        # später ändern, bereits erstellte Unterlagen sollen deswegen nicht
        # verschwinden) und unabhängig davon, ob beide Dateien existieren
        # (Anschreiben-Generierung kann fehlschlagen, ohne den Lebenslauf-
        # Link zu blockieren).
        css += " stelle-bewerbung"
        links = []
        if lv_docx:
            lv_dl = f"/download?pfad={urllib.parse.quote(str(lv_docx))}"
            links.append(f'📄 <a href="{lv_dl}" style="color:#27ae60; margin-right:12px;">Lebenslauf.docx</a>')
        if as_docx:
            as_dl = f"/download?pfad={urllib.parse.quote(str(as_docx))}"
            links.append(f'✉️ <a href="{as_dl}" style="color:#27ae60;">Anschreiben.docx</a>')
        lebenslauf_html = f"""
        <div style="margin-top:8px; padding:8px; background:#eafaf1; border-radius:4px; font-size:0.85em;">
            {''.join(links)}
        </div>"""
    else:
        # Noch nicht erstellt → Checkbox anzeigen (für jede Stelle, unabhängig vom Score)
        lebenslauf_html = f"""
        <div style="margin-top:8px; font-size:0.85em;" id="bew-box-{firma_safe}-{titel_safe}">
            <label style="cursor:pointer;">
                <input type="checkbox"
                    onchange="bewerbungErstellen(this, '{url_js}', '{firma_safe}', '{titel_safe}')">
                &nbsp;📋 Lebenslauf &amp; Anschreiben erstellen
            </label>
            <span id="bew-status-{firma_safe}-{titel_safe}" style="margin-left:8px; color:#888;"></span>
        </div>"""
    # Stellentext-Block (aufklappbar)
    stellentext = s.get("stellentext") or s.get("rohtext") or ""
    if stellentext:
        st_escaped = _html.escape(stellentext[:4000]).replace("\n", "<br>")
        stellentext_html = f"""
        <details class="stellentext-details">
            <summary>📄 Stellentext anzeigen</summary>
            <div class="stellentext-inhalt">{st_escaped}</div>
        </details>"""
    else:
        stellentext_html = ""

    # Steckbrief-Block
    steckbrief = s.get("steckbrief")
    if steckbrief:
        fragen_html = ""
        for fq in steckbrief.get("interview_fragen", []):
            fragen_html += f"""
            <details style="margin:4px 0;">
                <summary style="cursor:pointer;">{_html.escape(str(fq.get('frage', '')))}</summary>
                <p style="margin:6px 0 0 16px; color:#444;">{_html.escape(str(fq.get('antwort', '')))}</p>
            </details>"""
        steckbrief_html = f"""
        <details class="steckbrief-details">
            <summary>🧠 Steckbrief anzeigen</summary>
            <div class="steckbrief-inhalt">
                <p><strong>Firma:</strong> {_html.escape(str(steckbrief.get('firma_beschreibung', '')))}</p>
                <p><strong>Warum ich passe:</strong> {_html.escape(str(steckbrief.get('warum_ich_passe', '')))}</p>
                <p><strong>Interview-Fragen:</strong></p>
                {fragen_html}
            </div>
        </details>"""
    else:
        steckbrief_html = ""

    steckbrief_btn  = f'<button class="steckbrief-btn" onclick="steckbriefGenerieren(this, \'{url_js}\')">🧠 Steckbrief generieren</button>'
    bewertung_btn   = f'<button class="steckbrief-btn" onclick="bewertungStarten(this, \'{url_js}\')">⭐ Bewertung starten</button>' if (s.get("stellentext") or s.get("rohtext")) and not s.get("bewertung") else ""
    neu_laden_btn   = f'<button class="steckbrief-btn" onclick="neuLadenUndBewerten(this, \'{url_js}\')">🔄 Neu laden &amp; bewerten</button>' if not s.get("stellentext") and not s.get("rohtext") and not s.get("bewertung") else ""
    vormerken_badge = '<span class="pruef-vormerken-badge">⏳ Verfügbarkeit unsicher – beim nächsten Lauf bestätigt</span>' if s.get("pruef_vormerken") else ""
    pruef_btn           = f'<button class="pruef-btn" onclick="stellePruefen(this, \'{url_js}\')">🔍 Neu prüfen</button><span class="pruef-ergebnis"></span>'
    nicht_beworben_btn  = f'<button class="pruef-btn" style="background:#f9ebea;border-color:#c0392b;color:#c0392b;" onclick="nichtBeworben(this, \'{url_js}\')">🚫 Nicht beworben</button>'
    if scanner_status == 4:
        passend_btn = f'<button class="pruef-btn passend-toggle" style="background:#f9ebea;border-color:#c0392b;color:#c0392b;" onclick="passendSetzen(this, \'{url_js}\', false)">👎 Nicht passend</button>'
    elif scanner_status == 5:
        passend_btn = f'<button class="pruef-btn passend-toggle" style="background:#eafaf1;border-color:#27ae60;color:#27ae60;" onclick="passendSetzen(this, \'{url_js}\', true)">📋 Passend – bewerben</button>'
    else:
        passend_btn = ""
    vergeben_btn        = "" if ist_geloescht else f'<button class="pruef-btn" style="background:#f9ebea;border-color:#c0392b;color:#c0392b;" onclick="vergebenMarkieren(this, \'{url_js}\')">🗑️ Als vergeben markieren</button>'

    hat_lv = "1" if (lv_docx and lv_docx.exists()) else "0"
    _auto_min_attr = ""
    _transit_min_attr = ""
    if fahrzeit and not fahrzeit.get("kein_ziel"):
        if fahrzeit.get("auto_min") is not None:
            _auto_min_attr = str(fahrzeit["auto_min"])
        if fahrzeit.get("transit_min") is not None:
            _transit_min_attr = str(fahrzeit["transit_min"])

    _gm_attr = ' data-geringer-match="1"' if geringer_match else ''
    _zw_attr = ' data-zu-weit="1"' if zu_weit else ''
    _vm_attr = ' data-vorgemerkt="1"' if s.get("pruef_vormerken") else ''
    if zu_weit:
        css += " stelle-zu-weit"
    _scanner_status_attr = str(scanner_status) if scanner_status is not None else ""
    zu_weit_badge = '<span class="badge badge-zu-weit">ZU WEIT</span>' if zu_weit else ""
    return f"""<div class="{css}" data-url="{url_attr}" data-firma="{firma_escaped}" data-hat-lebenslauf="{hat_lv}" data-score="{score}" data-auto-min="{_auto_min_attr}" data-transit-min="{_transit_min_attr}"{_gm_attr}{_zw_attr}{_vm_attr} data-scanner-status="{_scanner_status_attr}">
    <a href="{url_attr}" target="_blank">{_html.escape(s['titel'])}</a>{neu_badge}{geloescht_badge}{status_badge}{zu_weit_badge}{firma_label}{standort_label}{datum_label}
    {vormerken_badge}
    {np_grund_html}{fahrzeit_html}<div class="tags">{tags}</div>
    {bewertung_html}
    {stellentext_html}
    {steckbrief_html}
    {steckbrief_btn}
    {bewertung_btn}
    {neu_laden_btn}
    {pruef_btn}
    {passend_btn}
    {nicht_beworben_btn}
    {vergeben_btn}
    {notizen_html}
    {lebenslauf_html}
</div>
"""


# CSS/JS liegen als eigene Dateien in report_assets/ (Syntax-Highlighting,
# kleinere report.py). Status-Konstanten injiziert erstelle_report() ins HTML.
ASSETS_DIR = BASIS_PFAD / "report_assets"
CSS = (ASSETS_DIR / "report.css").read_text(encoding="utf-8")
JS  = (ASSETS_DIR / "report.js").read_text(encoding="utf-8")


# =============================================================================
# REPORT ERSTELLEN
# =============================================================================

GERINGER_MATCH_SCHWELLE = 65
FAHRZEIT_MAX_AUTO_MIN   = 60

def _hat_geringen_score(s: dict) -> bool:
    b = s.get("bewertung")
    if not b:
        return False
    if (b.get("score_nach_anpassung") or 0) > GERINGER_MATCH_SCHWELLE:
        return False
    return (b.get("score") or 0) <= GERINGER_MATCH_SCHWELLE


def erstelle_report(stellen: list, config: dict | None = None) -> str:
    datum = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Fahrzeit-Daten vorberechnen (cached in DB, max 1 API-Call pro Zieladresse)
    api_key    = (config or {}).get("google_maps_key", "")
    startpunkt = (config or {}).get("fahrzeit_startpunkt", "")
    firma_adressen = (config or {}).get("firma_adressen", {})
    fahrzeit_daten: dict = {}  # url → fahrzeit-dict
    bekannte_status: dict = {s["url"]: s.get("status") for s in stellen}

    def _st(s: dict) -> int | None:
        return bekannte_status.get(s["url"])

    if api_key and startpunkt and api_key != "DEIN_GOOGLE_MAPS_API_KEY":
        try:
            from db import hole_fahrzeit_cache, speichere_fahrzeit_cache
            db_ok = True
        except Exception:
            db_ok = False

        relevante = [s for s in stellen if not s.get("nicht_passend")]
        _api_cache: dict = {}  # ziel → API-Ergebnis (verhindert doppelte API-Calls)

        for s in relevante:
            url      = s["url"]
            firma    = s.get("firma", "")
            arbeitsort = s.get("arbeitsort") or ""
            adresse = firma_adressen.get(firma)
            ziel    = adresse or arbeitsort or None
            genau   = adresse is not None

            if not ziel:
                fahrzeit_daten[url] = {"kein_ziel": True}
                continue

            # DB-Cache per URL prüfen
            if db_ok:
                cached = hole_fahrzeit_cache(url)
                if cached is not None:
                    fahrzeit_daten[url] = cached
                    continue

            # API-Call (Session-Cache vermeidet Doppel-Requests für gleiche Zieladresse)
            if ziel not in _api_cache:
                _api_cache[ziel] = hole_fahrzeit_daten(ziel, api_key, startpunkt)
            daten = _api_cache[ziel]

            eintrag = {
                "ziel":        ziel,
                "genau":       genau,
                "auto_min":    daten["auto_min"]    if daten else None,
                "auto_km":     daten["auto_km"]     if daten else None,
                "transit_min": daten["transit_min"] if daten else None,
            }
            if db_ok:
                speichere_fahrzeit_cache(url, eintrag)
            if daten:
                fahrzeit_daten[url] = eintrag

    # Fahrzeit-Filter nur anwenden wenn keine Whitelist aktiv ist: ein Ort auf der
    # Whitelist ist explizit gewollt (z.B. Apple München trotz >60min), die
    # Whitelist soll den Fahrzeit-Filter nicht nur ergänzen, sondern übersteuern.
    zu_weit_urls = set() if (config or {}).get("erlaubte_standorte") else {
        url for url, fz in fahrzeit_daten.items()
        if not fz.get("kein_ziel") and (fz.get("auto_min") or 0) > FAHRZEIT_MAX_AUTO_MIN
    }

    # Bewerbungsstatus aus Datenbank laden
    job_status = {}
    try:
        from db import verbindung as _db_verbindung
        with _db_verbindung() as _con:
            for _r in _con.execute("SELECT url, stufe FROM bewerbungsstatus WHERE stufe != ''").fetchall():
                job_status[_r["url"]] = {"stufe": _r["stufe"]}
    except Exception:
        pass
    absage_urls = {url for url, info in job_status.items() if info.get("stufe") == "absage"}

    nicht_beworben_urls = {url for url, st in bekannte_status.items() if st == 10}
    ohne_standort_urls = {
        s["url"] for s in stellen
        if not s.get("arbeitsort") and not s.get("nicht_passend") and not s.get("geloescht_am")
    }
    aktive        = [s for s in stellen if not s.get("geloescht_am") and not s.get("nicht_passend") and s["url"] not in zu_weit_urls and s["url"] not in nicht_beworben_urls and s["url"] not in ohne_standort_urls]
    zu_weit       = [s for s in stellen if s["url"] in zu_weit_urls and not s.get("geloescht_am")]

    def _ist_standort_grund(grund: str) -> bool:
        return grund.startswith("Außerhalb Umkreis") or grund.startswith("Verbotener Standort")

    nicht_passend_alle     = [s for s in stellen if s.get("nicht_passend") and not s.get("geloescht_am")]
    nicht_passend_standort = [s for s in nicht_passend_alle if _ist_standort_grund(s.get("nicht_passend_grund") or "")]
    nicht_passend_standort_urls = {s["url"] for s in nicht_passend_standort}
    nicht_passend = [s for s in nicht_passend_alle if s["url"] not in nicht_passend_standort_urls]
    ohne_standort    = [s for s in stellen if s["url"] in ohne_standort_urls]
    geloescht        = [s for s in stellen if s.get("geloescht_am")]
    nicht_beworben   = [s for s in stellen if s["url"] in nicht_beworben_urls and not s.get("geloescht_am")]
    absagen          = [s for s in aktive  if s["url"] in absage_urls]
    geringer_match = [s for s in aktive if _hat_geringen_score(s) and s["url"] not in absage_urls]
    geringer_urls  = {s["url"] for s in geringer_match}
    aktive_haupt   = [s for s in aktive if s["url"] not in geringer_urls and s["url"] not in absage_urls]

    # Status-Zähler für Dashboard
    status_counts = {}
    for s in stellen:
        sv = bekannte_status.get(s["url"])
        if sv is not None:
            status_counts[sv] = status_counts.get(sv, 0) + 1

    _dashboard_status = [
        (sv, f"{STATUS_EMOJIS.get(sv, '')} {STATUS_LABELS[sv]}") for sv in FILTER_STATUS_VALS
    ]
    status_zeilen = " &nbsp;|&nbsp;\n        ".join(
        f'<strong id="stat-status-{sv}" style="cursor:pointer;text-decoration:underline dotted;" '
        f'title="Klick: nach Scanner-Status {sv} filtern" '
        f'onclick="setzeStatusFilter({sv})">{status_counts.get(sv, 0)}</strong> {lbl}'
        for sv, lbl in _dashboard_status
    )

    # Vorgemerkte Stellen (1. fehlgeschlagener Erreichbarkeits-Check, werden beim
    # nächsten vergaben_check-Lauf endgültig als vergeben markiert)
    vorgemerkt_count = sum(1 for s in stellen if s.get("pruef_vormerken") and not s.get("geloescht_am"))
    if vorgemerkt_count:
        status_zeilen += (
            ' &nbsp;|&nbsp;\n        '
            f'<strong style="cursor:pointer;text-decoration:underline dotted;color:#ffc107;" '
            f'title="Klick: nur Stellen mit unsicherer Verfügbarkeit anzeigen" '
            f'onclick="document.getElementById(\'cb-vorgemerkt\').click()">{vorgemerkt_count}</strong> ⏳ Verfügbarkeit unsicher'
        )

    # Status-Konstanten aus status_def.py fürs JS bereitstellen (eine Quelle)
    status_js_konstanten = (
        f"const STATUS_LABELS = {json.dumps(STATUS_LABELS, ensure_ascii=False)};\n"
        f"const INAKTIVE_STATUS = {json.dumps(list(INAKTIVE_STATUSWERTE))};\n"
        f"const UNBEWERTETE_STATUS = {json.dumps(list(UNBEWERTETE_STATUSWERTE))};\n"
        f"const FILTER_STATUS = {json.dumps(list(FILTER_STATUS_VALS))};"
    )

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>Job-Scanner Report – {datum}</title>
    <style>{CSS}</style>
    <script>{status_js_konstanten}</script>
    <script>{JS}</script>
</head>
<body>
    <h1>🔍 Job-Scanner Report</h1>
    {_scan_status_html()}
    <div class="summary-box">
        {status_zeilen} &nbsp;|&nbsp;
        Stand: {datum}
    </div>
    """
    html += """
        <div class="scan-box">
        <button id="scan-start-btn" class="scan-btn" onclick="scanStarten()">🔄 Scan jetzt starten</button>
        <button id="scan-stop-btn" class="scan-btn" onclick="scanStoppen()"
            style="display:none; background:#e74c3c; margin-left:10px;">⛔ Scan abbrechen</button>
        <div id="scan-status"></div>
        <pre id="scan-output"></pre>
        </div>

       <div class="scan-box">
        <h3 style="margin-top:0;">🏢 Einzelne Firma testen</h3>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px;">
            <select id="firma-dropdown"
                style="flex:2; min-width:200px; padding:8px; border:1px solid #ccc; border-radius:4px;">
                <option value="">– Firma wählen –</option>
            </select>
            <button class="scan-btn" onclick="firmaTest()">▶️ Testen</button>
        </div>
        <div id="firma-status"></div>
        <pre id="firma-output" style="display:none; max-height:300px; overflow-y:auto;"></pre>
        </div>

       <div class="scan-box">
        <h3 style="margin-top:0;">📎 Stelle manuell einfügen</h3>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px;">
            <input type="text" id="manuell-url" placeholder="https://jobs.firma.de/stelle/123"
                style="flex:2; min-width:200px; padding:8px; border:1px solid #ccc; border-radius:4px;">
            <input type="text" id="manuell-firma" placeholder="Firmenname (optional)"
                style="flex:1; min-width:150px; padding:8px; border:1px solid #ccc; border-radius:4px;">
            <input type="text" id="manuell-titel" placeholder="Jobtitel (optional)"
                style="flex:1; min-width:150px; padding:8px; border:1px solid #ccc; border-radius:4px;">
            <button class="scan-btn" onclick="stelleEinfuegen()">➕ Einfügen</button>
        </div>
        <div id="manuell-status"></div>
        <pre id="manuell-output" style="display:none;"></pre>
        </div>

       <div class="scan-box">
        <h3 style="margin-top:0;">🏢 Neue Firma testen</h3>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px;">
            <input type="url" id="firma-test-url" placeholder="https://careers.firma.de/jobs"
                style="flex:2; min-width:200px; padding:8px; border:1px solid #ccc; border-radius:4px;">
            <input type="text" id="firma-test-name" placeholder="Firmenname"
                style="flex:1; min-width:150px; padding:8px; border:1px solid #ccc; border-radius:4px;">
        </div>
        <div style="margin-bottom:10px;">
            <label style="font-size:0.9em; cursor:pointer;">
                <input type="checkbox" id="firma-config-cb">
                &nbsp;Zur config.txt hinzufügen, wenn Jobtitel gefunden
            </label>
        </div>
        <button class="scan-btn" onclick="neueFirmaTesten()">🔍 Testen</button>
        </div>
    """

    # Firma-Filter-Dropdown: alle jemals gefundenen Firmen (unabhängig von Status)
    import html as _html_top
    alle_firmennamen = sorted({s["firma"] for s in stellen if s.get("firma")})
    firma_filter_gruppe = ""
    if alle_firmennamen:
        firma_optionen = "".join(
            f'            <option value="{_html_top.escape(f, quote=True)}">{_html_top.escape(f)}</option>\n'
            for f in alle_firmennamen
        )
        firma_filter_gruppe = (
            '<div class="filter-gruppe">\n'
            '            <span class="filter-label">Firma:</span>\n'
            '            <select id="firma-filter-dropdown" onchange="setzeFirmaFilter(this.value)"\n'
            '                style="padding:5px 10px; border-radius:16px; border:1px solid #ddd; font-size:0.85em; cursor:pointer; background:#f0f0f0; color:#555;">\n'
            '                <option value="">Alle Firmen</option>\n'
            f'{firma_optionen}'
            '            </select>\n'
            '        </div>\n'
        )

    # Status-Filter-Buttons dynamisch aus vorhandenen Werten erzeugen
    vorhandene_status_vals = sorted(set(v for v in bekannte_status.values() if v in FILTER_STATUS_VALS))
    status_filter_gruppe = ""
    if vorhandene_status_vals:
        status_filter_gruppe = '<div class="filter-gruppe">\n'
        status_filter_gruppe += '            <span class="filter-label">Scanner-Status:</span>\n'
        status_filter_gruppe += '            <button id="btn-status-alle" class="filter-btn aktiv" onclick="setzeStatusFilter(null)">Alle</button>\n'
        for _sv in vorhandene_status_vals:
            _lbl = STATUS_LABELS.get(_sv, str(_sv))
            status_filter_gruppe += f'            <button class="filter-btn btn-scanner-status" data-status="{_sv}" onclick="setzeStatusFilter({_sv})">{_lbl}</button>\n'
        status_filter_gruppe += '        </div>\n'

    html += """
    <div class="filter-bar" id="filter-bar">
        <div class="filter-gruppe">
            <span class="filter-label">Filter:</span>
            <button id="btn-alle" class="filter-btn aktiv" onclick="setzeFilter(null); setzeStatusFilter(null); document.getElementById('firma-filter-dropdown').value=''; setzeFirmaFilter(null);">Alle</button>
            <button id="btn-beworben" class="filter-btn" onclick="setzeStatusFilter(6)">✅ Beworben aktiv</button>
            <button id="btn-nicht-beworben" class="filter-btn" onclick="setzeStatusFilter(10)">🚫 Nicht beworben</button>
            <button id="btn-kennenlernen" class="filter-btn" onclick="setzeFilter('kennenlernen')">📞 Kennenlernen</button>
            <button id="btn-einladung" class="filter-btn" onclick="setzeFilter('einladung')">📅 Einladung</button>
            <button id="btn-zusage" class="filter-btn" onclick="setzeFilter('zusage')">🎉 Zusage</button>
            <button id="btn-absage" class="filter-btn" onclick="setzeFilter('absage')">❌ Absage</button>
        </div>
        """
    html += firma_filter_gruppe
    html += status_filter_gruppe
    html += """        <div class="filter-gruppe">
            <label style="font-size:0.85em; cursor:pointer; color:#666; display:flex; align-items:center; gap:5px;">
                <input type="checkbox" id="cb-geringer-match" onchange="toggleGeringerMatch(this.checked)">
                📉 Geringen Match einblenden
            </label>
        </div>
        <div class="filter-gruppe">
            <label style="font-size:0.85em; cursor:pointer; color:#666; display:flex; align-items:center; gap:5px;">
                <input type="checkbox" id="cb-zu-weit" onchange="toggleZuWeit(this.checked)">
                🚗 Zu weit einblenden (&gt;60 min)
            </label>
        </div>
        <div class="filter-gruppe">
            <label style="font-size:0.85em; cursor:pointer; color:#666; display:flex; align-items:center; gap:5px;">
                <input type="checkbox" id="cb-nicht-bewertet" onchange="toggleNichtBewertet(this.checked)">
                ❓ Nur nicht bewertet
            </label>
        </div>
        <div class="filter-gruppe">
            <label style="font-size:0.85em; cursor:pointer; color:#666; display:flex; align-items:center; gap:5px;">
                <input type="checkbox" id="cb-vorgemerkt" onchange="toggleVorgemerkt(this.checked)">
                ⏳ Nur Verfügbarkeit unsicher
            </label>
        </div>
        <div class="filter-gruppe">
            <span class="filter-label">Sortierung:</span>
            <button id="btn-sort-std" class="filter-btn aktiv" onclick="setzeSortierung(null)">Standard</button>
            <button id="btn-sort-score" class="filter-btn" onclick="setzeSortierung('score')">⭐ Passung</button>
            <button id="btn-sort-auto" class="filter-btn" onclick="setzeSortierung('auto')">🚗 Entfernung (Auto)</button>
            <button id="btn-sort-transit" class="filter-btn" onclick="setzeSortierung('transit')">🚌 Entfernung (ÖPNV)</button>
        </div>
    </div>
    <div id="flat-ansicht" style="display:none;"><div id="flat-ansicht-info"></div></div>
    <div id="hauptansicht">
    """

    # ── Neue Stellen (letzte 3 Tage, sortiert nach Score) ───────────
    drei_tage_ago = datetime.now() - timedelta(days=3)
    neueste = [
        s for s in aktive_haupt
        if s.get("gefunden_am") and
        datetime.strptime(s["gefunden_am"][:10], "%Y-%m-%d") >= drei_tage_ago.replace(hour=0, minute=0, second=0)
    ]
    neueste_sorted = sorted(neueste, key=lambda s: (s.get("bewertung") or {}).get("score", 0), reverse=True)
    def _fz(s):
        return fahrzeit_daten.get(s["url"])

    if neueste_sorted:
        html += '<div class="firma-block">\n'
        html += f'<h2>🆕 Neue Stellen – letzte 3 Tage ({len(neueste_sorted)})</h2>\n'
        for s in neueste_sorted:
            html += stelle_zu_html(s, zeige_firma=True, fahrzeit=_fz(s), scanner_status=_st(s))
        html += '</div>\n'

    # ── Top 10 nach KI-Score ────────────────────────────────────────
    top10 = sorted(
        [s for s in aktive_haupt if (s.get("bewertung") or {}).get("score", 0) > 0
         and bekannte_status.get(s["url"]) in (4, 6)],
        key=lambda s: max(s["bewertung"].get("score", 0), s["bewertung"].get("score_nach_anpassung") or 0),
        reverse=True
    )[:10]
    if top10:
        html += '<div class="firma-block">\n'
        html += '<h2>⭐ Top 10 nach KI-Score</h2>\n'
        for s in top10:
            html += stelle_zu_html(s, zeige_firma=True, fahrzeit=_fz(s), scanner_status=_st(s))
        html += '</div>\n'

    # ── Pro Firma ───────────────────────────────────────────────────
    firmen_dict = {}
    for s in aktive_haupt:
        firmen_dict.setdefault(s["firma"], []).append(s)

    alle_firmen = sorted(firmen_dict.keys())

    for firma_name in alle_firmen:
        firma_stellen = firmen_dict.get(firma_name, [])
        hoch = [s for s in firma_stellen if (s.get("bewertung") or {}).get("score", 0) >= 70]
        rest = [s for s in firma_stellen if s not in hoch]

        html += '<div class="firma-block">\n'
        html += f'<h2>{firma_name} ({len(firma_stellen)} Treffer)</h2>\n'

        if firma_stellen:
            if hoch:
                html += '<p><strong>⭐ Score ≥ 70%</strong></p>\n'
                for s in hoch:
                    html += stelle_zu_html(s, fahrzeit=_fz(s), scanner_status=_st(s))
            if rest:
                html += '<p><strong>Weitere Treffer</strong></p>\n'
                for s in rest:
                    html += stelle_zu_html(s, fahrzeit=_fz(s), scanner_status=_st(s))
        else:
            html += '<p class="leer">Keine passenden Stellen gefunden.</p>\n'

        html += '</div>\n'

    # ── Geringer Match (≤ 65 %, eingeklappt) ──────────────────────
    if geringer_match:
        geringer_match_sorted = sorted(
            geringer_match,
            key=lambda s: (s.get("bewertung") or {}).get("score", 0),
            reverse=True
        )
        html += f'<div id="geringer-match-section" style="display:none; margin:15px 0;">\n'
        html += f'<div class="firma-block">\n'
        html += f'<h2>📉 Geringer Match – Score ≤ {GERINGER_MATCH_SCHWELLE}% ({len(geringer_match)})</h2>\n'
        for s in geringer_match_sorted:
            html += stelle_zu_html(s, zeige_firma=True, fahrzeit=_fz(s), geringer_match=True, scanner_status=_st(s))
        html += '</div>\n</div>\n'

    # ── Vergangene Stellen (am Ende, eingeklappt) ───────────────────
    if geloescht:
        html += f'''<details style="margin: 15px 0;">
    <summary style="cursor:pointer; background:#e0e0e0; padding:12px 20px;
        border-radius:8px; font-weight:bold; font-size:1.05em;">
        🗑️ Vergeben / Nicht mehr verfügbar ({len(geloescht)})
    </summary>
    <div class="firma-block" style="border-radius:0 0 8px 8px; margin-top:0;">\n'''
        for s in geloescht:
            html += stelle_zu_html(s, zeige_firma=True, fahrzeit=_fz(s), scanner_status=_st(s))
        html += '</div>\n</details>\n'

    if absagen:
        html += f'''<details style="margin: 15px 0;">
    <summary style="cursor:pointer; background:#fde8e8; padding:12px 20px;
        border-radius:8px; font-weight:bold; font-size:1.05em;">
        ❌ Absagen ({len(absagen)})
    </summary>
    <div class="firma-block" style="border-radius:0 0 8px 8px; margin-top:0;">\n'''
        for s in absagen:
            html += stelle_zu_html(s, zeige_firma=True, fahrzeit=_fz(s), scanner_status=_st(s))
        html += '</div>\n</details>\n'

    if nicht_beworben:
        html += f'''<details style="margin: 15px 0;">
    <summary style="cursor:pointer; background:#f9ebea; padding:12px 20px;
        border-radius:8px; font-weight:bold; font-size:1.05em;">
        🚫 Nicht beworben ({len(nicht_beworben)})
    </summary>
    <div class="firma-block" style="border-radius:0 0 8px 8px; margin-top:0;">\n'''
        for s in nicht_beworben:
            html += stelle_zu_html(s, zeige_firma=True, fahrzeit=_fz(s), scanner_status=_st(s))
        html += '</div>\n</details>\n'

    if nicht_passend:
        html += f'''<details style="margin: 15px 0;">
    <summary style="cursor:pointer; background:#fdebd0; padding:12px 20px;
        border-radius:8px; font-weight:bold; font-size:1.05em;">
        🚫 Nicht passend – Ausschlusskriterium ({len(nicht_passend)})
    </summary>
    <div class="firma-block" style="border-radius:0 0 8px 8px; margin-top:0;">\n'''
        for s in nicht_passend:
            html += stelle_zu_html(s, zeige_firma=True, fahrzeit=_fz(s), scanner_status=_st(s))
        html += '</div>\n</details>\n'

    if nicht_passend_standort:
        html += f'''<details style="margin: 15px 0;">
    <summary style="cursor:pointer; background:#fdebd0; padding:12px 20px;
        border-radius:8px; font-weight:bold; font-size:1.05em;">
        📍 Nicht passend – Standort außerhalb/verboten ({len(nicht_passend_standort)})
    </summary>
    <div class="firma-block" style="border-radius:0 0 8px 8px; margin-top:0;">\n'''
        for s in nicht_passend_standort:
            html += stelle_zu_html(s, zeige_firma=True, fahrzeit=_fz(s), scanner_status=_st(s))
        html += '</div>\n</details>\n'

    if ohne_standort:
        html += f'''<details open style="margin: 15px 0;">
    <summary style="cursor:pointer; background:#eaf2f8; padding:12px 20px;
        border-radius:8px; font-weight:bold; font-size:1.05em;">
        📍 Stellen ohne Standort ({len(ohne_standort)})
    </summary>
    <div class="firma-block" style="border-radius:0 0 8px 8px; margin-top:0;">\n'''
        for s in ohne_standort:
            html += stelle_zu_html(s, zeige_firma=True, fahrzeit=_fz(s), scanner_status=_st(s))
        html += '</div>\n</details>\n'

    if zu_weit:
        html += f'<div id="zu-weit-section" style="display:none; margin:15px 0;">\n'
        html += f'<div class="firma-block">\n'
        html += f'<h2>🚗 Nicht passend – Zu weit (&gt;{FAHRZEIT_MAX_AUTO_MIN} min mit Auto) ({len(zu_weit)})</h2>\n'
        for s in zu_weit:
            html += stelle_zu_html(s, zeige_firma=True, fahrzeit=_fz(s), scanner_status=_st(s), zu_weit=True)
        html += '</div>\n</div>\n'

    html += '</div>\n'  # /hauptansicht

    html += f"""
    <hr>
    <p style="color:#999; font-size:0.85em;">Generiert am {datum}</p>
</body>
</html>"""

    return html


# =============================================================================
# ÄNDERUNGS-MAIL ERSTELLEN
# =============================================================================

def erstelle_aenderungs_html(stellen: list) -> str:
    datum    = datetime.now().strftime("%d.%m.%Y %H:%M")

    job_status: dict = {}
    try:
        from db import verbindung as _db_verbindung
        with _db_verbindung() as _con:
            for _r in _con.execute("SELECT url, stufe FROM bewerbungsstatus WHERE stufe != ''").fetchall():
                job_status[_r["url"]] = {"stufe": _r["stufe"]}
    except Exception:
        pass
    absage_urls = {url for url, info in job_status.items() if info.get("stufe") == "absage"}

    aktive        = [s for s in stellen if not s.get("geloescht_am") and not s.get("nicht_passend")]
    neue          = [s for s in aktive  if s.get("neu") and s["url"] not in absage_urls]
    geloescht     = [s for s in stellen if s.get("geloescht_am")]
    nicht_passend = [s for s in stellen if s.get("nicht_passend") and not s.get("geloescht_am")]
    absagen       = [s for s in aktive  if s["url"] in absage_urls]
    geringer_match = [s for s in aktive if _hat_geringen_score(s) and s["url"] not in absage_urls]
    aktive_haupt  = [s for s in aktive  if not _hat_geringen_score(s) and s["url"] not in absage_urls]

    def score_farbe(score: int) -> str:
        if score >= 70: return "#27ae60"
        if score >= 40: return "#f39c12"
        return "#e74c3c"

    def stelle_zeile(s: dict) -> str:
        titel    = _html.escape(s.get("titel", "–"))
        firma    = _html.escape(s.get("firma", ""))
        url      = _html.escape(s.get("url", "#"), quote=True)
        arbeitsort = _html.escape(s.get("arbeitsort") or "")
        standort_text = f" · {arbeitsort}" if arbeitsort else ""
        score    = (s.get("bewertung") or {}).get("score")
        score_na = (s.get("bewertung") or {}).get("score_nach_anpassung")
        if score:
            score_text = f'<span style="color:{score_farbe(score)};font-weight:bold;">{score}%</span>'
            if score_na and score_na > score:
                score_text += f' → <span style="color:{score_farbe(score_na)};font-weight:bold;">{score_na}%</span>'
        else:
            score_text = ""
        meta = " · ".join(filter(None, [firma + standort_text, score_text]))
        return (
            f'<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;">'
            f'<a href="{url}" style="color:#2c7be5;text-decoration:none;font-weight:500;font-size:0.95em;">{titel}</a>'
            f'<br><span style="color:#999;font-size:0.82em;">{meta}</span>'
            f'</td>'
            f'</tr>'
        )

    def sektion(label: str, farbe: str, liste: list) -> str:
        if not liste:
            return ""
        zeilen = "\n".join(stelle_zeile(s) for s in liste)
        return f"""
<tr><td style="padding:16px 0 6px 0;">
  <span style="color:{farbe};font-weight:bold;font-size:0.95em;">{label} ({len(liste)})</span>
</td></tr>
<tr><td style="background:#fff;border-radius:8px;border:1px solid #e8e8e8;">
  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
    {zeilen}
  </table>
</td></tr>"""

    stats = [
        ("aktive",        len(aktive_haupt), "#2c7be5"),
        ("neu",           len(neue),         "#27ae60"),
        ("ger. Match",    len(geringer_match),"#f39c12"),
        ("Absagen",       len(absagen),       "#e74c3c"),
        ("n. passend",    len(nicht_passend), "#bbb"),
        ("vergeben",      len(geloescht),     "#bbb"),
    ]
    stats_zellen = "".join(
        f'<td style="text-align:center;padding:12px 8px;border-right:1px solid #f0f0f0;">'
        f'<div style="font-size:1.5em;font-weight:bold;color:{farbe};">{wert}</div>'
        f'<div style="font-size:0.72em;color:#999;margin-top:3px;">{label}</div>'
        f'</td>'
        for label, wert, farbe in stats
    )

    html = f"""<!DOCTYPE html>
<html lang="de">
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;font-size:14px;color:#333;background:#f0f2f5;padding:20px;margin:0;">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center"><table width="600" cellpadding="0" cellspacing="0">

  <tr><td style="padding:0 0 14px 0;">
    <span style="font-size:1.2em;font-weight:bold;color:#222;">🔍 Job-Scanner</span>
    <span style="color:#bbb;font-size:0.82em;margin-left:10px;">Stand: {datum}</span>
  </td></tr>

  <tr><td>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#fff;border-radius:10px;border:1px solid #e0e0e0;margin-bottom:8px;">
      <tr>{stats_zellen}</tr>
    </table>
  </td></tr>

  {sektion("🆕 Neue Stellen", "#27ae60", neue)}

</table></td></tr>
</table>
</body></html>"""
    return html


# =============================================================================
# E-MAIL SENDEN
# =============================================================================

def sende_mail(aenderungs_html: str, config: dict, neue: int = 0, geloescht: int = 0):
    datum = datetime.now().strftime("%d.%m.%Y")

    msg = MIMEMultipart("alternative")
    msg["From"]    = config["email_absender"]
    msg["To"]      = config["email_empfaenger"]
    msg["Subject"] = f"Job-Scanner {datum} – {neue} neu, {geloescht} vergeben"

    plain = (
        f"Job-Scanner Änderungen vom {datum}:\n\n"
        f"  Neue Stellen:          {neue}\n"
        f"  Nicht mehr verfügbar:  {geloescht}\n"
    )
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(aenderungs_html, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.mail.me.com", 587, local_hostname="raspberrypi.local") as server:
            server.starttls()
            server.login(config["email_absender"], config["email_passwort"])
            server.send_message(msg)
        print(f"  📧 Mail gesendet an {config['email_empfaenger']}")
    except Exception as e:
        print(f"  ❌ Mail-Versand fehlgeschlagen: {e}")


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--keine-mail", action="store_true", help="Mail-Versand unterdrücken")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  REPORT  –  Schritt 4: HTML erstellen & Mail senden")
    print("=" * 60)
    config  = lade_config()
    from db import lade_alle_stellen, lade_bekannte_dict, upsert_stelle, exportiere_stellen_json, exportiere_bekannte_json, erstelle_schema as _erstelle_schema
    _erstelle_schema()
    stellen = lade_alle_stellen()

    if not stellen:
        print("ℹ️  Keine Stellen in DB – zuerst scanner.py ausführen.")
        return

    # Datenreparatur: geloescht_am löschen wenn Stelle laut DB noch aktiv ist.
    # Inaktive Status (0,7,8,9,10) und 6 (beworben) sind ausgenommen – genau
    # hier fehlte bis Juli 2026 die 9, wodurch vergebene Stellen bei jedem
    # Report-Lauf ihr geloescht_am verloren und wieder "aktiv" wurden.
    bekannte = lade_bekannte_dict()
    repariert = 0
    for s in stellen:
        if s.get("geloescht_am") and not s.get("nicht_passend"):
            eintrag = bekannte.get(s["url"], {})
            if eintrag.get("status", 0) not in (6, *INAKTIVE_STATUSWERTE):
                s["geloescht_am"] = None
                repariert += 1
                upsert_stelle({"url": s["url"], "geloescht_am": None})

    # Ausschluss-Prüfung: re-evaluiere Titel+Standort gegen aktuelle Config
    ausschlussbegriffe  = config["ausschlussbegriffe"]
    erlaubte_standorte  = config["erlaubte_standorte"]
    verbotene_standorte = config["verbotene_standorte"]
    neu_ausgeschlossen  = 0

    for s in stellen:
        if s.get("geloescht_am"):
            continue
        url = s.get("url", "")
        b   = bekannte.get(url, {})

        titel          = s.get("titel", "").lower()
        arbeitsort     = s.get("arbeitsort") or ""
        ausgeschlossen = ist_ausgeschlossen(titel, ausschlussbegriffe)
        standort_grund = standort_ablehnungsgrund(arbeitsort, erlaubte_standorte, verbotene_standorte)

        if ausgeschlossen or standort_grund:
            if ausgeschlossen:
                _b_treffer = text_matched(titel, ausschlussbegriffe)
                _b = _b_treffer[0] if _b_treffer else ""
                np_grund = f"Ausschlussbegriff: '{_b}'" if _b else "Ausschlussbegriff"
            else:
                np_grund = standort_grund
            s["nicht_passend"] = True
            s["nicht_passend_grund"] = np_grund
            neu_ausgeschlossen += 1
            upsert_stelle({"url": url, "nicht_passend": True, "nicht_passend_grund": np_grund})
        else:
            s["nicht_passend"] = False
            if b.get("nicht_passend"):
                upsert_stelle({"url": url, "nicht_passend": False})

    if repariert or neu_ausgeschlossen:
        exportiere_stellen_json(STELLEN_JSON)
        exportiere_bekannte_json(BEKANNTE_JSON)
        if repariert:
            print(f"  🔧 {repariert} Stelle(n) reaktiviert (geloescht_am-Datenreparatur)")
        if neu_ausgeschlossen:
            print(f"  🚫 {neu_ausgeschlossen} Stelle(n) als nicht passend markiert (Ausschluss-Prüfung)")

    # Vollständigen Report erstellen
    print("  📄 Erstelle Report...")
    report_html = erstelle_report(stellen, config)
    REPORT_PFAD.write_text(report_html, encoding="utf-8")
    print(f"  ✅ Report gespeichert: {REPORT_PFAD}")

    # E-Mail nur bei Änderungen
    neue      = [s for s in stellen if s.get("neu") and not s.get("geloescht_am")]
    geloescht = [s for s in stellen if s.get("geloescht_am")]

    if args.keine_mail:
        print("  ℹ️  Mail-Versand unterdrückt (--keine-mail)")
    elif config["email_aktiv"]:
        if neue or geloescht:
            print(f"  📧 Sende Änderungs-Mail ({len(neue)} neu, {len(geloescht)} vergeben)...")
            aenderungs_html = erstelle_aenderungs_html(stellen)
            sende_mail(aenderungs_html, config, neue=len(neue), geloescht=len(geloescht))
        else:
            print("  ℹ️  Keine Änderungen – keine Mail gesendet.")
    else:
        print("  ℹ️  E-Mail deaktiviert (EMAIL_AKTIV = false)")

    # neu-Flag zurücksetzen nach Report-Erstellung
    geaendert = 0
    for s in stellen:
        if s.get("neu"):
            s["neu"] = False
            geaendert += 1
    if geaendert:
        print(f"  🔄 neu-Flag für {geaendert} Stellen zurückgesetzt")

    from db import neu_flag_zuruecksetzen
    neu_flag_zuruecksetzen()
    exportiere_stellen_json(STELLEN_JSON)
    exportiere_bekannte_json(BEKANNTE_JSON)

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Report: {REPORT_PFAD}")
    print(f"{'='*60}\n")

    import webbrowser
    webbrowser.open(str(REPORT_PFAD))


if __name__ == "__main__":
    main()
