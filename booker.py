"""
booker.py v60 — GGG: pré-recherche anonyme + login + recherche authentifiée + confirmation.
Chronogolf: lien direct.
"""

import logging
import re
import httpx
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)
TIMEOUT = 30_000
HTTP_TIMEOUT = 20

GGG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    "Origin": "https://secure.gggolf.ca",
}


async def reserver_depart(terrain, confirm_url, username, password, date="", heure="", nb_joueurs=2):
    systeme = terrain.get("systeme", "")
    if systeme == "gggolf":
        return await _reserver_gggolf(terrain, username, password, date, heure, nb_joueurs, confirm_url)
    elif systeme == "chronogolf":
        return await _reserver_chronogolf(terrain, confirm_url, username, password, date, heure, nb_joueurs)
    return {"succes": False, "message": "Systeme non supporte."}


async def _reserver_chronogolf(terrain: dict, confirm_url: str, username: str, password: str, date: str = "", heure: str = "", nb_joueurs: int = 2) -> dict:
    """
    Flow Chronogolf:
    1. Playwright visite chronogolf.ca pour obtenir cf_clearance
    2. httpx login via /marketplace/sessions avec cf_clearance
    3. httpx GET teetimes -> teetime_id
    4. httpx POST /private_api/teetimes/{id}/freeze -> cookie teetime_freeze
    5. httpx POST /marketplace/reservations avec tous les cookies
    """
    slug = terrain.get("chronogolf_slug", terrain["id"])
    club_id = terrain.get("chronogolf_club_id")
    course_id = terrain.get("chronogolf_course_id")
    affiliation_id = terrain.get("chronogolf_affiliation_id", 98)
    url_prefix = terrain.get("chronogolf_url_prefix", "fr/marketplace")
    url_base = f"https://www.chronogolf.ca/club/{slug}"

    logger.info(f"[Booker Chrono] Debut: {terrain['nom']} — {date} {heure}")

    if not club_id or not course_id:
        return {"succes": False, "message": "Configuration manquante.", "url_fallback": url_base}

    heure_norm = f"{int(heure.split(':')[0]):02d}:{heure.split(':')[1]}" if heure and ":" in heure else ""

    try:
        # ── 1. Playwright pour obtenir cf_clearance ───────────────────────────
        logger.info(f"[Booker Chrono] Obtention cf_clearance via Playwright")
        cf_clearance = ""
        chronogolf_session = ""

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
            )
            page = await context.new_page()
            await page.goto(f"https://www.chronogolf.ca/club/{slug}", timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            all_cookies = await context.cookies()
            cookie_dict_pw = {}
            for c in all_cookies:
                cookie_dict_pw[c["name"]] = c["value"]
                if c["name"] == "cf_clearance":
                    cf_clearance = c["value"]
                elif c["name"] == "_chronogolf_session":
                    chronogolf_session = c["value"]
            logger.info(f"[Booker Chrono] Cookies Playwright: {list(cookie_dict_pw.keys())}")
            logger.info(f"[Booker Chrono] cf_clearance: {'oui' if cf_clearance else 'non'}")
            await browser.close()

        if not cf_clearance:
            return {"succes": False, "message": "Impossible d'obtenir cf_clearance.", "url_fallback": url_base}

        # ── 2. Login httpx avec cf_clearance ─────────────────────────────────
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://www.chronogolf.ca",
            "Referer": f"https://www.chronogolf.ca/club/{slug}",
            "X-Requested-With": "XMLHttpRequest",
        }
        # Utiliser TOUS les cookies Playwright (inclus _cf_bm)
        cookies = {**cookie_dict_pw}
        logger.info(f"[Booker Chrono] Cookies pour httpx: {list(cookies.keys())}")

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers, cookies=cookies) as client:
            login_resp = await client.post(
                "https://www.chronogolf.ca/marketplace/sessions",
                json={"session": {"email": username, "password": password}},
            )
            logger.info(f"[Booker Chrono] Login: {login_resp.status_code}")
            if login_resp.status_code not in [200, 201]:
                return {"succes": False, "message": "Identifiants Chronogolf incorrects."}

            # ── 3. GET teetimes ───────────────────────────────────────────────
            teetimes_url = f"https://www.chronogolf.ca/{url_prefix}/clubs/{club_id}/teetimes"
            params = {
                "date": date, "course_id": str(course_id),
                "nb_holes": "18", "nb_players": str(nb_joueurs),
                "affiliation_type_ids[]": str(affiliation_id),
            }
            tt_resp = await client.get(teetimes_url, params=params)
            logger.info(f"[Booker Chrono] Teetimes: {tt_resp.status_code}")
            if tt_resp.status_code != 200:
                return {"succes": False, "message": "Impossible de récupérer les départs.", "url_fallback": url_base}

            slots = tt_resp.json()
            if isinstance(slots, dict):
                slots = slots.get("tee_times") or slots.get("teetimes") or []

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
                return {"succes": False, "message": f"Le départ de {heure} n'est plus disponible.", "url_fallback": url_base}

            # ── 4. POST freeze pour générer teetime_freeze cookie ─────────────
            # Essayer plusieurs URLs de freeze
            freeze_urls = [
                f"https://www.chronogolf.ca/fr/private_api/teetimes/{teetime_id}/freeze",
                f"https://www.chronogolf.ca/private_api/teetimes/{teetime_id}/freeze",
                f"https://www.chronogolf.ca/marketplace/teetimes/{teetime_id}/freeze",
            ]
            freeze_ok = False
            for freeze_url in freeze_urls:
                freeze_resp = await client.post(
                    freeze_url, content=b"{}",
                    headers={**headers, "Content-Length": "2"},
                    follow_redirects=False,
                )
                logger.info(f"[Booker Chrono] Freeze {freeze_url.split('chronogolf.ca')[1]}: {freeze_resp.status_code}")
                if freeze_resp.status_code in [200, 201]:
                    freeze_ok = True
                    break

            teetime_freeze = client.cookies.get("teetime_freeze", "")
            logger.info(f"[Booker Chrono] teetime_freeze: {'oui' if teetime_freeze else 'non'}")

            if not freeze_ok:
                logger.warning(f"[Booker Chrono] Freeze échoué sur toutes les URLs")

            # ── 5. POST reservation ───────────────────────────────────────────
            rounds = [
                {"affiliation_type_id": affiliation_id, "guest": None, "state": "reserved"}
                for _ in range(nb_joueurs)
            ]
            payload = {
                "reservation": {
                    "club_id": club_id,
                    "teetime_id": teetime_id,
                    "state": "confirmed",
                    "holes": 18,
                    "medium": "profile",
                    "source": "chronogolf",
                    "booking_engine": 1,
                    "made_online": True,
                    "rounds_attributes": rounds,
                }
            }

            res_resp = await client.post(
                "https://www.chronogolf.ca/marketplace/reservations",
                json=payload,
                follow_redirects=False,
            )
            logger.info(f"[Booker Chrono] Reservation: {res_resp.status_code} — {res_resp.text[:200]}")

            if res_resp.status_code in [200, 201]:
                body = res_resp.text
                if body.strip().startswith("{") and '"id"' in body:
                    return {"succes": True, "message": "Réservation Chronogolf confirmée! Vérifiez votre courriel."}
                return {"succes": False, "message": "Session insuffisante.", "url_fallback": url_base}

            if res_resp.status_code == 302:
                return {"succes": False, "message": "Session expirée. Réessayez.", "url_fallback": url_base}

            return {"succes": False, "message": f"Erreur Chronogolf ({res_resp.status_code}).", "url_fallback": url_base}

    except Exception as e:
        logger.error(f"[Booker Chrono] Erreur: {e}")
        return {"succes": False, "message": "Erreur technique Chronogolf.", "url_fallback": url_base}



