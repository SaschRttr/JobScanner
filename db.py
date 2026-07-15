"""
db.py  –  Datenbankmodul für Job-Scanner
=========================================
Einzige Quelle der Wahrheit. stellen.json / bekannte_stellen.json
werden nur noch als lesbare Spiegel exportiert.
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import normalisiere_url
from status_def import INAKTIVE_STATUSWERTE, status_fuer_stufe


DB_PFAD = Path(__file__).parent / "jobscanner.db"


# =============================================================================
# VERBINDUNG & SCHEMA
# =============================================================================

def verbindung() -> sqlite3.Connection:
    DB_PFAD.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PFAD)
    con.row_factory = sqlite3.Row
    return con


def erstelle_schema():
    """Erstellt alle Tabellen falls nicht vorhanden."""
    with verbindung() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS stellen (
                url                  TEXT PRIMARY KEY,
                firma                TEXT NOT NULL,
                titel                TEXT NOT NULL,
                treffer              TEXT,
                gefunden_am          TEXT,
                geloescht_am         TEXT,
                neu                  INTEGER DEFAULT 1,
                rohtext              TEXT,
                stellentext          TEXT,
                status               INTEGER DEFAULT 1,
                arbeitsort           TEXT,
                standort             TEXT,
                nicht_passend        INTEGER DEFAULT 0,
                nicht_passend_grund  TEXT,
                nicht_ladbar         INTEGER DEFAULT 0,
                vergabe_status       INTEGER,
                vergaben_bestaetigt  INTEGER DEFAULT 0,
                steckbrief           TEXT,
                lebenslauf_pfad      TEXT,
                anschreiben_pfad     TEXT
            );

            CREATE TABLE IF NOT EXISTS bewertungen (
                url                    TEXT PRIMARY KEY,
                score                  INTEGER,
                score_potenzial        INTEGER,
                score_nach_anpassung   INTEGER,
                empfehlung             TEXT,
                score_begruendung      TEXT,
                staerken               TEXT,
                luecken                TEXT,
                punkteabzug            TEXT,
                schliessbare_luecken   TEXT,
                lebenslauf_anpassungen TEXT,
                profil_hinweise        TEXT,
                sprache                TEXT,
                bewertet_am            TEXT,
                FOREIGN KEY (url) REFERENCES stellen(url)
            );

            CREATE TABLE IF NOT EXISTS status_historie (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                url          TEXT NOT NULL,
                status_alt   INTEGER,
                status_neu   INTEGER,
                geaendert_am TEXT,
                FOREIGN KEY (url) REFERENCES stellen(url)
            );

            CREATE TABLE IF NOT EXISTS bewerbungsstatus (
                url              TEXT PRIMARY KEY,
                stufe            TEXT DEFAULT '',
                beworben_am      TEXT,
                kennenlernen_am  TEXT,
                einladung_am     TEXT,
                ergebnis_am      TEXT,
                kommentar        TEXT,
                FOREIGN KEY (url) REFERENCES stellen(url)
            );

            CREATE TABLE IF NOT EXISTS fahrzeit_cache (
                url          TEXT PRIMARY KEY,
                ziel         TEXT,
                genau        INTEGER DEFAULT 1,
                auto_min     INTEGER,
                auto_km      REAL,
                transit_min  INTEGER,
                abgerufen_am TEXT,
                FOREIGN KEY (url) REFERENCES stellen(url)
            );
        """)
    _migriere_schema()


