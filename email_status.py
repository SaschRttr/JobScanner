"""
email_status.py  –  E-Mail-basierte Bewerbungsstatus-Erkennung
==============================================================
Durchsucht das iCloud-Postfach nach Mails von Unternehmen aus der Datenbank
und setzt den Bewerbungsstatus automatisch (beworben / kennenlernen /
einladung / zusage / absage).

Nutzung:
    python email_status.py           # Live-Update der Datenbank
    python email_status.py --dry-run # Nur anzeigen, nichts speichern
    python email_status.py --alle    # Auch Stellen ohne Bewerbungsstatus prüfen
"""

import imaplib
import email
import email.header
import email.message
import sys
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import db

# =============================================================================
# KONFIGURATION
# =============================================================================

CONFIG_PFAD = Path(__file__).parent / "config.txt"
IMAP_HOST   = "imap.mail.me.com"
IMAP_PORT   = 993

# Stufen-Rangordnung  (höhere Zahl = höhere Stufe)
STUFEN_RANG = {
    "":            0,
    "beworben":    1,
    "kennenlernen":2,
    "einladung":   3,
    "zusage":      4,
    "absage":      4,   # Terminal, gleichwertig mit Zusage
}

# Keywords für jede Stufe  (alles kleingeschrieben)
KEYWORDS: dict[str, list[str]] = {
    "absage": [
        "leider", "absage", "nicht berücksichtigen", "haben uns für andere",
        "anderen kandidaten", "andere bewerber", "nicht weiterverfolgen",
        "kein match", "nicht in frage", "bedauern", "nicht erfolgreich",
        "abgesagt", "unfortunately", "not moving forward", "not selected",
        "we regret", "decided not to", "nicht weiterkommen",
        "ohne berücksichtigung", "keine stelle",
    ],
    "zusage": [
        "herzlichen glückwunsch", "zusage", "vertragsangebot", "offer letter",
        "job offer", "wir freuen uns ihnen mitteilen", "angebot unterbreiten",
        "willkommen im team", "welcome to the team", "congratulations",
    ],
    "einladung": [
        "vorstellungsgespräch", "einladen zum gespräch", "einladung zum interview",
        "interview einladen", "zu einem gespräch einladen", "persönliches gespräch",
        "zweites gespräch", "assessment center", "technical interview",
        "face-to-face", "on-site interview", "eingeladen",
    ],
    "kennenlernen": [
        "kennenlernen", "kurzes telefonat", "telefongespräch vereinbaren",
        "erstes gespräch", "screening call", "phone screen", "intro call",
        "kurzer call", "telefoninterview", "videocall", "video call",
    ],
    "beworben": [
        "bewerbung erhalten", "eingang ihrer bewerbung", "eingangsbestätigung",
        "bewerbungseingang", "we received your application",
        "your application has been received", "application confirmed",
        "bestätigen den eingang", "ihre bewerbung ist eingegangen",
        "danke für ihre bewerbung", "thank you for applying",
        "thank you for your application", "thank you for your interest in",
        "your application at", "application documents",
        "ihre bewerbungsunterlagen",
    ],
}

# Prioritätsreihenfolge beim Klassifizieren (höchste zuerst prüfen)
PRUEFREIHENFOLGE = ["absage", "zusage", "einladung", "kennenlernen", "beworben"]


# =============================================================================
# CONFIG LESEN
# =============================================================================

def lade_config() -> dict:
    if not CONFIG_PFAD.exists():
        print(f"❌  config.txt nicht gefunden: {CONFIG_PFAD}")
        sys.exit(1)

    result = {
        "email_absender": "",
        "email_passwort": "",
    }

    for zeile in CONFIG_PFAD.read_text(encoding="utf-8").splitlines():
        z = zeile.strip()
        if z.startswith("#") or not z:
            continue
        if z.upper().startswith("EMAIL_ABSENDER"):
            result["email_absender"] = z.split("=", 1)[1].strip()
        elif z.upper().startswith("EMAIL_PASSWORT"):
            result["email_passwort"] = z.split("=", 1)[1].strip()

    if not result["email_absender"] or not result["email_passwort"]:
        print("❌  E-Mail-Zugangsdaten fehlen in config.txt")
        sys.exit(1)
    return result


# =============================================================================
# IMAP-VERBINDUNG
# =============================================================================

def imap_verbinden(email_adresse: str, passwort: str) -> imaplib.IMAP4_SSL:
    print(f"  Verbinde mit {IMAP_HOST}:{IMAP_PORT} ...")
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(email_adresse, passwort)
    print(f"  Eingeloggt als {email_adresse}")
    return mail


