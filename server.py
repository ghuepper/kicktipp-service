import os
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Tuple
from playwright.sync_api import sync_playwright

app = FastAPI()

EMAIL = os.environ.get("KT_USER")
PASSWORD = os.environ.get("KT_PASS")
COMMUNITY = os.environ.get("KT_COMMUNITY", "kicktipp-muenster")

class TipPayload(BaseModel):
    tips: List[Tuple[int, int]]

@app.post("/submit-tips")
def submit_tips(payload: TipPayload):
    if not EMAIL or not PASSWORD:
        raise HTTPException(status_code=500, detail="Kicktipp-Zugangsdaten fehlen in den Umgebungsvariablen!")

    if len(payload.tips) < 9:
        raise HTTPException(status_code=400, detail="Es müssen genau 9 Spielergebnisse übermittelt werden.")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            page = context.new_page()

            # 1. Login bei Kicktipp
            page.goto("https://www.kicktipp.de/info/profil/login")
            page.fill('#kennung', EMAIL)
            page.fill('#passwort', PASSWORD)
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")

            # 2. Tippabgabe aufrufen
            page.goto(f"https://www.kicktipp.de/{COMMUNITY}/tippabgabe")
            page.wait_for_load_state("networkidle")

            inputs = page.query_selector_all('table.tippabgabe input[type="text"], table.tippabgabe input[type="number"]')
            visible = [inp for inp in inputs if inp.is_visible()]

            # 3. Felder befüllen
            for i, (heim, gast) in enumerate(payload.tips):
                idx = i * 2
                if idx + 1 < len(visible):
                    visible[idx].fill(str(heim))
                    visible[idx + 1].fill(str(gast))

            # 4. Abschicken
            page.click('input[name="submitbutton"]')
            time.sleep(2)
            browser.close()

        return {"status": "success", "message": "Tipps wurden erfolgreich bei Kicktipp eingetragen!"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
