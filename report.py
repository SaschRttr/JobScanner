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
import json
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
import urllib.parse


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD      = Path(__file__).parent
STELLEN_JSON  = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON = BASIS_PFAD / "bekannte_stellen.json"
REPORT_PFAD   = BASIS_PFAD / "report.html"
CONFIG_PFAD   = Path(__file__).parent / "config.txt"
BEWERBUNGEN_DIR = BASIS_PFAD / "bewerbungen"
RASPI_IP = ""


# =============================================================================
# CONFIG
# =============================================================================

def lade_config() -> dict:
    if not CONFIG_PFAD.exists():
        print(f"❌ config.txt nicht gefunden: {CONFIG_PFAD}")
        sys.exit(1)

    result = {
        "email_aktiv": False,
        "email_absender": "",
        "email_passwort": "",
        "email_empfaenger": "",
        "firmen_reihenfolge": [],
        "raspi_ip": "",
    }

    aktiver_abschnitt = None

    for zeile in CONFIG_PFAD.read_text(encoding="utf-8").splitlines():
        z = zeile.strip()

        if z.startswith("[\\") and z.endswith("]"):
            aktiver_abschnitt = None
            continue

        if z.startswith("[") and z.endswith("]") and not z.startswith("[\\"):
            aktiver_abschnitt = z[1:-1].lower()
            continue

        if z.startswith("#") or not z:
            continue

        if aktiver_abschnitt is None:
            if z.upper().startswith("EMAIL_AKTIV"):
                result["email_aktiv"] = z.split("=", 1)[1].strip().lower() == "true"
            elif z.upper().startswith("EMAIL_ABSENDER"):
                result["email_absender"] = z.split("=", 1)[1].strip()
            elif z.upper().startswith("EMAIL_PASSWORT"):
                result["email_passwort"] = z.split("=", 1)[1].strip()
            elif z.upper().startswith("EMAIL_EMPFAENGER"):
                result["email_empfaenger"] = z.split("=", 1)[1].strip()
            elif z.upper().startswith("RASPI_IP"):
                result["raspi_ip"] = z.split("=", 1)[1].strip()

        elif aktiver_abschnitt == "firmen":
            if "|" in z:
                name = z.split("|", 1)[0].strip()
                if name:
                    result["firmen_reihenfolge"].append(name)

    return result


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


def sicherer_pfadname(text: str, max_len: int = 50) -> str:
    bereinigt = re.sub(r'[^\w\s\-]', '', text).strip()
    bereinigt = re.sub(r'\s+', '_', bereinigt)
    return bereinigt[:max_len]


# =============================================================================
# HTML-BAUSTEINE
# =============================================================================

