"""
webui.py  –  Web-Interface für den Job-Scanner
================================================
Startet einen kleinen Webserver auf Port 5000.
Liefert den Report aus und erlaubt manuellen Scan-Start per Button.

Nutzung:
  python webui.py

Erreichbar unter:
  http://localhost:5000         (lokal)
  http://<raspi-ip>:5000        (im Netzwerk)

Voraussetzungen:
  pip install flask
"""

import subprocess
import os
import re
import sys
import time
import threading
import urllib.parse
import json
from pathlib import Path
from flask import Flask, Response, send_file, request, jsonify, redirect
print("WEBUI GESTARTET - Version mit manuell-stream")
# =============================================================================
# KONFIGURATION
# =============================================================================

BASIS_PFAD   = Path(__file__).parent
REPORT_HTML  = BASIS_PFAD / "report.html"
LOG_DATEI    = BASIS_PFAD / "scan.log"
STELLEN_JSON = BASIS_PFAD / "stellen.json"
PORT         = 5000

# Globaler Status: läuft gerade ein Scan?
scan_laeuft   = False
scan_stoppen  = False       # Signal: Scan soll abgebrochen werden
laufender_prozess = None    # Aktuell laufender Subprozess

# Reihenfolge der Scripts – identisch zu start.bat
PIPELINE = [
    "scanner.py",
    "extraktor.py",
    "bewertung.py",
    "report.py",
]

# =============================================================================
# FLASK APP
# =============================================================================

app = Flask(__name__)

@app.route("/")
def index():
    """Liefert den aktuellen Report aus."""
    if REPORT_HTML.exists():
        return send_file(REPORT_HTML)
    return "<h2>⚠️ Noch kein Report vorhanden. Bitte zuerst einen Scan starten.</h2>", 404