def _migriere_schema():
    """Fügt neue Spalten zur bestehenden DB hinzu (idempotent)."""
    neue_spalten = [
        "ALTER TABLE stellen ADD COLUMN arbeitsort TEXT",
        "ALTER TABLE stellen ADD COLUMN standort TEXT",
        "ALTER TABLE stellen ADD COLUMN nicht_passend INTEGER DEFAULT 0",
        "ALTER TABLE stellen ADD COLUMN nicht_passend_grund TEXT",
        "ALTER TABLE stellen ADD COLUMN nicht_ladbar INTEGER DEFAULT 0",
        "ALTER TABLE stellen ADD COLUMN vergabe_status INTEGER",
        "ALTER TABLE stellen ADD COLUMN vergaben_bestaetigt INTEGER DEFAULT 0",
        "ALTER TABLE stellen ADD COLUMN steckbrief TEXT",
        "ALTER TABLE stellen ADD COLUMN lebenslauf_pfad TEXT",
        "ALTER TABLE stellen ADD COLUMN anschreiben_pfad TEXT",
        "ALTER TABLE bewerbungsstatus ADD COLUMN kommentar TEXT",
        "ALTER TABLE stellen ADD COLUMN pruef_vormerken TEXT",
        "ALTER TABLE bewerbungsstatus ADD COLUMN nicht_beworben_grund TEXT",
        "ALTER TABLE bewertungen ADD COLUMN score_nach_anpassung INTEGER",
        "ALTER TABLE bewertungen ADD COLUMN sprache TEXT",
        "ALTER TABLE bewertungen ADD COLUMN profil_hinweise TEXT",
        "ALTER TABLE stellen ADD COLUMN gemerkt TEXT",
        "ALTER TABLE bewertungen ADD COLUMN score_potenzial INTEGER",
        "ALTER TABLE bewertungen ADD COLUMN schliessbare_luecken TEXT",
        "ALTER TABLE bewertungen ADD COLUMN punkteabzug TEXT",
    ]
    with verbindung() as con:
        for sql in neue_spalten:
            try:
                con.execute(sql)
            except Exception:
                pass  # Spalte existiert bereits


# =============================================================================
# STELLEN SCHREIBEN
# =============================================================================

