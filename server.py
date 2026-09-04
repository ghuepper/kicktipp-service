"""
Kicktipp Submit-Service v2 (FastAPI + Playwright)
=================================================
Nimmt Tipps per POST /submit-tips entgegen und trägt sie auf der
Kicktipp-Tippabgabeseite per NAMENSABGLEICH ein (Reihenfolge egal).

Korrigiert gegenüber der Vorversion:
1. Login wird verifiziert (harter Abbruch statt irreführender Folgefehler)
2. Selektor findet auch input[type='tel'] (Kicktipps Tippfelder)
3. get_core_name arbeitet tokenweise statt kaskadierend auf dem String
4. Fehlender Speichern-Button => harter Fehler statt stillem "Erfolg"
5. Verifikation liest nach dem Speichern die Feldwerte zurück und
   prüft gezielt auf den Kicktipp-Warnbanner
6. Payload-Tipps für bereits gesperrte Spiele werden als "locked"
   gemeldet statt als Fehler; unauffindbare Spiele bleiben harte Fehler
7. Optionaler Token-Schutz: Ist KT_TOKEN gesetzt, muss der Header
   X-Auth-Token mitgesendet werden (in n8n: Send Headers aktivieren)

Umgebungsvariablen:
  KT_USER / KICKTIPP_EMAIL        Login-E-Mail
  KT_PASS / KICKTIPP_PASSWORD     Passwort
  KT_COMMUNITY / KICKTIPP_TIPPRUNDE  Tipprunden-Name (Default: kicktipp-muenster)
  KT_TOKEN                        optional: geheimer Token für X-Auth-Token
"""

import os
import re
import unicodedata
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI()


class TipItem(BaseModel):
    home: str
    away: str
    tipHome: int
    tipAway: int


class TipPayload(BaseModel):
    tips: list[TipItem]


# ------------------------------------------------------------
# Namensnormalisierung — identisch zur n8n-Code-Node
# ------------------------------------------------------------

