"""
booker.py — GGG via httpx+Playwright hybride, Chronogolf entièrement via Playwright.
"""

import logging
import re
import httpx
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

logger = logging.getLogger(__name__)
TIMEOUT = 30_000
HTTP_TIMEOUT = 20


async def reserver_depart(
    terrain: dict, confirm_url: str, username: str, password: str,
    date: str = "", heure: str = "", nb_joueurs: int = 2
) -> dict:
    systeme = terrain.get("systeme", "site_propre")
    if systeme == "gggolf":
        return await _reserver_gggolf(terrain, username, password, date, heure, nb_joueurs)
    elif systeme == "chronogolf":
        return await _reserver_chronogolf(terrain, username, password, date, heure, nb_joueurs)
    return {"succes": False, "message": "Systeme non supporte."}


# ─────────────────────────────────────────────
# GGG Golf — httpx login + httpx recherche + Playwright confirmation
# ─────────────────────────────────────────────

async def _reserver_gggolf(terrain, username, password, date, heure, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    login_url    = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=user&lang=fr"
    teetimes_url = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"
    heure_h      = str(int(heure.split(":")[0])) if heure else "7"
    heure_h_pad  = heure_h.zfill(2)

    logger.info(f"[Booker GGG] Debut: {terrain['nom']} — {date} {heure} {nb_joueurs}j")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
            )
            page = await context.new_page()

            await page.goto(login_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            email_field = await page.query_selector("input[name='email'], input[id='email']")
            pwd_field   = await page.query_selector("input[name='password'], input[type='password']")

            if not email_field or not pwd_field:
                await browser.close()
                return {"succes": False, "message": "Page de connexion GGG introuvable."}

            await email_field.fill(username)
            await pwd_field.fill(password)

            async with page.expect_navigation(timeout=15000):
                submit = await page.query_selector("button:has-text('Connexion'), input[type='submit'], button[type='submit']")
                if submit:
                    await submit.click()
                else:
                    await pwd_field.press("Enter")

            logger.info(f"[Booker GGG] Apres login: {page.url}")
            content = await page.content()
            for fail in ["identifiant incorrect", "mot de passe incorrect", "invalid"]:
                if fail in content.lower():
                    await browser.close()
                    return {"succes": False, "message": "Identifiants GGG incorrects."}

            pw_cookies = await context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in pw_cookies}
            await browser.close()

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": teetimes_url,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-CA,fr;q=0.9",
            "Origin": "https://secure.gggolf.ca",
        }

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, cookies=cookie_dict, headers=headers) as client:
            search_html = ""
            for h_val in [heure_h, heure_h_pad]:
                payload = {"date": date, "hour": h_val, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"}
                resp = await client.post(teetimes_url, data=payload)
                logger.info(f"[Booker GGG] httpx POST {resp.status_code}: {len(resp.text)} chars (hour={h_val})")
                if resp.status_code == 200 and len(resp.text) > 18000:
                    search_html = resp.text
                    break

            confirm_url_fresh = _trouver_confirm_url_ggg(search_html, heure, slug)
            if not confirm_url_fresh:
                return {"succes": False, "message": f"Le depart de {heure} n'est plus disponible.", "url_fallback": teetimes_url}

            logger.info(f"[Booker GGG] confirm_url: {confirm_url_fresh}")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36", locale="fr-CA")
            await context.add_cookies([{"name": k, "value": v, "domain": "secure.gggolf.ca", "path": "/"} for k, v in cookie_dict.items()])
            page = await context.new_page()

            await page.goto(confirm_url_fresh, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            logger.info(f"[Booker GGG] URL confirm: {page.url}")

            confirm_content = await page.content()
            if "accepte" not in confirm_content.lower():
                await browser.close()
                return {"succes": False, "message": "Le depart n'est plus disponible.", "url_fallback": teetimes_url}

            confirm_btn = await page.query_selector("input[name='nook']")
            if not confirm_btn:
                all_submits = await page.query_selector_all("input[type='submit']")
                for btn in all_submits:
                    val = (await btn.get_attribute("value") or "").lower()
                    if "recherche" not in val and "cancel" not in val and val:
                        confirm_btn = btn
                        break

            if not confirm_btn:
                await browser.close()
                return {"succes": False, "message": "Bouton confirmation introuvable.", "url_fallback": teetimes_url}

            btn_val = await confirm_btn.get_attribute("value") or ""
            logger.info(f"[Booker GGG] Clic: '{btn_val[:60]}'")

            try:
                async with page.expect_navigation(timeout=20000):
                    await confirm_btn.click()
            except Exception:
                await page.wait_for_timeout(3000)

            final_content = await page.content()
            logger.info(f"[Booker GGG] Finale: {page.url} — {len(final_content)} chars")
            await browser.close()

            for indicator in ["numero de reservation", "numéro de réservation", "confirmée", "merci", "thank you"]:
                if indicator.lower() in final_content.lower():
                    return {"succes": True, "message": "Réservation GGG confirmée! Vérifiez votre courriel."}

            for indicator in ["erreur", "impossible", "déjà réservé", "already"]:
                if indicator.lower() in final_content.lower():
                    return {"succes": False, "message": "Erreur — le depart est peut-être déjà pris.", "url_fallback": teetimes_url}

            logger.warning(f"[Booker GGG] Aucun indicateur. HTML[0:400]: {final_content[:400]}")
            return {"succes": True, "message": "Réservation soumise. Vérifiez votre courriel."}

    except Exception as e:
        logger.error(f"[Booker GGG] Erreur: {e}")
        return {"succes": False, "message": "Erreur technique GGG.", "url_fallback": teetimes_url}


def _trouver_confirm_url_ggg(html, heure_cible, slug):
    if not html or not heure_cible:
        return ""
    heure_norm = f"{int(heure_cible.split(':')[0]):02d}:{heure_cible.split(':')[1]}"

    bloc = re.compile(r'data-confirm-url="([^"]+)"[^>]*>.*?teetimes_results-hour[^>]*>.*?Heure:?</span>\s*(\d{1,2}:\d{2})', re.DOTALL | re.IGNORECASE)
    for m in bloc.finditer(html):
        h = f"{int(m.group(2).split(':')[0]):02d}:{m.group(2).split(':')[1]}"
        if h == heure_norm:
            return m.group(1).replace("&amp;", "&")

    row_pat = re.compile(r'<tr[^>]*class="[^"]*autogrid(?:Even|Odd)[^"]*"[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
    for row_m in row_pat.finditer(html):
        row = row_m.group(1)
        hm = re.search(r'data-colno="1"[^>]*>\s*(\d{1,2}:\d{2})\s*<', row, re.IGNORECASE)
        if not hm:
            continue
        h = f"{int(hm.group(1).split(':')[0]):02d}:{hm.group(1).split(':')[1]}"
        if h != heure_norm:
            continue
        cm = re.search(r'data-colno="0"[^>]*>.*?href="([^"]*req=confirm[^"]*)"', row, re.DOTALL | re.IGNORECASE)
        if cm:
            return cm.group(1).replace("&amp;", "&")

    heures = re.findall(r'data-colno="1"[^>]*>\s*(\d{1,2}:\d{2})\s*<', html)
    logger.warning(f"[Booker GGG] Heure {heure_cible} non trouvee. Dispo: {heures[:8]}")
    return ""


# ─────────────────────────────────────────────
# Chronogolf — entièrement via Playwright
# ─────────────────────────────────────────────

async def _reserver_chronogolf(terrain, username, password, date, heure, nb_joueurs):
    slug = terrain.get("chronogolf_slug", terrain["id"])
    club_id = terrain.get("chronogolf_club_id")
    course_id = terrain.get("chronogolf_course_id")
    affiliation_id = terrain.get("chronogolf_affiliation_id", 98)
    url_prefix = terrain.get("chronogolf_url_prefix", "fr/marketplace")
    url_base = f"https://www.chronogolf.ca/club/{slug}"
    login_url = "https://www.chronogolf.com/login"

    logger.info(f"[Booker Chrono] Debut: {terrain['nom']} — {date} {heure}")

    if not club_id or not course_id:
        return {"succes": False, "message": "Configuration manquante.", "url_fallback": url_base}

    heure_norm = f"{int(heure.split(':')[0]):02d}:{heure.split(':')[1]}" if heure and ":" in heure else ""

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
            )
            page = await context.new_page()

            # ── Login ──────────────────────────────────────────
            await page.goto(login_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            email_field = await page.query_selector("input[name='email'], input[id='sessionEmail']")
            pwd_field   = await page.query_selector("input[name='password'], input[id='sessionPassword']")

            if not email_field or not pwd_field:
                await browser.close()
                return {"succes": False, "message": "Page de connexion Chronogolf introuvable."}

            await email_field.fill(username)
            await pwd_field.fill(password)

            login_btn = await page.query_selector("button:has-text('Log in'), button[type='submit']")
            if login_btn:
                await login_btn.click()
            else:
                await pwd_field.press("Enter")

            try:
                await page.wait_for_url("https://www.chronogolf.com/**", timeout=15000)
            except Exception:
                await page.wait_for_timeout(3000)

            logger.info(f"[Booker Chrono] Apres login: {page.url}")
            if "login" in page.url.lower():
                await browser.close()
                return {"succes": False, "message": "Identifiants Chronogolf incorrects."}

            # ── Naviguer vers la page de booking du terrain ───
            booking_url = f"https://www.chronogolf.ca/club/{slug}/booking/?source=chronogolf&medium=profile"
            await page.goto(booking_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            logger.info(f"[Booker Chrono] Page booking: {page.url} — {len(await page.content())} chars")

            # ── Extraire CSRF token depuis Angular ────────────
            csrf_token = await page.evaluate(
                "angular?.element(document)?.injector()?.get('$http')?.defaults?.headers?.common?.['X-CSRF-Token'] || ''"
            )
            logger.info(f"[Booker Chrono] CSRF: {'oui' if csrf_token else 'non'}")

            # ── GET teetimes pour trouver teetime_id via fetch JS ──
            teetimes_api = f"https://www.chronogolf.ca/{url_prefix}/clubs/{club_id}/teetimes"
            params_str = f"date={date}&course_id={course_id}&nb_holes=18&nb_players={nb_joueurs}&affiliation_type_ids[]={affiliation_id}"

            js_fetch = f"""
                fetch('{teetimes_api}?{params_str}', {{
                    headers: {{
                        'Accept': 'application/json',
                        'X-CSRF-Token': '{csrf_token}'
                    }},
                    credentials: 'include'
                }}).then(r => r.json()).then(d => JSON.stringify(d)).catch(e => JSON.stringify({{error: e.toString()}}))
            """
            teetimes_json = await page.evaluate(js_fetch)
            import json as _json
            slots = _json.loads(teetimes_json) if teetimes_json else []
            if isinstance(slots, dict):
                slots = slots.get("tee_times") or slots.get("teetimes") or []

            logger.info(f"[Booker Chrono] {len(slots)} departs, cherche {heure_norm}")

            teetime_id = None
            for slot in slots:
                start = slot.get("start_time") or slot.get("time") or ""
                if "T" in str(start):
                    start = str(start).split("T")[1][:5]
                h = f"{int(start.split(':')[0]):02d}:{start.split(':')[1]}" if ":" in str(start) else ""
                if h == heure_norm:
                    teetime_id = slot.get("id")
                    logger.info(f"[Booker Chrono] teetime_id: {teetime_id}")
                    break

            if not teetime_id:
                await browser.close()
                return {"succes": False, "message": f"Le depart de {heure} n'est plus disponible.", "url_fallback": url_base}

            # ── POST reservation via fetch JS (meme session) ──
            reservation_payload = _json.dumps({
                "reservation": {
                    "club_id": club_id,
                    "teetime_id": teetime_id,
                    "state": "confirmed",
                    "holes": 18,
                    "medium": "profile",
                    "source": "chronogolf",
                    "rounds_attributes": [{"affiliation_type_id": affiliation_id, "state": "reserved"}],
                }
            })

            js_post = f"""
                fetch('https://www.chronogolf.ca/marketplace/reservations', {{
                    method: 'POST',
                    headers: {{
                        'Accept': 'application/json',
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': '{csrf_token}'
                    }},
                    credentials: 'include',
                    body: '{reservation_payload.replace("'", "\\'")}'
                }}).then(r => r.json().then(d => JSON.stringify({{status: r.status, body: d}}))).catch(e => JSON.stringify({{error: e.toString()}}))
            """

            result_json = await page.evaluate(js_post)
            result = _json.loads(result_json) if result_json else {}
            status = result.get("status", 0)
            body = result.get("body", {})

            logger.info(f"[Booker Chrono] POST reservation: {status} — {str(body)[:200]}")
            await browser.close()

            if status in [200, 201]:
                logger.info(f"[Booker Chrono] SUCCES!")
                return {"succes": True, "message": "Réservation Chronogolf confirmée! Vérifiez votre courriel."}

            error_msg = body.get("error", {}).get("message", "") if isinstance(body, dict) else str(body)
            return {"succes": False, "message": f"Erreur Chronogolf: {error_msg}", "url_fallback": url_base}

    except Exception as e:
        logger.error(f"[Booker Chrono] Erreur: {e}")
        return {"succes": False, "message": "Erreur technique Chronogolf.", "url_fallback": url_base}
