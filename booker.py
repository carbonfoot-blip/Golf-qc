"""
booker.py — Réservation GGG Golf via Playwright login + httpx recherche + Playwright confirmation.
Chronogolf — lien direct (automation bloquée par Cloudflare Turnstile).
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
        return await _reserver_gggolf(terrain, username, password, date, heure, nb_joueurs, confirm_url)
    elif systeme == "chronogolf":
        return _reserver_chronogolf_link(terrain)
    return {"succes": False, "message": "Systeme non supporte."}


async def _confirmer_ggg(page, teetimes_url: str) -> dict:
    """Clique J'accepte et retourne le résultat."""
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

    logger.warning(f"[Booker GGG] Aucun indicateur. HTML[0:200]: {final_content[:200]}")
    return {"succes": True, "message": "Réservation soumise. Vérifiez votre courriel."}


def _reserver_chronogolf_link(terrain: dict) -> dict:
    slug = terrain.get("chronogolf_slug", terrain["id"])
    url = f"https://www.chronogolf.ca/club/{slug}"
    return {
        "succes": False,
        "chronogolf_redirect": True,
        "message": "Cliquez pour réserver directement sur Chronogolf.",
        "url_fallback": url,
    }


async def _reserver_gggolf(terrain, username, password, date, heure, nb_joueurs, confirm_url_direct=""):
    slug = terrain.get("ggg_slug", terrain["id"])
    login_url    = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=user&lang=fr"
    teetimes_url = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"
    heure_h      = str(int(heure.split(":")[0])) if heure else "7"
    heure_h_pad  = heure_h.zfill(2)

    logger.info(f"[Booker GGG] Debut: {terrain['nom']} — {date} {heure} {nb_joueurs}j")

    try:
        # ── Étape 1 : Recherche AVANT le login pour Keys fraîches ──────────────
        headers_pre = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": teetimes_url,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
            "Origin": "https://secure.gggolf.ca",
        }
        payloads_pre = [
            {"date": date, "hour": heure_h_pad, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
            {"date": date, "hour": heure_h,     "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
            {"date": date, "hour": "0",          "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
        ]
        pre_confirm_url = ""
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as pre_client:
            await pre_client.get(teetimes_url, headers=headers_pre)
            for payload in payloads_pre:
                resp = await pre_client.post(teetimes_url, data=payload, headers=headers_pre)
                logger.info(f"[Booker GGG] Pre-search POST: {len(resp.text)} chars (hour={payload['hour']})")
                if resp.status_code == 200:
                    found = _trouver_confirm_url_ggg(resp.text, heure, slug)
                    if found:
                        pre_confirm_url = found
                        logger.info(f"[Booker GGG] Pre-search Keys: {pre_confirm_url}")
                        break

        # ── Étape 2 : Login Playwright ───────────────────────
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
                submit = await page.query_selector(
                    "button:has-text('Connexion'), input[type='submit'], button[type='submit']"
                )
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

            # Extraire cookies de session
            pw_cookies = await context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in pw_cookies}
            logger.info(f"[Booker GGG] Cookies login: {list(cookie_dict.keys())}")
            await browser.close()

        # ── Étape 3a : Recherche avec cookies LOGIN pour Keys valides ───────────
        # Les Keys sont liées à la session — on doit les obtenir avec les cookies du login
        logger.info(f"[Booker GGG] Recherche avec cookies login pour Keys valides")
        confirm_url_direct = ""
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, cookies=cookie_dict) as auth_client:
            await auth_client.get(teetimes_url, headers=headers)
            for payload in payloads_pre:
                resp = await auth_client.post(teetimes_url, data=payload, headers=headers)
                logger.info(f"[Booker GGG] Auth POST: {len(resp.text)} chars (hour={payload['hour']})")
                if resp.status_code == 200:
                    found = _trouver_confirm_url_ggg(resp.text, heure, slug)
                    if found:
                        confirm_url_direct = found
                        logger.info(f"[Booker GGG] Auth Keys: {confirm_url_direct}")
                        break

        if not confirm_url_direct:
            # Fallback: utiliser les Keys de la pré-recherche anonyme
            confirm_url_direct = pre_confirm_url
            logger.info(f"[Booker GGG] Fallback Keys pre-recherche: {confirm_url_direct}")

        if confirm_url_direct and "Keys=" in confirm_url_direct:
            logger.info(f"[Booker GGG] Tentative avec confirm_url direct: {confirm_url_direct}")
            async with async_playwright() as pw2:
                browser2 = await pw2.chromium.launch(headless=True)
                context2 = await browser2.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                    locale="fr-CA",
                )
                await context2.add_cookies([
                    {"name": k, "value": v, "domain": "secure.gggolf.ca", "path": "/"}
                    for k, v in cookie_dict.items()
                ])
                page2 = await context2.new_page()
                await page2.goto(confirm_url_direct, timeout=TIMEOUT, wait_until="domcontentloaded")
                await page2.wait_for_timeout(1000)
                confirm_content2 = await page2.content()
                logger.info(f"[Booker GGG] Direct confirm URL: {page2.url} — {len(confirm_content2)} chars")

                if "accepte" in confirm_content2.lower():
                    # Confirmation directe possible!
                    result = await _confirmer_ggg(page2, teetimes_url)
                    await browser2.close()
                    return result
                await browser2.close()
            logger.info(f"[Booker GGG] confirm_url direct n'a pas fonctionné — recherche httpx")

        # ── Étape 2b (fallback) : Recherche httpx session anonyme ──────────────
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": teetimes_url,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
            "Origin": "https://secure.gggolf.ca",
        }

        # 4 payloads comme le scraper
        # hour=06 (padded) en premier — c'est ce qui fonctionne pour Vallée-des-Forts
        payloads = [
            {"date": date, "hour": heure_h_pad, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
            {"date": date, "hour": heure_h,     "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
            {"date": date, "hour": heure_h,     "minute": "00", "nb_players": str(nb_joueurs), "search": "Chercher les départs"},
            {"date": date, "hour": "0",          "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
        ]

        search_html = ""
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            await client.get(teetimes_url, headers=headers)
            for payload in payloads:
                resp = await client.post(teetimes_url, data=payload, headers=headers)
                logger.info(f"[Booker GGG] httpx POST {resp.status_code}: {len(resp.text)} chars (hour={payload['hour']})")
                if resp.status_code == 200 and len(resp.text) > 5000:
                    results_test = _trouver_confirm_url_ggg(resp.text, heure, slug)
                    if results_test:
                        search_html = resp.text
                        logger.info(f"[Booker GGG] HTML avec resultats (hour={payload['hour']})")
                        break
                    elif len(resp.text) > 15000:
                        search_html = resp.text  # Garder comme fallback

        # ── Étape 3 : Parser confirm_url depuis le HTML ──────
        confirm_url_fresh = _trouver_confirm_url_ggg(search_html, heure, slug)

        if not confirm_url_fresh:
            logger.warning(f"[Booker GGG] Heure {heure} non trouvee dans HTML ({len(search_html)} chars)")
            return {
                "succes": False,
                "message": f"Le depart de {heure} n'est plus disponible.",
                "url_fallback": teetimes_url,
            }

        logger.info(f"[Booker GGG] confirm_url: {confirm_url_fresh}")

        # ── Étape 4 : Playwright avec cookies → confirmation ─
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
            await page.goto(confirm_url_fresh, timeout=TIMEOUT, wait_until="domcontentloaded")
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


def _trouver_confirm_url_ggg(html: str, heure_cible: str, slug: str) -> str:
    if not html or not heure_cible:
        return ""

    try:
        heure_norm = f"{int(heure_cible.split(':')[0]):02d}:{heure_cible.split(':')[1]}"
    except Exception:
        return ""

    # Format 1 : teetimes_results-hour (Beloeil/standard)
    bloc = re.compile(
        r'data-confirm-url="([^"]+)"[^>]*>.*?teetimes_results-hour[^>]*>.*?Heure:?</span>\s*(\d{1,2}:\d{2})',
        re.DOTALL | re.IGNORECASE
    )
    for m in bloc.finditer(html):
        h = f"{int(m.group(2).split(':')[0]):02d}:{m.group(2).split(':')[1]}"
        if h == heure_norm:
            url = m.group(1).replace("&amp;", "&")
            logger.info(f"[Booker GGG] Format 1 trouve: heure={h}")
            return url

    # Format 2 : autogrid (Madeleine, Cerf, Vallée-des-Forts)
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
        cm = re.search(
            r'data-colno="0"[^>]*>.*?href="([^"]*req=confirm[^"]*)"',
            row, re.DOTALL | re.IGNORECASE
        )
        if cm:
            url = cm.group(1).replace("&amp;", "&")
            logger.info(f"[Booker GGG] Format 2 (autogrid) trouve: heure={h}")
            return url

    # Logger heures disponibles pour debug
    heures = re.findall(r'data-colno="1"[^>]*>\s*(\d{1,2}:\d{2})\s*<', html)
    heures_fmt1 = re.findall(r'Heure:?</span>\s*(\d{1,2}:\d{2})', html, re.IGNORECASE)
    logger.warning(f"[Booker GGG] {heure_cible} non trouve. Autogrid: {heures[:8]} Format1: {heures_fmt1[:8]}")
    return ""
