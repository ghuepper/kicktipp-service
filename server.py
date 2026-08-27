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
        # Großes Browserfenster setzen, damit alle Buttons im Viewport liegen
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
            await page.click('button[type="submit"], input[type="submit"], input[name="submitbutton"]', force=True)

            await page.wait_for_load_state("networkidle")

            # 2. Tippabgabe aufrufen
            await page.goto(f"https://www.kicktipp.de/{tipprunde}/tippabgabe", wait_until="networkidle")

            # 3. Tippfelder ausfüllen
            inputs = await page.query_selector_all("input[type='text'], input[type='number'], input[name^='tipp_']")
            
            tip_inputs = []
            for inp in inputs:
                name = await inp.get_attribute("name") or ""
                inp_id = await inp.get_attribute("id") or ""
                if "tipp" in name.lower() or "tipp" in inp_id.lower() or "heim" in name.lower() or "gast" in name.lower():
                    tip_inputs.append(inp)

            if not tip_inputs:
                tip_inputs = [inp for inp in inputs if await inp.is_visible()]

            flat_tips = [val for match in payload.tips for val in match]
            for i, val in enumerate(flat_tips):
                if i < len(tip_inputs):
                    await tip_inputs[i].fill(str(val))

            # 4. Speichern mit scroll_into_view_if_needed und force=True
            submit_btn = page.locator('button[type="submit"], input[type="submit"], button:has-text("Speichern"), button:has-text("Tipps speichern")').first
            await submit_btn.scroll_into_view_if_needed()
            await submit_btn.click(force=True)

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
