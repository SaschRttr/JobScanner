"""
email_status.py  –  E-Mail-basierte Bewerbungsstatus-Erkennung
==============================================================
Lädt die Mails der letzten N Tage und prüft Absender-Domains
gegen eine Zuordnungsliste aus der config.txt.

Nutzung:
    python email_status.py           # Live-Update der Datenbank
    python email_status.py --dry-run # Nur anzeigen, nichts speichern

config.txt – Abschnitt [firma_domains]:
    Advantest Hardware   = candidatecare.com
    Bertrandt            = bertrandtgroup.onlyfy.jobs
    Rohde & Schwarz      = rohde-schwarz.com
    (Firmenname links muss exakt wie in der DB stehen)
"""

import imaplib
import email
import email.header
import email.message
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import db

# =============================================================================
# KONFIGURATION
# =============================================================================

CONFIG_PFAD  = Path(__file__).parent / "config.txt"
IMAP_HOST    = "imap.mail.me.com"
IMAP_PORT    = 993
TAGE_ZURUECK = 10   # Wie viele Tage zurück gesucht wird

# Keywords je Status (Priorität: absage > zusage > einladung > kennenlernen > beworben)
KEYWORDS = {
    "absage": [
        "leider", "absage", "nicht berücksichtigen", "haben uns für andere",
        "anderen kandidaten", "andere bewerber", "nicht weiterverfolgen",
        "kein match", "nicht in frage", "bedauern", "nicht erfolgreich",
        "abgesagt", "unfortunately", "not moving forward", "not selected",
        "we regret", "decided not to",
    ],
    "zusage": [
        "herzlichen glückwunsch", "zusage", "vertragsangebot", "offer letter",
        "job offer", "willkommen im team", "welcome to the team", "congratulations",
    ],
    "einladung": [
        "vorstellungsgespräch", "einladung zum interview", "einladung zum gespräch",
        "zu einem gespräch einladen", "persönliches gespräch", "zweites gespräch",
        "assessment center", "technical interview", "face-to-face",
    ],
    "kennenlernen": [
        "kennenlernen", "kurzes telefonat", "telefongespräch",
        "erstes gespräch", "screening call", "phone screen", "intro call",
        "telefoninterview", "videocall", "video call",
    ],
    "beworben": [
        "bewerbung erhalten", "eingang ihrer bewerbung", "eingangsbestätigung",
        "bestätigen den eingang", "ihre bewerbung ist eingegangen",
        "danke für ihre bewerbung", "thank you for applying",
        "thank you for your application", "we received your application",
        "your application has been received", "application confirmed",
        "thank you for your interest in", "Thank you for your interest in position"
    ],
}
PRUEFREIHENFOLGE = ["absage", "zusage", "einladung", "kennenlernen", "beworben"]

STUFEN_RANG = {
    "": 0, "beworben": 1, "kennenlernen": 2, "einladung": 3, "zusage": 4, "absage": 4,
}


# =============================================================================
# CONFIG LESEN
# =============================================================================

def lade_config() -> tuple[dict, dict]:
    """
    Gibt zurück:
      - basis: dict mit email_absender, email_passwort
      - firma_domains: dict  Firmenname -> Domain  (aus [firma_domains])
    """
    if not CONFIG_PFAD.exists():
        print(f"Fehler: config.txt nicht gefunden: {CONFIG_PFAD}")
        sys.exit(1)

    basis = {"email_absender": "", "email_passwort": ""}
    firma_domains: dict[str, str] = {}
    in_abschnitt = False

    for zeile in CONFIG_PFAD.read_text(encoding="utf-8").splitlines():
        z = zeile.strip()
        if not z or z.startswith("#"):
            continue

        if z.lower() == "[firma_domains]":
            in_abschnitt = True
            continue
        if z.lower() == r"[\firma_domains]":
            in_abschnitt = False
            continue

        if in_abschnitt:
            if "=" in z:
                firma, _, domain = z.partition("=")
                firma_domains[firma.strip()] = domain.strip().lower()
            continue

        if "=" in z:
            key, _, val = z.partition("=")
            key = key.strip().upper()
            val = val.strip()
            if key == "EMAIL_ABSENDER":
                basis["email_absender"] = val
            elif key == "EMAIL_PASSWORT":
                basis["email_passwort"] = val

    if not basis["email_absender"] or not basis["email_passwort"]:
        print("Fehler: E-Mail-Zugangsdaten fehlen in config.txt")
        sys.exit(1)

    return basis, firma_domains


# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def decode_header(wert: str | None) -> str:
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


def html_zu_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extrahiere_body(msg: email.message.Message) -> str:
    """Bevorzugt Plaintext, Fallback auf HTML (max. 3000 Zeichen)."""
    plaintext = ""
    htmltext  = ""
    teile = msg.walk() if msg.is_multipart() else [msg]
    for teil in teile:
        ct = teil.get_content_type()
        cd = str(teil.get("Content-Disposition", ""))
        if "attachment" in cd:
            continue
        charset = teil.get_content_charset() or "utf-8"
        try:
            payload = teil.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(charset, errors="replace")
            if ct == "text/plain" and not plaintext:
                plaintext = decoded[:3000]
            elif ct == "text/html" and not htmltext:
                htmltext = html_zu_text(decoded)[:3000]
        except Exception:
            continue
    return plaintext if plaintext else htmltext


