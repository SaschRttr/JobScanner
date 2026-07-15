"""
bewertung.py  –  Job-Scanner (Schritt 3)
=========================================
Bewertet alle Stellen mit sauberem Stellentext per KI.
Prompt kommt aus config.txt.

Status-Übergänge:
  3 (Stellentext extrahiert) → 4 (KI-Bewertung ≥ 70 %, bewerben)
                             → 5 (KI-Bewertung < 70 %, nicht bewerben)

Nutzung:
  python bewertung.py
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import anthropic as anthropic_lib
except ImportError:
    print("anthropic nicht installiert: pip install anthropic")
    sys.exit(1)

from utils import lade_config, standort_verboten, effektiver_score


# =============================================================================
# PFADE
# =============================================================================

BASIS_PFAD      = Path(__file__).parent
STELLEN_JSON    = BASIS_PFAD / "stellen.json"
BEKANNTE_JSON   = BASIS_PFAD / "bekannte_stellen.json"
LEBENSLAUF_PFAD = BASIS_PFAD / "lebenslauf.txt"

KI_MODELL = "claude-haiku-4-5-20251001"


# =============================================================================
# KI-BEWERTUNG
# =============================================================================

def _parse_json_antwort(text: str) -> dict:
    text = text.removeprefix("```json").removesuffix("```").strip()
    start = text.find("{")
    ende  = text.rfind("}") + 1
    if start != -1 and ende > start:
        text = text[start:ende]
    return json.loads(text)


def _berechne_score_aus_abzuegen(ergebnis: dict) -> float | None:
    """
    Berechnet score_aktuell deterministisch aus punkteabzug statt der vom Modell
    geschätzten Zahl zu vertrauen. In der Praxis werden oft nur 2-3 von 5+ im
    "luecken"-Array genannten Lücken auch tatsächlich im "punkteabzug"-Array
    verrechnet (mehrfach beobachtet, z.B. bei ADS-TEC: 5 Lücken genannt, nur 2 im
    punkteabzug-Array, macht 91% statt der eigentlich zu erwartenden ~70%).
    Gibt None zurück, falls kein gültiges punkteabzug-Array vorhanden ist.
    """
    punkteabzug = ergebnis.get("punkteabzug")
    if not isinstance(punkteabzug, list) or not punkteabzug:
        return None

    summe = 0
    for eintrag in punkteabzug:
        abzug = eintrag.get("abzug") if isinstance(eintrag, dict) else None
        if isinstance(abzug, (int, float)):
            summe += abzug

    return max(0, min(100, 100 + summe))


def _berechne_potenzial_aus_schliessbaren(ergebnis: dict, score_aktuell: float) -> float | None:
    """
    Berechnet score_potenzial deterministisch aus score_aktuell + schliessbare_luecken,
    statt der vom Modell geschätzten Zahl zu vertrauen (gleiches Muster wie
    _berechne_score_aus_abzuegen: Modell-Arithmetik über mehrere Felder hinweg ist
    unzuverlässig, z.B. beobachtet score_aktuell=84 + Summe(punkte_zurueck)=13 → 97,
    Modell nannte aber 92).
    Gibt None zurück, falls kein gültiges schliessbare_luecken-Array vorhanden ist.
    """
    schliessbare = ergebnis.get("schliessbare_luecken")
    if not isinstance(schliessbare, list) or not schliessbare:
        return None

    summe = 0
    for eintrag in schliessbare:
        punkte = eintrag.get("punkte_zurueck") if isinstance(eintrag, dict) else None
        if isinstance(punkte, (int, float)):
            summe += punkte

    return max(score_aktuell, min(100, score_aktuell + summe))


def bewerte_stelle(stellentext: str, lebenslauf: str, prompt_vorlage: str, client) -> dict | None:
    prompt = prompt_vorlage.replace("{stellentext}", stellentext[:5000])
    prompt = prompt.replace("{lebenslauf}", lebenslauf)

    # Anweisungsteil als System-Prompt, Stellenanzeige (+ Antwortformat) als User-Message
    marker = "=== STELLENANZEIGE ==="
    if marker in prompt:
        system_teil, _, rest = prompt.partition(marker)
        system_teil = system_teil.strip()
        user_teil   = marker + rest
    else:
        system_teil = ""
        user_teil   = prompt

    # System-Teil ist über alle Stellen identisch → Prompt-Caching
    # (greift erst ab 4096 Tokens Prefix bei Haiku 4.5, darunter wirkungslos)
    if system_teil:
        system_param = [{
            "type": "text",
            "text": system_teil,
            "cache_control": {"type": "ephemeral"},
        }]
    else:
        system_param = anthropic_lib.NOT_GIVEN

    for versuch in range(1, 4):
        try:
            antwort = client.messages.create(
                model=KI_MODELL,
                max_tokens=8192,
                system=system_param,
                messages=[{"role": "user", "content": user_teil}],
            )
            text = antwort.content[0].text.strip()
            if antwort.stop_reason == "max_tokens":
                print(f"  ⚠️  Antwort abgeschnitten – max_tokens erreicht (Versuch {versuch}/3)")
                if versuch == 3:
                    return None
                continue
            ergebnis = _parse_json_antwort(text)
            berechneter_score = _berechne_score_aus_abzuegen(ergebnis)
            if berechneter_score is not None:
                modell_score = ergebnis.get("score_aktuell")
                if isinstance(modell_score, (int, float)) and abs(modell_score - berechneter_score) > 5:
                    luecken     = ergebnis.get("luecken") or []
                    punkteabzug = ergebnis.get("punkteabzug") or []
                    print(f"  ⚠️  Score-Abweichung: Modell nannte {modell_score}%, Punkteabzug ergibt "
                          f"{berechneter_score}% – nutze berechneten Wert. Anzahl Lücken: {len(luecken)}, "
                          f"Anzahl Punkteabzüge: {len(punkteabzug)}")
                ergebnis["score_aktuell"] = berechneter_score
            if "score_aktuell" in ergebnis and "score" not in ergebnis:
                ergebnis["score"] = ergebnis["score_aktuell"]
            if not isinstance(ergebnis.get("score"), (int, float)):
                # Ohne gültigen Score nicht speichern – sonst landet 0% in der DB
                print(f"  ⚠️  Kein gültiger Score in der Antwort (Versuch {versuch}/3)")
                if versuch == 3:
                    return None
                continue
            if not isinstance(ergebnis.get("punkteabzug"), list):
                ergebnis["punkteabzug"] = []
            if not isinstance(ergebnis.get("schliessbare_luecken"), list):
                ergebnis["schliessbare_luecken"] = []
            berechnetes_potenzial = _berechne_potenzial_aus_schliessbaren(ergebnis, ergebnis["score_aktuell"])
            if berechnetes_potenzial is not None:
                modell_potenzial = ergebnis.get("score_potenzial")
                if isinstance(modell_potenzial, (int, float)) and abs(modell_potenzial - berechnetes_potenzial) > 5:
                    print(f"  ⚠️  Potenzial-Abweichung: Modell nannte {modell_potenzial}%, schliessbare_luecken "
                          f"ergibt {berechnetes_potenzial}% – nutze berechneten Wert.")
                ergebnis["score_potenzial"] = berechnetes_potenzial
            elif not isinstance(ergebnis.get("score_potenzial"), (int, float)):
                ergebnis["score_potenzial"] = ergebnis["score"]
            if not isinstance(ergebnis.get("score_nach_anpassung"), (int, float)):
                ergebnis["score_nach_anpassung"] = ergebnis["score"]
            # Bewerben-Entscheidung hängt am höchsten der drei Scores: score_aktuell
            # bleibt bewusst streng, aber das erkannte Potenzial (score_potenzial)
            # oder der Profil-Fit (score_nach_anpassung) sollen eine Chance nicht
            # verdecken, nur weil ein einzelner Score sie konservativ einschätzt.
            ergebnis["empfehlung"] = "bewerben" if effektiver_score(ergebnis) >= 70 else "nicht bewerben"
            if "sprache" not in ergebnis:
                ergebnis["sprache"] = "de"
            return ergebnis
        except json.JSONDecodeError as e:
            print(f"  ⚠️  JSON-Parse-Fehler (Versuch {versuch}/3): {e}")
            if versuch == 3:
                return None
        except Exception as e:
            print(f"  ❌ API-Fehler: {e}")
            return None
    return None


# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=None, help="Nur diese URL bewerten")
    parser.add_argument("--firma", default=None, help="Nur diese Firma bewerten")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  BEWERTUNG  –  Schritt 3: KI-Bewertung")
    if args.url:
        print(f"  Filter: nur {args.url[:60]}")
    if args.firma:
        print(f"  Filter: nur Firma '{args.firma}'")
    print("=" * 60)

    config = lade_config()

    if not config["api_key"]:
        print("❌ Kein API-Key in config.txt")
        sys.exit(1)

    if not config["prompt"]:
        print("❌ Kein [prompt] in config.txt")
        sys.exit(1)

    if not LEBENSLAUF_PFAD.exists():
        print(f"❌ Lebenslauf nicht gefunden: {LEBENSLAUF_PFAD}")
        sys.exit(1)

    lebenslauf  = LEBENSLAUF_PFAD.read_text(encoding="utf-8")
    client      = anthropic_lib.Anthropic(api_key=config["api_key"])
    sys.path.insert(0, str(BASIS_PFAD))
    from db import lade_alle_stellen, upsert_stelle, upsert_bewertung, exportiere_stellen_json, exportiere_bekannte_json, erstelle_schema
    erstelle_schema()
    stellen: list = lade_alle_stellen()

    if not stellen:
        print("ℹ️  Keine Stellen in DB – zuerst scanner.py ausführen.")
        return

    verbotene = config["verbotene_standorte"]

    def standort_ok(s: dict) -> bool:
        arbeitsort = s.get("arbeitsort") or ""
        if not arbeitsort:
            return True
        # standort_verboten normalisiert Umlaute (München == Muenchen)
        return not standort_verboten(arbeitsort, verbotene)

    # Stellen mit verbotenem Standort explizit als nicht_passend markieren
    zu_markieren = [
        (i, s) for i, s in enumerate(stellen)
        if s.get("status") == 3
        and s.get("stellentext")
        and not standort_ok(s)
        and not s.get("nicht_passend")
        and (args.firma is None or s.get("firma") == args.firma)
    ]
    if zu_markieren:
        print(f"  {len(zu_markieren)} Stellen wegen verbotenem Standort → nicht_passend:")
        for idx, stelle in zu_markieren:
            stellen[idx]["nicht_passend"] = True
            np_grund = f"Verbotener Standort: {stelle.get('arbeitsort', '?')}"
            stellen[idx]["nicht_passend_grund"] = np_grund
            upsert_stelle({"url": stelle["url"], "nicht_passend": True, "nicht_passend_grund": np_grund})
            print(f"    🚫 {stelle['firma']}: {stelle['titel'][:60]} ({stelle.get('arbeitsort','')})")
        exportiere_stellen_json(STELLEN_JSON)
        exportiere_bekannte_json(BEKANNTE_JSON)

    # Status-3 Stellen bleiben bei nicht_passend=True einfach auf Status 3 stehen
    # (Status 9 ist für per HTTP bestätigte Vergaben reserviert – siehe vergaben_check.py).
    # nicht_passend hält sie schon aus allen aktiven Report-Listen raus; sobald
    # nicht_passend wieder False wird (z.B. nach Whitelist-Änderung), landen sie
    # automatisch wieder in zu_bearbeiten unten und werden neu bewertet.

    zu_bearbeiten = [
        (i, s) for i, s in enumerate(stellen)
        if s.get("status") == 3
        and s.get("stellentext")
        and standort_ok(s)
        and not s.get("nicht_passend")
        and (args.url is None or s["url"] == args.url)
        and (args.firma is None or s.get("firma") == args.firma)
    ]

    print(f"  {len(zu_bearbeiten)} Stellen zu bewerten (Status 3)")

    # Diagnose: zeige noch verbliebene status=3 Stellen die übersprungen werden
    nicht_bewertet = [
        s for s in stellen
        if s.get("status") == 3 and s not in [st for _, st in zu_bearbeiten]
    ]
    if nicht_bewertet and len(zu_bearbeiten) == 0:
        print(f"  ℹ️  {len(nicht_bewertet)} Status-3 Stellen werden übersprungen:")
        for s in nicht_bewertet:
            if not s.get("stellentext"):
                grund = "⚠️  kein Stellentext"
            else:
                grund = f"Arbeitsort: {s.get('arbeitsort', '?')}"
            print(f"     {s['firma']}: {s['titel'][:55]} → {grund}")

    bewertet = 0

    for idx, stelle in zu_bearbeiten:
        url         = stelle["url"]
        titel       = stelle["titel"]
        firma       = stelle["firma"]
        stellentext = stelle["stellentext"]

        print(f"\n  {'─'*50}")
        print(f"  {firma}: {titel[:60]}")
        print(f"  🤖 Bewerte...")

        bewertung = bewerte_stelle(stellentext, lebenslauf, config["prompt"], client)

        if bewertung:
            stellen[idx]["bewertung"] = bewertung
            score      = bewertung.get("score", 0)
            potenzial  = bewertung.get("score_potenzial", score)
            profil     = bewertung.get("score_nach_anpassung", score)
            empf       = bewertung.get("empfehlung", "?")
            neuer_status = 4 if effektiver_score(bewertung) >= 70 else 5
            stellen[idx]["status"] = neuer_status
            print(f"  ⭐ Lebenslauf: {score}%  →  Optimierbar: {potenzial}%  →  Profil: {profil}%  |  {empf.upper()}  →  Status {neuer_status}")
            bewertet += 1
            upsert_stelle({"url": url, "status": neuer_status})
            upsert_bewertung(url, bewertung)
        else:
            print(f"  ⚠️  Bewertung fehlgeschlagen")

    # JSON-Spiegel einmal am Ende aktualisieren (DB ist pro Stelle schon aktuell)
    if zu_bearbeiten:
        exportiere_stellen_json(STELLEN_JSON)
        exportiere_bekannte_json(BEKANNTE_JSON)

    print(f"\n{'='*60}")
    print(f"  FERTIG")
    print(f"  Stellen bewertet: {bewertet}")
    print(f"{'='*60}\n")

    # Top 5 ausgeben
    bewertete = [s for s in stellen if s.get("bewertung")]
    if bewertete:
        top = sorted(bewertete, key=lambda s: s["bewertung"].get("score", 0), reverse=True)[:5]
        print("  🏆 TOP 5:")
        for s in top:
            score = s["bewertung"].get("score", 0)
            empf  = s["bewertung"].get("empfehlung", "?")
            print(f"     {score:3d}%  [{empf[:5]}]  {s['firma']}: {s['titel'][:50]}")
        print()


if __name__ == "__main__":
    main()