def stelle_zu_html(s: dict, zeige_firma: bool = False) -> str:
    ist_neu      = s.get("neu", False)
    ist_geloescht = s.get("geloescht_am") is not None

    if ist_geloescht:
        css = "stelle stelle-geloescht"
    elif ist_neu:
        css = "stelle stelle-neu"
    else:
        css = "stelle"
    
    tags = "".join(f'<span class="tag">{t}</span>' for t in s.get("treffer", []))
    neu_badge      = '<span class="badge badge-neu">NEU</span>' if ist_neu else ""
    geloescht_badge = '<span class="badge badge-weg">VERGEBEN</span>' if ist_geloescht else ""
    url_escaped    = s["url"].replace('"', "&quot;").replace("'", "\\'")
    firma_label    = f'<span class="firma-label"> — {s["firma"]}</span>' if zeige_firma else ""
    gefunden_am    = s.get("gefunden_am", "")[:10]
    geloescht_am   = s.get("geloescht_am", "")
    datum_label    = f'<span style="color:#aaa; font-size:0.8em; margin-left:8px;">📅 gefunden: {gefunden_am}'
    if geloescht_am:
        datum_label += f' &nbsp;|&nbsp; 🗑️ vergeben: {geloescht_am[:10]}'
    datum_label += '</span>'

    # KI-Bewertung
    bewertung_html = ""
    if s.get("bewertung"):
        b     = s["bewertung"]
        score = b.get("score", 0)
        empf  = b.get("empfehlung", "?")
        farbe       = "#27ae60" if score >= 70 else "#f39c12" if score >= 40 else "#e74c3c"
        empf_farbe  = "#27ae60" if empf == "bewerben" else "#e74c3c"
        staerken    = "".join(f"<li>{p}</li>" for p in b.get("staerken", []))
        luecken     = "".join(f"<li>{p}</li>" for p in b.get("luecken", []))
        anpassungen = "".join(f"<li>{p}</li>" for p in b.get("lebenslauf_anpassungen", []))
        begruendung = b.get("score_begruendung", "")
        bewertung_html = f"""
        <div class="bewertung">
            <strong style="color:{farbe};">Score: {score}%</strong>
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
                        onchange="speichern('{url_escaped}', 'stufe', this.value)">
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
                    onblur="speichern('{url_escaped}', 'kommentar', this.value)"></textarea>
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

    if score >= 70:
        if lv_docx and as_docx:
            # Beide DOCX vorhanden → Download-Links anzeigen
            css = "stelle stelle-bewerbung"
            lv_dl  = f"/download?pfad={urllib.parse.quote(str(lv_docx))}"
            as_dl  = f"/download?pfad={urllib.parse.quote(str(as_docx))}"
            lebenslauf_html = f"""
        <div style="margin-top:8px; padding:8px; background:#eafaf1; border-radius:4px; font-size:0.85em;">
            📄 <a href="{lv_dl}" style="color:#27ae60; margin-right:12px;">Lebenslauf.docx</a>
            ✉️ <a href="{as_dl}" style="color:#27ae60;">Anschreiben.docx</a>
        </div>"""
        else:
            # Noch nicht erstellt oder gelöscht → Checkbox anzeigen
            lebenslauf_html = f"""
        <div style="margin-top:8px; font-size:0.85em;" id="bew-box-{firma_safe}-{titel_safe}">
            <label style="cursor:pointer;">
                <input type="checkbox"
                    onchange="bewerbungErstellen(this, '{url_escaped}', '{firma_safe}', '{titel_safe}')">
                &nbsp;📋 Lebenslauf &amp; Anschreiben erstellen
            </label>
            <span id="bew-status-{firma_safe}-{titel_safe}" style="margin-left:8px; color:#888;"></span>
        </div>"""
    else:
        lebenslauf_html = ""
    import html as html_mod

    # Stellentext-Block (aufklappbar)
    stellentext = s.get("stellentext") or s.get("rohtext") or ""
    if stellentext:
        st_escaped = html_mod.escape(stellentext[:4000]).replace("\n", "<br>")
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
                <summary style="cursor:pointer;">{html_mod.escape(str(fq.get('frage', '')))}</summary>
                <p style="margin:6px 0 0 16px; color:#444;">{html_mod.escape(str(fq.get('antwort', '')))}</p>
            </details>"""
        steckbrief_html = f"""
        <details class="steckbrief-details">
            <summary>🧠 Steckbrief anzeigen</summary>
            <div class="steckbrief-inhalt">
                <p><strong>Firma:</strong> {html_mod.escape(str(steckbrief.get('firma_beschreibung', '')))}</p>
                <p><strong>Warum ich passe:</strong> {html_mod.escape(str(steckbrief.get('warum_ich_passe', '')))}</p>
                <p><strong>Interview-Fragen:</strong></p>
                {fragen_html}
            </div>
        </details>"""
    else:
        steckbrief_html = ""

    steckbrief_btn = f'<button class="steckbrief-btn" onclick="steckbriefGenerieren(this, \'{url_escaped}\')">🧠 Steckbrief generieren</button>'

    return f"""<div class="{css}" data-url="{url_escaped}">
    <a href="{s['url']}" target="_blank">{s['titel']}</a>{neu_badge}{geloescht_badge}{firma_label}{datum_label}
    <div class="tags">{tags}</div>
    {bewertung_html}
    {stellentext_html}
    {steckbrief_html}
    {steckbrief_btn}
    {notizen_html}
    {lebenslauf_html}
</div>
"""


