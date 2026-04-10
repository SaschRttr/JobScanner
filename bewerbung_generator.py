"""
bewerbung_generator.py  –  Job-Scanner (Schritt 6)
====================================================
Liest eine angepasste Lebenslauf.txt und generiert daraus:
  - Lebenslauf.docx   (ATS-optimiert, kein Tabellen-Layout)
  - Anschreiben.docx  (AI-generiert aus Lebenslauf + Stelleninfos)

Aufruf:
  python bewerbung_generator.py /pfad/zu/Lebenslauf.txt

Oder als Modul (von webui.py):
  from bewerbung_generator import generiere_bewerbung
  generiere_bewerbung(txt_pfad)
"""

import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    import anthropic as anthropic_lib
except ImportError:
    print("anthropic nicht installiert: pip install anthropic")
    sys.exit(1)

# =============================================================================
# PFADE & KONSTANTEN
# =============================================================================

BASIS_PFAD   = Path(__file__).parent
CONFIG_PFAD  = BASIS_PFAD / "config.txt"
STELLEN_JSON = BASIS_PFAD / "stellen.json"

KI_MODELL = "claude-sonnet-4-20250514"

# Style aus Vorlage: Arial, Blau #1B4F72, A4, 2cm Ränder
BLAU   = "1B4F72"
GRAU   = "555555"
MARGIN = 1134   # DXA (~2cm)
PAGE_W = 11906  # A4
PAGE_H = 16838


# =============================================================================
# CONFIG
# =============================================================================

def lade_config() -> dict:
    if not CONFIG_PFAD.exists():
        print("❌ config.txt nicht gefunden")
        sys.exit(1)
    result = {"api_key": ""}
    for zeile in CONFIG_PFAD.read_text(encoding="utf-8").splitlines():
        z = zeile.strip()
        if z.upper().startswith("API_KEY"):
            result["api_key"] = z.split("=", 1)[1].strip()
    return result


# =============================================================================
# TXT PARSEN
# =============================================================================

def parse_txt(txt_pfad: Path) -> dict:
    """Liest Metadaten + CV-Inhalt aus der angepassten Lebenslauf.txt."""
    inhalt = txt_pfad.read_text(encoding="utf-8")

    def meta(key):
        for zeile in inhalt.splitlines():
            if zeile.startswith(key + ":"):
                return zeile.split(":", 1)[1].strip()
        return ""

    firma = meta("Firma")
    titel = meta("Stelle")
    url   = meta("URL")

    # CV-Text nach der =====-Trennlinie
    trenn = inhalt.find("=" * 10)
    lv_text = inhalt[trenn:].lstrip("=").strip() if trenn != -1 else inhalt

    return {"firma": firma, "titel": titel, "url": url, "lebenslauf_text": lv_text}


