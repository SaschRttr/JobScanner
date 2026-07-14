"""
docx_patcher.py  –  Lebenslauf DOCX mit Tracked Changes erzeugen
=================================================================
Vergleicht den KI-angepassten Text (Lebenslauf.txt) mit der
DOCX-Vorlage (lebenslauf_vorlage.docx) und schreibt alle
Abweichungen als Tracked Changes (Änderungen verfolgen) in
eine neue Lebenslauf.docx.

Abschnitte werden per Marker in der TXT erkannt:
  ---ABSCHNITT---
  Inhalt
  ---/ABSCHNITT---

Nutzung (direkt):
  python docx_patcher.py <lebenslauf.txt> <vorlage.docx> <ausgabe.docx>

Nutzung (als Modul):
  from docx_patcher import erzeuge_docx_mit_changes
  erzeuge_docx_mit_changes(txt_pfad, vorlage_pfad, ausgabe_pfad)
"""

import difflib
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

# XML-Namespaces die Word verwendet
NS = {
    "w":  "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r":  "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

AUTOR    = "JobScanner"
DATUM    = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# SCHRITT 1: TXT-Abschnitte einlesen
# =============================================================================

def lese_abschnitte_txt(txt_pfad: Path) -> dict:
    """
    Liest alle Marker-Abschnitte aus der angepassten TXT-Datei.
    Gibt dict zurück: { "KOMPETENZPROFIL": "...", "STELLE_1_AUFGABEN": "...", ... }
    Unterstützt verschachtelte Marker (z.B. STELLE_1_AUFGABEN innerhalb BERUFSERFAHRUNG).
    """
    inhalt = txt_pfad.read_text(encoding="utf-8")
    abschnitte = {}
    stack = []

    for line in inhalt.splitlines():
        open_match  = re.match(r"^---([A-Z0-9_]+)---$", line.strip())
        close_match = re.match(r"^---/([A-Z0-9_]+)---$", line.strip())

        if open_match:
            stack.append((open_match.group(1), []))
        elif close_match and stack:
            name, zeilen = stack.pop()
            abschnitte[name] = "\n".join(zeilen).strip()
        elif stack:
            stack[-1][1].append(line)

    return abschnitte


# =============================================================================
# SCHRITT 2: DOCX entpacken / packen
# =============================================================================

def entpacke_docx(docx_pfad: Path, ziel_ordner: Path):
    """Entpackt die DOCX (ZIP) in einen Ordner."""
    ziel_ordner.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(docx_pfad, "r") as z:
        z.extractall(ziel_ordner)


def packe_docx(quell_ordner: Path, ausgabe_pfad: Path):
    """Packt den Ordner wieder als DOCX (ZIP)."""
    with zipfile.ZipFile(ausgabe_pfad, "w", zipfile.ZIP_DEFLATED) as z:
        for datei in quell_ordner.rglob("*"):
            if datei.is_file():
                z.write(datei, datei.relative_to(quell_ordner))


# =============================================================================
# SCHRITT 3: XML-Hilfsfunktionen
# =============================================================================

def _registriere_word_namespaces():
    """Registriert alle von Word verwendeten Namespaces, damit sie beim Schreiben erhalten bleiben."""
    ET.register_namespace("wpc", "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas")
    ET.register_namespace("cx",  "http://schemas.microsoft.com/office/drawing/2014/chartex")
    ET.register_namespace("cx1", "http://schemas.microsoft.com/office/drawing/2015/9/8/chartex")
    ET.register_namespace("cx2", "http://schemas.microsoft.com/office/drawing/2015/10/21/chartex")
    ET.register_namespace("cx3", "http://schemas.microsoft.com/office/drawing/2016/5/9/chartex")
    ET.register_namespace("cx4", "http://schemas.microsoft.com/office/drawing/2016/5/10/chartex")
    ET.register_namespace("cx5", "http://schemas.microsoft.com/office/drawing/2016/5/11/chartex")
    ET.register_namespace("cx6", "http://schemas.microsoft.com/office/drawing/2016/5/12/chartex")
    ET.register_namespace("cx7", "http://schemas.microsoft.com/office/drawing/2016/5/13/chartex")
    ET.register_namespace("cx8", "http://schemas.microsoft.com/office/drawing/2016/5/14/chartex")
    ET.register_namespace("mc",  "http://schemas.openxmlformats.org/markup-compatibility/2006")
    ET.register_namespace("aink","http://schemas.microsoft.com/office/drawing/2016/ink")
    ET.register_namespace("am3d","http://schemas.microsoft.com/office/drawing/2017/model3d")
    ET.register_namespace("o",   "urn:schemas-microsoft-com:office:office")
    ET.register_namespace("oel", "http://schemas.microsoft.com/office/2019/extlst")
    ET.register_namespace("r",   "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
    ET.register_namespace("m",   "http://schemas.openxmlformats.org/officeDocument/2006/math")
    ET.register_namespace("v",   "urn:schemas-microsoft-com:vml")
    ET.register_namespace("wp14","http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing")
    ET.register_namespace("wp",  "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing")
    ET.register_namespace("w10", "urn:schemas-microsoft-com:office:word")
    ET.register_namespace("w",   "http://schemas.openxmlformats.org/wordprocessingml/2006/main")
    ET.register_namespace("w14", "http://schemas.microsoft.com/office/word/2010/wordml")
    ET.register_namespace("w15", "http://schemas.microsoft.com/office/word/2012/wordml")
    ET.register_namespace("w16cex","http://schemas.microsoft.com/office/word/2018/wordml/cex")
    ET.register_namespace("w16cid","http://schemas.microsoft.com/office/word/2016/wordml/cid")
    ET.register_namespace("w16",  "http://schemas.microsoft.com/office/word/2018/wordml")
    ET.register_namespace("w16sdtdh","http://schemas.microsoft.com/office/word/2020/wordml/sdtdatahash")
    ET.register_namespace("w16se","http://schemas.microsoft.com/office/word/2015/wordml/symex")
    ET.register_namespace("wpg", "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup")
    ET.register_namespace("wpi", "http://schemas.microsoft.com/office/word/2010/wordprocessingInk")
    ET.register_namespace("wne", "http://schemas.microsoft.com/office/word/2006/wordml")
    ET.register_namespace("wps", "http://schemas.microsoft.com/office/word/2010/wordprocessingShape")


def get_para_text(para) -> str:
    """Gibt den vollen Textinhalt eines <w:p> Elements zurück."""
    teile = []
    for node in para.iter():
        if node.tag == f"{{{W}}}t" and node.text:
            teile.append(node.text)
    return "".join(teile)


def baue_del_ins(alter_text: str, neuer_text: str, change_id: int):
    """
    Erzeugt ein <w:del> + <w:ins> Element-Paar als Liste.
    Beide sind direkte Kinder von <w:p> (nicht innerhalb <w:r>).
    """
    elemente = []

    # Löschung (alter Text rot durchgestrichen)
    del_el = ET.Element(f"{{{W}}}del")
    del_el.set(f"{{{W}}}id",     str(change_id))
    del_el.set(f"{{{W}}}author", AUTOR)
    del_el.set(f"{{{W}}}date",   DATUM)
    del_r = ET.SubElement(del_el, f"{{{W}}}r")
    del_rpr = ET.SubElement(del_r, f"{{{W}}}rPr")  # leeres rPr
    del_t = ET.SubElement(del_r, f"{{{W}}}delText")
    del_t.text = alter_text
    if alter_text and (alter_text[0] == " " or alter_text[-1] == " "):
        del_t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    elemente.append(del_el)

    # Einfügung (neuer Text rot unterstrichen)
    ins_el = ET.Element(f"{{{W}}}ins")
    ins_el.set(f"{{{W}}}id",     str(change_id + 1))
    ins_el.set(f"{{{W}}}author", AUTOR)
    ins_el.set(f"{{{W}}}date",   DATUM)
    ins_r = ET.SubElement(ins_el, f"{{{W}}}r")
    ins_rpr = ET.SubElement(ins_r, f"{{{W}}}rPr")  # leeres rPr
    ins_t = ET.SubElement(ins_r, f"{{{W}}}t")
    ins_t.text = neuer_text
    if neuer_text and (neuer_text[0] == " " or neuer_text[-1] == " "):
        ins_t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    elemente.append(ins_el)

    return elemente


def ersetze_para_inhalt_mit_change(para, alter_text: str, neuer_text: str, change_id: int):
    """
    Ersetzt den Inhalt eines <w:p> durch ein del/ins Paar.
    Entfernt alle bestehenden <w:r> Kinder, fügt del+ins ein.
    """
    # Alle <w:r> und <w:ins>/<w:del> entfernen
    kinder_entfernen = [
        kind for kind in list(para)
        if kind.tag in (
            f"{{{W}}}r",
            f"{{{W}}}ins",
            f"{{{W}}}del",
            f"{{{W}}}hyperlink",
        )
    ]
    for kind in kinder_entfernen:
        para.remove(kind)

    # del + ins anhängen
    for el in baue_del_ins(alter_text, neuer_text, change_id):
        para.append(el)


def ersetze_para_text_direkt(para, neuer_text: str):
    """
    Ersetzt den Inhalt eines <w:p> direkt durch reinen Text (ohne Tracked Changes).
    Wird nur beim einmaligen Bau übersetzter Vorlagen verwendet (nicht im laufenden
    Anpassungs-Workflow, dort werden Änderungen immer als del/ins nachverfolgt).
    """
    kinder_entfernen = [
        kind for kind in list(para)
        if kind.tag in (
            f"{{{W}}}r",
            f"{{{W}}}ins",
            f"{{{W}}}del",
            f"{{{W}}}hyperlink",
        )
    ]
    for kind in kinder_entfernen:
        para.remove(kind)

    r = ET.SubElement(para, f"{{{W}}}r")
    ET.SubElement(r, f"{{{W}}}rPr")  # leeres rPr
    t = ET.SubElement(r, f"{{{W}}}t")
    t.text = neuer_text
    if neuer_text and (neuer_text[0] == " " or neuer_text[-1] == " "):
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


# =============================================================================
# SCHRITT 4: Abschnitte in XML suchen und patchen
# =============================================================================

def finde_paras_fuer_abschnitt(root, marker_text: str):
    """
    Sucht Absätze die einen bestimmten Marker-Text enthalten.
    Gibt eine Liste von (paragraph_element, paragraph_text) zurück.
    Wird benutzt um die Position im Dokument zu finden.
    """
    gefunden = []
    for para in root.iter(f"{{{W}}}p"):
        text = get_para_text(para)
        if marker_text.lower() in text.lower():
            gefunden.append((para, text))
    return gefunden


def finde_para_ab_position(paragraphen: list, start_index: int, marker_text: str):
    """
    Sucht ab start_index (einschließlich) den ersten Absatz der marker_text enthält.
    Absätze VOR start_index werden ignoriert – verhindert, dass ein Bullet mit
    ähnlichem Wortlaut aus einer bereits verarbeiteten, früheren Stelle (z.B.
    STELLE_1) fälschlich getroffen wird, wenn gerade eine spätere Stelle
    (z.B. STELLE_2) gepatcht wird.
    Gibt (index, paragraph) zurück, oder (None, None) wenn nichts gefunden wurde.
    """
    marker_lower = marker_text.lower()
    for i in range(start_index, len(paragraphen)):
        if marker_lower in get_para_text(paragraphen[i]).lower():
            return i, paragraphen[i]
    return None, None


def patch_abschnitt(paragraphen: list, cursor: int, vorlage_text: str, neu_text: str, change_id_start: int):
    """
    Vergleicht vorlage_text und neu_text per Zeilen-Diff (difflib), nicht per
    Positions-Index – so bleiben nachfolgende Zeilen korrekt zugeordnet, auch
    wenn Zeilen eingefügt, gelöscht oder umsortiert wurden.
    Für jede geänderte Zeile: sucht den passenden Absatz im XML – aber nur AB
    der aktuellen Cursor-Position, nie davor – und ersetzt ihn mit einem
    Tracked Change. Der Cursor wandert dabei monoton durchs Dokument, damit
    Änderungen nicht versehentlich in einer anderen (z.B. früheren) Stelle
    landen, nur weil dort ein ähnlicher Satz vorkommt.
    Gibt (nächste freie change_id, neue Cursor-Position) zurück.
    """
    change_id = change_id_start

    vorlage_zeilen = [z.strip() for z in vorlage_text.splitlines() if z.strip()]
    neu_zeilen     = [z.strip() for z in neu_text.splitlines()     if z.strip()]

    matcher = difflib.SequenceMatcher(None, vorlage_zeilen, neu_zeilen, autojunk=False)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        alte_teilzeilen = vorlage_zeilen[i1:i2]
        neue_teilzeilen = neu_zeilen[j1:j2]

        # Innerhalb des geänderten Blocks paarweise zuordnen (soweit vorhanden)
        for k in range(max(len(alte_teilzeilen), len(neue_teilzeilen))):
            alt = alte_teilzeilen[k] if k < len(alte_teilzeilen) else ""
            neu = neue_teilzeilen[k] if k < len(neue_teilzeilen) else ""

            if not alt:
                # Neue Zeile ohne Entsprechung in Vorlage – überspringen
                continue

            # Absatz im XML finden
            # Bullets in der TXT haben "- " vorne, im DOCX-XML nicht → abschneiden
            alt_bereinigt = alt.lstrip("- ").strip()
            neu_bereinigt = neu.lstrip("- ").strip()

            if alt_bereinigt == neu_bereinigt:
                continue  # keine Änderung

            suchtext = alt_bereinigt[:40]
            index, para = finde_para_ab_position(paragraphen, cursor, suchtext)

            if para is None:
                print(f"  ⚠️  Absatz nicht im XML gefunden: '{alt_bereinigt[:60]}'")
                continue

            # Tracked Change ohne "- " speichern (Formatierung kommt vom DOCX)
            ersetze_para_inhalt_mit_change(para, alt_bereinigt, neu_bereinigt, change_id)
            change_id += 2  # del braucht eine ID, ins die nächste
            cursor = index + 1
            print(f"  ✏️  Tracked Change eingefügt (ID {change_id-2}/{change_id-1})")

    return change_id, cursor


# =============================================================================
# HAUPTFUNKTION
# =============================================================================

def erzeuge_docx_mit_changes(
    txt_pfad:    Path,
    vorlage_pfad: Path,
    ausgabe_pfad: Path,
    vorlage_txt_pfad: Path = None,
) -> bool:
    """
    Hauptfunktion: Erzeugt eine DOCX mit Tracked Changes.

    txt_pfad:         Angepasster Lebenslauf (KI-Output, .txt)
    vorlage_pfad:     Basis-DOCX (lebenslauf_vorlage.docx)
    ausgabe_pfad:     Ziel-DOCX (Lebenslauf.docx)
    vorlage_txt_pfad: Optional – lebenslauf_vorlage.txt für Marker-Vergleich
                      (Standard: lebenslauf_vorlage.txt neben der DOCX)

    Gibt True zurück wenn erfolgreich, sonst False.
    """
    print(f"\n  📄 DOCX Patcher gestartet")
    print(f"     TXT:     {txt_pfad}")
    print(f"     Vorlage: {vorlage_pfad}")
    print(f"     Ausgabe: {ausgabe_pfad}")

    # Pfade prüfen
    if not txt_pfad.exists():
        print(f"  ❌ TXT nicht gefunden: {txt_pfad}")
        return False
    if not vorlage_pfad.exists():
        print(f"  ❌ DOCX-Vorlage nicht gefunden: {vorlage_pfad}")
        return False

    # Vorlage TXT finden (für Marker-Vergleich)
    if vorlage_txt_pfad is None:
        vorlage_txt_pfad = vorlage_pfad.parent / "lebenslauf_vorlage.txt"
    if not vorlage_txt_pfad.exists():
        print(f"  ❌ lebenslauf_vorlage.txt nicht gefunden: {vorlage_txt_pfad}")
        return False

    # Abschnitte aus beiden TXT-Dateien lesen
    neu_abschnitte      = lese_abschnitte_txt(txt_pfad)
    vorlage_abschnitte  = lese_abschnitte_txt(vorlage_txt_pfad)

    if not neu_abschnitte:
        print(f"  ❌ Keine Marker-Abschnitte in TXT gefunden")
        return False

    geaendert = [
        name for name, inhalt in neu_abschnitte.items()
        if inhalt != vorlage_abschnitte.get(name, "")
    ]
    print(f"  📌 Geänderte Abschnitte: {', '.join(geaendert) or 'keine'}")

    if not geaendert:
        # Keine Änderungen – einfach Vorlage kopieren
        shutil.copy2(vorlage_pfad, ausgabe_pfad)
        print(f"  ℹ️  Keine Änderungen – Vorlage unverändert kopiert")
        return True

    # DOCX entpacken
    tmp_dir = ausgabe_pfad.parent / "_docx_tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    entpacke_docx(vorlage_pfad, tmp_dir)

    # document.xml parsen
    doc_xml = tmp_dir / "word" / "document.xml"
    if not doc_xml.exists():
        print(f"  ❌ document.xml nicht in DOCX gefunden")
        shutil.rmtree(tmp_dir)
        return False

    _registriere_word_namespaces()

    tree = ET.parse(doc_xml)
    root = tree.getroot()
    paragraphen = list(root.iter(f"{{{W}}}p"))

    # Für jeden geänderten Abschnitt Tracked Changes einbauen. Der Such-Cursor
    # wandert dabei über alle Abschnitte hinweg monoton vorwärts durchs Dokument
    # (siehe patch_abschnitt), damit ein Bullet nicht versehentlich in einer
    # anderen Stelle landet, nur weil dort ein ähnlicher Satz vorkommt.
    change_id = 200
    cursor = 0
    for name in geaendert:
        alt = vorlage_abschnitte.get(name, "")
        neu = neu_abschnitte[name]
        print(f"\n  🔧 Patche Abschnitt: {name}")
        change_id, cursor = patch_abschnitt(paragraphen, cursor, alt, neu, change_id)

    # Geänderte document.xml zurückschreiben
    tree.write(doc_xml, xml_declaration=True, encoding="UTF-8")

    # Zurück zu DOCX packen
    packe_docx(tmp_dir, ausgabe_pfad)
    shutil.rmtree(tmp_dir)

    print(f"\n  ✅ Fertig: {ausgabe_pfad}")
    return True


# =============================================================================
# EINMALIGER BAU: ÜBERSETZTE DOCX-VORLAGE AUS DER DEUTSCHEN DOCX
# =============================================================================

def baue_uebersetzte_docx(
    vorlage_de_docx: Path,
    vorlage_de_txt:  Path,
    vorlage_en_txt:  Path,
    ausgabe_docx:    Path,
    marker:          list[str],
) -> bool:
    """
    Baut eine übersetzte DOCX-Vorlage aus der deutschen DOCX, indem für jeden
    Marker die Absätze per deutschem Text im XML gesucht und ihr Inhalt direkt
    (ohne Tracked Changes) durch den passenden Text aus vorlage_en_txt ersetzt
    wird. Kein Teil des laufenden Anpassungs-Workflows – dient nur dazu, einmalig
    lebenslauf_vorlage_en.docx / anschreiben_vorlage_en.docx aus der jeweiligen
    deutschen Vorlage + bereits übersetzter TXT zu erzeugen.
    """
    if not vorlage_de_docx.exists():
        print(f"  ❌ DOCX-Vorlage nicht gefunden: {vorlage_de_docx}")
        return False

    de_abschnitte = lese_abschnitte_txt(vorlage_de_txt)
    en_abschnitte = lese_abschnitte_txt(vorlage_en_txt)

    tmp_dir = ausgabe_docx.parent / "_docx_tmp_uebersetzung"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    entpacke_docx(vorlage_de_docx, tmp_dir)

    doc_xml = tmp_dir / "word" / "document.xml"
    if not doc_xml.exists():
        print(f"  ❌ document.xml nicht in DOCX gefunden")
        shutil.rmtree(tmp_dir)
        return False

    _registriere_word_namespaces()

    tree = ET.parse(doc_xml)
    root = tree.getroot()

    for name in marker:
        de_text = de_abschnitte.get(name, "")
        en_text = en_abschnitte.get(name, "")
        if not de_text or not en_text or de_text == en_text:
            continue

        de_zeilen = [z.strip() for z in de_text.splitlines() if z.strip()]
        en_zeilen = [z.strip() for z in en_text.splitlines() if z.strip()]

        for i in range(min(len(de_zeilen), len(en_zeilen))):
            alt = de_zeilen[i].lstrip("- ").strip()
            neu = en_zeilen[i].lstrip("- ").strip()
            if alt == neu:
                continue

            treffer = finde_paras_fuer_abschnitt(root, alt[:40])
            if not treffer:
                print(f"  ⚠️  [{name}] Absatz nicht im XML gefunden: '{alt[:60]}'")
                continue

            para, _ = treffer[0]
            ersetze_para_text_direkt(para, neu)

        if len(de_zeilen) != len(en_zeilen):
            print(f"  ⚠️  [{name}] Zeilenanzahl DE ({len(de_zeilen)}) != EN ({len(en_zeilen)}) – nur die ersten {min(len(de_zeilen), len(en_zeilen))} Zeilen übernommen")

    tree.write(doc_xml, xml_declaration=True, encoding="UTF-8")
    packe_docx(tmp_dir, ausgabe_docx)
    shutil.rmtree(tmp_dir)

    print(f"  ✅ Übersetzte Vorlage gespeichert: {ausgabe_docx}")
    return True


def ersetze_exakte_textknoten(docx_pfad: Path, mapping: dict) -> int:
    """
    Ersetzt in einer DOCX alle <w:t>-Textknoten, deren Inhalt EXAKT einem Key
    in `mapping` entspricht, durch den zugehörigen Value. Wird für Absätze
    verwendet, in denen mehrere Zeilen (Datum, Titel, Firma) über <w:tab/>
    und <w:br/> in EINEM <w:p> zusammengefasst sind – dort schlägt die
    zeilenweise Absatzsuche fehl (finde_paras_fuer_abschnitt sucht ganze
    Absätze, keine einzelnen Runs), weshalb hier direkt auf Run-Ebene ersetzt
    wird. Gibt die Anzahl ersetzter Textknoten zurück.
    """
    tmp_dir = docx_pfad.parent / "_docx_tmp_textknoten"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    entpacke_docx(docx_pfad, tmp_dir)

    doc_xml = tmp_dir / "word" / "document.xml"
    _registriere_word_namespaces()
    tree = ET.parse(doc_xml)
    root = tree.getroot()

    anzahl = 0
    for t in root.iter(f"{{{W}}}t"):
        if t.text in mapping:
            t.text = mapping[t.text]
            anzahl += 1

    tree.write(doc_xml, xml_declaration=True, encoding="UTF-8")
    packe_docx(tmp_dir, docx_pfad)
    shutil.rmtree(tmp_dir)
    return anzahl


# =============================================================================
# DIREKT AUFRUFEN
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Nutzung: python docx_patcher.py <lebenslauf.txt> <vorlage.docx> <ausgabe.docx>")
        sys.exit(1)

    ok = erzeuge_docx_mit_changes(
        txt_pfad     = Path(sys.argv[1]),
        vorlage_pfad = Path(sys.argv[2]),
        ausgabe_pfad = Path(sys.argv[3]),
    )
    sys.exit(0 if ok else 1)