CSS = """
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        max-width: 960px; margin: 40px auto; padding: 0 20px;
        background: #f5f5f5; color: #333;
    }
    h1 { color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }
    h2 { color: #2c3e50; margin-top: 30px; }
    .firma-block {
        background: white; border-radius: 8px; padding: 20px;
        margin: 15px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    .stelle {
        border-left: 4px solid #3498db; padding: 10px 15px;
        margin: 10px 0; background: #f8f9fa; border-radius: 0 4px 4px 0;
    }
    .stelle a {
        color: #2980b9; text-decoration: none; font-weight: bold; font-size: 1.05em;
    }
    .stelle a:hover { text-decoration: underline; }
    .stelle-neu      { background: #f3e8fd; border-left-color: #8e44ad; }
    .stelle-geloescht { background: #f0f0f0; border-left-color: #aaa; opacity: 0.7; }
    .stelle.beworben      { background: #fff9c4; border-left-color: #f1c40f; }
    .stelle.kennenlernen  { background: #ddeeff; border-left-color: #2980b9; }
    .stelle.einladung     { background: #ddeeff; border-left-color: #2980b9; }
    .stelle.zusage        { background: #d5f5e3; border-left-color: #27ae60; }
    .stelle.absage        { background: #fde8e8; border-left-color: #e74c3c; }
    .stelle-bewerbung { background: #d6eaf8; border-left-color: #2980b9; }
    .tags { margin-top: 5px; }
    .tag {
        display: inline-block; background: #e8f4fd; color: #2980b9;
        padding: 2px 8px; border-radius: 12px; font-size: 0.8em; margin: 2px;
    }
    .badge {
        padding: 2px 8px; border-radius: 10px;
        font-size: 0.8em; margin-left: 8px; color: white;
    }
    .badge-neu { background: #e74c3c; }
    .badge-weg { background: #aaa; }
    .firma-label { color: #999; font-size: 0.85em; }
    .bewertung {
        margin-top: 10px; padding: 10px; background: #fff;
        border-radius: 6px; font-size: 0.9em;
    }
    .bewertung details { margin-top: 8px; }
    .stellentext-details { margin-top: 8px; }
    .stellentext-details summary {
        cursor: pointer; color: #555; font-size: 0.9em;
        padding: 4px 0; user-select: none;
    }
    .stellentext-inhalt {
        margin-top: 8px; padding: 12px; background: #fff;
        border: 1px solid #ddd; border-radius: 4px;
        font-size: 0.85em; line-height: 1.6; color: #333;
        max-height: 400px; overflow-y: auto;
        white-space: pre-wrap;
    }
    .begruendung { color: #555; }
    .notizen { margin-top: 8px; }
    .notizen summary { cursor: pointer; color: #888; font-size: 0.85em; }
    .kommentar {
        width: 100%; margin-top: 4px; font-size: 0.85em;
        border: 1px solid #ddd; border-radius: 4px; padding: 4px;
        box-sizing: border-box;
    }
    .summary-box {
        background: #2c3e50; color: white; padding: 15px 20px;
        border-radius: 8px; margin-bottom: 20px;
    }
    .summary-box strong { color: #3498db; }
    .leer { color: #999; font-style: italic; }
    .stufen-select {
        font-size: 0.85em; padding: 3px 6px; border-radius: 4px;
        border: 1px solid #ccc; cursor: pointer; margin-bottom: 6px;
        background: white;
    }
    .stufen-ts {
        font-size: 0.78em; color: #888; margin-left: 6px;
    }
    .steckbrief-details { margin-top: 8px; }
    .steckbrief-details summary {
        cursor: pointer; color: #555; font-size: 0.9em;
        padding: 4px 0; user-select: none;
    }
    .steckbrief-inhalt {
        margin-top: 8px; padding: 12px; background: #fff;
        border: 1px solid #ddd; border-radius: 4px;
        font-size: 0.85em; line-height: 1.6; color: #333;
    }
    .steckbrief-btn {
        margin-top: 8px; background: none; color: inherit;
        border: none; padding: 0; font-size: 0.85em;
        cursor: pointer; text-decoration: none;
    }
    .steckbrief-btn:disabled { color: #aaa; cursor: not-allowed; }
    .scan-box {
        background: white; border-radius: 8px; padding: 20px;
        margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        text-align: center;
    }
    .scan-btn {
        background: #3498db; color: white; border: none;
        padding: 12px 28px; border-radius: 6px; font-size: 1em;
        cursor: pointer;
    }
    .scan-btn:hover  { background: #2980b9; }
    .scan-btn:disabled { background: #aaa; cursor: not-allowed; }
    #scan-output {
        text-align: left; background: #1e1e1e; color: #d4d4d4;
        border-radius: 6px; padding: 15px; margin-top: 15px;
        font-size: 0.85em; max-height: 400px; overflow-y: auto;
        display: none; white-space: pre-wrap;
    }
    #scan-status { margin-top: 10px; font-size: 0.95em; color: #555; }
"""

