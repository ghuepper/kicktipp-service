import os
import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI()

class TipPayload(BaseModel):
    tips: list[list[int]]

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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            # 1. Login-Seite aufrufen
            await page.goto(f"https://www.kicktipp.de/{tipprunde}/profil/login", wait_until="networkidle")

            # Cookie-Banner wegklicken (falls vorhanden)
            try:
                cookie_button = page.locator("button:has-text('Akzeptieren'), button:has-text('Zustimmen'), #cmpwelcomebtnyes")
                if await cookie_button.first.is_visible(timeout=3000):
                    await cookie_button.first.click()
            except Exception:
                pass

            # Login-Felder ausfüllen
            await page.fill('input[name="kennung"]', email)
            await page.fill('input[name="passwort"]', password)
            await page.click('button[type="submit"], input[type="submit"], input[name="submitbutton"]')

            await page.wait_for_load_state("networkidle")

            # 2. Tippabgabe-Seite aufrufen
            await page.goto(f"https://www.kicktipp.de/{tipprunde}/tippabgabe", wait_until="networkidle")

            # 3. Tippfelder suchen und ausfüllen
            inputs = await page.query_selector_all("input[type='text'], input[type='number'], input[name^='tipp_']")
            
            # Filtert nur echte Tipp-Eingabefelder heraus
            tip_inputs = []
            for inp in inputs:
                name = await inp.get_attribute("name") or ""
                inp_id = await inp.get_attribute("id") or ""
                if "tipp" in name.lower() or "tipp" in inp_id.lower() or "heim" in name.lower() or "gast" in name.lower():
                    tip_inputs.append(inp)

            # Falls der Namensfilter leer war, nimm alle sichtbaren Text-/Number-Inputs der Tipptabelle
            if not tip_inputs:
                tip_inputs = [inp for inp in inputs if await inp.is_visible()]

            # Werte eintragen
            flat_tips = [val for match in payload.tips for val in match]
            for i, val in enumerate(flat_tips):
                if i < len(tip_inputs):
                    await tip_inputs[i].fill(str(val))

            # 4. Speichern-Button flexibel ansteuern
            submit_selectors = [
                'input[name="submitbutton"]',
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Speichern")',
                'input[value="Speichern"]',
                'button:has-text("Tipps speichern")',
                'input[value="Tipps speichern"]'
            ]

            clicked = False
            for selector in submit_selectors:
                btn = page.locator(selector)
                if await btn.first.is_visible(timeout=1500):
                    await btn.first.click()
                    clicked = True
                    break

            if not clicked:
                # Fallback: Erstes Submit-Element
                await page.locator('input[type="submit"], button[type="submit"]').first.click()

            await page.wait_for_timeout(3000)
            await page.wait_for_load_state("networkidle")

            return {
                "status": "success",
                "message": f"{len(payload.tips)} Spiele erfolgreich getippt und gespeichert!"
            }

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        finally:
            await browser.close()
