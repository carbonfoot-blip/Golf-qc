"""
booker.py — Réservation GGG Golf.
Flow simplifié : login + recherche + clic départ + confirmation
Tout dans la même session Playwright. On ignore les Keys de l'utilisateur.
"""

import logging
import re
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

logger = logging.getLogger(__name__)
TIMEOUT = 30_000


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

            # ── Étape 1 : Login ──────────────────────────────
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
                    return {"succes": False, "message": "Identifiants incorrects. Vérifiez votre courriel et mot de passe GGG Golf."}

            # ── Étape 2 : Faire la recherche dans la session connectée ──
            logger.info(f"[Booker GGG] Recherche: {teetimes_url} date={date} heure={heure_h} joueurs={nb_joueurs}")
            await page.goto(teetimes_url, timeout=TIMEOUT, wait_until="domcontentloaded")

            # Remplir la date
            if date:
                date_input = await page.query_selector(
                    "input.jquery_ui_datepicker, input[name='date'], input[id='date']"
                )
                if date_input:
                    await date_input.fill(date)
                    await date_input.dispatch_event("change")
                    await page.wait_for_timeout(500)

            # Remplir l'heure
            hour_sel = await page.query_selector("select[name='hour'], select[name='heure'], select[id='hour']")
            if hour_sel:
                for h_val in [heure_h, heure_h_pad]:
                    try:
                        await hour_sel.select_option(value=h_val)
                        logger.info(f"[Booker GGG] Heure selectionnee: {h_val}")
                        break
                    except Exception:
                        continue

            # Remplir les minutes
            min_sel = await page.query_selector("select[name='minute'], select[id='minute']")
            if min_sel:
                try:
                    await min_sel.select_option(value="00")
                except Exception:
                    pass

            # Remplir joueurs
            players_sel = await page.query_selector(
                "select[name*='player'], select[name*='joueur'], select[name='nbplayers'], select[id*='player']"
            )
            if players_sel:
                try:
                    await players_sel.select_option(value=str(nb_joueurs))
                except Exception:
                    pass

            # Soumettre la recherche
            submit_btn = await page.query_selector(
                "input[name='sSearch'], input[type='submit'][value*='Chercher'], input[type='submit']"
            )
            if submit_btn:
                await submit_btn.click()
                await page.wait_for_load_state("networkidle", timeout=15000)

            search_content = await page.content()
            logger.info(f"[Booker GGG] Resultats recherche: {len(search_content)} chars — URL: {page.url}")

            # ── Étape 3 : Trouver et cliquer sur le départ voulu ──
            found = await _trouver_et_cliquer_depart(page, heure, terrain)

            if not found:
                await browser.close()
                return {
                    "succes": False,
                    "message": f"Le départ de {heure} n'est plus disponible.",
                    "url_fallback": teetimes_url,
                }

            # ── Étape 4 : Page de confirmation — cliquer "J'accepte les termes" ──
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            confirm_content = await page.content()
            logger.info(f"[Booker GGG] Page confirm: {len(confirm_content)} chars — URL: {page.url}")

            if "accepte" not in confirm_content.lower():
                logger.warning(f"[Booker GGG] Pas sur page confirm. HTML[0:300]: {confirm_content[:300]}")
                await browser.close()
                return {
                    "succes": False,
                    "message": "Le départ n'est plus disponible.",
                    "url_fallback": teetimes_url,
                }

            # Trouver et cliquer le bouton de confirmation
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
            logger.info(f"[Booker GGG] Clic confirmation: '{btn_val[:60]}'")

            try:
                async with page.expect_navigation(timeout=20000):
                    await confirm_btn.click()
            except Exception:
                await page.wait_for_timeout(3000)

            final_content = await page.content()
            logger.info(f"[Booker GGG] Page finale: {len(final_content)} chars — URL: {page.url}")

            # ── Étape 5 : Vérifier le succès ──
            for indicator in ["numero de reservation", "numéro de réservation", "confirmée", "merci", "thank you"]:
                if indicator.lower() in final_content.lower():
                    logger.info(f"[Booker GGG] SUCCES — '{indicator}'")
                    await browser.close()
                    return {"succes": True, "message": "Réservation confirmée! Vous recevrez une confirmation par courriel."}

            for indicator in ["erreur", "impossible", "déjà réservé", "already"]:
                if indicator.lower() in final_content.lower():
                    await browser.close()
                    return {"succes": False, "message": "Erreur — le départ est peut-être déjà pris.", "url_fallback": teetimes_url}

            logger.warning(f"[Booker GGG] Aucun indicateur. HTML[0:500]: {final_content[:500]}")
            await browser.close()
            return {"succes": True, "message": "Réservation soumise. Vérifiez votre courriel."}

    except Exception as e:
        logger.error(f"[Booker GGG] Erreur: {e}")
        return {"succes": False, "message": "Erreur technique. Essayez directement sur GGG Golf.", "url_fallback": teetimes_url}