JS = """
    const SERVER = window.location.origin;

    async function speichern(url, feld, wert) {
        const status = JSON.parse(localStorage.getItem('job_status') || '{}');
        if (!status[url]) status[url] = {};

        if (feld === 'stufe') {
            const jetzt = new Date().toLocaleString('de-DE', {
                day:'2-digit', month:'2-digit', year:'numeric',
                hour:'2-digit', minute:'2-digit'
            });
            // Timestamp nur beim ersten Mal setzen
            const tsKey = wert + '_am';
            if (wert && !status[url][tsKey]) {
                status[url][tsKey] = jetzt;
            }
            status[url]['stufe'] = wert;

            // CSS-Klassen aktualisieren
            const el = document.querySelector(`[data-url="${CSS.escape(url)}"]`);
            if (el) {
                ['beworben','kennenlernen','einladung','zusage','absage'].forEach(k => el.classList.remove(k));
                if (wert) el.classList.add(wert);
            }
            // Timestamp anzeigen
            const tsEl = document.querySelector(`[data-url="${CSS.escape(url)}"] .stufen-ts`);
            if (tsEl) {
                tsEl.textContent = (wert && status[url][tsKey]) ? ('seit ' + status[url][tsKey]) : '';
            }
        } else {
            status[url][feld] = wert;
        }

        localStorage.setItem('job_status', JSON.stringify(status));

        if (SERVER) {
            await fetch(SERVER + '/status', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url, feld, wert })
            });
        }
    }

    async function ladeStatus() {
        let status = {};
        if (!SERVER) {
            status = JSON.parse(localStorage.getItem('job_status') || '{}');
        } else {
            try {
                const res = await fetch(SERVER + '/status');
                status = await res.json();
            } catch (e) {
                console.warn('Statusserver nicht erreichbar', e);
                status = JSON.parse(localStorage.getItem('job_status') || '{}');
            }
        }
        document.querySelectorAll('.stelle[data-url]').forEach(el => {
            const s = status[el.dataset.url];
            if (!s) return;

            // Stufe wiederherstellen
            const stufe = s.stufe || '';
            if (stufe) {
                ['beworben','kennenlernen','einladung','zusage','absage'].forEach(k => el.classList.remove(k));
                el.classList.add(stufe);
            }
            // Dropdown setzen
            const sel = el.querySelector('.stufen-select');
            if (sel && stufe) sel.value = stufe;

            // Timestamp anzeigen
            const tsEl = el.querySelector('.stufen-ts');
            if (tsEl) {
                const tsKey = stufe + '_am';
                tsEl.textContent = (stufe && s[tsKey]) ? ('seit ' + s[tsKey]) : '';
            }

            // Kommentar wiederherstellen
            const ta = el.querySelector('.kommentar');
            if (s.kommentar && ta) ta.value = s.kommentar;
        });
    }

    window.onload = function() { ladeStatus(); ladeFirmen(); };

    async function ladeFirmen() {
        try {
            const r = await fetch('/firmen');
            const namen = await r.json();
            const sel = document.getElementById('firma-dropdown');
            namen.forEach(n => {
                const opt = document.createElement('option');
                opt.value = opt.textContent = n;
                sel.appendChild(opt);
            });
        } catch(e) {}
    }

    function firmaTest() {
        const sel    = document.getElementById('firma-dropdown');
        const status = document.getElementById('firma-status');
        const output = document.getElementById('firma-output');
        const firma  = sel.value;
        if (!firma) { status.textContent = '⚠️ Bitte Firma wählen'; return; }

        sel.disabled = true;
        status.textContent = `⏳ Scanne ${firma}...`;
        output.style.display = 'block';
        output.textContent = '';

        const quelle = new EventSource('/firma-testen?firma=' + encodeURIComponent(firma));
        quelle.onmessage = function(e) {
            if (e.data === 'FERTIG') {
                quelle.close();
                sel.disabled = false;
                status.textContent = '✅ Fertig';
                return;
            }
            output.textContent += e.data + '\\n';
            output.scrollTop = output.scrollHeight;
        };
        quelle.onerror = function() {
            quelle.close();
            sel.disabled = false;
            status.textContent = '❌ Verbindungsfehler';
        };
    }

    function scanStarten() {
        const btn     = document.getElementById('scan-start-btn');
        const stopBtn = document.getElementById('scan-stop-btn');
        const output  = document.getElementById('scan-output');
        const status  = document.getElementById('scan-status');

        btn.disabled = true;
        btn.textContent = '⏳ Scan läuft...';
        stopBtn.style.display = 'inline-block';
        output.style.display = 'block';
        output.textContent = '';
        status.textContent = '';

        const quelle = new EventSource('/starten');

        quelle.onmessage = function(e) {
            if (e.data === 'FERTIG') {
                quelle.close();
                stopBtn.style.display = 'none';
                btn.disabled = false;
                btn.textContent = '🔄 Scan jetzt starten';
                status.textContent = '✅ Fertig – Seite wird neu geladen...';
                setTimeout(() => location.reload(), 2000);
                return;
            }
            output.textContent += e.data + '\\n';
            output.scrollTop = output.scrollHeight;
        };

        quelle.onerror = function() {
            quelle.close();
            stopBtn.style.display = 'none';
            btn.disabled = false;
            btn.textContent = '🔄 Scan jetzt starten';
            status.textContent = '❌ Fehler: Flask-Server nicht erreichbar. Läuft webui.py?';
            status.style.color = '#e74c3c';
        };
    }

    async function scanStoppen() {
        const stopBtn = document.getElementById('scan-stop-btn');
        const status  = document.getElementById('scan-status');
        stopBtn.disabled = true;
        stopBtn.textContent = '⏳ Wird abgebrochen...';
        try {
            const r = await fetch('/stoppen');
            const d = await r.json();
            status.textContent = d.nachricht || 'Abbruch angefordert';
        } catch(e) {
            status.textContent = '❌ Fehler beim Abbrechen';
        }
    }

    async function steckbriefGenerieren(btn, stellenUrl) {
        btn.disabled = true;
        btn.textContent = '⏳ Generiere...';
        try {
            const res = await fetch(SERVER + '/steckbrief-erstellen', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: stellenUrl })
            });
            const data = await res.json();
            if (data.ok) {
                location.reload();
            } else {
                btn.disabled = false;
                btn.textContent = '🧠 Steckbrief generieren';
                alert('Fehler: ' + (data.fehler || 'Unbekannt'));
            }
        } catch(e) {
            btn.disabled = false;
            btn.textContent = '🧠 Steckbrief generieren';
            alert('Server nicht erreichbar');
        }
    }

    async function bewerbungErstellen(checkbox, stellenUrl, firma, titel) {
        if (!checkbox.checked) return;

        const statusEl = document.getElementById('bew-status-' + firma + '-' + titel);
        checkbox.disabled = true;
        // Label blau färben als visuelles Feedback
        const label = checkbox.closest('label');
        if (label) { label.style.color = '#2980b9'; label.style.fontWeight = 'bold'; }
        statusEl.textContent = '⏳ Wird erstellt...';
        statusEl.style.color = '#2980b9';

        try {
            const server = window.location.origin;

            const res  = await fetch(server + '/bewerbung-erstellen?url=' + encodeURIComponent(stellenUrl));
            const data = await res.json();

            if (data.ok) {
                const box = document.getElementById('bew-box-' + firma + '-' + titel);
                box.innerHTML = `
                    <div style="padding:8px; background:#eafaf1; border-radius:4px; font-size:0.85em;">
                        📄 <a href="${server + data.lebenslauf_url}" style="color:#27ae60; margin-right:12px;">Lebenslauf.docx</a>
                        ✉️ <a href="${server + data.anschreiben_url}" style="color:#27ae60;">Anschreiben.docx</a>
                    </div>`;
            } else {
                statusEl.textContent = '❌ ' + (data.fehler || 'Unbekannter Fehler');
                statusEl.style.color = '#e74c3c';
                checkbox.disabled = false;
                checkbox.checked  = false;
            }
        } catch (e) {
            statusEl.textContent = '❌ Server nicht erreichbar';
            statusEl.style.color = '#e74c3c';
            checkbox.disabled = false;
            checkbox.checked  = false;
        }
    }
    async function stelleEinfuegen() {
       const url = document.getElementById('manuell-url').value.trim();
        const firma = document.getElementById('manuell-firma').value.trim();
        const titel = document.getElementById('manuell-titel').value.trim();
        const statusEl = document.getElementById('manuell-status');
        const output = document.getElementById('manuell-output');

        if (!url) {
            statusEl.textContent = 'Bitte eine URL eingeben.';
            statusEl.style.color = '#e74c3c';
            return;
        }

        statusEl.textContent = 'Stelle wird eingetragen...';
        statusEl.style.color = '#2980b9';

        const server = window.location.origin;

        const res = await fetch(server + '/stelle-einfuegen', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, firma, titel })
        });
        const data = await res.json();

        if (!data.ok) {
            statusEl.textContent = 'Fehler: ' + (data.fehler || 'Unbekannt');
            statusEl.style.color = '#e74c3c';
            return;
        }

        statusEl.textContent = 'Eingetragen - Pipeline laeuft...';
        statusEl.style.color = '#27ae60';
        output.style.display = 'block';
        output.textContent = '';

        const quelle = new EventSource(server + '/manuell-stream');
        quelle.onmessage = function(e) {
            if (e.data === 'FERTIG') {
                quelle.close();
                statusEl.textContent = 'Fertig - Seite wird neu geladen...';
                setTimeout(() => location.reload(), 2000);
                return;
            }
            output.textContent += e.data + '\\n';
            output.scrollTop = output.scrollHeight;
        };
        quelle.onerror = function() {
            quelle.close();
            statusEl.textContent = 'Verbindungsfehler zum Server';
            statusEl.style.color = '#e74c3c';
        };
    }

    async function neueFirmaTesten() {
        const url      = document.getElementById('firma-test-url').value.trim();
        const name     = document.getElementById('firma-test-name').value.trim();
        const checkbox = document.getElementById('firma-config-cb');
        const output   = document.getElementById('scan-output');
        const status   = document.getElementById('scan-status');

        if (!url || !name) {
            status.textContent = '⚠️ Karriere-URL und Firmenname sind Pflichtfelder';
            return;
        }

        output.style.display = 'block';
        output.textContent   = '';
        status.textContent   = '⏳ Teste ' + name + '...';

        let letzteZeile = '';

        const params = new URLSearchParams({url, firmenname: name});
        const quelle = new EventSource('/firmen-testen-stream?' + params.toString());
        quelle.onmessage = function(e) {
            if (e.data === 'FERTIG') {
                quelle.close();
                status.textContent = '✅ Test abgeschlossen';
                if (checkbox.checked && letzteZeile.includes('✅')) {
                    fetch('/firmen-config-hinzufuegen', {
                        method:  'POST',
                        headers: {'Content-Type': 'application/json'},
                        body:    JSON.stringify({firmenname: name, url})
                    }).then(r => r.json()).then(d => {
                        output.textContent += d.ok
                            ? '\\n✅ ' + name + ' zur config.txt hinzugefügt'
                            : '\\n❌ config.txt Fehler: ' + (d.fehler || '');
                        output.scrollTop = output.scrollHeight;
                    }).catch(() => {
                        output.textContent += '\\n❌ Netzwerkfehler beim Speichern in config.txt';
                        output.scrollTop = output.scrollHeight;
                    });
                }
                return;
            }
            letzteZeile = e.data;
            output.textContent += e.data + '\\n';
            output.scrollTop = output.scrollHeight;
        };
        quelle.onerror = function() {
            quelle.close();
            status.textContent = '❌ Verbindungsfehler zum Server';
        };
    }
"""


