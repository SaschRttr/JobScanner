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
import json
import time
import threading
import urllib.parse
from pathlib import Path
from flask import Flask, Response, send_file, jsonify


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
                start_new_session=True,
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

    scan_laeuft = False


@app.route("/starten")
def starten():
    """Startet die Pipeline als Hintergrund-Thread und leitet zum Stream weiter."""
    global scan_laeuft
    if scan_laeuft:
        return "⚠️ Scan läuft bereits.", 409

    t = threading.Thread(target=pipeline_im_hintergrund, daemon=True)
    t.start()

    time.sleep(0.3)

    from flask import redirect
    return redirect("/stream")


@app.route("/stream")
def stream():
    """
    Streamt scan.log live zum Browser per SSE.
    Läuft unabhängig vom eigentlichen Scan-Prozess.
    Bricht der Browser ab: Scan läuft trotzdem weiter.
    """
    def log_lesen():
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
# BEWERBUNG ERSTELLEN  (Checkbox im Report triggert diese Route)
# =============================================================================

@app.route("/bewerbung-erstellen")
def bewerbung_erstellen():
    """
    Startet anpasser.py für eine einzelne Stelle (per URL-Parameter).
    Danach ruft er bewerbung_generator.py auf um DOCX-Dateien zu erzeugen.
    Gibt JSON zurück: { ok, nachricht, lebenslauf_url, anschreiben_url }
    Aufruf: GET /bewerbung-erstellen?url=<stellenurl>
    """
    from flask import request as flask_req

    stellen_url = urllib.parse.unquote(flask_req.args.get("url", ""))
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


@app.route("/download")
@app.route("/status", methods=["GET"])
def get_status():
    import db
    with db.verbindung() as con:
        rows = con.execute("""
            SELECT url, stufe, beworben_am, kennenlernen_am, einladung_am, ergebnis_am
            FROM bewerbungsstatus
        """).fetchall()
    result = {}
    for r in rows:
        result[r["url"]] = dict(r)
    return jsonify(result)


@app.route("/status", methods=["POST"])
def post_status():
    from flask import request as flask_req
    import db
    data = flask_req.get_json()
    print("STATUS POST:", data)
    if not data or "url" not in data or "feld" not in data or "wert" not in data:
        return jsonify({"fehler": "url, feld, wert erwartet"}), 400
    if data["feld"] == "stufe":
        db.upsert_bewerbungsstatus(data["url"], data["wert"])
    return jsonify({"ok": True})
def download():
    """Liefert eine Datei vom Raspi als Download."""
    from flask import request as flask_req
    pfad = urllib.parse.unquote(flask_req.args.get("pfad", ""))
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
# START
# =============================================================================

if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"  Job-Scanner Web-Interface")
    print(f"  http://localhost:{PORT}")
    print(f"  Zum Beenden: Strg+C")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