async def _trouver_et_cliquer_depart(page, heure_cible: str, terrain: dict) -> bool:
    """Trouve le départ à l'heure cible et clique son lien de confirmation."""
    if not heure_cible:
        return False

    html = await page.content()
    slug = terrain.get("ggg_slug", terrain["id"])
    logger.info(f"[Booker GGG] Recherche heure {heure_cible} dans {len(html)} chars")

    # Format 1 : teetimes_results-hour (Beloeil)
    bloc_pattern = re.compile(
        r'data-confirm-url="([^"]+)"[^>]*>.*?teetimes_results-hour[^>]*>.*?Heure:?</span>\s*(\d{1,2}:\d{2})',
        re.DOTALL | re.IGNORECASE
    )
    for m in bloc_pattern.finditer(html):
        if m.group(2).strip() == heure_cible:
            confirm_url = m.group(1).replace("&amp;", "&")
            logger.info(f"[Booker GGG] Format 1 trouve: {confirm_url}")
            await page.goto(confirm_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            return True

    # Format 2 : autogrid (Madeleine, Cerf, Vallée-des-Forts)
    row_pattern = re.compile(
        r'<tr[^>]*class="[^"]*autogrid(?:Even|Odd)[^"]*"[^>]*>(.*?)</tr>',
        re.DOTALL | re.IGNORECASE
    )
    for row_m in row_pattern.finditer(html):
        row_html = row_m.group(1)
        heure_match = re.search(
            r'data-colno="1"[^>]*>\s*(\d{1,2}:\d{2})\s*<',
            row_html, re.IGNORECASE
        )
        if not heure_match:
            continue
        h_trouvee = heure_match.group(1).strip()
        # Normaliser pour comparaison (8:48 == 08:48)
        h_norm = f"{int(h_trouvee.split(':')[0]):02d}:{h_trouvee.split(':')[1]}"
        h_cible_norm = f"{int(heure_cible.split(':')[0]):02d}:{heure_cible.split(':')[1]}"

        if h_norm != h_cible_norm:
            continue

        confirm_match = re.search(
            r'data-colno="0"[^>]*>.*?href="([^"]*req=confirm[^"]*)"',
            row_html, re.DOTALL | re.IGNORECASE
        )
        if confirm_match:
            confirm_url = confirm_match.group(1).replace("&amp;", "&")
            logger.info(f"[Booker GGG] Format 2 (autogrid) trouve: {confirm_url}")
            await page.goto(confirm_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            return True

    # Logger les heures trouvées pour debug
    heures_trouvees = re.findall(r'data-colno="1"[^>]*>\s*(\d{1,2}:\d{2})\s*<', html)
    logger.warning(f"[Booker GGG] Heure {heure_cible} non trouvee. Heures dispo: {heures_trouvees[:10]}")
    return False


async def _reserver_chronogolf(terrain: dict, confirm_url: str, username: str, password: str) -> dict:
    return {
        "succes": False,
        "message": "La réservation directe Chronogolf n'est pas encore disponible. Cliquez sur 'Voir sur Chronogolf' pour réserver.",
        "url_fallback": terrain.get("url_reservation", confirm_url),
    }