# =============================================================================
# REPORT ERSTELLEN
# =============================================================================

def erstelle_report(stellen: list, firmen_reihenfolge: list) -> str:
    datum = datetime.now().strftime("%d.%m.%Y %H:%M")

    aktive        = [s for s in stellen if not s.get("geloescht_am") and not s.get("nicht_passend")]
    neue          = [s for s in aktive  if s.get("neu")]
    nicht_passend = [s for s in stellen if s.get("nicht_passend") and not s.get("geloescht_am")]
    geloescht     = [s for s in stellen if s.get("geloescht_am")]

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>Job-Scanner Report – {datum}</title>
    <style>{CSS}</style>
    <script>{JS}</script>
</head>
<body>
    <h1>🔍 Job-Scanner Report</h1>
    <div class="summary-box">
        <strong>{len(aktive)}</strong> aktive Stellen &nbsp;|&nbsp;
        <strong>{len(neue)}</strong> neu &nbsp;|&nbsp;
        <strong>{len(nicht_passend)}</strong> nicht passend &nbsp;|&nbsp;
        <strong>{len(geloescht)}</strong> vergeben &nbsp;|&nbsp;
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

    # ── Neue Stellen ────────────────────────────────────────────────
    if neue:
        html += '<div class="firma-block">\n'
        html += f'<h2>🆕 Neue Stellen ({len(neue)})</h2>\n'
        for s in neue:
            html += stelle_zu_html(s, zeige_firma=True)
        html += '</div>\n'

    # ── Top 10 nach KI-Score ────────────────────────────────────────
    top10 = sorted(
        [s for s in aktive if (s.get("bewertung") or {}).get("score", 0) > 0],
        key=lambda s: s["bewertung"]["score"],
        reverse=True
    )[:10]
    if top10:
        html += '<div class="firma-block">\n'
        html += '<h2>⭐ Top 10 nach KI-Score</h2>\n'
        for s in top10:
            html += stelle_zu_html(s, zeige_firma=True)
        html += '</div>\n'

    # ── Pro Firma ───────────────────────────────────────────────────
    firmen_dict = {}
    for s in aktive:
        firmen_dict.setdefault(s["firma"], []).append(s)

    # Reihenfolge aus config, dann alphabetisch für unbekannte
    alle_firmen = firmen_reihenfolge + sorted(
        [f for f in firmen_dict if f not in firmen_reihenfolge]
    )

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
                    html += stelle_zu_html(s)
            if rest:
                html += '<p><strong>Weitere Treffer</strong></p>\n'
                for s in rest:
                    html += stelle_zu_html(s)
        else:
            html += '<p class="leer">Keine passenden Stellen gefunden.</p>\n'

        html += '</div>\n'

    # ── Vergangene Stellen (am Ende, eingeklappt) ───────────────────
    if geloescht:
        html += f'''<details style="margin: 15px 0;">
    <summary style="cursor:pointer; background:#e0e0e0; padding:12px 20px;
        border-radius:8px; font-weight:bold; font-size:1.05em;">
        🗑️ Vergeben / Nicht mehr verfügbar ({len(geloescht)})
    </summary>
    <div class="firma-block" style="border-radius:0 0 8px 8px; margin-top:0;">\n'''
        for s in geloescht:
            html += stelle_zu_html(s, zeige_firma=True)
        html += '</div>\n</details>\n'

    if nicht_passend:
        html += f'''<details style="margin: 15px 0;">
    <summary style="cursor:pointer; background:#fdebd0; padding:12px 20px;
        border-radius:8px; font-weight:bold; font-size:1.05em;">
        🚫 Nicht passend – Ausschlusskriterium ({len(nicht_passend)})
    </summary>
    <div class="firma-block" style="border-radius:0 0 8px 8px; margin-top:0;">\n'''
        for s in nicht_passend:
            html += stelle_zu_html(s, zeige_firma=True)
        html += '</div>\n</details>\n'

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
    datum         = datetime.now().strftime("%d.%m.%Y %H:%M")
    neue          = [s for s in stellen if s.get("neu") and not s.get("geloescht_am") and not s.get("nicht_passend")]
    nicht_passend = [s for s in stellen if s.get("nicht_passend") and not s.get("geloescht_am")]
    geloescht     = [s for s in stellen if s.get("geloescht_am")]

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>Job-Scanner Änderungen – {datum}</title>
    <style>{CSS}</style>
