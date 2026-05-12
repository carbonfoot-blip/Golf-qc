"""
booker.py — Réservation GGG Golf.
Flow complet dans une seule session Playwright :
  1. Login
  2. Refaire la recherche (date + heure + joueurs) pour obtenir les Keys valides
  3. Cliquer sur le bon départ (correspondant à l'heure voulue)
  4. Confirmer avec "J'accepte les termes"
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
        return await _reserver_gggolf(terrain, confirm_url, username, password, date, heure, nb_joueurs)
    elif systeme == "chronogolf":
        return await _reserver_chronogolf(terrain, confirm_url, username, password)
    return {"succes": False, "message": "Système non supporté."}


async def _reserver_gggolf(terrain: dict, confirm_url: str, username: str, password: str, date: str = "", heure: str = "", nb_joueurs: int = 2) -> dict:
    """
    Flow GGG :
    1. Login
    2. Extraire date/heure/joueurs depuis confirm_url (Keys contient ces infos)
    3. Refaire la recherche avec ces paramètres dans la session connectée
    4. Cliquer sur le lien "i" du départ voulu (même heure)
    5. Confirmer
    """
    slug = terrain.get("ggg_slug", terrain["id"])
    login_url = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=user&lang=fr"
    teetimes_url = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"

    # Extraire les paramètres depuis confirm_url
    # Format: ...req=confirm&lang=fr&Keys=391817&NbHoles=18
    keys_match = re.search(r'Keys=(\d+)', confirm_url)
    holes_match = re.search(r'NbHoles=(\d+)', confirm_url)
    keys_val = keys_match.group(1) if keys_match else None

    logger.info(f"[Booker GGG] Debut: {terrain['nom']} — Keys={keys_val}")

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
                    return {"succes": False, "message": "Identifiants incorrects."}

            # ── Étape 2 : Naviguer vers confirm_url directement ──
            # On tente d'abord avec l'URL originale — si ça marche, tant mieux
            logger.info(f"[Booker GGG] Tentative confirm directe: {confirm_url}")
            await page.goto(confirm_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)

            confirm_content = await page.content()
            logger.info(f"[Booker GGG] URL apres goto: {page.url} — {len(confirm_content)} chars")

            # ── Étape 3 : Si redirigé, refaire la recherche et trouver le bon départ ──
            if "j'accepte" not in confirm_content.lower() and "accepte les termes" not in confirm_content.lower():
                logger.info(f"[Booker GGG] Confirm URL invalide — on refait la recherche")

                # Extraire heure et joueurs depuis le confirm_url via les infos stockées
                # On va chercher l'heure dans le confirm_url si disponible, sinon depuis les données passées
                # Le confirm_url contient Keys= qui encode l'heure côté GGG
                # On doit passer l'heure et la date via les paramètres supplémentaires

                # Soumettre le formulaire de recherche avec les mêmes paramètres
                # On extrait date/heure/joueurs depuis les infos du terrain dans confirm_url
                # Utiliser les paramètres passés directement
                date_val    = date
                heure_val   = str(int(heure.split(":")[0])) if heure else "7"
                joueurs_val = str(nb_joueurs)
                heure_cible = heure  # ex: "13:12"

                logger.info(f"[Booker GGG] Recherche: date={date_val} heure={heure_val} joueurs={joueurs_val} cible={heure_cible}")

                await page.goto(teetimes_url, timeout=TIMEOUT, wait_until="domcontentloaded")

                # Remplir le formulaire
                if date_val:
                    date_input = await page.query_selector("input.jquery_ui_datepicker, input[name='date']")
                    if date_input:
                        await date_input.fill(date_val)
                        await date_input.dispatch_event("change")

                hour_sel = await page.query_selector("select[name='hour'], select[name='heure']")
                if hour_sel:
                    await hour_sel.select_option(value=heure_val)

                players_sel = await page.query_selector("select[name*='player'], select[name*='joueur']")
                if players_sel:
                    await players_sel.select_option(value=str(joueurs_val))

                submit_btn = await page.query_selector("input[name='sSearch'], input[type='submit']")
                if submit_btn:
                    await submit_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)

                confirm_content = await page.content()
                logger.info(f"[Booker GGG] Apres recherche: {len(confirm_content)} chars")

                # Trouver le lien "i" du bon départ (même heure)
                # On cherche l'heure cible dans les résultats et on clique son lien confirm
                found_link = await _trouver_et_cliquer_depart(page, heure_cible, terrain)

                if not found_link:
                    await browser.close()
                    return {
                        "succes": False,
                        "message": "Ce départ n'est plus disponible.",
                        "url_fallback": teetimes_url,
                    }

                confirm_content = await page.content()
                logger.info(f"[Booker GGG] Apres clic depart: {page.url} — {len(confirm_content)} chars")

            # ── Étape 4 : Confirmer ──────────────────────────
            if "j'accepte" not in confirm_content.lower() and "accepte les termes" not in confirm_content.lower():
                logger.warning(f"[Booker GGG] Page confirm non trouvee. URL: {page.url}")
                await browser.close()
                return {
                    "succes": False,
                    "message": "Ce départ n'est plus disponible.",
                    "url_fallback": teetimes_url,
                }

            # Cliquer "J'accepte les termes"
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

            # Verifier succes
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
        return {"succes": False, "message": "Erreur technique. Essayez directement sur GGG Golf.", "url_fallback": teetimes_url if 'teetimes_url' in dir() else confirm_url}


async def _trouver_et_cliquer_depart(page, heure_cible: str, terrain: dict) -> bool:
    """
    Trouve le départ correspondant à l'heure cible dans les résultats
    et clique sur son lien de confirmation.
    """
    if not heure_cible:
        return False

    html = await page.content()
    slug = terrain.get("ggg_slug", terrain["id"])

    # Chercher dans le format teetimes_results-hour (Beloeil)
    # Pattern: data-confirm-url avec l'heure dans teetimes_results-hour
    bloc_pattern = re.compile(
        r'data-confirm-url="([^"]+)"[^>]*>.*?teetimes_results-hour[^>]*>.*?Heure:?</span>\s*(\d{1,2}:\d{2})',
        re.DOTALL | re.IGNORECASE
    )
    for m in bloc_pattern.finditer(html):
        if m.group(2).strip() == heure_cible:
            confirm_url = m.group(1)
            logger.info(f"[Booker GGG] Depart trouve (format 1): {confirm_url}")
            await page.goto(confirm_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            return True

    # Chercher dans le format autogrid (Madeleine)
    row_pattern = re.compile(
        r'<tr[^>]*class="[^"]*autogrid(?:Even|Odd)[^"]*"[^>]*>(.*?)</tr>',
        re.DOTALL | re.IGNORECASE
    )
    for row_m in row_pattern.finditer(html):
        row_html = row_m.group(1)
        heure_match = re.search(r'data-colno="1"[^>]*>\s*(\d{1,2}:\d{2})\s*<', row_html, re.IGNORECASE)
        if not heure_match or heure_match.group(1).strip() != heure_cible:
            continue

        # Trouver le lien confirm dans data-colno="0"
        confirm_match = re.search(r'data-colno="0"[^>]*>.*?href="([^"]*req=confirm[^"]*)"', row_html, re.DOTALL | re.IGNORECASE)
        if confirm_match:
            confirm_url = confirm_match.group(1).replace("&amp;", "&")
            logger.info(f"[Booker GGG] Depart trouve (autogrid): {confirm_url}")
            await page.goto(confirm_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            return True

    logger.warning(f"[Booker GGG] Heure {heure_cible} non trouvee dans les resultats")
    return False


async def _reserver_chronogolf(terrain: dict, confirm_url: str, username: str, password: str) -> dict:
    return {
        "succes": False,
        "message": "La réservation directe Chronogolf n'est pas encore disponible. Cliquez sur 'Voir sur Chronogolf' pour réserver.",
        "url_fallback": terrain.get("url_reservation", confirm_url),
    }