def upsert_stelle(s: dict):
    """Legt eine neue Stelle an oder aktualisiert eine bestehende."""
    with verbindung() as con:
        vorhandene = con.execute(
            "SELECT status FROM stellen WHERE url = ?", (s["url"],)
        ).fetchone()

        if vorhandene is None:
            con.execute("""
                INSERT INTO stellen
                    (url, firma, titel, treffer, gefunden_am, geloescht_am,
                     neu, rohtext, stellentext, status, arbeitsort, standort,
                     nicht_passend, nicht_passend_grund, nicht_ladbar,
                     vergabe_status, vergaben_bestaetigt,
                     steckbrief, lebenslauf_pfad, anschreiben_pfad)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["url"],
                s.get("firma", ""),
                s.get("titel", ""),
                json.dumps(s.get("treffer", []), ensure_ascii=False),
                s.get("gefunden_am"),
                s.get("geloescht_am"),
                1 if s.get("neu") else 0,
                s.get("rohtext"),
                s.get("stellentext"),
                s.get("status", 1),
                s.get("arbeitsort") or None,
                s.get("standort") or None,
                1 if s.get("nicht_passend") else 0,
                s.get("nicht_passend_grund") or None,
                1 if s.get("nicht_ladbar") else 0,
                s.get("vergabe_status"),
                1 if s.get("vergaben_bestaetigt") else 0,
                json.dumps(s["steckbrief"], ensure_ascii=False) if s.get("steckbrief") else None,
                s.get("lebenslauf_pfad"),
                s.get("anschreiben_pfad"),
            ))
        else:
            felder = []
            werte  = []

            for feld in ["rohtext", "stellentext"]:
                if feld in s:
                    felder.append(f"{feld} = ?")
                    werte.append(s[feld])

            if s.get("geloescht_am") is not None:
                felder.append("geloescht_am = ?")
                werte.append(s["geloescht_am"])
            elif "geloescht_am" in s and s["geloescht_am"] is None:
                felder.append("geloescht_am = ?")
                werte.append(None)

            if "neu" in s:
                felder.append("neu = ?")
                werte.append(1 if s["neu"] else 0)

            if "arbeitsort" in s:
                felder.append("arbeitsort = ?")
                werte.append(s["arbeitsort"] or None)

            if "standort" in s:
                felder.append("standort = ?")
                werte.append(s["standort"] or None)

            if "nicht_passend" in s:
                felder.append("nicht_passend = ?")
                werte.append(1 if s["nicht_passend"] else 0)
                if not s["nicht_passend"] and "nicht_passend_grund" not in s:
                    felder.append("nicht_passend_grund = ?")
                    werte.append(None)

            if "nicht_passend_grund" in s:
                felder.append("nicht_passend_grund = ?")
                werte.append(s["nicht_passend_grund"] or None)

            if "nicht_ladbar" in s:
                felder.append("nicht_ladbar = ?")
                werte.append(1 if s["nicht_ladbar"] else 0)

            if "vergabe_status" in s:
                felder.append("vergabe_status = ?")
                werte.append(s["vergabe_status"])

            if "vergaben_bestaetigt" in s:
                felder.append("vergaben_bestaetigt = ?")
                werte.append(1 if s["vergaben_bestaetigt"] else 0)

            if "steckbrief" in s:
                felder.append("steckbrief = ?")
                werte.append(
                    json.dumps(s["steckbrief"], ensure_ascii=False) if s["steckbrief"] else None
                )

            if "lebenslauf_pfad" in s:
                felder.append("lebenslauf_pfad = ?")
                werte.append(s["lebenslauf_pfad"])

            if "anschreiben_pfad" in s:
                felder.append("anschreiben_pfad = ?")
                werte.append(s["anschreiben_pfad"])

            if "pruef_vormerken" in s:
                felder.append("pruef_vormerken = ?")
                werte.append(s["pruef_vormerken"])  # None löscht, Timestamp setzt

            if "gemerkt" in s:
                felder.append("gemerkt = ?")
                werte.append(s["gemerkt"])  # None löscht, Timestamp setzt

            if "titel" in s and s["titel"]:
                felder.append("titel = ?")
                werte.append(s["titel"])

            if "firma" in s and s["firma"]:
                felder.append("firma = ?")
                werte.append(s["firma"])

            if "status" in s:
                alter_status = vorhandene["status"]
                neuer_status = s["status"]
                if alter_status != neuer_status:
                    con.execute("""
                        INSERT INTO status_historie
                            (url, status_alt, status_neu, geaendert_am)
                        VALUES (?, ?, ?, ?)
                    """, (s["url"], alter_status, neuer_status,
                          datetime.now().strftime("%Y-%m-%d %H:%M")))
                felder.append("status = ?")
                werte.append(neuer_status)

            if felder:
                werte.append(s["url"])
                con.execute(
                    f"UPDATE stellen SET {', '.join(felder)} WHERE url = ?",
                    werte
                )


def upsert_bewertung(url: str, b: dict):
    """Legt eine Bewertung an oder überschreibt sie."""
    with verbindung() as con:
        con.execute("""
            INSERT INTO bewertungen
                (url, score, score_potenzial, score_nach_anpassung, empfehlung, score_begruendung,
                 staerken, luecken, punkteabzug, schliessbare_luecken, lebenslauf_anpassungen,
                 profil_hinweise, sprache, bewertet_am)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                score                  = excluded.score,
                score_potenzial        = excluded.score_potenzial,
                score_nach_anpassung   = excluded.score_nach_anpassung,
                empfehlung             = excluded.empfehlung,
                score_begruendung      = excluded.score_begruendung,
                staerken               = excluded.staerken,
                luecken                = excluded.luecken,
                punkteabzug            = excluded.punkteabzug,
                schliessbare_luecken   = excluded.schliessbare_luecken,
                lebenslauf_anpassungen = excluded.lebenslauf_anpassungen,
                profil_hinweise        = excluded.profil_hinweise,
                sprache                = excluded.sprache,
                bewertet_am            = excluded.bewertet_am
        """, (
            url,
            b.get("score", 0),
            b.get("score_potenzial"),
            b.get("score_nach_anpassung"),
            b.get("empfehlung", ""),
            b.get("score_begruendung", ""),
            json.dumps(b.get("staerken", []),               ensure_ascii=False),
            json.dumps(b.get("luecken", []),                ensure_ascii=False),
            json.dumps(b.get("punkteabzug", []),            ensure_ascii=False),
            json.dumps(b.get("schliessbare_luecken", []),   ensure_ascii=False),
            json.dumps(b.get("lebenslauf_anpassungen", []), ensure_ascii=False),
            json.dumps(b.get("profil_hinweise", []),        ensure_ascii=False),
            b.get("sprache") or "de",
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        ))


def reset_stelle_fuer_neuverarbeitung(url: str):
    """Setzt rohtext/stellentext auf NULL und status=1, damit die Pipeline neu verarbeitet."""
    with verbindung() as con:
        alter = con.execute("SELECT status FROM stellen WHERE url = ?", (url,)).fetchone()
        if alter and alter["status"] != 1:
            con.execute("""
                INSERT INTO status_historie (url, status_alt, status_neu, geaendert_am)
                VALUES (?, ?, 1, ?)
            """, (url, alter["status"], datetime.now().strftime("%Y-%m-%d %H:%M")))
        con.execute("""
            UPDATE stellen
            SET rohtext = NULL, stellentext = NULL, status = 1,
                nicht_passend = 0, nicht_passend_grund = NULL, nicht_ladbar = 0
            WHERE url = ?
        """, (url,))


def status_bei_vergabe(url: str, con) -> int:
    """Bestimmt den korrekten Vergabe-Status anhand der Bewerbungsstufe."""
    row = con.execute(
        "SELECT stufe FROM bewerbungsstatus WHERE url = ?", (url,)
    ).fetchone()
    stufe = (row["stufe"] if row else "") or ""
    return status_fuer_stufe(stufe)


_STATUS_PRIO = {6: 10, 5: 8, 4: 7, 7: 6, 8: 5, 9: 4, 10: 3, 3: 3, 2: 2, 1: 1, 0: 0}


def repariere_inkonsistente_status():
    """
    Läuft bei jedem vergaben_check-Start. Behebt:
    1. status=0 + aktive Bewerbung → status=7/8 (Ghosting/Absage)
    2. status in (1-5) + stufe='beworben' + nicht gelöscht → status=6
    2b. vergaben_bestaetigt=1 + wieder aktiv → zurück auf Vergabe-Status
    3. URL-Duplikate (trailing slash) → beste Version behalten
    4. Titel+Firma-Duplikate → beste Version behalten, Bewerbungsstatus migrieren
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with verbindung() as con:
        # 1. status=0 mit aktiver Bewerbung → korrekten Vergabe-Status setzen
        betroffene = con.execute("""
            SELECT s.url, b.stufe FROM stellen s
            LEFT JOIN bewerbungsstatus b ON s.url = b.url
            WHERE s.status = 0 AND s.geloescht_am IS NOT NULL
              AND b.stufe IN ('beworben','kennenlernen','einladung','absage','zusage')
        """).fetchall()
        for r in betroffene:
            stufe = r["stufe"] or ""
            neu_st = 7 if stufe in ("beworben", "kennenlernen", "einladung") else 8
            con.execute("UPDATE stellen SET status = ? WHERE url = ?", (neu_st, r["url"]))
            con.execute("""
                INSERT INTO status_historie (url, status_alt, status_neu, geaendert_am)
                VALUES (?, 0, ?, ?)
            """, (r["url"], neu_st, ts))

        # 2. status in (1-5) + beworben + nicht gelöscht → status=6
        con.execute("""
            UPDATE stellen SET status = 6
            WHERE status IN (1,2,3,4,5) AND geloescht_am IS NULL
              AND url IN (SELECT url FROM bewerbungsstatus WHERE stufe = 'beworben')
        """)

        # 2b. Bestätigt vergebene Stellen, die fälschlich wieder aktiv sind
        #     (vergaben_bestaetigt=1 setzt nur der Vergaben-Check nach doppelter
        #     Bestätigung bzw. eine manuelle Markierung; eine reguläre
        #     Reaktivierung durch scanner.py setzt das Flag zurück). Solche
        #     Zombie-Stellen entstehen z.B. durch Alt-Skripte, die Status/
        #     geloescht_am ohne Historie überschrieben haben.
        betroffene = con.execute("""
            SELECT url, status FROM stellen
            WHERE vergaben_bestaetigt = 1 AND geloescht_am IS NULL
              AND status IN (1,2,3,4,5)
        """).fetchall()
        for r in betroffene:
            neu_st = status_bei_vergabe(r["url"], con)
            con.execute(
                "UPDATE stellen SET status = ?, geloescht_am = ?, pruef_vormerken = NULL WHERE url = ?",
                (neu_st, ts, r["url"]))
            con.execute("""
                INSERT INTO status_historie (url, status_alt, status_neu, geaendert_am)
                VALUES (?, ?, ?, ?)
            """, (r["url"], r["status"], neu_st, ts))
            print(f"  🧹 Bestätigt vergebene Stelle war wieder aktiv → Status {neu_st}: {r['url'][:80]}")

        # 3+4. Duplikate bereinigen (trailing slash + gleicher Titel+Firma)
        alle = con.execute("""
            SELECT url, status, geloescht_am, gefunden_am FROM stellen
        """).fetchall()

        # Trailing-Slash-Duplikate
        url_set = {r["url"] for r in alle}
        for r in list(alle):
            url = r["url"]
            alt = url.rstrip("/") if url.endswith("/") else url + "/"
            if alt in url_set and url in url_set:
                prio_orig = (_STATUS_PRIO.get(r["status"], 0), 0 if r["geloescht_am"] else 1)
                r_alt = next(x for x in alle if x["url"] == alt)
                prio_alt  = (_STATUS_PRIO.get(r_alt["status"], 0), 0 if r_alt["geloescht_am"] else 1)
                loeschen = url if prio_alt > prio_orig else alt
                con.execute("DELETE FROM stellen WHERE url = ?", (loeschen,))
                con.execute("DELETE FROM bewertungen WHERE url = ?", (loeschen,))
                con.execute("DELETE FROM bewerbungsstatus WHERE url = ?", (loeschen,))
                con.execute("DELETE FROM fahrzeit_cache WHERE url = ?", (loeschen,))
                url_set.discard(loeschen)

        # URL-Encoding-Duplikate (z.B. %28 vs %2528)
        alle = con.execute("SELECT url, status, geloescht_am, gefunden_am FROM stellen").fetchall()
        norm_gruppen: dict = {}
        for r in alle:
            norm = normalisiere_url(r["url"])
            norm_gruppen.setdefault(norm, []).append(r)
        for norm, gruppe in norm_gruppen.items():
            if len(gruppe) < 2:
                continue
            def _prio(r):
                live = 1 if not r["geloescht_am"] else 0
                return (live, _STATUS_PRIO.get(r["status"], 0), r["gefunden_am"] or "")
            behalten = max(gruppe, key=_prio)
            for r in gruppe:
                if r["url"] == behalten["url"]:
                    continue
                con.execute("DELETE FROM stellen WHERE url = ?", (r["url"],))
                con.execute("DELETE FROM bewertungen WHERE url = ?", (r["url"],))
                con.execute("DELETE FROM bewerbungsstatus WHERE url = ?", (r["url"],))
                con.execute("DELETE FROM fahrzeit_cache WHERE url = ?", (r["url"],))
                print(f"  🧹 URL-Encoding-Duplikat entfernt: {r['url'][:80]}")

        # Titel+Firma-Duplikate
        groups = con.execute("""
            SELECT lower(titel) as lt, lower(firma) as lf, COUNT(*) as cnt
            FROM stellen GROUP BY lower(titel), lower(firma) HAVING cnt > 1
        """).fetchall()
        for g in groups:
            rows = con.execute("""
                SELECT url, status, geloescht_am, gefunden_am FROM stellen
                WHERE lower(titel) = ? AND lower(firma) = ?
            """, (g["lt"], g["lf"])).fetchall()

            def _prio(r):
                live = 1 if not r["geloescht_am"] else 0
                return (live, _STATUS_PRIO.get(r["status"], 0), r["gefunden_am"] or "")

            behalten  = max(rows, key=_prio)
            loeschen  = [r for r in rows if r["url"] != behalten["url"]]
            for r in loeschen:
                # Bewerbungsstatus migrieren falls vorhanden
                bew = con.execute(
                    "SELECT stufe FROM bewerbungsstatus WHERE url = ?", (r["url"],)
                ).fetchone()
                if bew and bew["stufe"]:
                    ex = con.execute(
                        "SELECT stufe FROM bewerbungsstatus WHERE url = ?", (behalten["url"],)
                    ).fetchone()
                    if not ex or not ex["stufe"]:
                        con.execute(
                            "UPDATE bewerbungsstatus SET url = ? WHERE url = ?",
                            (behalten["url"], r["url"])
                        )
                con.execute("DELETE FROM stellen WHERE url = ?", (r["url"],))
                con.execute("DELETE FROM bewertungen WHERE url = ?", (r["url"],))
                con.execute("DELETE FROM bewerbungsstatus WHERE url = ?", (r["url"],))
                con.execute("DELETE FROM fahrzeit_cache WHERE url = ?", (r["url"],))


def neu_flag_zuruecksetzen():
    with verbindung() as con:
        con.execute("UPDATE stellen SET neu = 0 WHERE neu = 1")


def upsert_bewerbungsstatus(url: str, stufe: str):
    jetzt = datetime.now().strftime("%Y-%m-%d %H:%M")
    with verbindung() as con:
        row = con.execute(
            "SELECT * FROM bewerbungsstatus WHERE url = ?", (url,)
        ).fetchone()

        if row is None:
            felder = {
                "beworben_am":     jetzt if stufe == "beworben"            else None,
                "kennenlernen_am": jetzt if stufe == "kennenlernen"        else None,
                "einladung_am":    jetzt if stufe == "einladung"           else None,
                "ergebnis_am":     jetzt if stufe in ("zusage", "absage") else None,
            }
            con.execute("""
                INSERT INTO bewerbungsstatus
                    (url, stufe, beworben_am, kennenlernen_am, einladung_am, ergebnis_am)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (url, stufe,
                  felder["beworben_am"], felder["kennenlernen_am"],
                  felder["einladung_am"], felder["ergebnis_am"]))
        else:
            updates = {"stufe": stufe}
            if stufe == "beworben"               and not row["beworben_am"]:
                updates["beworben_am"]     = jetzt
            if stufe == "kennenlernen"            and not row["kennenlernen_am"]:
                updates["kennenlernen_am"] = jetzt
            if stufe == "einladung"               and not row["einladung_am"]:
                updates["einladung_am"]    = jetzt
            if stufe in ("zusage", "absage")      and not row["ergebnis_am"]:
                updates["ergebnis_am"]     = jetzt
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            con.execute(
                f"UPDATE bewerbungsstatus SET {set_clause} WHERE url = ?",
                (*updates.values(), url)
            )


# =============================================================================
# FAHRZEIT-CACHE
# =============================================================================

def hole_fahrzeit_cache(url: str) -> dict | None:
    with verbindung() as con:
        r = con.execute(
            "SELECT ziel, genau, auto_min, auto_km, transit_min FROM fahrzeit_cache WHERE url = ?", (url,)
        ).fetchone()
    if r is None:
        return None
    return {
        "ziel":        r["ziel"],
        "genau":       bool(r["genau"]),
        "auto_min":    r["auto_min"],
        "auto_km":     r["auto_km"],
        "transit_min": r["transit_min"],
    }


def speichere_fahrzeit_cache(url: str, daten: dict):
    with verbindung() as con:
        con.execute("""
            INSERT INTO fahrzeit_cache (url, ziel, genau, auto_min, auto_km, transit_min, abgerufen_am)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                ziel         = excluded.ziel,
                genau        = excluded.genau,
                auto_min     = excluded.auto_min,
                auto_km      = excluded.auto_km,
                transit_min  = excluded.transit_min,
                abgerufen_am = excluded.abgerufen_am
        """, (
            url,
            daten.get("ziel"),
            1 if daten.get("genau") else 0,
            daten.get("auto_min"),
            daten.get("auto_km"),
            daten.get("transit_min"),
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        ))


# =============================================================================
# STELLEN LESEN
# =============================================================================

def lade_alle_stellen() -> list[dict]:
    """Gibt alle Stellen inkl. Bewertung als Liste von dicts zurück."""
    with verbindung() as con:
        rows = con.execute("""
            SELECT
                s.url, s.firma, s.titel, s.treffer,
                s.gefunden_am, s.geloescht_am, s.neu,
                s.rohtext, s.stellentext, s.status,
                s.arbeitsort, s.standort, s.nicht_passend, s.nicht_passend_grund, s.nicht_ladbar,
                s.vergabe_status, s.vergaben_bestaetigt,
                s.steckbrief, s.lebenslauf_pfad, s.anschreiben_pfad, s.pruef_vormerken, s.gemerkt,
                b.score, b.score_potenzial, b.score_nach_anpassung, b.empfehlung, b.score_begruendung,
                b.staerken, b.luecken, b.punkteabzug, b.schliessbare_luecken, b.lebenslauf_anpassungen,
                b.profil_hinweise, b.sprache, b.bewertet_am
            FROM stellen s
            LEFT JOIN bewertungen b ON s.url = b.url
            ORDER BY s.gefunden_am DESC
        """).fetchall()

    ergebnis = []
    for r in rows:
        bewertung = None
        if r["score"] is not None:
            bewertung = {
                "score":                  r["score"],
                "score_potenzial":        r["score_potenzial"],
                "score_nach_anpassung":   r["score_nach_anpassung"],
                "empfehlung":             r["empfehlung"],
                "score_begruendung":      r["score_begruendung"],
                "staerken":               json.loads(r["staerken"] or "[]"),
                "luecken":                json.loads(r["luecken"] or "[]"),
                "punkteabzug":            json.loads(r["punkteabzug"] or "[]"),
                "schliessbare_luecken":   json.loads(r["schliessbare_luecken"] or "[]"),
                "lebenslauf_anpassungen": json.loads(r["lebenslauf_anpassungen"] or "[]"),
                "profil_hinweise":        json.loads(r["profil_hinweise"] or "[]"),
                "sprache":                r["sprache"] or "de",
            }
        steckbrief = None
        if r["steckbrief"]:
            try:
                steckbrief = json.loads(r["steckbrief"])
            except Exception:
                steckbrief = r["steckbrief"]

        ergebnis.append({
            "url":                 r["url"],
            "firma":               r["firma"],
            "titel":               r["titel"],
            "treffer":             json.loads(r["treffer"] or "[]"),
            "gefunden_am":         r["gefunden_am"],
            "geloescht_am":        r["geloescht_am"],
            "neu":                 bool(r["neu"]),
            "rohtext":             r["rohtext"],
            "stellentext":         r["stellentext"],
            "status":              r["status"],
            "arbeitsort":          r["arbeitsort"] or "",
            "standort":            r["standort"] or "",
            "nicht_passend":       bool(r["nicht_passend"]),
            "nicht_passend_grund": r["nicht_passend_grund"] or "",
            "nicht_ladbar":        bool(r["nicht_ladbar"]),
            "vergabe_status":      r["vergabe_status"],
            "vergaben_bestaetigt": bool(r["vergaben_bestaetigt"]),
            "pruef_vormerken":     r["pruef_vormerken"],
            "gemerkt":             r["gemerkt"],
            "steckbrief":          steckbrief,
            "lebenslauf_pfad":     r["lebenslauf_pfad"],
            "anschreiben_pfad":    r["anschreiben_pfad"],
            "bewertung":           bewertung,
        })
    return ergebnis


def lade_bekannte_dict() -> dict:
    """Gibt {url: {status, gefunden_am, geloescht_am, nicht_passend, vergaben_bestaetigt, pruef_vormerken}} zurück."""
    with verbindung() as con:
        rows = con.execute("""
            SELECT url, status, gefunden_am, geloescht_am, nicht_passend, vergaben_bestaetigt, pruef_vormerken
            FROM stellen
        """).fetchall()
    return {
        r["url"]: {
            "status":              r["status"],
            "gefunden_am":         r["gefunden_am"] or "",
            "geloescht_am":        r["geloescht_am"],
            "nicht_passend":       bool(r["nicht_passend"]),
            "vergaben_bestaetigt": bool(r["vergaben_bestaetigt"]),
            "pruef_vormerken":     r["pruef_vormerken"],
        }
        for r in rows
    }


def status_von(url: str) -> int | None:
    with verbindung() as con:
        r = con.execute(
            "SELECT status FROM stellen WHERE url = ?", (url,)
        ).fetchone()
    return r["status"] if r else None


# =============================================================================
# JSON-EXPORT (Spiegel der DB)
# =============================================================================

def exportiere_stellen_json(pfad: Path):
    """Schreibt stellen.json als lesbaren Spiegel der DB."""
    stellen = lade_alle_stellen()
    pfad.write_text(json.dumps(stellen, ensure_ascii=False, indent=2), encoding="utf-8")


def exportiere_bekannte_json(pfad: Path):
    """Schreibt bekannte_stellen.json als lesbaren Spiegel der DB."""
    with verbindung() as con:
        rows = con.execute("""
            SELECT url, status, gefunden_am, geloescht_am, nicht_passend
            FROM stellen
        """).fetchall()
    result = {}
    for r in rows:
        eintrag = {
            "status":       r["status"],
            "gefunden_am":  r["gefunden_am"] or "",
            "geloescht_am": r["geloescht_am"],
        }
        if r["nicht_passend"]:
            eintrag["nicht_passend"] = True
        result[r["url"]] = eintrag
    pfad.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


# =============================================================================
# SYNC: stellen.json → Datenbank (Einmalig / Notfall)
# =============================================================================

def sync_von_json(stellen: list):
    """Importiert alle Einträge aus stellen.json in die Datenbank."""
    erstelle_schema()
    neu = 0
    aktualisiert = 0

    for s in stellen:
        vorher = status_von(s["url"])
        upsert_stelle({
            **s,
            "status": _status_aus_dict(s),
        })
        if s.get("bewertung"):
            upsert_bewertung(s["url"], s["bewertung"])

        if vorher is None:
            neu += 1
        else:
            aktualisiert += 1

    print(f"  Sync: {neu} neu, {aktualisiert} aktualisiert")


def _status_aus_dict(s: dict) -> int:
    if s.get("geloescht_am"):
        return 0
    if s.get("bewertung"):
        score = (s["bewertung"] or {}).get("score", 0)
        return 4 if score >= 70 else 5
    if s.get("stellentext"):
        return 3
    if s.get("rohtext"):
        return 2
    return 1


# =============================================================================
# STATISTIK
# =============================================================================

def statistik() -> dict:
    inaktiv_sql = ",".join(str(s) for s in INAKTIVE_STATUSWERTE)
    with verbindung() as con:
        gesamt   = con.execute("SELECT COUNT(*) FROM stellen").fetchone()[0]
        aktiv    = con.execute(f"SELECT COUNT(*) FROM stellen WHERE status NOT IN ({inaktiv_sql})").fetchone()[0]
        vergeben = con.execute(f"SELECT COUNT(*) FROM stellen WHERE status IN ({inaktiv_sql})").fetchone()[0]
        bewertet = con.execute("SELECT COUNT(*) FROM bewertungen").fetchone()[0]
        beworben = con.execute("SELECT COUNT(*) FROM stellen WHERE status = 6").fetchone()[0]
        verg_bew = con.execute("SELECT COUNT(*) FROM stellen WHERE status = 7").fetchone()[0]
        absagen  = con.execute("SELECT COUNT(*) FROM stellen WHERE status = 8").fetchone()[0]
        verg_nie = con.execute("SELECT COUNT(*) FROM stellen WHERE status = 9").fetchone()[0]
        top      = con.execute(f"""
            SELECT s.firma, s.titel, s.url, b.score, b.empfehlung
            FROM stellen s
            JOIN bewertungen b ON s.url = b.url
            WHERE s.status NOT IN ({inaktiv_sql})
            ORDER BY b.score DESC
            LIMIT 10
        """).fetchall()
        pro_firma = con.execute(f"""
            SELECT firma, COUNT(*) as anzahl
            FROM stellen
            WHERE status NOT IN ({inaktiv_sql})
            GROUP BY firma
            ORDER BY anzahl DESC
        """).fetchall()

    return {
        "gesamt":    gesamt,
        "aktiv":     aktiv,
        "vergeben":  vergeben,
        "bewertet":  bewertet,
        "beworben":  beworben,
        "verg_bew":  verg_bew,
        "absagen":   absagen,
        "verg_nie":  verg_nie,
        "top10":     [dict(r) for r in top],
        "pro_firma": [dict(r) for r in pro_firma],
    }


# =============================================================================
# KOMMANDOZEILE: python db.py
# =============================================================================

if __name__ == "__main__":
    import sys

    STELLEN_JSON = Path(__file__).parent / "stellen.json"

    if "--sync" in sys.argv:
        if not STELLEN_JSON.exists():
            print(f"stellen.json nicht gefunden: {STELLEN_JSON}")
            sys.exit(1)
        stellen = json.loads(STELLEN_JSON.read_text(encoding="utf-8"))
        print(f"  Importiere {len(stellen)} Eintraege...")
        sync_von_json(stellen)
        print("  Migration abgeschlossen")

    elif "--stats" in sys.argv:
        erstelle_schema()
        stats = statistik()
        print(f"\n  Gesamt:   {stats['gesamt']}")
        print(f"  Aktiv:    {stats['aktiv']}")
        print(f"  Vergeben: {stats['vergeben']}")
        print(f"  Bewertet: {stats['bewertet']}")
        print(f"\n  Top 10:")
        for s in stats["top10"]:
            print(f"    {s['score']:3d}%  {s['firma']}: {s['titel'][:50]}")

    elif "--export" in sys.argv:
        erstelle_schema()
        basis = Path(__file__).parent
        exportiere_stellen_json(basis / "stellen.json")
        exportiere_bekannte_json(basis / "bekannte_stellen.json")
        print("  JSON-Spiegel exportiert")

    else:
        erstelle_schema()
        print("  Nutzung:")
        print("    python db.py --sync    # stellen.json -> Datenbank importieren")
        print("    python db.py --stats   # Statistik anzeigen")
        print("    python db.py --export  # JSON-Spiegel aus DB generieren")