def klassifiziere(absender: str, betreff: str, body: str) -> str | None:
    text = (absender + " " + betreff + " " + body).lower()
    for stufe in PRUEFREIHENFOLGE:
        for kw in KEYWORDS[stufe]:
            if kw in text:
                return stufe
    return None


def absender_domain(absender: str) -> str:
    """Extrahiert die Domain aus einer Absenderadresse."""
    match = re.search(r"@([\w.\-]+)", absender)
    return match.group(1).lower() if match else ""


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def hauptprogramm(dry_run: bool = False):
    print("\n" + "=" * 60)
    print("  E-Mail-Status-Checker")
    print("=" * 60)
    if dry_run:
        print("  [DRY-RUN] Keine Aenderungen werden gespeichert.\n")

    config, firma_domains = lade_config()

    if not firma_domains:
        print("Warnung: Keine Eintraege in [firma_domains] gefunden.")
        print("  Bitte config.txt ergaenzen – Beispiel:")
        print("  [firma_domains]")
        print("  Advantest Hardware = candidatecare.com")
        print(r"  [\firma_domains]")
        sys.exit(1)

    print(f"  {len(firma_domains)} Firma-Domain-Zuordnung(en) geladen:")
    for f, d in firma_domains.items():
        print(f"    {f:30s} -> {d}")
    print()

    db.erstelle_schema()

    # --- IMAP-Login ---
    print(f"  Verbinde mit {IMAP_HOST} ...")
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(config["email_absender"], config["email_passwort"])
    imap.select("INBOX")
    print(f"  Eingeloggt als {config['email_absender']}")

    # --- Mails der letzten N Tage laden ---
    seit = (datetime.now() - timedelta(days=TAGE_ZURUECK)).strftime("%d-%b-%Y")
    print(f"  Suche Mails seit {seit} ...\n")

    _, daten = imap.search(None, f'SINCE "{seit}"')
    mail_ids = daten[0].split() if daten and daten[0] else []
    print(f"  {len(mail_ids)} Mail(s) gefunden\n")

    if not mail_ids:
        imap.logout()
        print("  Keine Mails im Zeitraum.")
        return

    # --- Stellen aus DB laden ---
    with db.verbindung() as con:
        rows = con.execute("""
            SELECT s.url, s.firma, s.titel,
                   COALESCE(bs.stufe, '') AS stufe
            FROM stellen s
            LEFT JOIN bewerbungsstatus bs ON s.url = bs.url
            WHERE s.status != 0
        """).fetchall()
    stellen = [dict(r) for r in rows]
    print(f"  {len(stellen)} aktive Stellen in der Datenbank\n")

    # --- Jede Mail laden und prüfen ---
    aktualisierungen = []

    for mail_id in mail_ids:
        try:
            _, msg_daten = imap.fetch(mail_id, "(BODY.PEEK[]<0.60000>)")
        except Exception:
            continue

        raw = None
        for teil in msg_daten:
            if isinstance(teil, tuple) and isinstance(teil[1], bytes):
                raw = teil[1]
                break
        if not raw:
            continue

        msg      = email.message_from_bytes(raw)
        absender = decode_header(msg.get("From", ""))
        betreff  = decode_header(msg.get("Subject", ""))
        body     = extrahiere_body(msg)
        datum    = msg.get("Date", "")[:16]
        domain   = absender_domain(absender)

        # --- Welche Firmen passen zu dieser Domain? ---
        passende_firmen = [
            firma for firma, fd in firma_domains.items()
            if fd and fd in domain
        ]
        if not passende_firmen:
            continue

        stufe = klassifiziere(absender, betreff, body)
        if not stufe:
            continue

        # --- Passende Stellen in DB aktualisieren ---
        for firma in passende_firmen:
            for stelle in stellen:
                if stelle["firma"] != firma:
                    continue
                if stelle["stufe"] in ("zusage", "absage"):
                    continue
                if STUFEN_RANG.get(stufe, 0) <= STUFEN_RANG.get(stelle["stufe"], 0):
                    continue

                aktualisierungen.append({
                    "url":      stelle["url"],
                    "firma":    firma,
                    "titel":    stelle["titel"],
                    "alt":      stelle["stufe"] or "-",
                    "neu":      stufe,
                    "betreff":  betreff,
                    "absender": absender,
                    "datum":    datum,
                })
                stelle["stufe"] = stufe  # in-memory updaten gegen Duplikate

    imap.logout()

    # --- Ergebnis ---
    print("-" * 60)
    if not aktualisierungen:
        print("  Keine Status-Aenderungen erkannt.")
    else:
        print(f"  {len(aktualisierungen)} Status-Aenderung(en):\n")
        for akt in aktualisierungen:
            print(f"  {'[DRY]' if dry_run else '[OK]'} {akt['firma']}  -  {akt['titel'][:45]}")
            print(f"       {akt['alt']}  ->  {akt['neu']}")
            print(f"       Von    : {akt['absender'][:60]}")
            print(f"       Betreff: {akt['betreff'][:60]}")
            print(f"       Datum  : {akt['datum']}")
            print()

        if not dry_run:
            for akt in aktualisierungen:
                db.upsert_bewerbungsstatus(akt["url"], akt["neu"])
            print(f"  {len(aktualisierungen)} Eintraege in der Datenbank gespeichert.")

    print("=" * 60 + "\n")


# =============================================================================
# EINSTIEGSPUNKT
# =============================================================================

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    hauptprogramm(dry_run=dry_run)