async def _rechercher_ggg(teetimes_url, date, heure_h, heure_h_pad, nb_joueurs, slug, heure_complete, cookies=None):
    """Recherche les départs GGG et retourne la confirm_url si trouvée."""
    payloads = [
        {"date": date, "hour": heure_h_pad, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
        {"date": date, "hour": heure_h,     "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
        {"date": date, "hour": "0",          "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
    ]
    headers = {**GGG_HEADERS, "Referer": teetimes_url}
    kwargs = {"cookies": cookies} if cookies else {}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, **kwargs) as client:
        await client.get(teetimes_url, headers=headers)
        for payload in payloads:
            resp = await client.post(teetimes_url, data=payload, headers=headers)
            tag = "auth" if cookies else "anon"
            logger.info(f"[Booker GGG] POST {tag}: {len(resp.text)} chars (hour={payload['hour']})")
            if resp.status_code == 200:
                found = _trouver_confirm_url_ggg(resp.text, heure_complete, slug)
                if found:
                    return found
    return ""


async def _reserver_gggolf(terrain, username, password, date, heure, nb_joueurs, confirm_url_direct=""):
    slug = terrain.get("ggg_slug", terrain["id"])
    teetimes_url = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"
    login_url    = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=user&lang=fr"
    heure_h      = str(int(heure.split(":")[0])) if heure else "7"
    heure_h_pad  = heure_h.zfill(2)

    logger.info(f"[Booker GGG] Debut: {terrain['nom']} — {date} {heure} {nb_joueurs}j")

    try:
        # ── 1. Pré-recherche anonyme pour vérifier disponibilité ─────────────
        pre_url = await _rechercher_ggg(teetimes_url, date, heure_h, heure_h_pad, nb_joueurs, slug, heure)
        if pre_url:
            logger.info(f"[Booker GGG] Pre-recherche: {pre_url}")
        else:
            logger.warning(f"[Booker GGG] Depart {heure} non trouve en pre-recherche")
            return {"succes": False, "message": f"Le depart de {heure} n'est plus disponible.", "url_fallback": teetimes_url}

        # ── 2. Login Playwright ───────────────────────────────────────────────
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
            logger.info(f"[Booker GGG] Cookies login: {list(cookie_dict.keys())}")
            await browser.close()

        # ── 3. Recherche avec cookies login pour Keys valides ─────────────────
        auth_url = await _rechercher_ggg(teetimes_url, date, heure_h, heure_h_pad, nb_joueurs, slug, heure, cookie_dict)
        if auth_url:
            logger.info(f"[Booker GGG] Auth Keys: {auth_url}")
            confirm_url_final = auth_url
        else:
            logger.warning(f"[Booker GGG] Pas de Keys auth — fallback pre-recherche")
            confirm_url_final = pre_url

        # ── 4. Confirmation Playwright avec cookies login ─────────────────────
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
            )
            await context.add_cookies([
                {"name": k, "value": v, "domain": "secure.gggolf.ca", "path": "/"}
                for k, v in cookie_dict.items()
            ])
            page = await context.new_page()
            await page.goto(confirm_url_final, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            logger.info(f"[Booker GGG] Page confirm: {page.url}")

            confirm_content = await page.content()
            if "accepte" not in confirm_content.lower():
                await browser.close()
                return {"succes": False, "message": "Le depart n'est plus disponible.", "url_fallback": teetimes_url}

            result = await _confirmer_ggg(page, teetimes_url)
            await browser.close()
            return result

    except Exception as e:
        logger.error(f"[Booker GGG] Erreur: {e}")
        return {"succes": False, "message": "Erreur technique GGG.", "url_fallback": teetimes_url}


async def _confirmer_ggg(page, teetimes_url):
    confirm_btn = await page.query_selector("input[name='nook']")
    if not confirm_btn:
        all_submits = await page.query_selector_all("input[type='submit']")
        for btn in all_submits:
            val = (await btn.get_attribute("value") or "").lower()
            if "recherche" not in val and "cancel" not in val and val:
                confirm_btn = btn
                break

    if not confirm_btn:
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

    for indicator in ["numero de reservation", "numéro de réservation", "confirmée", "merci", "thank you"]:
        if indicator.lower() in final_content.lower():
            logger.info(f"[Booker GGG] SUCCES")
            return {"succes": True, "message": "Réservation GGG confirmée! Vérifiez votre courriel."}

    for indicator in ["erreur", "impossible", "déjà réservé", "already"]:
        if indicator.lower() in final_content.lower():
            return {"succes": False, "message": "Erreur — départ peut-être déjà pris.", "url_fallback": teetimes_url}

    logger.warning(f"[Booker GGG] Aucun indicateur clair. HTML[0:200]: {final_content[:200]}")
    return {"succes": True, "message": "Réservation soumise. Vérifiez votre courriel."}


def _trouver_confirm_url_ggg(html, heure_cible, slug):
    if not html or not heure_cible:
        return ""
    try:
        heure_norm = f"{int(heure_cible.split(':')[0]):02d}:{heure_cible.split(':')[1]}"
    except Exception:
        return ""

    # Format 1 : teetimes_results-hour
    bloc = re.compile(
        r'data-confirm-url="([^"]+)"[^>]*>.*?teetimes_results-hour[^>]*>.*?Heure:?</span>\s*(\d{1,2}:\d{2})',
        re.DOTALL | re.IGNORECASE
    )
    for m in bloc.finditer(html):
        h = f"{int(m.group(2).split(':')[0]):02d}:{m.group(2).split(':')[1]}"
        if h == heure_norm:
            return m.group(1).replace("&amp;", "&")

    # Format 2 : autogrid
    row_pat = re.compile(
        r'<tr[^>]*class="[^"]*autogrid(?:Even|Odd)[^"]*"[^>]*>(.*?)</tr>',
        re.DOTALL | re.IGNORECASE
    )
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
            url = cm.group(1).replace("&amp;", "&")
            logger.info(f"[Booker GGG] Format 2 (autogrid) trouve: heure={h}")
            return url

    heures = re.findall(r'data-colno="1"[^>]*>\s*(\d{1,2}:\d{2})\s*<', html)
    heures_fmt1 = re.findall(r'Heure:?</span>\s*(\d{1,2}:\d{2})', html, re.IGNORECASE)
    if heures or heures_fmt1:
        logger.warning(f"[Booker GGG] {heure_cible} non trouve. Autogrid: {heures[:8]} Format1: {heures_fmt1[:8]}")
    return ""
