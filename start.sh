#!/bin/bash
# Job-Scanner Pipeline
cd "$(dirname "$0")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# Log-Datei begrenzen (falls stdout per '>> log.txt' umgeleitet wird):
# wird sie zu groß, auf die letzten 5MB kürzen, bevor weiter angehängt wird.
LOG_MAX_BYTES=$((5 * 1024 * 1024))
LOG_ZIEL="$(readlink -f /proc/self/fd/1 2>/dev/null)"
if [ -n "$LOG_ZIEL" ] && [ -f "$LOG_ZIEL" ]; then
    LOG_GROESSE=$(stat -c%s "$LOG_ZIEL" 2>/dev/null || echo 0)
    if [ "$LOG_GROESSE" -gt "$LOG_MAX_BYTES" ]; then
        # In-place kürzen (gleiche Inode!), sonst schreibt der bereits offene
        # append-Filehandle des aufrufenden Prozesses (z.B. cron) weiter in die alte Datei.
        tail -c "$LOG_MAX_BYTES" "$LOG_ZIEL" > "$LOG_ZIEL.tmp"
        : > "$LOG_ZIEL"
        cat "$LOG_ZIEL.tmp" >> "$LOG_ZIEL"
        rm -f "$LOG_ZIEL.tmp"
    fi
fi

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
