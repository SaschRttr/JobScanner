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
from pathlib import Path
from flask import Flask, Response, send_file

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

    # Browser direkt auf den Stream umleiten
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
                    # Noch keine neue Zeile – kurz warten
                    time.sleep(0.3)

    return Response(log_lesen(), mimetype="text/event-stream")


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