def parse_lebenslauf_abschnitte(lv_text: str) -> dict:
    """Zerlegt den CV-Text in strukturierte Abschnitte."""
    zeilen = lv_text.splitlines()

    abschnitte = {
        "name":       "",
        "kontakt":    [],
        "profil":     [],
        "stellen":    [],
        "ausbildung": [],
        "skills":     [],
        "sprachen":   [],
    }

    # Name = erste nicht-leere Zeile
    for z in zeilen:
        if z.strip():
            abschnitte["name"] = z.strip()
            break

    # Kontakt-Zeilen (PLZ, Telefon, Email, Straße)
    kontakt_muster = [r'\d{5}', r'0\d{3}', r'@', r'Stra\xdfe|Str\.|Weg|Gasse|Allee|Platz']
    for z in zeilen[1:8]:
        z = z.strip()
        if z and any(re.search(m, z) for m in kontakt_muster):
            abschnitte["kontakt"].append(z)

    sektion         = None
    aktuell_stelle  = None
    aktuell_bildung = None

    SEKTIONEN_MAP = {
        "PROFILE": "profil", "PROFIL": "profil", "KOMPETENZPROFIL": "profil",
        "PROFESSIONAL EXPERIENCE": "erfahrung", "BERUFSERFAHRUNG": "erfahrung",
        "EDUCATION": "ausbildung", "AUSBILDUNG": "ausbildung",
        "TECHNICAL SKILLS": "skills", "SKILLS": "skills",
        "FÄHIGKEITEN": "skills", "KENNTNISSE": "skills",
        "LANGUAGES": "sprachen", "SPRACHEN": "sprachen",
    }

    for z in zeilen:
        z_strip = z.strip()
        z_upper = z_strip.upper()

        if z_upper in SEKTIONEN_MAP:
            # Offene Einträge schließen
            if aktuell_stelle:
                abschnitte["stellen"].append(aktuell_stelle)
                aktuell_stelle = None
            if aktuell_bildung:
                abschnitte["ausbildung"].append(aktuell_bildung)
                aktuell_bildung = None
            sektion = SEKTIONEN_MAP[z_upper]
            continue

        if not z_strip:
            continue

        if sektion == "profil":
            abschnitte["profil"].append(z_strip)

        elif sektion == "erfahrung":
            if re.search(r'\d{2}/\d{4}', z_strip) or re.search(r'\d{4}\s*[–\-]\s*(\d{4}|heute|present)', z_strip):
                if aktuell_stelle:
                    abschnitte["stellen"].append(aktuell_stelle)
                teile = re.split(r'\t+', z_strip)
                aktuell_stelle = {
                    "titel":    teile[0].strip(),
                    "zeitraum": teile[-1].strip() if len(teile) > 1 else "",
                    "firma":    "",
                    "aufgaben": [],
                }
            elif aktuell_stelle:
                if not aktuell_stelle["firma"] and not re.match(r'^[–\-•·]', z_strip):
                    aktuell_stelle["firma"] = z_strip
                else:
                    sauber = z_strip.lstrip("–- •·").strip()
                    if sauber:
                        aktuell_stelle["aufgaben"].append(sauber)

        elif sektion == "ausbildung":
            if re.search(r'\d{2}/\d{4}|\d{4}\s*[–\-]', z_strip):
                if aktuell_bildung:
                    abschnitte["ausbildung"].append(aktuell_bildung)
                teile = re.split(r'\t+', z_strip)
                aktuell_bildung = {
                    "titel":    teile[0].strip(),
                    "zeitraum": teile[-1].strip() if len(teile) > 1 else "",
                    "ort":      "",
                    "details":  [],
                }
            elif aktuell_bildung:
                if not aktuell_bildung["ort"]:
                    aktuell_bildung["ort"] = z_strip
                else:
                    aktuell_bildung["details"].append(z_strip)

        elif sektion == "skills":
            abschnitte["skills"].append(z_strip)

        elif sektion == "sprachen":
            abschnitte["sprachen"].append(z_strip)

    # Letzte offene Einträge schließen
    if aktuell_stelle:
        abschnitte["stellen"].append(aktuell_stelle)
    if aktuell_bildung:
        abschnitte["ausbildung"].append(aktuell_bildung)

    return abschnitte


# =============================================================================
# ANSCHREIBEN PER AI GENERIEREN
# =============================================================================