def pipeline_im_hintergrund():
    """
    Läuft in einem eigenen Thread – komplett unabhängig vom Browser.
    Schreibt Output in scan.log statt direkt zum Browser.
    """
    global scan_laeuft, scan_stoppen, laufender_prozess
    scan_laeuft  = True
    scan_stoppen = False

    try:
        with open(LOG_DATEI, "w", encoding="utf-8") as log:
            for script in PIPELINE:
                if scan_stoppen:
                    log.write("\n⛔ Scan wurde manuell abgebrochen.\n")
                    log.flush()
                    break

                script_pfad = BASIS_PFAD / script

                if not script_pfad.exists():
                    log.write(f"⚠️  {script} nicht gefunden – übersprungen\n")
                    log.flush()
                    continue

                log.write(f"\n▶️  Starte {script} ...\n")
                log.flush()

                prozess = subprocess.Popen(
                    [sys.executable, str(script_pfad)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(BASIS_PFAD),
                    env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
                    start_new_session=True,  # Unabhängig vom Flask-Prozess
                )
                laufender_prozess = prozess

                for zeile in prozess.stdout:
                    zeile = zeile.rstrip("\n")
                    if zeile:
                        log.write(zeile + "\n")
                        log.flush()

                prozess.wait()
                laufender_prozess = None

                if scan_stoppen:
                    log.write(f"\n⛔ Scan wurde nach {script} abgebrochen.\n")
                    log.flush()
                    break

                if prozess.returncode == 0:
                    log.write(f"✅ {script} fertig\n")
                else:
                    log.write(f"❌ {script} mit Fehler beendet (Code {prozess.returncode})\n")
                log.flush()

            if not scan_stoppen:
                log.write("\nFERTIG\n")
            else:
                log.write("\nFERTIG\n")  # Stream-Ende-Signal auch beim Abbruch
            log.flush()
    finally:
        scan_laeuft       = False
        scan_stoppen      = False
        laufender_prozess = None


@app.route("/starten")
def starten():
    """Startet die Pipeline als Hintergrund-Thread und leitet zum Stream weiter."""
    global scan_laeuft
    if scan_laeuft:
        return "⚠️ Scan läuft bereits.", 409

    t = threading.Thread(target=pipeline_im_hintergrund, daemon=True)
    t.start()

    # Kurz warten damit die Logdatei angelegt wird
    time.sleep(0.3)

    return redirect("/stream")


@app.route("/stoppen")
def stoppen():
    """Bricht den laufenden Scan nach dem aktuellen Script ab."""
    global scan_stoppen, laufender_prozess
    if not scan_laeuft:
        return jsonify({"ok": False, "nachricht": "Kein Scan aktiv"}), 400
    scan_stoppen = True
    if laufender_prozess and laufender_prozess.poll() is None:
        laufender_prozess.terminate()
    return jsonify({"ok": True, "nachricht": "Scan wird abgebrochen..."})


@app.route("/stream")
def stream():
    """
    Streamt scan.log live zum Browser per SSE.
    Läuft unabhängig vom eigentlichen Scan-Prozess.
    Bricht der Browser ab: Scan läuft trotzdem weiter.
    """
    def log_lesen():
        # Warten bis Logdatei existiert
        for _ in range(20):
            if LOG_DATEI.exists():
                break
            time.sleep(0.2)
        else:
            yield "data: ⚠️ Logdatei nicht gefunden\n\n"
            return

        with open(LOG_DATEI, "r", encoding="utf-8") as f:
            while True:
                zeile = f.readline()
                if zeile:
                    zeile = zeile.rstrip("\n")
                    if zeile == "FERTIG":
                        yield "data: ✅ Pipeline abgeschlossen – Seite wird neu geladen...\n\n"
                        yield "data: FERTIG\n\n"
                        return
                    if zeile:
                        yield f"data: {zeile}\n\n"
                else:
                    time.sleep(0.3)

    return Response(log_lesen(), mimetype="text/event-stream")


# =============================================================================
# FIRMEN-LISTE & EINZELTEST
# =============================================================================

@app.route("/firmen")
def firmen_liste():
    """Gibt alle Firmennamen als JSON zurück (API-Firmen + Playwright-Firmen)."""
    namen = []

    # API-Firmen aus config.txt [api_firmen]
    try:
        inhalt = (BASIS_PFAD / "config.txt").read_text(encoding="utf-8")
        start  = inhalt.find("[api_firmen]")
        ende   = inhalt.find("[\\api_firmen]")
        if start != -1 and ende != -1:
            block = inhalt[start + len("[api_firmen]"):ende].strip()
            for f in json.loads(block):
                if f.get("name"):
                    namen.append(f["name"])
    except Exception:
        pass

    # Playwright-Firmen aus config.txt [firmen]
    try:
        start = inhalt.find("[firmen]")
        ende  = inhalt.find("[\\firmen]")
        if start != -1 and ende != -1:
            for zeile in inhalt[start + len("[firmen]"):ende].splitlines():
                z = zeile.strip()
                if z and not z.startswith("#") and "|" in z:
                    namen.append(z.split("|")[0].strip())
    except Exception:
        pass

    return jsonify(sorted(namen))


@app.route("/firma-testen")
def firma_testen():
    """Startet scanner.py --firma <name> und streamt den Output per SSE."""
    firma = request.args.get("firma", "").strip()
    if not firma:
        return "Kein firma-Parameter", 400

    def stream_firma():
        import subprocess, os
        pipeline = [
            [sys.executable, str(BASIS_PFAD / "scanner.py"), "--firma", firma],
            [sys.executable, str(BASIS_PFAD / "extraktor.py")],
            [sys.executable, str(BASIS_PFAD / "bewertung.py")],
            [sys.executable, str(BASIS_PFAD / "report.py"), "--keine-mail"],
        ]
        for cmd in pipeline:
            prozess = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(BASIS_PFAD),
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            )
            for zeile in prozess.stdout:
                zeile = zeile.rstrip("\n")
                if zeile:
                    yield f"data: {zeile}\n\n"
            prozess.wait()
            if prozess.returncode != 0:
                yield f"data: ❌ {cmd[1]} fehlgeschlagen (Code {prozess.returncode})\n\n"
                break
        yield "data: FERTIG\n\n"

    return Response(stream_firma(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# =============================================================================
# BEWERBUNG ERSTELLEN
# =============================================================================

@app.route("/bewerbung-erstellen")
def bewerbung_erstellen():
    """
    Startet anpasser.py für eine einzelne Stelle (per URL-Parameter).
    Danach ruft er bewerbung_generator.py auf um DOCX-Dateien zu erzeugen.
    Gibt JSON zurück: { ok, nachricht, lebenslauf_url, anschreiben_url }
    Aufruf: GET /bewerbung-erstellen?url=<stellenurl>
    """
    stellen_url = request.args.get("url", "")
    if not stellen_url:
        return jsonify({"ok": False, "fehler": "Kein url-Parameter übergeben"}), 400

    # Schritt 1: TXT erzeugen via anpasser
    try:
        sys.path.insert(0, str(BASIS_PFAD))
        from anpasser import passe_stelle_an
        ergebnis = passe_stelle_an(stellen_url)
    except Exception as e:
        return jsonify({"ok": False, "fehler": f"anpasser Fehler: {e}"}), 500

    if not ergebnis["ok"]:
        return jsonify(ergebnis), 500

    txt_pfad = Path(ergebnis["pfad"])

    # Dateinamen-Suffix aus Stellentitel ableiten
    try:
        from bewerbung_generator import parse_txt as _parse_txt
        _titel = _parse_txt(txt_pfad).get("titel", "Stelle")
    except Exception:
        _titel = "Stelle"
    _titel_sauber = re.sub(r'[\\/:*?"<>|]', '', _titel).strip().replace(' ', '_') or "Stelle"
    datei_suffix  = f"Sascha_Rüttiger_{_titel_sauber}"

    # Schritt 2: Lebenslauf.docx mit Tracked Changes erzeugen
    from docx_patcher import erzeuge_docx_mit_changes

    vorlage_docx = BASIS_PFAD / "lebenslauf_vorlage.docx"
    lv_pfad      = txt_pfad.parent / f"Lebenslauf_{datei_suffix}.docx"

    ok = erzeuge_docx_mit_changes(
        txt_pfad      = txt_pfad,
        vorlage_pfad  = vorlage_docx,
        ausgabe_pfad  = lv_pfad,
    )
    if not ok:
        return jsonify({"ok": False, "fehler": "DOCX-Generierung fehlgeschlagen"}), 500

    # Schritt 3: Anschreiben.docx aus der von anpasser.py erzeugten Anschreiben.txt
    as_txt          = txt_pfad.parent / "Anschreiben.txt"
    as_pfad         = txt_pfad.parent / f"Anschreiben_{datei_suffix}.docx"
    anschreiben_url = None
    try:
        if not as_txt.exists():
            raise FileNotFoundError("Anschreiben.txt nicht gefunden – anpasser.py lief nicht durch?")
        vorlage_as_docx = BASIS_PFAD / "anschreiben_vorlage.docx"
        vorlage_as_txt  = BASIS_PFAD / "anschreiben_vorlage.txt"
        ok_as = erzeuge_docx_mit_changes(
            txt_pfad         = as_txt,
            vorlage_pfad     = vorlage_as_docx,
            ausgabe_pfad     = as_pfad,
            vorlage_txt_pfad = vorlage_as_txt,
        )
        if not ok_as:
            raise RuntimeError("DOCX-Patch für Anschreiben fehlgeschlagen")
        anschreiben_url = f"/download?pfad={urllib.parse.quote(str(as_pfad))}"
    except Exception as e:
        print(f"⚠️ Anschreiben-Fehler (nicht kritisch): {e}")

    # Schritt 4: Pfade in stellen.json speichern damit report.py sie findet
    try:
        stellen = json.loads(STELLEN_JSON.read_text(encoding="utf-8")) if STELLEN_JSON.exists() else []
        for s in stellen:
            if s.get("url") == stellen_url:
                s["lebenslauf_pfad"] = str(lv_pfad)
                if as_pfad.exists():
                    s["anschreiben_pfad"] = str(as_pfad)
                break
        STELLEN_JSON.write_text(json.dumps(stellen, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠️ Konnte stellen.json nicht aktualisieren: {e}")

    # Schritt 5: Report neu generieren damit Links dauerhaft erscheinen
    subprocess.run(
        [sys.executable, str(BASIS_PFAD / "report.py"), "--keine-mail"],
        cwd=str(BASIS_PFAD),
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )

    return jsonify({
        "ok":             True,
        "nachricht":      "Bewerbungsunterlagen erstellt",
        "lebenslauf_url": f"/download?pfad={urllib.parse.quote(str(lv_pfad))}",
        "anschreiben_url": anschreiben_url,
    })


# =============================================================================
# DOWNLOAD
# =============================================================================

@app.route("/download")
def download():
    """Liefert eine Datei vom Raspi als Download."""
    pfad = urllib.parse.unquote(request.args.get("pfad", ""))
    if not pfad:
        return "Kein Pfad angegeben", 400
    p = Path(pfad)
    if not p.exists():
        return f"Datei nicht gefunden: {pfad}", 404
    bewerbungen_dir = BASIS_PFAD / "bewerbungen"
    if bewerbungen_dir not in p.parents:
        return "Zugriff verweigert", 403
    return send_file(p, as_attachment=True)


# =============================================================================
# STATUS (SQLite)
# =============================================================================

@app.route("/status", methods=["GET"])
def get_status():
    import db
    with db.verbindung() as con:
        rows = con.execute("""
            SELECT url, stufe, beworben_am, kennenlernen_am, einladung_am, ergebnis_am, kommentar
            FROM bewerbungsstatus
        """).fetchall()
    result = {}
    for r in rows:
        result[r["url"]] = dict(r)
    return jsonify(result)


@app.route("/status", methods=["POST"])
def post_status():
    data = request.get_json()
    print("STATUS POST:", data)
    if not data or "url" not in data or "feld" not in data or "wert" not in data:
        return jsonify({"fehler": "url, feld, wert erwartet"}), 400
    if data["feld"] == "stufe":
        import db
        db.upsert_bewerbungsstatus(data["url"], data["wert"])
    elif data["feld"] == "kommentar":
        import db
        con = db.verbindung()
        con.execute("""
            INSERT INTO bewerbungsstatus (url, kommentar)
            VALUES (?, ?)
            ON CONFLICT(url) DO UPDATE SET kommentar = excluded.kommentar
        """, (data["url"], data["wert"]))
        con.commit()
    return jsonify({"ok": True})

@app.route("/steckbrief-erstellen", methods=["POST"])
def steckbrief_erstellen():
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"fehler": "Parameter 'url' fehlt"}), 400

    url = data["url"]
    stellen = json.loads(STELLEN_JSON.read_text(encoding="utf-8")) if STELLEN_JSON.exists() else []
    stelle = next((s for s in stellen if s["url"] == url), None)
    if not stelle:
        return jsonify({"fehler": "Stelle nicht gefunden"}), 404

    config_pfad = BASIS_PFAD / "config.txt"
    api_key = ""
    steckbrief_prompt = ""
    aktiver_abschnitt = None
    puffer = []
    for zeile in config_pfad.read_text(encoding="utf-8").splitlines():
        z = zeile.strip()
        if z.startswith("[\\") and z.endswith("]"):
            if z[2:-1].lower() == "steckbrief_prompt":
                steckbrief_prompt = "\n".join(puffer).strip()
            aktiver_abschnitt = None
            puffer = []
            continue
        if z.startswith("[") and z.endswith("]") and not z.startswith("[\\"):
            aktiver_abschnitt = z[1:-1].lower()
            puffer = []
            continue
        if aktiver_abschnitt == "steckbrief_prompt":
            puffer.append(zeile)
        elif aktiver_abschnitt is None and z.upper().startswith("API_KEY"):
            api_key = z.split("=", 1)[1].strip()

    if not api_key or not steckbrief_prompt:
        return jsonify({"fehler": "API-Key oder Steckbrief-Prompt fehlt"}), 500

    lebenslauf_pfad = BASIS_PFAD / "lebenslauf.txt"
    lebenslauf = lebenslauf_pfad.read_text(encoding="utf-8") if lebenslauf_pfad.exists() else ""
    stellentext = stelle.get("stellentext") or stelle.get("rohtext") or ""

    prompt = (steckbrief_prompt
        .replace("{titel}", stelle.get("titel", ""))
        .replace("{firma}", stelle.get("firma", ""))
        .replace("{stellentext}", stellentext[:6000])
        .replace("{lebenslauf}", lebenslauf[:4000])
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        antwort = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = antwort.content[0].text.strip()
        text = text.removeprefix("```json").removesuffix("```").strip()
        steckbrief = json.loads(text)
    except Exception as e:
        return jsonify({"fehler": f"Claude-Fehler: {e}"}), 500

    stelle["steckbrief"] = steckbrief
    STELLEN_JSON.write_text(json.dumps(stellen, ensure_ascii=False, indent=2), encoding="utf-8")

    subprocess.run(
        [sys.executable, str(BASIS_PFAD / "report.py"), "--keine-mail"],
        cwd=str(BASIS_PFAD),
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )

    return jsonify({"ok": True, "steckbrief": steckbrief})


@app.route("/bewertung-erstellen", methods=["POST"])
def bewertung_erstellen():
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"fehler": "Parameter 'url' fehlt"}), 400

    url = data["url"]
    stellen = json.loads(STELLEN_JSON.read_text(encoding="utf-8")) if STELLEN_JSON.exists() else []
    stelle = next((s for s in stellen if s["url"] == url), None)
    if not stelle:
        return jsonify({"fehler": "Stelle nicht gefunden"}), 404

    stellentext = stelle.get("stellentext") or stelle.get("rohtext") or ""
    if not stellentext:
        return jsonify({"fehler": "Kein Stellentext vorhanden"}), 400

    config_pfad = BASIS_PFAD / "config.txt"
    api_key = ""
    prompt_vorlage = ""
    aktiver_abschnitt = None
    puffer = []
    for zeile in config_pfad.read_text(encoding="utf-8").splitlines():
        z = zeile.strip()
        if z.startswith("[\\") and z.endswith("]"):
            if z[2:-1].lower() == "prompt":
                prompt_vorlage = "\n".join(puffer).strip()
            aktiver_abschnitt = None
            puffer = []
            continue
        if z.startswith("[") and z.endswith("]") and not z.startswith("[\\"):
            aktiver_abschnitt = z[1:-1].lower()
            puffer = []
            continue
        if aktiver_abschnitt == "prompt":
            puffer.append(zeile)
        elif aktiver_abschnitt is None and z.upper().startswith("API_KEY"):
            api_key = z.split("=", 1)[1].strip()

    if not api_key or not prompt_vorlage:
        return jsonify({"fehler": "API-Key oder Prompt fehlt"}), 500

    lebenslauf_pfad = BASIS_PFAD / "lebenslauf.txt"
    lebenslauf = lebenslauf_pfad.read_text(encoding="utf-8") if lebenslauf_pfad.exists() else ""

    try:
        import anthropic
        from bewertung import bewerte_stelle
        client = anthropic.Anthropic(api_key=api_key)
        bewertung = bewerte_stelle(stellentext, lebenslauf, prompt_vorlage, client)
    except Exception as e:
        return jsonify({"fehler": f"Bewertungs-Fehler: {e}"}), 500

    if not bewertung:
        return jsonify({"fehler": "KI-Bewertung fehlgeschlagen"}), 500

    stelle["bewertung"] = bewertung
    stelle["nicht_passend"] = False  # manuelles Bewerten überschreibt Ausschluss
    STELLEN_JSON.write_text(json.dumps(stellen, ensure_ascii=False, indent=2), encoding="utf-8")

    bekannte_pfad = BASIS_PFAD / "bekannte_stellen.json"
    if bekannte_pfad.exists():
        bekannte = json.loads(bekannte_pfad.read_text(encoding="utf-8"))
        if url in bekannte:
            bekannte[url]["status"] = 4
            bekannte[url]["nicht_passend"] = False
            bekannte_pfad.write_text(json.dumps(bekannte, ensure_ascii=False, indent=2), encoding="utf-8")

    subprocess.run(
        [sys.executable, str(BASIS_PFAD / "report.py"), "--keine-mail"],
        cwd=str(BASIS_PFAD),
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )

    return jsonify({"ok": True})


@app.route("/stelle-einfuegen", methods=["POST"])
def stelle_einfuegen():
    """
    Trägt eine Stelle sofort in stellen.json, bekannte_stellen.json und DB ein.
    Kein HTTP-Fetch hier — das macht rohtext_holen.py im Pipeline-Stream.
    Erwartet JSON: { url, firma (optional) }
    Gibt zurück: { ok, fehler }
    """
    data = request.get_json()
    if not data or not data.get("url"):
        return jsonify({"ok": False, "fehler": "Kein url-Parameter übergeben"}), 400

    stellen_url = data["url"].strip()
    ist_pdf = stellen_url.lower().endswith(".pdf")
    firma       = data.get("firma", "").strip() or "Unbekannt"
    titel       = data.get("titel", "").strip() or "(manuell eingetragen)"

    from datetime import datetime
    jetzt = datetime.now().strftime("%Y-%m-%d %H:%M")

    # --- stellen.json + bekannte_stellen.json ----------------------------
    try:
        stellen_pfad  = BASIS_PFAD / "stellen.json"
        bekannte_pfad = BASIS_PFAD / "bekannte_stellen.json"

        stellen  = json.loads(stellen_pfad.read_text(encoding="utf-8"))  if stellen_pfad.exists()  else []
        bekannte = json.loads(bekannte_pfad.read_text(encoding="utf-8")) if bekannte_pfad.exists() else {}

        if any(s.get("url") == stellen_url for s in stellen):
            return jsonify({"ok": False, "fehler": "Stelle bereits vorhanden"}), 409

        rohtext = None
        if ist_pdf:
            try:
                import requests, pdfplumber, io
                r = requests.get(stellen_url, timeout=15)
                with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                    rohtext = "\n".join(p.extract_text() or "" for p in pdf.pages)
            except Exception as e:
                print(f"  ⚠️  PDF-Extraktion fehlgeschlagen: {e}")

        stellen.append({
            "url":         stellen_url,
            "firma":       firma,
            "titel":       titel,
            "treffer":     [],
            "gefunden_am": jetzt,
            "neu":         True,
            "rohtext":     rohtext,
        })
        stellen_pfad.write_text(
            json.dumps(stellen, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        bekannte[stellen_url] = {"status": 2 if rohtext else 1, "gefunden_am": jetzt}
        bekannte_pfad.write_text(
            json.dumps(bekannte, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        return jsonify({"ok": False, "fehler": f"JSON-Fehler: {e}"}), 500

    # --- SQLite-DB -------------------------------------------------------
    try:
        sys.path.insert(0, str(BASIS_PFAD))
        import db
        db.erstelle_schema()
        db.upsert_stelle({
            "url":         stellen_url,
            "firma":       firma,
            "titel":       titel,
            "treffer":     [],
            "gefunden_am": jetzt,
            "neu":         True,
            "status":      1,
        })
    except Exception as e:
        return jsonify({"ok": False, "fehler": f"Datenbankfehler: {e}"}), 500

    return jsonify({"ok": True})


@app.route("/manuell-stream")
def manuell_stream():
    global scan_laeuft

    # Safety-Reset: Falls scan_laeuft durch eine Exception stecken blieb
    if scan_laeuft:
        try:
            if not LOG_DATEI.exists():
                scan_laeuft = False  # Log nicht da – Flag war stale
            else:
                inhalt = LOG_DATEI.read_text(encoding="utf-8", errors="replace")
                log_alter = time.time() - LOG_DATEI.stat().st_mtime
                if "FERTIG" in inhalt or log_alter > 60:
                    scan_laeuft = False  # Scan beendet oder seit >60s tot
        except OSError:
            scan_laeuft = False

    if scan_laeuft:
        return "Scan läuft bereits.", 409

    TEIL_PIPELINE = ["rohtext_holen.py", "extraktor.py", "bewertung.py", "report.py"]

    def pipeline_manuell():
        global scan_laeuft
        scan_laeuft = True
        try:
            with open(LOG_DATEI, "w", encoding="utf-8") as log:
                for script in TEIL_PIPELINE:
                    script_pfad = BASIS_PFAD / script
                    log.write(f"\n Starte {script} ...\n")
                    log.flush()
                    extra_args = ["--keine-mail"] if script == "report.py" else []
                    prozess = subprocess.Popen(
                        [sys.executable, str(script_pfad)] + extra_args,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        cwd=str(BASIS_PFAD),
                        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
                        start_new_session=True,
                    )
                    for zeile in prozess.stdout:
                        zeile = zeile.rstrip("\n")
                        if zeile:
                            log.write(zeile + "\n")
                            log.flush()
                    prozess.wait()
                    if prozess.returncode == 0:
                        log.write(f"OK {script} fertig\n")
                    else:
                        log.write(f"FEHLER {script} (Code {prozess.returncode})\n")
                    log.flush()
                log.write("\nFERTIG\n")
                log.flush()
        finally:
            scan_laeuft = False

    t = threading.Thread(target=pipeline_manuell, daemon=True)
    t.start()
    time.sleep(0.3)

    # Direkt den Stream zurückgeben statt redirect
    def log_lesen():
        for _ in range(20):
            if LOG_DATEI.exists():
                break
            time.sleep(0.2)
        else:
            yield "data: Logdatei nicht gefunden\n\n"
            return

        with open(LOG_DATEI, "r", encoding="utf-8") as f:
            while True:
                zeile = f.readline()
                if zeile:
                    zeile = zeile.rstrip("\n")
                    if zeile == "FERTIG":
                        yield "data: Fertig - Seite wird neu geladen...\n\n"
                        yield "data: FERTIG\n\n"
                        return
                    if zeile:
                        yield f"data: {zeile}\n\n"
                else:
                    time.sleep(0.3)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return Response(log_lesen(), mimetype="text/event-stream", headers=headers)


# =============================================================================
# NEUE FIRMA TESTEN (SSE-Stream via Playwright)
# =============================================================================

_NAVIGATION_BEGRIFFE = {
    "impressum", "kontakt", "home", "datenschutz", "karriere", "jobs",
    "about", "über uns", "startseite", "login", "registrieren",
    "newsletter", "blog", "news", "presse", "agb", "cookies",
    "sitemap", "zurück", "weiter", "mehr", "alle stellen", "alle jobs",
    "stellenangebote", "zur übersicht", "english", "deutsch",
}


@app.route("/firmen-testen-stream")
def firmen_testen_stream():
    """SSE-Endpoint: öffnet Karriere-URL per Playwright, filtert Jobtitel-Links."""
    test_url   = request.args.get("url", "").strip()
    firmenname = request.args.get("firmenname", "").strip()
    if not test_url or not firmenname:
        return "url und firmenname erforderlich", 400

    def stream_generator():
        try:
            import platform
            from playwright.sync_api import sync_playwright

            yield f"data: ⏳ Öffne {test_url} ...\n\n"

            with sync_playwright() as pw:
                if platform.system() == "Linux":
                    browser = pw.chromium.launch(
                        headless=True,
                        executable_path="/usr/bin/chromium-browser",
                        args=["--no-sandbox", "--disable-gpu"],
                    )
                else:
                    browser = pw.chromium.launch(
                        headless=True,
                        args=["--no-sandbox", "--disable-gpu"],
                    )

                page = browser.new_page(viewport={"width": 1920, "height": 1080})
                page.goto(test_url, timeout=30000)
                page.wait_for_timeout(3000)

                link_elemente = page.query_selector_all("a")
                rohtexte = []
                pdf_links = []
                for el in link_elemente:
                    try:
                        href = el.get_attribute("href") or ""
                        t = el.inner_text().strip()
                        if href.lower().endswith(".pdf"):
                            dateiname = href.rstrip("/").split("/")[-1]
                            label = t if len(t) >= 5 else dateiname[:-4].replace("-", " ").replace("_", " ")
                            pdf_links.append(label)
                        elif t:
                            rohtexte.append(t)
                    except Exception:
                        pass

                browser.close()

            yield f"data: 🔍 {len(rohtexte)} Links + {len(pdf_links)} PDF(s) gefunden – filtere Jobtitel...\n\n"

            gesehen   = set()
            gefunden  = []
            for text in rohtexte:
                if len(text) < 15:
                    continue
                if text.lower().strip() in _NAVIGATION_BEGRIFFE:
                    continue
                if text in gesehen:
                    continue
                gesehen.add(text)
                gefunden.append(text)

            for label in pdf_links:
                if label not in gesehen:
                    gesehen.add(label)
                    gefunden.append(f"[PDF] {label}")

            if gefunden:
                for titel in gefunden:
                    yield f"data: {titel}\n\n"
                yield f"data: ✅ {len(gefunden)} Jobtitel gefunden\n\n"
            else:
                yield f"data: ❌ Keine Jobtitel gefunden\n\n"

        except Exception as e:
            yield f"data: ❌ Fehler: {e}\n\n"

        yield "data: FERTIG\n\n"

    return Response(
        stream_generator(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/firmen-config-hinzufuegen", methods=["POST"])
def firmen_config_hinzufuegen():
    """Fügt eine neue Zeile in den [firmen]-Block von config.txt ein."""
    data = request.get_json()
    if not data or not data.get("firmenname") or not data.get("url"):
        return jsonify({"ok": False, "fehler": "firmenname und url erforderlich"}), 400

    firmenname = data["firmenname"].strip()
    url        = data["url"].strip()

    try:
        config_pfad = BASIS_PFAD / "config.txt"
        inhalt      = config_pfad.read_text(encoding="utf-8")

        marker = "[\\firmen]"
        idx    = inhalt.find(marker)
        if idx == -1:
            return jsonify({"ok": False, "fehler": "[\\firmen] nicht in config.txt gefunden"}), 500

        neue_zeile = f"{firmenname:<20} | {url}\n"
        inhalt     = inhalt[:idx] + neue_zeile + inhalt[idx:]
        config_pfad.write_text(inhalt, encoding="utf-8")
        return jsonify({"ok": True})

    except Exception as e:
        return jsonify({"ok": False, "fehler": str(e)}), 500


# =============================================================================
# START
# =============================================================================

if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"  Job-Scanner Web-Interface")
    print(f"  http://localhost:{PORT}")
    print(f"  Zum Beenden: Strg+C")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)