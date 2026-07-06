#!/bin/bash
# Job-Scanner Pipeline
cd "$(dirname "$0")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "=== Pipeline gestartet ==="

source venv/bin/activate
if [ -z "$VIRTUAL_ENV" ]; then
    log "❌ venv/bin/activate fehlgeschlagen – breche ab (sonst läuft alles mit System-Python ohne playwright/anthropic)"
    exit 1
fi

log "Starte scanner.py"
python scanner.py
log "scanner.py fertig (Exit-Code $?)"

log "Starte rohtext_holen.py"
python rohtext_holen.py
log "rohtext_holen.py fertig (Exit-Code $?)"

log "Starte vergaben_check.py"
python vergaben_check.py
log "vergaben_check.py fertig (Exit-Code $?)"

log "Starte extraktor.py"
python extraktor.py
log "extraktor.py fertig (Exit-Code $?)"

log "Starte bewertung.py"
python bewertung.py
log "bewertung.py fertig (Exit-Code $?)"

log "Starte report.py"
python report.py
log "report.py fertig (Exit-Code $?)"

log "=== Pipeline beendet ==="
