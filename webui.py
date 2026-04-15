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

BASIS_PFAD  = Path(__file__).parent
REPORT_HTML = BASIS_PFAD / "report.html"
LOG_DATEI   = BASIS_PFAD / "scan.log"
PORT        = 5000

# Globaler Status: läuft gerade ein Scan?
scan_laeuft = False

# Reihenfolge der Scripts – identisch zu start.bat
PIPELINE = [
    "scanner.py",
    "extraktor.py",
    "bewertung.py",
    "report.py",
    "anpasser.py",
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
    global scan_laeuft
    scan_laeuft = True

    try:
        with open(LOG_DATEI, "w", encoding="utf-8") as log:
            for script in PIPELINE:
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

                for zeile in prozess.stdout:
                    zeile = zeile.rstrip("\n")
                    if zeile:
                        log.write(zeile + "\n")
                        log.flush()

                prozess.wait()

                if prozess.returncode == 0:
                    log.write(f"✅ {script} fertig\n")
                else:
                    log.write(f"❌ {script} mit Fehler beendet (Code {prozess.returncode})\n")
                log.flush()

            log.write("\nFERTIG\n")
            log.flush()
    finally:
        scan_laeuft = False


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

    # Schritt 2: DOCX erzeugen
    generator_pfad = BASIS_PFAD / "bewerbung_generator.py"
    if not generator_pfad.exists():
        return jsonify({"ok": False, "fehler": "bewerbung_generator.py nicht gefunden"}), 500

    proc = subprocess.run(
        [sys.executable, str(generator_pfad), str(txt_pfad)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(BASIS_PFAD),
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )

    if proc.returncode != 0:
        print("GENERATOR FEHLER:", proc.stderr)
        print("GENERATOR STDOUT:", proc.stdout)
        return jsonify({"ok": False, "fehler": f"Generator Fehler: {proc.stderr[:500]}"}), 500

    ordner  = txt_pfad.parent
    lv_pfad = ordner / "Lebenslauf.docx"
    as_pfad = ordner / "Anschreiben.docx"

    # Schritt 3: Report neu generieren damit Links dauerhaft erscheinen
    subprocess.run(
        [sys.executable, str(BASIS_PFAD / "report.py")],
        cwd=str(BASIS_PFAD),
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )

    return jsonify({
        "ok": True,
        "nachricht": "Bewerbungsunterlagen erstellt",
        "lebenslauf_url":  f"/download?pfad={urllib.parse.quote(str(lv_pfad))}",
        "anschreiben_url": f"/download?pfad={urllib.parse.quote(str(as_pfad))}",
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
                    prozess = subprocess.Popen(
                        [sys.executable, str(script_pfad)],
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
# START
# =============================================================================

if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"  Job-Scanner Web-Interface")
    print(f"  http://localhost:{PORT}")
    print(f"  Zum Beenden: Strg+C")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)