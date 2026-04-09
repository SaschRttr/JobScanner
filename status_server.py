"""
status_server.py  –  Job-Scanner Statusserver
===============================================
Speichert Beworben-Status und Notizen serverseitig in status.json.
Liefert angepasste Lebensläufe zum Download aus.
Läuft dauerhaft auf dem Raspi, ersetzt localStorage im Browser.

Starten:
    python status_server.py

Autostart via cron:
    crontab -e
    @reboot cd /pfad/zum/projekt && python status_server.py >> status_server.log 2>&1 &

Endpunkte:
    GET  /              → gibt report.html aus
    GET  /status        → gibt komplette status.json zurück
    POST /status        → speichert { url, feld, wert } in status.json
    GET  /lebenslauf?firma=X&titel=Y  → Lebenslauf.txt als Download
"""

import json
from pathlib import Path
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

BASIS_PFAD      = Path(__file__).parent
STATUS_JSON     = BASIS_PFAD / "status.json"
REPORT_HTML     = BASIS_PFAD / "report.html"
BEWERBUNGEN_DIR = BASIS_PFAD / "bewerbungen"

app = Flask(__name__)
CORS(app)  # Erlaubt Browser-Zugriff von beliebiger Herkunft


def lade_status() -> dict:
    if STATUS_JSON.exists():
        try:
            return json.loads(STATUS_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def speichere_status(status: dict):
    STATUS_JSON.write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@app.route("/")
def index():
    if REPORT_HTML.exists():
        return send_file(REPORT_HTML)
    return "report.html nicht gefunden – zuerst report.py ausführen.", 404


@app.route("/status", methods=["GET"])
def get_status():
    return jsonify(lade_status())


@app.route("/status", methods=["POST"])
def post_status():
    data = request.get_json()
    if not data or "url" not in data or "feld" not in data or "wert" not in data:
        return jsonify({"fehler": "Fehlende Felder: url, feld, wert erwartet"}), 400

    status = lade_status()
    url  = data["url"]
    feld = data["feld"]
    wert = data["wert"]

    if url not in status:
        status[url] = {}
    status[url][feld] = wert

    speichere_status(status)
    return jsonify({"ok": True})


@app.route("/lebenslauf")
def get_lebenslauf():
    firma = request.args.get("firma", "")
    titel = request.args.get("titel", "")
    if not firma or not titel:
        return "Parameter fehlen: firma und titel erwartet", 400
    pfad = BEWERBUNGEN_DIR / firma / titel / "Lebenslauf.txt"
    if not pfad.exists():
        return f"Nicht gefunden: {pfad}", 404
    return send_file(pfad, as_attachment=True, download_name=f"Lebenslauf_{firma}.txt")


if __name__ == "__main__":
    print("=" * 50)
    print("  Job-Scanner Statusserver gestartet")
    print(f"  http://0.0.0.0:5001")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5001, debug=False)