</head>
<body>
    <h1>🔍 Job-Scanner Änderungen</h1>
    <div class="summary-box">
        <strong>{len(neue)}</strong> neue Stellen &nbsp;|&nbsp;
        <strong>{len(nicht_passend)}</strong> nicht passend &nbsp;|&nbsp;
        <strong>{len(geloescht)}</strong> vergeben &nbsp;|&nbsp;
        {datum}
    </div>
"""

    if neue:
        html += '<div class="firma-block">\n'
        html += f'<h2>🆕 Neue Stellen ({len(neue)})</h2>\n'
        for s in neue:
            html += stelle_zu_html(s, zeige_firma=True)
        html += '</div>\n'

    if nicht_passend:
        html += '<div class="firma-block">\n'
        html += f'<h2>🚫 Nicht passend – Ausschlusskriterium ({len(nicht_passend)})</h2>\n'
        for s in nicht_passend:
            html += stelle_zu_html(s, zeige_firma=True)
        html += '</div>\n'

    if geloescht:
        html += '<div class="firma-block">\n'
        html += f'<h2>🗑️ Vergeben / Nicht mehr verfügbar ({len(geloescht)})</h2>\n'
        for s in geloescht:
            html += stelle_zu_html(s, zeige_firma=True)
        html += '</div>\n'

    html += "</body></html>"
    return html


# =============================================================================
# E-MAIL SENDEN
# =============================================================================

def sende_mail(aenderungs_html: str, config: dict):
    datum = datetime.now().strftime("%d.%m.%Y")

    neue      = aenderungs_html.count("badge-neu")
    geloescht = aenderungs_html.count("badge-weg")

    msg = MIMEMultipart()
    msg["From"]    = config["email_absender"]
    msg["To"]      = config["email_empfaenger"]
    msg["Subject"] = f"Job-Scanner {datum} – {neue} neu, {geloescht} vergeben"

    # Kurztext im Body
    body = (
        f"Job-Scanner Änderungen vom {datum}:\n\n"
        f"  🆕 Neue Stellen:       {neue}\n"
        f"  🗑️  Nicht mehr verfügbar: {geloescht}\n\n"
        f"Details im Anhang."
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # HTML als Anhang
    anhang = MIMEApplication(
        aenderungs_html.encode("utf-8"),
        Name=f"aenderungen_{datum}.html"
    )
    anhang["Content-Disposition"] = f'attachment; filename="aenderungen_{datum}.html"'
    msg.attach(anhang)

    try:
        with smtplib.SMTP("smtp.mail.me.com", 587) as server:
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
    global RASPI_IP
    RASPI_IP = config["raspi_ip"]
    stellen = lade_json(STELLEN_JSON, [])

    if not stellen:
        print("ℹ️  stellen.json ist leer – zuerst scanner.py ausführen.")
        return

    # Datenreparatur: geloescht_am in stellen.json löschen wenn bekannte_stellen aktiv zeigt
    bekannte = lade_json(BEKANNTE_JSON, {})
    repariert = 0
    for s in stellen:
        if s.get("geloescht_am") and not s.get("nicht_passend"):
            eintrag = bekannte.get(s["url"], {})
            if eintrag.get("status", 0) != 0:
                s["geloescht_am"] = None
                repariert += 1
    if repariert:
        import json as _json
        STELLEN_JSON.write_text(
            _json.dumps(stellen, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  🔧 {repariert} Stelle(n) reaktiviert (geloescht_am-Datenreparatur)")

    # Vollständigen Report erstellen
    print("  📄 Erstelle Report...")
    report_html = erstelle_report(stellen, config["firmen_reihenfolge"])
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
            sende_mail(aenderungs_html, config)
        else:
            print("  ℹ️  Keine Änderungen – keine Mail gesendet.")
    else:
        print("  ℹ️  E-Mail deaktiviert (EMAIL_AKTIV = false)")

    # neu-Flag zurücksetzen nach Report-Erstellung
    # (damit beim nächsten Lauf nur wirklich neue Stellen als NEU markiert sind)
    geaendert = 0
    for s in stellen:
        if s.get("neu"):
            s["neu"] = False
            geaendert += 1
    if geaendert:
        STELLEN_JSON.write_text(
            json.dumps(stellen, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  🔄 neu-Flag für {geaendert} Stellen zurückgesetzt")

    # Auch in Datenbank zurücksetzen
    try:
        from db import neu_flag_zuruecksetzen, erstelle_schema
        erstelle_schema()
        neu_flag_zuruecksetzen()
    except Exception as e:
        print(f"  ⚠️  Datenbank-Fehler (nicht kritisch): {e}")

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Report: {REPORT_PFAD}")
    print(f"{'='*60}\n")

    import webbrowser
    webbrowser.open(str(REPORT_PFAD))


if __name__ == "__main__":
    main()
