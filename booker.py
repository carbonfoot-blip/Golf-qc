"""
booker.py — Réservation GGG Golf.
Flow hybride Playwright + httpx :
  1. Playwright : login → cookies de session
  2. httpx avec cookies : POST recherche → HTML avec Keys fraîches
  3. Parser les Keys du départ voulu
  4. Playwright avec cookies : GET req=confirm → confirmer
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
        return await _reserver_chronogolf(terrain, confirm_url, username, password)
    return {"succes": False, "message": "Système non supporté."}


async def _reserver_gggolf(
    terrain: dict, username: str, password: str,
    date: str, heure: str, nb_joueurs: int
) -> dict:
    slug = terrain.get("ggg_slug", terrain["id"])
    base    = f"https://secure.gggolf.ca/{slug}"
    login_url    = f"{base}/index.php?option=com_ggpublic&req=user&lang=fr"
    teetimes_url = f"{base}/index.php?option=com_ggpublic&req=teetimes&lang=fr"

    heure_h     = str(int(heure.split(":")[0])) if heure else "7"
    heure_h_pad = heure_h.zfill(2)

    logger.info(f"[Booker GGG] Debut: {terrain['nom']} — {date} {heure} {nb_joueurs}j")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
            )
            page = await context.new_page()

            # ── Étape 1 : Login via Playwright ───────────────
            await page.goto(login_url, timeout=TIMEOUT, wait_until="domcontentloaded")

            email_field = await page.query_selector("input[name='email'], input[id='email']")
            pwd_field   = await page.query_selector("input[name='password'], input[type='password']")

            if not email_field or not pwd_field:
                await browser.close()
                return {"succes": False, "message": "Page de connexion introuvable."}

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

            # Verifier echec login
            content = await page.content()
            for fail in ["identifiant incorrect", "mot de passe incorrect", "invalid"]:
                if fail in content.lower():
                    await browser.close()
                    return {"succes": False, "message": "Identifiants incorrects."}

            # ── Étape 2 : Extraire les cookies Playwright ────
            pw_cookies = await context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in pw_cookies}
            logger.info(f"[Booker GGG] Cookies session: {list(cookie_dict.keys())}")

            # ── Étape 3 : POST recherche via httpx avec cookies ──
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": teetimes_url,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-CA,fr;q=0.9",
                "Origin": f"https://secure.gggolf.ca",
            }

            payloads = [
                {"date": date, "hour": heure_h,     "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
                {"date": date, "hour": heure_h_pad, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
            ]

            search_html = ""
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                follow_redirects=True,
                cookies=cookie_dict,
                headers=headers,
            ) as client:
                for payload in payloads:
                    resp = await client.post(teetimes_url, data=payload)
                    logger.info(f"[Booker GGG] httpx POST {resp.status_code}: {len(resp.text)} chars (hour={payload['hour']})")
                    if resp.status_code == 200 and len(resp.text) > 18000:
                        search_html = resp.text
                        logger.info(f"[Booker GGG] HTML riche obtenu via httpx")
                        break

            if not search_html:
                # Fallback: essayer sans cookies (moins de chars mais peut avoir les Keys)
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers) as client:
                    await client.get(teetimes_url)
                    resp = await client.post(teetimes_url, data=payloads[0])
                    search_html = resp.text
                    logger.info(f"[Booker GGG] Fallback sans cookies: {len(search_html)} chars")

            # ── Étape 4 : Parser les Keys du départ voulu ───
            confirm_url_fresh = _trouver_confirm_url(search_html, heure, slug)

            if not confirm_url_fresh:
                logger.warning(f"[Booker GGG] Heure {heure} non trouvee dans HTML httpx")
                await browser.close()
                return {
                    "succes": False,
                    "message": f"Le départ de {heure} n'est plus disponible.",
                    "url_fallback": teetimes_url,
                }

            logger.info(f"[Booker GGG] confirm_url frais: {confirm_url_fresh}")

            # ── Étape 5 : Naviguer vers confirm avec Playwright (session connectée) ──
            await page.goto(confirm_url_fresh, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            logger.info(f"[Booker GGG] URL apres goto confirm: {page.url}")

            confirm_content = await page.content()
            logger.info(f"[Booker GGG] Page confirm: {len(confirm_content)} chars")

            if "accepte" not in confirm_content.lower():
                logger.warning(f"[Booker GGG] Pas sur page confirm. URL: {page.url}")
                await browser.close()
                return {
                    "succes": False,
                    "message": "Le départ n'est plus disponible.",
                    "url_fallback": teetimes_url,
                }

            # ── Étape 6 : Cliquer "J'accepte les termes" ────
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
                return {"succes": False, "message": "Bouton de confirmation introuvable.", "url_fallback": teetimes_url}

            btn_val = await confirm_btn.get_attribute("value") or ""
            logger.info(f"[Booker GGG] Clic: '{btn_val[:60]}'")

            try:
                async with page.expect_navigation(timeout=20000):
                    await confirm_btn.click()
            except Exception:
                await page.wait_for_timeout(3000)

            final_content = await page.content()
            logger.info(f"[Booker GGG] Finale: {page.url} — {len(final_content)} chars")

            # ── Étape 7 : Vérifier le succès ────────────────
            for indicator in ["numero de reservation", "numéro de réservation", "confirmée", "merci", "thank you"]:
                if indicator.lower() in final_content.lower():
                    logger.info(f"[Booker GGG] SUCCES — '{indicator}'")
                    await browser.close()
                    return {"succes": True, "message": "Réservation confirmée! Vous recevrez une confirmation par courriel."}

            for indicator in ["erreur", "impossible", "déjà réservé", "already"]:
                if indicator.lower() in final_content.lower():
                    await browser.close()
                    return {"succes": False, "message": "Erreur — le départ est peut-être déjà pris.", "url_fallback": teetimes_url}

            logger.warning(f"[Booker GGG] Aucun indicateur. HTML[0:400]: {final_content[:400]}")
            await browser.close()
            return {"succes": True, "message": "Réservation soumise. Vérifiez votre courriel."}

    except Exception as e:
        logger.error(f"[Booker GGG] Erreur: {e}")
        return {"succes": False, "message": "Erreur technique. Essayez directement sur GGG Golf.", "url_fallback": teetimes_url}


def _trouver_confirm_url(html: str, heure_cible: str, slug: str) -> str:
    """Trouve l'URL de confirmation pour l'heure cible dans le HTML de recherche."""

    heure_norm = f"{int(heure_cible.split(':')[0]):02d}:{heure_cible.split(':')[1]}"

    # Format 1 : teetimes_results-hour (Beloeil)
    bloc_pattern = re.compile(
        r'data-confirm-url="([^"]+)"[^>]*>.*?teetimes_results-hour[^>]*>.*?Heure:?</span>\s*(\d{1,2}:\d{2})',
        re.DOTALL | re.IGNORECASE
    )
    for m in bloc_pattern.finditer(html):
        h = f"{int(m.group(2).split(':')[0]):02d}:{m.group(2).split(':')[1]}"
        if h == heure_norm:
            return m.group(1).replace("&amp;", "&")

    # Format 2 : autogrid (Madeleine, Cerf...)
    row_pattern = re.compile(
        r'<tr[^>]*class="[^"]*autogrid(?:Even|Odd)[^"]*"[^>]*>(.*?)</tr>',
        re.DOTALL | re.IGNORECASE
    )
    for row_m in row_pattern.finditer(html):
        row_html = row_m.group(1)
        heure_match = re.search(r'data-colno="1"[^>]*>\s*(\d{1,2}:\d{2})\s*<', row_html, re.IGNORECASE)
        if not heure_match:
            continue
        h = f"{int(heure_match.group(1).split(':')[0]):02d}:{heure_match.group(1).split(':')[1]}"
        if h != heure_norm:
            continue
        confirm_match = re.search(
            r'data-colno="0"[^>]*>.*?href="([^"]*req=confirm[^"]*)"',
            row_html, re.DOTALL | re.IGNORECASE
        )
        if confirm_match:
            return confirm_match.group(1).replace("&amp;", "&")

    # Chercher toutes les heures disponibles pour debug
    heures = re.findall(r'data-colno="1"[^>]*>\s*(\d{1,2}:\d{2})\s*<', html)
    if heures:
        logger.info(f"[Booker GGG] Heures dans HTML: {heures}")
    else:
        logger.warning(f"[Booker GGG] Aucune heure autogrid. HTML size: {len(html)}")

    return ""


async def _reserver_chronogolf(terrain: dict, confirm_url: str, username: str, password: str) -> dict:
    return {
        "succes": False,
        "message": "La réservation directe Chronogolf n'est pas encore disponible. Cliquez sur 'Voir sur Chronogolf' pour réserver.",
        "url_fallback": terrain.get("url_reservation", confirm_url),
    }
