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
STELLEN_JSON    = BASIS_PFAD / "stellen.json"
CONFIG_PFAD     = BASIS_PFAD / "config.txt"
LEBENSLAUF_TXT  = BASIS_PFAD / "lebenslauf.txt"

app = Flask(__name__)
CORS(app)  # Erlaubt Browser-Zugriff von beliebiger Herkunft


def lade_stellen() -> list:
    if STELLEN_JSON.exists():
        try:
            return json.loads(STELLEN_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def lade_steckbrief_config() -> dict:
    result = {"api_key": "", "steckbrief_prompt": ""}
    if not CONFIG_PFAD.exists():
        return result
    aktiver_abschnitt = None
    puffer = []
    for zeile in CONFIG_PFAD.read_text(encoding="utf-8").splitlines():
        z = zeile.strip()
        if z.startswith("[\\") and z.endswith("]"):
            abschnitt = z[2:-1].lower()
            if abschnitt == "steckbrief_prompt":
                result["steckbrief_prompt"] = "\n".join(puffer).strip()
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
            result["api_key"] = z.split("=", 1)[1].strip()
    return result


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


@app.route("/steckbrief-erstellen", methods=["POST"])
def post_steckbrief():
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"fehler": "Parameter 'url' fehlt"}), 400

    url = data["url"]
    stellen = lade_stellen()
    stelle = next((s for s in stellen if s["url"] == url), None)
    if not stelle:
        return jsonify({"fehler": "Stelle nicht gefunden"}), 404

    config = lade_steckbrief_config()
    if not config["api_key"]:
        return jsonify({"fehler": "Kein API-Key in config.txt"}), 500
    if not config["steckbrief_prompt"]:
        return jsonify({"fehler": "Kein [steckbrief_prompt] in config.txt"}), 500

    lebenslauf = LEBENSLAUF_TXT.read_text(encoding="utf-8") if LEBENSLAUF_TXT.exists() else ""
    stellentext = stelle.get("stellentext") or stelle.get("rohtext") or ""

    prompt = (config["steckbrief_prompt"]
        .replace("{titel}", stelle.get("titel", ""))
        .replace("{firma}", stelle.get("firma", ""))
        .replace("{stellentext}", stellentext[:6000])
        .replace("{lebenslauf}", lebenslauf[:4000])
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config["api_key"])
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
    STELLEN_JSON.write_text(
        json.dumps(stellen, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify({"ok": True, "steckbrief": steckbrief})


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
