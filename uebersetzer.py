"""
uebersetzer.py  –  Einmalige Übersetzung der Lebenslauf-Vorlage
================================================================
Übersetzt lebenslauf_vorlage.txt ins Englische und speichert
das Ergebnis als lebenslauf_vorlage_en.txt.

Marker-Struktur bleibt erhalten, nur der Inhalt wird übersetzt.

Nutzung:
  python uebersetzer.py
"""

import sys
from pathlib import Path

try:
    import anthropic as anthropic_lib
except ImportError:
    print("anthropic nicht installiert: pip install anthropic")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from utils import lade_config, extrahiere_abschnitt, ersetze_abschnitt


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD   = Path(__file__).parent
VORLAGE_DE   = BASIS_PFAD / "lebenslauf_vorlage.txt"
VORLAGE_EN   = BASIS_PFAD / "lebenslauf_vorlage_en.txt"

# claude-sonnet-4-20250514 wurde am 15.06.2026 abgeschaltet (HTTP 404)
KI_MODELL = "claude-sonnet-5"

# Äußerste Marker der Lebenslauf-Vorlage (keine verschachtelten)
AEUSSERE_MARKER = [
    "KONTAKT", "KOMPETENZPROFIL",
    "STELLE_1_AUFGABEN", "STELLE_2_AUFGABEN", "STELLE_3_AUFGABEN",
    "STELLE_4_AUFGABEN", "STELLE_5_AUFGABEN",
    "AUSBILDUNG", "FAEHIGKEITEN", "SPRACHEN",
]


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
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in antwort.content if b.type == "text"), "").strip()
        return text or None
    except Exception as e:
        print(f"  ❌ API-Fehler bei {abschnitt_name}: {e}")
        return None


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

    api_key = lade_config()["api_key"]
    if not api_key:
        print("❌ Kein API-Key in config.txt")
        sys.exit(1)

    client  = anthropic_lib.Anthropic(api_key=api_key)
    vorlage = VORLAGE_DE.read_text(encoding="utf-8")

    abschnitte = [(m, extrahiere_abschnitt(vorlage, m)) for m in AEUSSERE_MARKER]
    abschnitte = [(m, inhalt) for m, inhalt in abschnitte if inhalt is not None]
    print(f"  {len(abschnitte)} Abschnitte gefunden")

    uebersetzt_vorlage = vorlage

    for marker, inhalt in abschnitte:
        print(f"  🤖 Übersetze {marker}...")
        uebersetzt = uebersetze_abschnitt(inhalt, marker, client)

        if uebersetzt:
            uebersetzt_vorlage = ersetze_abschnitt(uebersetzt_vorlage, marker, uebersetzt)
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
