"""
browser.py  –  Gemeinsame Scan-Konstanten und Playwright-Browser-Start
=======================================================================
Wird von scanner.py, rohtext_holen.py, vergaben_check.py und webui.py genutzt.
Importiert Playwright selbst NICHT auf Modulebene – die Funktionen bekommen
die sync_playwright-Instanz übergeben, damit z.B. vergaben_check.py nur die
Konstanten nutzen kann, ohne dass Playwright installiert sein muss.
"""

import io
import platform
import urllib.request

try:
    from playwright_stealth import Stealth
    STEALTH_OK = True
except ImportError:
    STEALTH_OK = False

try:
    import pypdf
    PYPDF_OK = True
except ImportError:
    PYPDF_OK = False


BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

MIN_ROHTEXT_LAENGE = 500   # Rohtext unter dieser Länge gilt als unvollständig


def starte_browser(p):
    """Startet Chromium headless; auf Linux (Raspi) mit System-Chromium."""
    kwargs = {"headless": True, "args": BROWSER_ARGS}
    if platform.system() == "Linux":
        kwargs["executable_path"] = "/usr/bin/chromium-browser"
    return p.chromium.launch(**kwargs)


def neuer_context(browser, extra_headers: dict | None = None):
    return browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="de-DE",
        user_agent=USER_AGENT,
        extra_http_headers={
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            **(extra_headers or {}),
        },
    )


def neue_seite(context):
    """Neue Seite mit Stealth (falls installiert) und webdriver-Maskierung."""
    seite = context.new_page()
    if STEALTH_OK:
        try:
            Stealth().apply_stealth_sync(seite)
        except Exception:
            pass
    seite.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return seite


def lade_pdf_text(url: str) -> str | None:
    """Lädt ein PDF per HTTP und extrahiert den Text (pypdf)."""
    if not PYPDF_OK:
        print("  ⚠️  pypdf nicht installiert – PDF übersprungen")
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        reader = pypdf.PdfReader(io.BytesIO(data))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
        return text or None
    except Exception as e:
        print(f"  ❌ PDF-Fehler: {e}")
        return None
