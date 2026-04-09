"""
db.py  –  Datenbankmodul für Job-Scanner
=========================================
Schreibt und liest Job-Daten aus einer SQLite-Datenbank.
Wird von scanner.py, bewertung.py und report.py aufgerufen.

Datenbank: ~/Documents/Python/Jobsuche/jobscanner.db
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path


DB_PFAD = Path(__file__).parent / "jobscanner.db"


# =============================================================================
# VERBINDUNG & SCHEMA
# =============================================================================

def verbindung() -> sqlite3.Connection:
    DB_PFAD.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PFAD)
    con.row_factory = sqlite3.Row  # Zugriff per Spaltenname
    return con


def erstelle_schema():
    """Erstellt alle Tabellen falls nicht vorhanden."""
    with verbindung() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS stellen (
                url             TEXT PRIMARY KEY,
                firma           TEXT NOT NULL,
                titel           TEXT NOT NULL,
                treffer         TEXT,           -- JSON-Array
                gefunden_am     TEXT,
                geloescht_am    TEXT,
                neu             INTEGER DEFAULT 1,  -- 1=ja, 0=nein
                rohtext         TEXT,
                stellentext     TEXT,
                status          INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS bewertungen (
                url                    TEXT PRIMARY KEY,
                score                  INTEGER,
                empfehlung             TEXT,
                score_begruendung      TEXT,
                staerken               TEXT,   -- JSON-Array
                luecken                TEXT,   -- JSON-Array
                lebenslauf_anpassungen TEXT,   -- JSON-Array
                bewertet_am            TEXT,
                FOREIGN KEY (url) REFERENCES stellen(url)
            );

            CREATE TABLE IF NOT EXISTS status_historie (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT NOT NULL,
                status_alt  INTEGER,
                status_neu  INTEGER,
                geaendert_am TEXT,
                FOREIGN KEY (url) REFERENCES stellen(url)
            );

            CREATE TABLE IF NOT EXISTS bewerbungsstatus (
                url              TEXT PRIMARY KEY,
                stufe            TEXT DEFAULT '',     -- beworben/kennenlernen/einladung/zusage/absage
                beworben_am      TEXT,
                kennenlernen_am  TEXT,
                einladung_am     TEXT,
                ergebnis_am      TEXT,
                FOREIGN KEY (url) REFERENCES stellen(url)
            );
        """)
    print(f"  🗄️  Datenbank bereit: {DB_PFAD}")


# =============================================================================
# STELLEN SCHREIBEN
# =============================================================================

def upsert_stelle(s: dict):
    """
    Legt eine neue Stelle an oder aktualisiert eine bestehende.
    Felder die None sind werden nicht überschrieben.
    """
    with verbindung() as con:
        # Existiert die Stelle schon?
        vorhandene = con.execute(
            "SELECT status FROM stellen WHERE url = ?", (s["url"],)
        ).fetchone()

        if vorhandene is None:
            # Neu einfügen
            con.execute("""
                INSERT INTO stellen
                    (url, firma, titel, treffer, gefunden_am, geloescht_am,
                     neu, rohtext, stellentext, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ))
        else:
            # Nur geänderte Felder aktualisieren
            felder = []
            werte  = []

            if s.get("rohtext") is not None:
                felder.append("rohtext = ?")
                werte.append(s["rohtext"])
            if s.get("stellentext") is not None:
                felder.append("stellentext = ?")
                werte.append(s["stellentext"])
            if s.get("geloescht_am") is not None:
                felder.append("geloescht_am = ?")
                werte.append(s["geloescht_am"])
            if "neu" in s:
                felder.append("neu = ?")
                werte.append(1 if s["neu"] else 0)
            if "status" in s:
                # Status-Historie schreiben
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
                (url, score, empfehlung, score_begruendung,
                 staerken, luecken, lebenslauf_anpassungen, bewertet_am)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                score                  = excluded.score,
                empfehlung             = excluded.empfehlung,
                score_begruendung      = excluded.score_begruendung,
                staerken               = excluded.staerken,
                luecken                = excluded.luecken,
                lebenslauf_anpassungen = excluded.lebenslauf_anpassungen,
                bewertet_am            = excluded.bewertet_am
        """, (
            url,
            b.get("score", 0),
            b.get("empfehlung", ""),
            b.get("score_begruendung", ""),
            json.dumps(b.get("staerken", []),               ensure_ascii=False),
            json.dumps(b.get("luecken", []),                ensure_ascii=False),
            json.dumps(b.get("lebenslauf_anpassungen", []), ensure_ascii=False),
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        ))