def generiere_anschreiben_text(lv_text: str, stelle: dict, client) -> dict:
    """Lässt Claude ein Anschreiben generieren. Gibt strukturiertes dict zurück."""
    stellenbeschreibung = (stelle.get("beschreibung") or "")[:1500]

    prompt = f"""Du bist ein professioneller Bewerbungsberater.

Schreibe ein Anschreiben auf Basis des folgenden Lebenslaufs für die Stelle.

Firma: {stelle.get('firma', '')}
Stelle: {stelle.get('titel', '')}
Stellenbeschreibung (Auszug): {stellenbeschreibung}

LEBENSLAUF:
{lv_text[:3000]}

REGELN:
- Sprache: falls die Stellenbeschreibung auf Englisch ist → Englisch, sonst Deutsch
- Anrede: "Dear Hiring Team," (EN) oder "Sehr geehrtes Recruiting-Team," (DE)
- Maximal 4 Absätze: 1. Direkter Einstieg mit Bezug zur Stelle, 2. Relevanteste Erfahrung mit konkreten Zahlen, 3. Mehrwert für die Firma, 4. Sachlicher Abschluss
- Keine Floskeln, keine leeren Formulierungen
- Nur Fakten aus dem Lebenslauf verwenden

Antworte NUR mit JSON, kein Markdown, keine Backticks:
{{"sprache":"en","betreff":"Application for: <Titel>","anrede":"Dear Hiring Team,","absaetze":["...","...","...","..."],"gruss":"Yours sincerely,"}}"""

    try:
        antwort = client.messages.create(
            model=KI_MODELL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = re.sub(r"```json|```", "", antwort.content[0].text.strip()).strip()
        return json.loads(text)
    except Exception as e:
        print(f"  ❌ AI-Fehler Anschreiben: {e}")
        return {
            "sprache": "de",
            "betreff": f"Bewerbung als {stelle.get('titel', '')}",
            "anrede":  "Sehr geehrtes Recruiting-Team,",
            "absaetze": ["[Anschreiben konnte nicht generiert werden – bitte manuell ausfüllen]"],
            "gruss":   "Mit freundlichen Grüßen,"
        }


# =============================================================================
# NODE.JS HELPER
# =============================================================================

def _run_node(js_code: str, label: str):
    """Schreibt JS in Temp-Datei und führt sie mit Node aus."""
    with tempfile.NamedTemporaryFile(suffix=".js", mode="w", encoding="utf-8", delete=False) as f:
        f.write(js_code)
        tmp = f.name
    try:
        result = subprocess.run(
            ["node", tmp],
            capture_output=True, text=True, encoding="utf-8"
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr[:400])
        print(f"  ✅ {label}")
    finally:
        Path(tmp).unlink(missing_ok=True)


# =============================================================================
# LEBENSLAUF DOCX
# =============================================================================

def erstelle_lebenslauf_docx(abschnitte: dict, ziel: Path):
    stellen_arr    = json.dumps(abschnitte["stellen"],    ensure_ascii=False)
    ausbildung_arr = json.dumps(abschnitte["ausbildung"], ensure_ascii=False)
    skills_js      = json.dumps(abschnitte["skills"],     ensure_ascii=False)
    sprachen_js    = json.dumps(abschnitte["sprachen"],   ensure_ascii=False)
    name_js        = json.dumps(abschnitte["name"],       ensure_ascii=False)
    kontakt_js     = json.dumps(" · ".join(abschnitte["kontakt"]), ensure_ascii=False)
    profil_js      = json.dumps(" ".join(abschnitte["profil"]),    ensure_ascii=False)
    ziel_js        = json.dumps(str(ziel),                ensure_ascii=False)

    js = f"""
const {{ Document, Packer, Paragraph, TextRun, BorderStyle, TabStopType }} = require('docx');
const fs = require('fs');

const BLAU    = "{BLAU}";
const GRAU    = "{GRAU}";
const MARGIN  = {MARGIN};
const PAGE_W  = {PAGE_W};
const PAGE_H  = {PAGE_H};
const CWIDTH  = PAGE_W - MARGIN * 2;

function ub(text) {{
  return [
    new Paragraph({{
      spacing: {{ before: 280, after: 40 }},
      children: [new TextRun({{ text, font: "Arial", bold: true, color: BLAU, size: 22 }})]
    }}),
    new Paragraph({{
      spacing: {{ after: 80 }},
      border: {{ bottom: {{ style: BorderStyle.SINGLE, size: 8, color: BLAU, space: 1 }} }},
      children: []
    }}),
  ];
}}

function p(text, after, opts) {{
  return new Paragraph({{
    spacing: {{ after: after || 100 }},
    children: [new TextRun(Object.assign({{ text, font: "Arial", size: 20 }}, opts || {{}}))]
  }});
}}

function erfahrung(s) {{
  const els = [];
  els.push(new Paragraph({{
    spacing: {{ after: 20 }},
    tabStops: [{{ type: TabStopType.RIGHT, position: CWIDTH }}],
    children: [
      new TextRun({{ text: s.titel, font: "Arial", bold: true, size: 20 }}),
      new TextRun({{ text: "\\t" + s.zeitraum, font: "Arial", size: 20, color: GRAU }}),
    ]
  }}));
  if (s.firma) els.push(p(s.firma, 60, {{ italics: true, color: GRAU }}));
  for (const a of s.aufgaben) {{
    els.push(new Paragraph({{
      spacing: {{ after: 40 }},
      indent: {{ left: 360 }},
      children: [new TextRun({{ text: "\\u2013 " + a, font: "Arial", size: 20 }})]
    }}));
  }}
  els.push(new Paragraph({{ spacing: {{ after: 60 }}, children: [] }}));
  return els;
}}

const stellen    = {stellen_arr};
const ausbildung = {ausbildung_arr};
const skills     = {skills_js};
const sprachen   = {sprachen_js};
const k = [];

// Kopf
k.push(new Paragraph({{ spacing: {{ after: 60 }}, children: [new TextRun({{ text: {name_js}, font: "Arial", bold: true, color: BLAU, size: 36 }})] }}));
k.push(new Paragraph({{ spacing: {{ after: 60 }}, children: [new TextRun({{ text: {kontakt_js}, font: "Arial", size: 18, color: GRAU }})] }}));
k.push(new Paragraph({{ spacing: {{ after: 80 }}, border: {{ bottom: {{ style: BorderStyle.SINGLE, size: 8, color: BLAU, space: 1 }} }}, children: [] }}));

// Profil
k.push(...ub("PROFILE"));
k.push(p({profil_js}, 120));

// Erfahrung
k.push(...ub("PROFESSIONAL EXPERIENCE"));
for (const s of stellen) {{ k.push(...erfahrung(s)); }}

// Ausbildung
k.push(...ub("EDUCATION"));
for (const a of ausbildung) {{
  k.push(new Paragraph({{
    spacing: {{ after: 20 }},
    tabStops: [{{ type: TabStopType.RIGHT, position: CWIDTH }}],
    children: [
      new TextRun({{ text: a.titel, font: "Arial", bold: true, size: 20 }}),
      new TextRun({{ text: "\\t" + a.zeitraum, font: "Arial", size: 20, color: GRAU }}),
    ]
  }}));
  if (a.ort) k.push(p(a.ort, 40, {{ italics: true, color: GRAU }}));
  for (const d of a.details) k.push(new Paragraph({{ spacing: {{ after: 30 }}, indent: {{ left: 360 }}, children: [new TextRun({{ text: d, font: "Arial", size: 20 }})] }}));
  k.push(new Paragraph({{ spacing: {{ after: 60 }}, children: [] }}));
}}

// Skills (ATS: einfache Textzeilen, keine Tabelle)
k.push(...ub("TECHNICAL SKILLS"));
for (const s of skills) k.push(p(s, 60));

// Sprachen
k.push(...ub("LANGUAGES"));
for (const s of sprachen) k.push(p(s, 60));

const doc = new Document({{
  sections: [{{ properties: {{ page: {{ size: {{ width: PAGE_W, height: PAGE_H }}, margin: {{ top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN }} }} }}, children: k }}]
}});
Packer.toBuffer(doc).then(buf => {{ fs.writeFileSync({ziel_js}, buf); console.log("OK"); }}).catch(e => {{ console.error(e.message); process.exit(1); }});
"""
    _run_node(js, f"Lebenslauf.docx → {ziel}")


# =============================================================================
# ANSCHREIBEN DOCX
# =============================================================================

def erstelle_anschreiben_docx(anschreiben: dict, abschnitte: dict, ziel: Path):
    name_js     = json.dumps(abschnitte["name"],                    ensure_ascii=False)
    kontakt_js  = json.dumps(" · ".join(abschnitte["kontakt"]),     ensure_ascii=False)
    datum_js    = json.dumps(datetime.now().strftime("%d. %B %Y"),  ensure_ascii=False)
    betreff_js  = json.dumps(anschreiben.get("betreff", ""),        ensure_ascii=False)
    anrede_js   = json.dumps(anschreiben.get("anrede", ""),         ensure_ascii=False)
    absaetze_js = json.dumps(anschreiben.get("absaetze", []),       ensure_ascii=False)
    gruss_js    = json.dumps(anschreiben.get("gruss", ""),          ensure_ascii=False)
    ziel_js     = json.dumps(str(ziel),                             ensure_ascii=False)

    js = f"""
const {{ Document, Packer, Paragraph, TextRun, AlignmentType }} = require('docx');
const fs = require('fs');

const GRAU   = "{GRAU}";
const MARGIN = {MARGIN};
const PAGE_W = {PAGE_W};
const PAGE_H = {PAGE_H};

function p(text, after, opts) {{
  return new Paragraph({{
    spacing: {{ after: after || 140 }},
    children: [new TextRun(Object.assign({{ text, font: "Arial", size: 20 }}, opts || {{}}))]
  }});
}}

const k = [];

// Absender
k.push(p({name_js},    0, {{ bold: true, size: 22 }}));
k.push(p({kontakt_js}, 0, {{ color: GRAU }}));
k.push(new Paragraph({{ spacing: {{ after: 0 }}, children: [] }}));

// Datum
k.push(new Paragraph({{ spacing: {{ after: 280 }}, alignment: AlignmentType.RIGHT, children: [new TextRun({{ text: {datum_js}, font: "Arial", size: 20 }})] }}));

// Betreff
k.push(p({betreff_js}, 240, {{ bold: true, size: 24 }}));

// Anrede
k.push(p({anrede_js}, 180));

// Absätze
for (const a of {absaetze_js}) k.push(p(a, 140));

// Gruss + Unterschrift
k.push(p({gruss_js},  480));
k.push(p({name_js},   0));

const doc = new Document({{
  sections: [{{ properties: {{ page: {{ size: {{ width: PAGE_W, height: PAGE_H }}, margin: {{ top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN }} }} }}, children: k }}]
}});
Packer.toBuffer(doc).then(buf => {{ fs.writeFileSync({ziel_js}, buf); console.log("OK"); }}).catch(e => {{ console.error(e.message); process.exit(1); }});
"""
    _run_node(js, f"Anschreiben.docx → {ziel}")


# =============================================================================
# HAUPTFUNKTION
# =============================================================================

def generiere_bewerbung(txt_pfad) -> dict:
    """
    Liest TXT, generiert Lebenslauf.docx + Anschreiben.docx.
    Rückgabe: { ok, lebenslauf, anschreiben } oder { ok, fehler }
    """
    txt_pfad = Path(txt_pfad)
    if not txt_pfad.exists():
        return {"ok": False, "fehler": f"TXT nicht gefunden: {txt_pfad}"}

    print(f"\n{'='*55}")
    print(f"  BEWERBUNG GENERATOR")
    print(f"{'='*55}")

    config     = lade_config()
    client     = anthropic_lib.Anthropic(api_key=config["api_key"])
    meta       = parse_txt(txt_pfad)
    abschnitte = parse_lebenslauf_abschnitte(meta["lebenslauf_text"])

    print(f"  👤 {abschnitte['name']}")
    print(f"  🏢 {meta['firma']} – {meta['titel']}")

    # Stelle für Anschreiben nachschlagen
    stellen = json.loads(STELLEN_JSON.read_text(encoding="utf-8")) if STELLEN_JSON.exists() else []
    stelle  = next((s for s in stellen if s.get("url") == meta["url"]), {
        "firma": meta["firma"], "titel": meta["titel"], "beschreibung": ""
    })

    ordner  = txt_pfad.parent
    lv_pfad = ordner / "Lebenslauf.docx"
    as_pfad = ordner / "Anschreiben.docx"

    # Lebenslauf.docx
    print("  📝 Erstelle Lebenslauf.docx ...")
    try:
        erstelle_lebenslauf_docx(abschnitte, lv_pfad)
    except Exception as e:
        return {"ok": False, "fehler": f"Lebenslauf.docx: {e}"}

    # Anschreiben via AI + DOCX
    print("  🤖 Generiere Anschreiben ...")
    anschreiben = generiere_anschreiben_text(meta["lebenslauf_text"], stelle, client)

    print("  📝 Erstelle Anschreiben.docx ...")
    try:
        erstelle_anschreiben_docx(anschreiben, abschnitte, as_pfad)
    except Exception as e:
        return {"ok": False, "fehler": f"Anschreiben.docx: {e}"}

    print(f"  ✅ Fertig: {ordner}")
    return {"ok": True, "lebenslauf": str(lv_pfad), "anschreiben": str(as_pfad)}


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Nutzung: python bewerbung_generator.py /pfad/zu/Lebenslauf.txt")
        sys.exit(1)
    ergebnis = generiere_bewerbung(Path(sys.argv[1]))
    if not ergebnis["ok"]:
        print(f"❌ {ergebnis['fehler']}")
        sys.exit(1)
