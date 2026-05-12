"""
booker.py — Réservation directe via Playwright.
GGG Golf : login + clic sur data-confirm-url
Chronogolf : login + API authentifiée (à venir)
"""

import logging
import re
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

logger = logging.getLogger(__name__)
TIMEOUT = 30_000


async def reserver_depart(
    terrain: dict,
    confirm_url: str,
    username: str,
    password: str,
) -> dict:
    """
    Tente de réserver un départ.
    Retourne {"succes": True/False, "message": "..."}
    """
    systeme = terrain.get("systeme", "site_propre")

    if systeme == "gggolf":
        return await _reserver_gggolf(terrain, confirm_url, username, password)
    elif systeme == "chronogolf":
        return await _reserver_chronogolf(terrain, confirm_url, username, password)
    else:
        return {"succes": False, "message": "Système de réservation non supporté pour la réservation directe."}


# ─────────────────────────────────────────────
# GGG Golf
# ─────────────────────────────────────────────

async def _reserver_gggolf(terrain: dict, confirm_url: str, username: str, password: str) -> dict:
    """
    Flow GGG Golf :
    1. Aller sur la page de login du terrain
    2. Entrer les credentials
    3. Naviguer vers confirm_url
    4. Confirmer la réservation
    """
    slug = terrain.get("ggg_slug", terrain["id"])
    login_url = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=user&lang=fr"

    logger.info(f"[Booker GGG] Réservation: {terrain['nom']} — {confirm_url}")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
            )
            page = await context.new_page()

            # ── Étape 1 : Login ──────────────────────────────
            logger.info(f"[Booker GGG] Login: {login_url}")
            await page.goto(login_url, timeout=TIMEOUT, wait_until="domcontentloaded")

            # Chercher les champs login GGG
            # GGG Golf: name="email" et name="password" (confirme par inspection HTML)
            username_field = await page.query_selector(
                "input[name='email'], input[id='email'], input[name='username'], #username"
            )
            password_field = await page.query_selector(
                "input[name='password'], input[type='password'], input[id='password'], #password"
            )

            logger.info(f"[Booker GGG] username_field trouve: {username_field is not None}")
            logger.info(f"[Booker GGG] password_field trouve: {password_field is not None}")

            if not username_field or not password_field:
                await browser.close()
                return {
                    "succes": False,
                    "message": "Page de connexion introuvable. Vérifiez vos identifiants GGG Golf.",
                }

            await username_field.fill(username)
            await password_field.fill(password)

            # Soumettre le formulaire de login
            # GGG Golf: bouton "Connexion"
            submit = await page.query_selector(
                "button:has-text('Connexion'), input[value*='Connexion'], "
                "button[type='submit'], input[type='submit']"
            )
            if submit:
                logger.info(f"[Booker GGG] Clic sur bouton Connexion")
                await submit.click()
            else:
                logger.info(f"[Booker GGG] Bouton non trouve — Enter")
                await page.keyboard.press("Enter")

            await page.wait_for_load_state("networkidle", timeout=10000)

            # Vérifier si le login a réussi
            current_url = page.url
            page_content = await page.content()

            # GGG redirige vers le profil ou la page principale après login réussi
            login_failed_indicators = [
                "identifiant incorrect",
                "mot de passe incorrect",
                "invalid credentials",
                "login failed",
                "échec de connexion",
                "incorrect username",
            ]
            for indicator in login_failed_indicators:
                if indicator.lower() in page_content.lower():
                    await browser.close()
                    return {
                        "succes": False,
                        "message": "Identifiants incorrects. Vérifiez votre nom d'utilisateur et mot de passe GGG Golf.",
                    }

            logger.info(f"[Booker GGG] Login reussi — URL actuelle: {page.url}")
            logger.info(f"[Booker GGG] Navigation vers: {confirm_url}")

            # ── Étape 2 : Naviguer vers la page de confirmation ──
            # Attendre un peu pour que la session soit bien etablie
            await page.wait_for_timeout(1000)
            await page.goto(confirm_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=10000)
            logger.info(f"[Booker GGG] URL apres navigation: {page.url}")

            content = await page.content()
            logger.info(f"[Booker GGG] Page confirmation: {len(content)} chars — URL: {page.url}")

            # Ne pas bloquer sur l'URL — GGG peut rediriger
            # Verifier seulement si la page contient une erreur explicite
            hard_errors = ["introuvable", "not found", "404", "acces refus"]
            for err in hard_errors:
                if err in content.lower():
                    await browser.close()
                    return {
                        "succes": False,
                        "message": "Ce départ n'est plus disponible.",
                        "url_fallback": confirm_url,
                    }

            # ── Étape 3 : Cliquer "J'accepte les termes et je confirme ma réservation" ──
            # Bouton GGG confirme: input[type="submit"] avec value="J'accepte les termes..."
            # Utiliser des selecteurs sans apostrophe dans la valeur
            confirm_btn = await page.query_selector("input[name='nook']")
            if not confirm_btn:
                confirm_btn = await page.query_selector("input[value*='accepte']")
            if not confirm_btn:
                confirm_btn = await page.query_selector("input[value*='confirme']")
            if not confirm_btn:
                confirm_btn = await page.query_selector("input[value*='Accepte']")

            if not confirm_btn:
                # Chercher n'importe quel submit qui n'est pas "Faire une autre recherche"
                all_submits = await page.query_selector_all("input[type='submit'], button[type='submit']")
                for btn in all_submits:
                    val = await btn.get_attribute("value") or await btn.inner_text()
                    if val and "recherche" not in val.lower() and "cancel" not in val.lower():
                        confirm_btn = btn
                        break

            if confirm_btn:
                btn_val = await confirm_btn.get_attribute("value") or ""
                logger.info(f"[Booker GGG] Clic bouton confirmation: '{btn_val[:50]}'")
                await confirm_btn.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                final_content = await page.content()
                logger.info(f"[Booker GGG] Page finale: {len(final_content)} chars")

                # Verifier le succes
                success_indicators = [
                    "numero de reservation", "numéro de réservation",
                    "reservation confirmee", "réservation confirmée",
                    "confirmation number", "booking confirmed",
                    "merci", "thank you",
                ]
                for indicator in success_indicators:
                    if indicator.lower() in final_content.lower():
                        await browser.close()
                        logger.info(f"[Booker GGG] SUCCES — indicateur: '{indicator}'")
                        return {
                            "succes": True,
                            "message": "Réservation confirmée! Vous recevrez une confirmation par courriel.",
                        }

                # Verifier erreurs
                error_indicators = ["erreur", "error", "impossible", "failed", "déjà", "already"]
                for indicator in error_indicators:
                    if indicator.lower() in final_content.lower():
                        await browser.close()
                        return {
                            "succes": False,
                            "message": "Erreur lors de la confirmation. Le départ est peut-être déjà pris.",
                            "url_fallback": confirm_url,
                        }

                # Aucun indicateur clair — logger pour debug
                logger.warning(f"[Booker GGG] Aucun indicateur clair. HTML[1000:2000]: {final_content[1000:2000]}")
                await browser.close()
                return {
                    "succes": True,
                    "message": "Réservation soumise. Vérifiez votre courriel pour la confirmation.",
                }
            else:
                logger.warning(f"[Booker GGG] Bouton de confirmation non trouve")
                logger.warning(f"[Booker GGG] HTML[500:1500]: {content[500:1500]}")
                await browser.close()
                return {
                    "succes": False,
                    "message": "Impossible de trouver le bouton de confirmation. Essayez directement sur le site GGG Golf.",
                    "url_fallback": confirm_url,
                }

    except Exception as e:
        logger.error(f"[Booker GGG] Erreur: {e}")
        return {
            "succes": False,
            "message": f"Erreur technique lors de la réservation. Essayez directement sur le site GGG Golf.",
            "url_fallback": confirm_url,
        }


# ─────────────────────────────────────────────
# Chronogolf (à venir)
# ─────────────────────────────────────────────

async def _reserver_chronogolf(terrain: dict, confirm_url: str, username: str, password: str) -> dict:
    """
    Chronogolf — réservation via API authentifiée.
    À implémenter dans une prochaine version.
    """
    return {
        "succes": False,
        "message": "La réservation directe Chronogolf n'est pas encore disponible. Cliquez sur 'Voir sur Chronogolf' pour réserver.",
        "url_fallback": terrain.get("url_reservation", confirm_url),
    }