def stelle_als_geloescht_markieren(url: str, zeitstempel: str):
    with verbindung() as con:
        alter_status = con.execute(
            "SELECT status FROM stellen WHERE url = ?", (url,)
        ).fetchone()

        if alter_status:
            con.execute("""
                INSERT INTO status_historie
                    (url, status_alt, status_neu, geaendert_am)
                VALUES (?, ?, 0, ?)
            """, (url, alter_status["status"], zeitstempel))

        con.execute("""
            UPDATE stellen
            SET status = 0, geloescht_am = ?, neu = 0
            WHERE url = ?
        """, (zeitstempel, url))


def neu_flag_zuruecksetzen():
    """Setzt neu=0 für alle Stellen — nach Report-Erstellung aufrufen."""
    with verbindung() as con:
        con.execute("UPDATE stellen SET neu = 0 WHERE neu = 1")


def upsert_bewerbungsstatus(url: str, stufe: str):
    """
    Speichert die Bewerbungsstufe für eine Stelle.
    Setzt den Timestamp der jeweiligen Stufe nur beim ersten Mal.
    Stufen: beworben | kennenlernen | einladung | zusage | absage | (leer = zurücksetzen)
    """
    jetzt = datetime.now().strftime("%Y-%m-%d %H:%M")
    with verbindung() as con:
        row = con.execute(
            "SELECT * FROM bewerbungsstatus WHERE url = ?", (url,)
        ).fetchone()

        if row is None:
            # Neu anlegen
            felder = {
                "beworben_am":     jetzt if stufe == "beworben"              else None,
                "kennenlernen_am": jetzt if stufe == "kennenlernen"          else None,
                "einladung_am":    jetzt if stufe == "einladung"             else None,
                "ergebnis_am":     jetzt if stufe in ("zusage", "absage")   else None,
            }
            con.execute("""
                INSERT INTO bewerbungsstatus
                    (url, stufe, beworben_am, kennenlernen_am, einladung_am, ergebnis_am)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (url, stufe,
                  felder["beworben_am"], felder["kennenlernen_am"],
                  felder["einladung_am"], felder["ergebnis_am"]))
        else:
            # Stufe immer aktualisieren, Timestamps nur beim ersten Mal setzen
            updates = {"stufe": stufe}
            if stufe == "beworben"                    and not row["beworben_am"]:
                updates["beworben_am"]     = jetzt
            if stufe == "kennenlernen"                and not row["kennenlernen_am"]:
                updates["kennenlernen_am"] = jetzt
            if stufe == "einladung"                   and not row["einladung_am"]:
                updates["einladung_am"]    = jetzt
            if stufe in ("zusage", "absage")          and not row["ergebnis_am"]:
                updates["ergebnis_am"]     = jetzt

            set_clause = ", ".join(f"{k} = ?" for k in updates)
            con.execute(
                f"UPDATE bewerbungsstatus SET {set_clause} WHERE url = ?",
                (*updates.values(), url)
            )


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
                b.score, b.empfehlung, b.score_begruendung,
                b.staerken, b.luecken, b.lebenslauf_anpassungen,
                b.bewertet_am
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
                "empfehlung":             r["empfehlung"],
                "score_begruendung":      r["score_begruendung"],
                "staerken":               json.loads(r["staerken"] or "[]"),
                "luecken":                json.loads(r["luecken"] or "[]"),
                "lebenslauf_anpassungen": json.loads(r["lebenslauf_anpassungen"] or "[]"),
            }
        ergebnis.append({
            "url":          r["url"],
            "firma":        r["firma"],
            "titel":        r["titel"],
            "treffer":      json.loads(r["treffer"] or "[]"),
            "gefunden_am":  r["gefunden_am"],
            "geloescht_am": r["geloescht_am"],
            "neu":          bool(r["neu"]),
            "rohtext":      r["rohtext"],
            "stellentext":  r["stellentext"],
            "status":       r["status"],
            "bewertung":    bewertung,
        })
    return ergebnis


def url_bekannt(url: str) -> bool:
    with verbindung() as con:
        r = con.execute(
            "SELECT 1 FROM stellen WHERE url = ?", (url,)
        ).fetchone()
    return r is not None


def status_von(url: str) -> int | None:
    with verbindung() as con:
        r = con.execute(
            "SELECT status FROM stellen WHERE url = ?", (url,)
        ).fetchone()
    return r["status"] if r else None


def alle_aktiven_urls() -> set:
    with verbindung() as con:
        rows = con.execute(
            "SELECT url FROM stellen WHERE status != 0"
        ).fetchall()
    return {r["url"] for r in rows}


# =============================================================================
# SYNC: stellen.json → Datenbank
# =============================================================================

def sync_von_json(stellen: list):
    """
    Importiert alle Einträge aus stellen.json in die Datenbank.
    Nützlich für die einmalige Migration vom alten System.
    """
    erstelle_schema()
    neu = 0
    aktualisiert = 0

    for s in stellen:
        vorher = status_von(s["url"])
        upsert_stelle({
            "url":          s["url"],
            "firma":        s.get("firma", ""),
            "titel":        s.get("titel", ""),
            "treffer":      s.get("treffer", []),
            "gefunden_am":  s.get("gefunden_am"),
            "geloescht_am": s.get("geloescht_am"),
            "neu":          s.get("neu", False),
            "rohtext":      s.get("rohtext"),
            "stellentext":  s.get("stellentext"),
            "status":       _status_aus_dict(s),
        })
        if s.get("bewertung"):
            upsert_bewertung(s["url"], s["bewertung"])

        if vorher is None:
            neu += 1
        else:
            aktualisiert += 1

    print(f"  🗄️  Sync: {neu} neu, {aktualisiert} aktualisiert")


def _status_aus_dict(s: dict) -> int:
    """Leitet den Status-Code aus einem stellen.json-Eintrag ab."""
    if s.get("geloescht_am"):
        return 0
    if s.get("bewertung"):
        return 4
    if s.get("stellentext"):
        return 3
    if s.get("rohtext"):
        return 2
    return 1


# =============================================================================
# STATISTIK (für Dashboard)
# =============================================================================

def statistik() -> dict:
    with verbindung() as con:
        gesamt   = con.execute("SELECT COUNT(*) FROM stellen").fetchone()[0]
        aktiv    = con.execute("SELECT COUNT(*) FROM stellen WHERE status != 0").fetchone()[0]
        vergeben = con.execute("SELECT COUNT(*) FROM stellen WHERE status = 0").fetchone()[0]
        bewertet = con.execute("SELECT COUNT(*) FROM bewertungen").fetchone()[0]
        top      = con.execute("""
            SELECT s.firma, s.titel, s.url, b.score, b.empfehlung
            FROM stellen s
            JOIN bewertungen b ON s.url = b.url
            WHERE s.status != 0
            ORDER BY b.score DESC
            LIMIT 10
        """).fetchall()

        pro_firma = con.execute("""
            SELECT firma, COUNT(*) as anzahl
            FROM stellen
            WHERE status != 0
            GROUP BY firma
            ORDER BY anzahl DESC
        """).fetchall()

    return {
        "gesamt":    gesamt,
        "aktiv":     aktiv,
        "vergeben":  vergeben,
        "bewertet":  bewertet,
        "top10":     [dict(r) for r in top],
        "pro_firma": [dict(r) for r in pro_firma],
    }


# =============================================================================
# KOMMANDOZEILE: python db.py
# =============================================================================

if __name__ == "__main__":
    import sys
    from pathlib import Path

    STELLEN_JSON = Path(__file__).parent / "stellen.json"

    if "--sync" in sys.argv:
        # Einmalige Migration: stellen.json → DB
        if not STELLEN_JSON.exists():
            print(f"❌ stellen.json nicht gefunden: {STELLEN_JSON}")
            sys.exit(1)
        stellen = json.loads(STELLEN_JSON.read_text(encoding="utf-8"))
        print(f"  📥 Importiere {len(stellen)} Einträge...")
        sync_von_json(stellen)
        print("  ✅ Migration abgeschlossen")

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

    else:
        erstelle_schema()
        print("  Nutzung:")
        print("    python db.py --sync    # stellen.json → Datenbank importieren")
        print("    python db.py --stats   # Statistik anzeigen")