def norm(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    s = s.replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


# Vereins-Präfixe, die KEINE Unterscheidungskraft haben. Tokenweise
# entfernt (auf den Wörtern des Originalnamens, NICHT kaskadierend auf
# dem zusammengeklebten String — das erzeugte vorher Fragmente wie
# "nmunchen" aus "FC Bayern München").
PREFIX_TOKENS = {
    "1", "04", "05", "09", "96", "98", "1899", "1846", "1860",
    "fc", "sv", "svw", "vfl", "sc", "vfb", "tsv", "tsg", "spvgg",
    "fsv", "tus", "rb", "bor", "borussia", "bayer", "eintracht",
    "fortuna", "hertha", "arminia", "hannover", "werder",
}
# Achtung: "hannover"/"werder" nur drin lassen, wenn die Stadt allein
# eindeutig ist ("hannover 96" -> "96" wäre sonst der Kern). Praktisch
# nehmen wir den LÄNGSTEN verbleibenden Token als Kern und fallen bei
# Leere auf den vollen Namen zurück — damit ist die Liste unkritisch.


def get_core_name(name: str) -> str:
    """Kern eines Teamnamens: Präfix-Tokens abwerfen, längstes
    verbleibendes Wort normalisiert zurückgeben. Fallback: voller Name."""
    tokens = re.split(r"[\s.\-/]+", name.strip())
    remaining = [t for t in tokens if t and norm(t) not in PREFIX_TOKENS]
    if not remaining:
        return norm(name)
    core = max(remaining, key=lambda t: len(norm(t)))
    core_n = norm(core)
    return core_n if core_n else norm(name)


# ------------------------------------------------------------
# Seiten-Helfer
# ------------------------------------------------------------

# Kicktipp nutzt für Tippfelder input[type=tel] (mobile Zifferntastatur);
# text/number bleiben als Rückfallebene drin.
INPUT_SELECTOR = (
    "input[type='tel'], input[type='text'], "
    "input[type='number'], input.tippfeld"
)


async def scan_rows(page):
    """Alle Tabellenzeilen mit (aktiven oder gesperrten) Tippfeldern
    einsammeln."""
    rows = []
    for tr in await page.locator("tr").all():
        inputs = await tr.locator(INPUT_SELECTOR).all()
        if not inputs:
            continue
        text = await tr.inner_text()
        active, locked = [], False
        for inp in inputs:
            if not await inp.is_visible():
                continue
            disabled = await inp.get_attribute("disabled")
            readonly = await inp.get_attribute("readonly")
            if disabled is not None or readonly is not None:
                locked = True
            else:
                active.append(inp)
        if active or locked:
            rows.append({
                "tr": tr,
                "text": text,
                "norm": norm(text),
                "inputs": active,
                "is_locked": locked or len(active) < 2,
            })
    return rows


def match_tip_to_row(tip: TipItem, rows, used_row_ids):
    """Findet die Seitenzeile zu einem Payload-Tipp. Gibt (row, None)
    oder (None, Fehlertext) zurück; kein Treffer -> (None, None)."""
    h_core = get_core_name(tip.home)
    a_core = get_core_name(tip.away)
    hits = [
        r for i, r in enumerate(rows)
        if i not in used_row_ids
        and h_core in r["norm"] and a_core in r["norm"]
    ]
    if len(hits) > 1:
        return None, (
            f"Mehrdeutiger Namensabgleich für '{tip.home} - {tip.away}' "
            f"(Kerne '{h_core}'/'{a_core}'): {len(hits)} Zeilen passen."
        )
    return (hits[0] if hits else None), None


# ------------------------------------------------------------
# Endpunkte
# ------------------------------------------------------------

@app.get("/")
async def health():
    return {"status": "ok"}


@app.post("/submit-tips")
async def submit_tips(
    payload: TipPayload,
    x_auth_token: str | None = Header(None, alias="X-Auth-Token"),
):
    # --- Optionaler Token-Schutz ---
    expected_token = os.getenv("KT_TOKEN")
    if expected_token and x_auth_token != expected_token:
        raise HTTPException(status_code=401, detail="Ungültiger oder fehlender X-Auth-Token.")

    email = os.getenv("KT_USER") or os.getenv("KICKTIPP_EMAIL")
    password = os.getenv("KT_PASS") or os.getenv("KICKTIPP_PASSWORD")
    tipprunde = (
        os.getenv("KT_COMMUNITY")
        or os.getenv("KICKTIPP_TIPPRUNDE")
        or "kicktipp-muenster"
    )
    if not email or not password:
        raise HTTPException(status_code=500, detail="Umgebungsvariablen KT_USER / KT_PASS fehlen.")

    if not payload.tips:
        raise HTTPException(status_code=400, detail="Payload enthält keine Tipps.")

    tippabgabe_url = f"https://www.kicktipp.de/{tipprunde}/tippabgabe"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            # ---------- 1. Login ----------
            await page.goto(
                f"https://www.kicktipp.de/{tipprunde}/profil/login",
                wait_until="networkidle",
            )

            try:
                cookie_button = page.locator(
                    "button:has-text('Akzeptieren'), "
                    "button:has-text('Zustimmen'), #cmpwelcomebtnyes"
                ).first
                await cookie_button.wait_for(state="visible", timeout=3000)
                await cookie_button.click()
            except Exception:
                pass  # kein Banner — in Ordnung

            await page.fill('input[name="kennung"]', email)
            await page.fill('input[name="passwort"]', password)

            # Submit-Button NUR innerhalb des Login-Formulars suchen —
            # der frühere seitenweite Selektor konnte den Cookie-Banner-
            # Button treffen, dann wurden die Zugangsdaten nie abgeschickt.
            login_form = page.locator('form:has(input[name="passwort"])')
            login_btn = login_form.locator(
                'button[type="submit"], input[type="submit"], input[name="submitbutton"]'
            ).first
            if await login_btn.count() > 0:
                await login_btn.evaluate("node => node.click()")
            else:
                # Fallback: Formular per Enter absenden
                await page.press('input[name="passwort"]', "Enter")
            await page.wait_for_load_state("networkidle")

            # Login VERIFIZIEREN — aber richtig: nicht über die URL
            # (die matcht auch ".../loginaction" nach ERFOLGREICHEM Login),
            # sondern über die Frage, ob das Login-Formular noch da ist.
            still_login_form = await page.locator('input[name="kennung"]').count() > 0
            if still_login_form:
                # Kicktipps eigene Fehlermeldung mitnehmen (z. B. falsches Passwort)
                kt_error = ""
                err_loc = page.locator(".errorbox, .error, .alert, .warning, .validationmessage")
                if await err_loc.count() > 0:
                    try:
                        kt_error = (await err_loc.first.inner_text()).strip()
                    except Exception:
                        pass
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Login fehlgeschlagen — Login-Formular weiterhin sichtbar "
                        f"(URL: {page.url}). "
                        + (f"Kicktipp meldet: '{kt_error}'. " if kt_error else "")
                        + "Zugangsdaten (KT_USER/KT_PASS auf dem Server) und "
                          "Cookie-Banner prüfen."
                    ),
                )

            # ---------- 2. Tippabgabe öffnen und Zeilen scannen ----------
            await page.goto(tippabgabe_url, wait_until="networkidle")
            rows = await scan_rows(page)
            if not rows:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Keine Tipp-Zeilen mit Eingabefeldern gefunden. "
                        f"Aktuelle URL: {page.url}, Titel: '{await page.title()}'. "
                        "Selektor, Login oder Spieltagsseite prüfen."
                    ),
                )

            # ---------- 3. Payload -> Zeilen zuordnen und ausfüllen ----------
            page_text_norm = norm(await page.inner_text("body"))
            used_row_ids: set[int] = set()
            tipped, locked_skipped, not_found = [], [], []

            for tip in payload.tips:
                row, err = None, None
                for i, r in enumerate(rows):
                    if i in used_row_ids:
                        continue
                    # inline statt Funktionsaufruf, um used_row_ids sauber zu führen
                    h_core = get_core_name(tip.home)
                    a_core = get_core_name(tip.away)
                    if h_core in r["norm"] and a_core in r["norm"]:
                        if row is not None:
                            err = (
                                f"Mehrdeutiger Namensabgleich für "
                                f"'{tip.home} - {tip.away}' (Kerne '{h_core}'/'{a_core}')."
                            )
                            break
                        row, row_idx = r, i
                if err:
                    raise HTTPException(status_code=500, detail=err)

                if row is None:
                    # Spiel evtl. angepfiffen: Zeile existiert ohne Inputs.
                    # Heuristik: Steht der Namenskern irgendwo auf der Seite,
                    # ist das Spiel da, aber gesperrt -> überspringen.
                    if get_core_name(tip.home) in page_text_norm:
                        locked_skipped.append(f"{tip.home} - {tip.away}")
                        continue
                    not_found.append(f"{tip.home} - {tip.away}")
                    continue

                used_row_ids.add(row_idx)
                if row["is_locked"]:
                    locked_skipped.append(f"{tip.home} - {tip.away}")
                    continue

                await row["inputs"][0].fill(str(tip.tipHome))
                await row["inputs"][1].fill(str(tip.tipAway))
                tipped.append({
                    "match": f"{tip.home} - {tip.away}",
                    "tip": f"{tip.tipHome}:{tip.tipAway}",
                })

            # Unauffindbare Spiele bleiben HARTER Fehler (falscher Spieltag
            # oder Namensabgleich kaputt) — gesperrte dagegen nicht.
            if not_found:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Diese Payload-Tipps wurden auf der Seite NICHT gefunden: "
                        f"{not_found}. Spieltag/Namensabgleich prüfen — nichts gespeichert."
                    ),
                )

            if not tipped:
                # Alles gesperrt: nichts zu speichern, aber kein Fehler.
                return {
                    "status": "success",
                    "tipped": [],
                    "locked": locked_skipped,
                    "message": "Keine aktiven Zeilen — alle Spiele bereits gesperrt.",
                }

            # ---------- 4. Speichern (fehlender Button = harter Fehler) ----------
            submit_btn = page.locator(
                'button[type="submit"], input[type="submit"], '
                'button:has-text("Speichern"), button:has-text("Tipps speichern")'
            ).first
            if await submit_btn.count() == 0:
                raise HTTPException(
                    status_code=500,
                    detail="Speichern-Button nicht gefunden — NICHTS wurde gespeichert.",
                )
            await submit_btn.evaluate("node => node.click()")
            await page.wait_for_timeout(4000)
            await page.wait_for_load_state("networkidle")

            # ---------- 5. Verifizieren: neu laden, Banner + Feldwerte prüfen ----------
            await page.goto(tippabgabe_url, wait_until="networkidle")

            banner = page.locator(":text('Nicht alle gesendeten Tipps')")
            if await banner.count() > 0 and await banner.first.is_visible():
                raise HTTPException(
                    status_code=500,
                    detail=f"Kicktipp hat Tipps abgelehnt: {await banner.first.inner_text()}",
                )

            fresh_rows = await scan_rows(page)
            mismatches = []
            for t in payload.tips:
                entry = f"{t.home} - {t.away}"
                if entry in locked_skipped:
                    continue
                h_core, a_core = get_core_name(t.home), get_core_name(t.away)
                row = next(
                    (r for r in fresh_rows if h_core in r["norm"] and a_core in r["norm"]),
                    None,
                )
                if row is None or row["is_locked"]:
                    # Zwischen Speichern und Kontrolle angepfiffen — hinnehmbar
                    continue
                v_home = (await row["inputs"][0].input_value()).strip()
                v_away = (await row["inputs"][1].input_value()).strip()
                if v_home != str(t.tipHome) or v_away != str(t.tipAway):
                    mismatches.append(
                        f"{entry}: Seite zeigt '{v_home}:{v_away}', "
                        f"gesendet '{t.tipHome}:{t.tipAway}'"
                    )
            if mismatches:
                raise HTTPException(
                    status_code=500,
                    detail=f"Verifikation fehlgeschlagen: {mismatches}",
                )

            return {
                "status": "success",
                "tipped": tipped,
                "locked": locked_skipped,
                "message": (
                    f"{len(tipped)} Spiele getippt, gespeichert und verifiziert "
                    f"({len(locked_skipped)} gesperrt/übersprungen)."
                ),
            }

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Unerwarteter Fehler: {e}")
        finally:
            await browser.close()
