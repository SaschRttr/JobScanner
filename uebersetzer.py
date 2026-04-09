"""
uebersetzer.py  –  Einmalige Übersetzung der Lebenslauf-Vorlage
================================================================
Übersetzt lebenslauf_vorlage.txt ins Englische und speichert
das Ergebnis als lebenslauf_vorlage_en.txt.

Marker-Struktur bleibt erhalten, nur der Inhalt wird übersetzt.

Nutzung:
  python uebersetzer.py
"""

import re
import sys
from pathlib import Path

try:
    import anthropic as anthropic_lib
except ImportError:
    print("anthropic nicht installiert: pip install anthropic")
    sys.exit(1)


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD   = Path(__file__).parent
VORLAGE_DE   = BASIS_PFAD / "lebenslauf_vorlage.txt"
VORLAGE_EN   = BASIS_PFAD / "lebenslauf_vorlage_en.txt"
CONFIG_PFAD  = BASIS_PFAD / "config.txt"

KI_MODELL = "claude-sonnet-4-20250514"


# =============================================================================
# CONFIG
# =============================================================================

def lade_api_key() -> str:
    if not CONFIG_PFAD.exists():
        print(f"❌ config.txt nicht gefunden")
        sys.exit(1)
    for zeile in CONFIG_PFAD.read_text(encoding="utf-8").splitlines():
        z = zeile.strip()
        if z.upper().startswith("API_KEY"):
            return z.split("=", 1)[1].strip()
    return ""


# =============================================================================
# ÜBERSETZUNG
# =============================================================================

def uebersetze_abschnitt(inhalt: str, abschnitt_name: str, client) -> str | None:
    """Übersetzt einen einzelnen Abschnitt ins Englische."""

    prompt = f"""Translate the following section of a German CV into professional English.

Section: {abschnitt_name}

Rules:
- Translate naturally and professionally, as a native English speaker would write a CV
- Keep all dates, numbers, company names, product names and technical terms as-is
- Keep bullet point formatting (dash at start of line) exactly as-is
- - Return ONLY the translated text, no comments, no markers, no section headings or titles

=== GERMAN TEXT ===
{inhalt}"""

    try:
        antwort = client.messages.create(
            model=KI_MODELL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return antwort.content[0].text.strip()
    except Exception as e:
        print(f"  ❌ API-Fehler bei {abschnitt_name}: {e}")
        return None


def extrahiere_abschnitte(vorlage: str) -> list[tuple[str, str, str]]:
    """
    Gibt eine Liste von (marker, inhalt, voller_block) zurück.
    Nur äußerste Marker — keine verschachtelten.
    """
    # Äußerste Marker finden (keine verschachtelten wie STELLE_1_AUFGABEN)
    aeussere_marker = [
        "KONTAKT", "KOMPETENZPROFIL",
        "STELLE_1_AUFGABEN", "STELLE_2_AUFGABEN", "STELLE_3_AUFGABEN",
        "STELLE_4_AUFGABEN", "STELLE_5_AUFGABEN",
        "AUSBILDUNG", "FAEHIGKEITEN", "SPRACHEN"
    ]

    ergebnis = []
    for marker in aeussere_marker:
        start_tag = f"---{marker}---"
        ende_tag  = f"---/{marker}---"
        start = vorlage.find(start_tag)
        ende  = vorlage.find(ende_tag)
        if start == -1 or ende == -1:
            continue
        inhalt_start = vorlage.find("\n", start) + 1
        inhalt = vorlage[inhalt_start:ende].strip()
        voller_block = vorlage[start:ende + len(ende_tag)]
        ergebnis.append((marker, inhalt, voller_block))

    return ergebnis


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    print("\n" + "=" * 60)
    print("  UEBERSETZER  –  Lebenslauf-Vorlage ins Englische")
    print("=" * 60)

    if not VORLAGE_DE.exists():
        print(f"❌ lebenslauf_vorlage.txt nicht gefunden: {VORLAGE_DE}")
        sys.exit(1)

    if VORLAGE_EN.exists():
        antwort = input("  ⚠️  lebenslauf_vorlage_en.txt existiert bereits. Neu erstellen? [j/N] ").strip().lower()
        if antwort != "j":
            print("  ⏭️  Abgebrochen.")
            return

    api_key = lade_api_key()
    if not api_key:
        print("❌ Kein API-Key in config.txt")
        sys.exit(1)

    client  = anthropic_lib.Anthropic(api_key=api_key)
    vorlage = VORLAGE_DE.read_text(encoding="utf-8")

    abschnitte = extrahiere_abschnitte(vorlage)
    print(f"  {len(abschnitte)} Abschnitte gefunden")

    uebersetzt_vorlage = vorlage

    for marker, inhalt, _ in abschnitte:
        print(f"  🤖 Übersetze {marker}...")
        uebersetzt = uebersetze_abschnitt(inhalt, marker, client)

        if uebersetzt:
            # Inhalt zwischen den Markern ersetzen
            start_tag    = f"---{marker}---"
            ende_tag     = f"---/{marker}---"
            start        = uebersetzt_vorlage.find(start_tag)
            inhalt_start = uebersetzt_vorlage.find("\n", start) + 1
            ende         = uebersetzt_vorlage.find(ende_tag)
            uebersetzt_vorlage = (
                uebersetzt_vorlage[:inhalt_start]
                + uebersetzt
                + "\n"
                + uebersetzt_vorlage[ende:]
            )
            print(f"  ✅ {marker} übersetzt")
        else:
            print(f"  ⚠️  {marker} nicht übersetzt – Original behalten")

    VORLAGE_EN.write_text(uebersetzt_vorlage, encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Gespeichert: {VORLAGE_EN}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
