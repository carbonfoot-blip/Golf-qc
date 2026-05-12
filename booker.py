"""
booker.py — Réservation GGG Golf via Playwright (session persistée).
Le login ET la confirmation se font dans le même contexte Playwright.
"""

import logging
import re
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

logger = logging.getLogger(__name__)
TIMEOUT = 30_000


async def reserver_depart(terrain: dict, confirm_url: str, username: str, password: str) -> dict:
    systeme = terrain.get("systeme", "site_propre")
    if systeme == "gggolf":
        return await _reserver_gggolf(terrain, confirm_url, username, password)
    elif systeme == "chronogolf":
        return await _reserver_chronogolf(terrain, confirm_url, username, password)
    return {"succes": False, "message": "Système non supporté."}


async def _reserver_gggolf(terrain: dict, confirm_url: str, username: str, password: str) -> dict:
    slug = terrain.get("ggg_slug", terrain["id"])
    login_url = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=user&lang=fr"

    logger.info(f"[Booker GGG] Debut reservation: {terrain['nom']}")
    logger.info(f"[Booker GGG] confirm_url: {confirm_url}")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
            )
            page = await context.new_page()

            # ── Étape 1 : Charger la page de login ──
            await page.goto(login_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            logger.info(f"[Booker GGG] Page login chargee: {page.url}")

            # ── Étape 2 : Remplir et soumettre le formulaire ──
            email_field = await page.query_selector("input[name='email'], input[id='email']")
            pwd_field   = await page.query_selector("input[name='password'], input[type='password']")

            if not email_field or not pwd_field:
                await browser.close()
                return {"succes": False, "message": "Page de connexion introuvable."}

            await email_field.fill(username)
            await pwd_field.fill(password)
            logger.info(f"[Booker GGG] Credentials remplis")

            # Cliquer le bouton Connexion et attendre la navigation
            async with page.expect_navigation(timeout=15000):
                submit = await page.query_selector(
                    "button:has-text('Connexion'), input[type='submit'], button[type='submit']"
                )
                if submit:
                    await submit.click()
                else:
                    await pwd_field.press("Enter")

            logger.info(f"[Booker GGG] Apres login — URL: {page.url}")

            # Verifier si login reussi
            content_apres_login = await page.content()
            fail_indicators = ["identifiant incorrect", "mot de passe incorrect", "invalid credentials"]
            for fi in fail_indicators:
                if fi in content_apres_login.lower():
                    await browser.close()
                    return {"succes": False, "message": "Identifiants incorrects. Vérifiez votre courriel et mot de passe GGG Golf."}

            # ── Étape 3 : Naviguer vers confirm_url dans le MÊME contexte ──
            logger.info(f"[Booker GGG] Navigation confirm: {confirm_url}")
            await page.goto(confirm_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)  # Laisser GGG traiter la session
            logger.info(f"[Booker GGG] URL apres goto confirm: {page.url}")

            # Si GGG redirige encore vers teetimes, essayer avec wait_until="commit"
            if "confirm" not in page.url:
                logger.info(f"[Booker GGG] Redirection detectee, 2e tentative...")
                await page.goto(confirm_url, timeout=TIMEOUT, wait_until="commit")
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                logger.info(f"[Booker GGG] URL apres 2e tentative: {page.url}")

            confirm_content = await page.content()
            logger.info(f"[Booker GGG] Page confirm: {len(confirm_content)} chars")

            # Verifier qu'on est sur la bonne page
            if "j'accepte" not in confirm_content.lower() and "accepte les termes" not in confirm_content.lower():
                logger.warning(f"[Booker GGG] Pas sur page confirm. HTML[0:300]: {confirm_content[:300]}")
                await browser.close()
                return {
                    "succes": False,
                    "message": "Ce départ n'est plus disponible ou la session n'a pas pu être établie.",
                    "url_fallback": confirm_url,
                }

            # ── Étape 4 : Cliquer "J'accepte les termes" ──
            confirm_btn = await page.query_selector("input[name='nook']")
            if not confirm_btn:
                confirm_btn = await page.query_selector("input[value*='accepte']")
            if not confirm_btn:
                # Chercher tout bouton submit qui n'est pas "Faire une autre recherche"
                all_submits = await page.query_selector_all("input[type='submit']")
                for btn in all_submits:
                    val = (await btn.get_attribute("value") or "").lower()
                    if "recherche" not in val and "cancel" not in val and val:
                        confirm_btn = btn
                        logger.info(f"[Booker GGG] Bouton trouve: '{val}'")
                        break

            if not confirm_btn:
                logger.warning(f"[Booker GGG] Bouton confirmation non trouve")
                await browser.close()
                return {
                    "succes": False,
                    "message": "Impossible de trouver le bouton de confirmation.",
                    "url_fallback": confirm_url,
                }

            btn_val = await confirm_btn.get_attribute("value") or ""
            logger.info(f"[Booker GGG] Clic: '{btn_val[:60]}'")

            async with page.expect_navigation(timeout=20000):
                await confirm_btn.click()

            final_url = page.url
            final_content = await page.content()
            logger.info(f"[Booker GGG] Page finale: {len(final_content)} chars — URL: {final_url}")

            # ── Étape 5 : Vérifier le succès ──
            success_indicators = [
                "numero de reservation", "numéro de réservation",
                "reservation confirmee", "réservation confirmée",
                "confirmation", "merci", "thank you",
                "confirmli", "votre reservation",
            ]
            for indicator in success_indicators:
                if indicator.lower() in final_content.lower():
                    logger.info(f"[Booker GGG] SUCCES — '{indicator}'")
                    await browser.close()
                    return {
                        "succes": True,
                        "message": "Réservation confirmée! Vous recevrez une confirmation par courriel.",
                    }

            error_indicators = ["erreur", "impossible", "deja reserve", "déjà réservé", "already booked"]
            for indicator in error_indicators:
                if indicator.lower() in final_content.lower():
                    await browser.close()
                    return {
                        "succes": False,
                        "message": "Erreur — le départ est peut-être déjà pris.",
                        "url_fallback": confirm_url,
                    }

            logger.warning(f"[Booker GGG] Aucun indicateur. HTML[0:800]: {final_content[:800]}")
            await browser.close()
            return {
                "succes": True,
                "message": "Réservation soumise. Vérifiez votre courriel.",
            }

    except Exception as e:
        logger.error(f"[Booker GGG] Erreur: {e}")
        return {
            "succes": False,
            "message": "Erreur technique. Essayez directement sur le site GGG Golf.",
            "url_fallback": confirm_url,
        }


async def _reserver_chronogolf(terrain: dict, confirm_url: str, username: str, password: str) -> dict:
    return {
        "succes": False,
        "message": "La réservation directe Chronogolf n'est pas encore disponible. Cliquez sur 'Voir sur Chronogolf' pour réserver.",
        "url_fallback": terrain.get("url_reservation", confirm_url),
    }
