"""
status_def.py  –  Zentrale Status-Definitionen für den Job-Scanner
====================================================================
Einzige Quelle für Status-Nummern, Labels, Farben und abgeleitete Mengen.
Wird von db.py, report.py, dashboard.py, webui.py und vergaben_check.py genutzt.

Status  Bedeutung
0       Stelle vergeben / nicht mehr erreichbar
1       Nur Link gefunden, noch kein Text geholt
2       Rohtext geholt, kein Volltext extrahiert
3       Stellentext extrahiert, noch nicht bewertet
4       KI bewertet → Score ≥ 70%, bewerben empfohlen
5       KI bewertet → Score < 70%, nicht bewerben
6       Beworben, Stelle noch aktiv
7       Beworben, Stelle weg / vergeben (Ghosting)
8       Absage erhalten
9       Vergeben, nie beworben (per HTTP bestätigt)
10      Nicht beworben (manuell entschieden)
11      Grenzfall: Score im Unsicherheitsband um den 70%-Cutoff, manuell zu prüfen
"""

STATUS_LABELS = {
    0: "vergeben",
    1: "Link",
    2: "Rohtext",
    3: "Stellentext",
    4: "bewerben",
    5: "nicht bewerben",
    6: "Beworben, aktiv",
    7: "Beworben, Ghosting",
    8: "Absage erhalten",
    9: "Vergeben, nie beworben",
    10: "nicht beworben",
    11: "Grenzfall",
}

STATUS_EMOJIS = {
    4: "📋", 5: "👎", 6: "✅", 7: "👻", 8: "❌", 9: "🗑️", 10: "🚫", 11: "⚖️",
}

STATUS_FARBEN = {
    4: "#3498db", 5: "#e67e22", 6: "#27ae60",
    7: "#f39c12", 8: "#e74c3c", 9: "#95a5a6", 10: "#c0392b", 11: "#8e44ad",
}

# Stellen mit diesen Status gelten als inaktiv (vergeben/abgeschlossen)
INAKTIVE_STATUSWERTE = (0, 7, 8, 9, 10)

# Stellen mit diesen Status sind noch nicht KI-bewertet
UNBEWERTETE_STATUSWERTE = (1, 2, 3)

# Status-Werte, für die der Report Filter-Buttons anbietet
FILTER_STATUS_VALS = (11, 4, 5, 6, 7, 8, 9, 10)


def status_fuer_stufe(stufe: str) -> int:
    """Bestimmt den Vergabe-Status anhand der Bewerbungsstufe:
    lief eine Bewerbung → 7 (Ghosting), gab es ein Ergebnis → 8, sonst → 9."""
    if stufe in ("beworben", "kennenlernen", "einladung"):
        return 7
    if stufe in ("absage", "zusage"):
        return 8
    return 9
