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
from pathlib import Path
from flask import Flask, Response, send_file

# =============================================================================
# KONFIGURATION
# =============================================================================

BASIS_PFAD  = Path(__file__).parent
REPORT_HTML = BASIS_PFAD / "report.html"
PORT        = 5000

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


@app.route("/starten")
def starten():
    """
    Startet die Pipeline und streamt den Output live zum Browser.
    Nutzt SSE (Server-Sent Events) – der Browser bekommt jede
    Ausgabezeile sofort, ohne die Seite neu zu laden.
    """
    def pipeline_ausfuehren():
        for script in PIPELINE:
            script_pfad = BASIS_PFAD / script

            if not script_pfad.exists():
                yield f"data: ⚠️  {script} nicht gefunden – übersprungen\n\n"
                continue

            yield f"data: \n\n"
            yield f"data: ▶️  Starte {script} ...\n\n"

            prozess = subprocess.Popen(
                [sys.executable, str(script_pfad)],
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

            if prozess.returncode == 0:
                yield f"data: ✅ {script} fertig\n\n"
            else:
                yield f"data: ❌ {script} mit Fehler beendet (Code {prozess.returncode})\n\n"

        yield "data: \n\n"
        yield "data: ✅ Pipeline abgeschlossen – Seite wird neu geladen...\n\n"
        yield "data: FERTIG\n\n"  # Signal für Browser: jetzt neu laden

    return Response(pipeline_ausfuehren(), mimetype="text/event-stream")


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