# =============================================================================
# E-MAIL HILFSFUNKTIONEN
# =============================================================================

def decode_header_wert(wert: str | None) -> str:
    """Dekodiert MIME-enkodierte Header-Werte zu einem lesbaren String."""
    if not wert:
        return ""
    teile = email.header.decode_header(wert)
    ergebnis = []
    for teil, kodierung in teile:
        if isinstance(teil, bytes):
            ergebnis.append(teil.decode(kodierung or "utf-8", errors="replace"))
        else:
            ergebnis.append(str(teil))
    return " ".join(ergebnis)


def mail_text_extrahieren(msg: email.message.Message) -> str:
    """Extrahiert den Plaintext-Body einer E-Mail (maximal 2000 Zeichen)."""
    if msg.is_multipart():
        for teil in msg.walk():
            ct = teil.get_content_type()
            cd = str(teil.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset = teil.get_content_charset() or "utf-8"
                try:
                    return teil.get_payload(decode=True).decode(charset, errors="replace")[:2000]
                except Exception:
                    return ""
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            return msg.get_payload(decode=True).decode(charset, errors="replace")[:2000]
        except Exception:
            return ""
    return ""


def klassifiziere_mail(betreff: str, absender: str, body: str) -> str | None:
    """
    Gibt die erkannte Bewerbungsstufe zurück, oder None wenn keine Zuordnung möglich.
    Reihenfolge: absage > zusage > einladung > kennenlernen > beworben
    """
    text = (betreff + " " + absender + " " + body).lower()
    for stufe in PRUEFREIHENFOLGE:
        for keyword in KEYWORDS[stufe]:
            if keyword in text:
                return stufe
    return None


# =============================================================================
# STELLEN AUS DATENBANK
# =============================================================================

def lade_stellen_mit_status() -> list[dict]:
    """Lädt alle Stellen mit ihrem aktuellen Bewerbungsstatus."""
    with db.verbindung() as con:
        rows = con.execute("""
            SELECT
                s.url,
                s.firma,
                s.titel,
                COALESCE(bs.stufe, '') AS stufe,
                bs.beworben_am,
                bs.kennenlernen_am,
                bs.einladung_am,
                bs.ergebnis_am
            FROM stellen s
            LEFT JOIN bewerbungsstatus bs ON s.url = bs.url
            WHERE s.status != 0
            ORDER BY s.firma
        """).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# MAILBOX DURCHSUCHEN
# =============================================================================

def _letzter_scan_datum() -> str:
    """
    Gibt das Datum des letzten Scanner-Laufs als IMAP-Datumsstring zurück.
    Fallback: 10 Tage zurück.
    """
    fallback = (datetime.now() - timedelta(days=10)).strftime("%d-%b-%Y")
    try:
        with db.verbindung() as con:
            row = con.execute(
                "SELECT MAX(gefunden_am) AS letzter FROM stellen"
            ).fetchone()
        if row and row["letzter"]:
            letzter = datetime.strptime(row["letzter"][:10], "%Y-%m-%d")
            print(f"  Suche Mails seit letztem Scan: {letzter.strftime('%d.%m.%Y')}")
            return letzter.strftime("%d-%b-%Y")
    except Exception:
        pass
    print(f"  Kein Scan-Datum gefunden, Fallback: letzte 10 Tage")
    return fallback


def _raw_aus_fetch(msg_daten: list) -> bytes | None:
    """Extrahiert die rohen Mail-Bytes aus einer imaplib-Fetch-Antwort."""
    for teil in msg_daten:
        if isinstance(teil, tuple) and len(teil) >= 2 and isinstance(teil[1], bytes):
            return teil[1]
    return None


def lade_aktuelle_headers(mail: imaplib.IMAP4_SSL, seit_datum: str) -> list[dict]:
    """
    Lädt NUR die Headers (From, Subject, Date) aller Mails seit seit_datum.
    Sehr schnell weil keine Bodies übertragen werden.
    """
    from email.utils import parsedate_to_datetime
    seit_dt = datetime.strptime(seit_datum, "%d-%b-%Y")

    try:
        _, daten = mail.search(None, f'SINCE "{seit_datum}"')
    except Exception:
        return []

    if not daten or not daten[0]:
        return []

    ids = daten[0].split()
    print(f"  {len(ids)} Mail(s) seit {seit_datum} in INBOX", flush=True)

    headers = []
    for mail_id in ids:
        try:
            _, hdrs = mail.fetch(mail_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw = _raw_aus_fetch(hdrs)
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            datum_str = msg.get("Date", "")
            # Noch einmal Python-seitig prüfen (SINCE auf iCloud manchmal ungenau)
            try:
                mail_dt = parsedate_to_datetime(datum_str).replace(tzinfo=None)
                if mail_dt < seit_dt:
                    continue
            except Exception:
                pass
            headers.append({
                "id":      mail_id,
                "from":    decode_header_wert(msg.get("From", "")).lower(),
                "subject": decode_header_wert(msg.get("Subject", "")).lower(),
                "date":    datum_str,
            })
        except Exception:
            continue

    return headers


def hole_mail_body(mail: imaplib.IMAP4_SSL, mail_id: bytes) -> str:
    """Lädt den Body einer einzelnen Mail (max. 50 KB)."""
    try:
        _, msg_daten = mail.fetch(mail_id, "(BODY.PEEK[]<0.50000>)")
        raw = _raw_aus_fetch(msg_daten)
        if not raw:
            return ""
        msg = email.message_from_bytes(raw)
        return mail_text_extrahieren(msg)
    except Exception:
        return ""


def suche_mails_fuer_firma(
    firma: str,
    titel_liste: list[str],
    alle_headers: list[dict],
    mail: imaplib.IMAP4_SSL,
) -> list[dict]:
    """
    Filtert aus den vorgeladenen Headers die Mails für diese Firma.
    Lädt den Body nur für Treffer.
    Gibt klassifizierte Mails zurück, angereichert mit passendem Stellen-Titel.
    """
    generisch = {"hardware", "software", "production", "group", "gmbh", "ag", "se", "kg"}
    woerter = [w for w in firma.split() if w.lower() not in generisch]
    firma_key = (woerter[0] if woerter else firma.split()[0]).lower()

    gefundene_mails = []

    # Wort-Grenze prüfen damit "te" nicht in "advantest" matcht
    import re as _re
    muster = _re.compile(r'\b' + _re.escape(firma_key) + r'\b')

    for hdr in alle_headers:
        if not muster.search(hdr["from"]) and not muster.search(hdr["subject"]):
            continue

        # Body nachladen und klassifizieren
        body = hole_mail_body(mail, hdr["id"])
        betreff_orig = hdr["subject"]
        absender_orig = hdr["from"]

        stufe = klassifiziere_mail(betreff_orig, absender_orig, body)
        if not stufe:
            continue

        # Titel-Matching: welche Stelle passt am besten?
        mail_text = (betreff_orig + " " + body).lower()
        passende_titel = [
            t for t in titel_liste
            # mindestens 2 Wörter des Titels müssen im Mail-Text vorkommen
            if sum(1 for w in t.lower().split() if len(w) > 3 and w in mail_text) >= 2
        ]

        gefundene_mails.append({
            "betreff":        betreff_orig,
            "absender":       absender_orig,
            "datum":          hdr["date"],
            "stufe":          stufe,
            "passende_titel": passende_titel,
            "body_vorschau":  body[:200].replace("\n", " ").strip(),
        })

    return gefundene_mails


# =============================================================================
# HAUPTLOGIK
# =============================================================================

def hauptprogramm(dry_run: bool = False, alle: bool = False):
    print("\n" + "=" * 60)
    print("  E-Mail-Status-Checker")
    print("=" * 60)

    if dry_run:
        print("  [DRY-RUN] Keine Änderungen werden gespeichert.\n")

    config = lade_config()
    db.erstelle_schema()

    stellen = lade_stellen_mit_status()
    print(f"  {len(stellen)} Stellen in der Datenbank\n")

    # Stellen mit terminalem Status überspringen (es sei denn --alle)
    if not alle:
        zu_pruefen = [s for s in stellen if s["stufe"] not in ("zusage", "absage")]
        uebersprungen = len(stellen) - len(zu_pruefen)
        print(f"  Prüfe {len(zu_pruefen)} Stellen ({uebersprungen} mit Zusage/Absage übersprungen)\n")
    else:
        zu_pruefen = stellen
        print(f"  Prüfe alle {len(zu_pruefen)} Stellen\n")

    if not zu_pruefen:
        print("  Keine Stellen zu prüfen.")
        return

    mail = imap_verbinden(config["email_absender"], config["email_passwort"])
    mail.select("INBOX")

    aktualisierungen: list[dict] = []
    keine_mails: list[str] = []

    # Firmen deduplizieren – pro Firma nur einmal suchen, dann auf Stellen verteilen
    firmen_map: dict[str, list[dict]] = {}
    for stelle in zu_pruefen:
        firma = stelle["firma"].strip()
        firmen_map.setdefault(firma, []).append(stelle)

    seit_datum = _letzter_scan_datum()

    # Einmalig alle Header seit letztem Scan laden (schnell, kein Body)
    alle_headers = lade_aktuelle_headers(mail, seit_datum)
    if not alle_headers:
        print("  Keine neuen Mails seit letztem Scan gefunden.")
        mail.logout()
        return

    print(f"  Durchsuche {len(alle_headers)} Mail(s) nach {len(firmen_map)} Firmen ...\n")

    for i, (firma, stellen_der_firma) in enumerate(firmen_map.items(), 1):
        print(f"  [{i}/{len(firmen_map)}] {firma} ...          ", end="\r", flush=True)
        titel_liste = [s["titel"] for s in stellen_der_firma]
        gefundene_mails = suche_mails_fuer_firma(firma, titel_liste, alle_headers, mail)

        if not gefundene_mails:
            keine_mails.append(firma)
            continue

        print(f"  [{i}/{len(firmen_map)}] {firma} → {len(gefundene_mails)} Mail(s) gefunden")

        for gefunden in gefundene_mails:
            beste_stufe  = gefunden["stufe"]
            passende_titel = gefunden["passende_titel"]

            print(f"  ✉  Betreff : {gefunden['betreff'][:70]}")
            print(f"     Von     : {gefunden['absender'][:60]}")
            print(f"     Erkannt : {beste_stufe.upper()}")

            # Welche Stellen werden aktualisiert?
            # Wenn Titel-Matching möglich → nur passende; sonst alle (nur wenn 1 Stelle)
            if passende_titel:
                kandidaten = [s for s in stellen_der_firma if s["titel"] in passende_titel]
            elif len(stellen_der_firma) == 1:
                kandidaten = stellen_der_firma
            else:
                print(f"     ⚠  Kein Titel-Match bei {len(stellen_der_firma)} Stellen → übersprungen")
                continue

            for stelle in kandidaten:
                aktuelle_stufe = stelle["stufe"]
                aktueller_rang = STUFEN_RANG.get(aktuelle_stufe, 0)
                neuer_rang     = STUFEN_RANG.get(beste_stufe, 0)

                if aktuelle_stufe in ("zusage", "absage"):
                    print(f"     → {stelle['titel'][:50]}: bereits terminal ({aktuelle_stufe}), übersprungen")
                    continue

                if neuer_rang > aktueller_rang or (aktuelle_stufe == "" and neuer_rang >= 1):
                    print(f"     → {stelle['titel'][:50]}: {aktuelle_stufe or '–'} → {beste_stufe}")
                    aktualisierungen.append({
                        "url":   stelle["url"],
                        "firma": firma,
                        "titel": stelle["titel"],
                        "alt":   aktuelle_stufe,
                        "neu":   beste_stufe,
                        "mail":  gefunden,
                    })
                else:
                    print(f"     → {stelle['titel'][:50]}: bereits {aktuelle_stufe or '–'}, keine Änderung")

        print()

    mail.logout()

    # Ergebnis-Zusammenfassung
    print("─" * 60)
    print(f"  Keine Mails gefunden für {len(keine_mails)} Firmen")
    print(f"  Status-Aktualisierungen: {len(aktualisierungen)}")

    # Duplikate entfernen (gleiche URL nur einmal, höchste Stufe gewinnt)
    dedup: dict[str, dict] = {}
    for akt in aktualisierungen:
        url = akt["url"]
        if url not in dedup or STUFEN_RANG.get(akt["neu"], 0) > STUFEN_RANG.get(dedup[url]["neu"], 0):
            dedup[url] = akt
    aktualisierungen = list(dedup.values())

    if aktualisierungen:
        print()
        for akt in aktualisierungen:
            alt = akt["alt"] or "–"
            print(f"  {'[DRY]' if dry_run else '✅'} {akt['firma']} – {akt['titel'][:40]}")
            print(f"      {alt} → {akt['neu']}")
            print(f"      Mail: {akt['mail']['betreff'][:60]}  [{akt['mail']['datum'][:16]}]")

        if not dry_run:
            print()
            for akt in aktualisierungen:
                db.upsert_bewerbungsstatus(akt["url"], akt["neu"])
            print(f"  ✅  {len(aktualisierungen)} Einträge in der Datenbank aktualisiert.")
    else:
        print("  Keine Änderungen notwendig.")

    print("=" * 60 + "\n")


# =============================================================================
# EINSTIEGSPUNKT
# =============================================================================

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    alle    = "--alle"    in sys.argv
    hauptprogramm(dry_run=dry_run, alle=alle)
