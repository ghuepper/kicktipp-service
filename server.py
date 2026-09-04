import os
import re
import unicodedata
import asyncio
from fastapi import FastAPI, HTTPException
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

@app.get("/")
async def health():
    return {"status": "ok"}

@app.post("/submit-tips")
async def submit_tips(payload: TipPayload):
    email = os.getenv("KT_USER") or os.getenv("KICKTIPP_EMAIL")
    password = os.getenv("KT_PASS") or os.getenv("KICKTIPP_PASSWORD")
    tipprunde = os.getenv("KT_COMMUNITY") or os.getenv("KICKTIPP_TIPPRUNDE") or "kicktipp-muenster"

    if not email or not password:
        raise HTTPException(
            status_code=500,
            detail="Umgebungsvariablen KT_USER / KT_PASS fehlen."
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            # 1. Login
            await page.goto(f"https://www.kicktipp.de/{tipprunde}/profil/login", wait_until="networkidle")

            try:
                cookie_button = page.locator("button:has-text('Akzeptieren'), button:has-text('Zustimmen'), #cmpwelcomebtnyes")
                if await cookie_button.first.is_visible(timeout=3000):
                    await cookie_button.first.click()
            except Exception:
                pass

            await page.fill('input[name="kennung"]', email)
            await page.fill('input[name="passwort"]', password)
            
            login_btn = page.locator('button[type="submit"], input[type="submit"], input[name="submitbutton"]').first
            await login_btn.evaluate("node => node.click()")

            await page.wait_for_load_state("networkidle")

            # 2. Tippabgabe aufrufen
            await page.goto(f"https://www.kicktipp.de/{tipprunde}/tippabgabe", wait_until="networkidle")

            # 3. Zeilen auf der Kicktipp-Seite auslesen und intelligent per Teamname matchen
            tr_elements = await page.locator("tr").all()
            
            rows_data = []
            for tr in tr_elements:
                text = await tr.inner_text()
                inputs = await tr.locator("input[type='text'], input[type='number']").all()
                
                is_locked = False
                score_inputs = []
                for inp in inputs:
                    if await inp.is_visible():
                        disabled = await inp.get_attribute("disabled")
                        readonly = await inp.get_attribute("readonly")
                        if disabled is not None or readonly is not None:
                            is_locked = True
                        else:
                            score_inputs.append(inp)

                if len(text.strip()) > 3 and ("-" in text or len(inputs) > 0):
                    rows_data.append({
                        "tr": tr,
                        "text": text,
                        "inputs": score_inputs,
                        "is_locked": is_locked or (len(inputs) == 0 and "-" in text)
                    })

            if not rows_data:
                raise HTTPException(status_code=500, detail="Keine Tipp-Zeilen auf der Kicktipp-Seite gefunden.")

            used_payload_indices = set()
            matched_count = 0
            locked_count = 0

            for r in rows_data:
                row_norm = norm(r["text"])
                
                matching_indices = []
                for idx, tip in enumerate(payload.tips):
                    if idx in used_payload_indices:
                        continue
                    h_norm = norm(tip.home)
                    a_norm = norm(tip.away)
                    
                    # Substring-Prüfung in beide Richtungen auf den normalisierten Strings
                    if (h_norm in row_norm or row_norm in h_norm) and (a_norm in row_norm or row_norm in a_norm):
                        matching_indices.append(idx)
                    elif h_norm in row_norm and a_norm in row_norm:
                        matching_indices.append(idx)

                if len(matching_indices) == 1:
                    idx = matching_indices[0]
                    used_payload_indices.add(idx)
                    tip = payload.tips[idx]

                    if r["is_locked"] or len(r["inputs"]) < 2:
                        locked_count += 1
                        continue

                    # Tipps eintragen
                    await r["inputs"][0].fill(str(tip.tipHome))
                    await r["inputs"][1].fill(str(tip.tipAway))
                    matched_count += 1

                elif len(matching_indices) > 1:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Mehrdeutiger Namensabgleich für Zeile '{r['text'].strip()}'. Mehrere Payload-Spiele passen."
                    )
                else:
                    if r["is_locked"]:
                        locked_count += 1
                    else:
                        if len(r["inputs"]) >= 2:
                            raise HTTPException(
                                status_code=500,
                                detail=f"Kein passender Payload-Tipp für aktive Seitenzeile gefunden: '{r['text'].strip()}'."
                            )

            if len(used_payload_indices) != len(payload.tips):
                unmatched = [f"{t.home} - {t.away}" for i, t in enumerate(payload.tips) if i not in used_payload_indices]
                raise HTTPException(
                    status_code=500,
                    detail=f"Nicht alle Payload-Tipps konnten Seitenzeilen zugeordnet werden: {unmatched}."
                )

            # 4. Speichern per JavaScript-Klick
            submit_btn = page.locator('button[type="submit"], input[type="submit"], button:has-text("Speichern"), button:has-text("Tipps speichern")').first
            if await submit_btn.count() > 0:
                await submit_btn.evaluate("node => node.click()")
                await page.wait_for_timeout(4000)
                await page.wait_for_load_state("networkidle")

            # 5. Erfolg verifizieren (Seite neu laden und auf Warnbanner prüfen)
            await page.goto(f"https://www.kicktipp.de/{tipprunde}/tippabgabe", wait_until="networkidle")
            
            error_banner = page.locator(".alert-danger, .error, :text('Nicht alle gesendeten Tipps'), :text('Fehler')")
            if await error_banner.count() > 0 and await error_banner.first.is_visible():
                banner_text = await error_banner.first.inner_text()
                raise HTTPException(
                    status_code=500,
                    detail=f"Kicktipp hat die Tipps abgelehnt oder Warnung ausgegeben: {banner_text}"
                )

            return {
                "status": "success",
                "message": f"{matched_count} Spiele erfolgreich getippt und gespeichert! ({locked_count} bereits gesperrt)"
            }

        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=500, detail=str(e))

        finally:
            await browser.close()
