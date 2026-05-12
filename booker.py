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

            logger.info(f"[Booker GGG] Login réussi, navigation vers confirm_url")

            # ── Étape 2 : Naviguer vers l'URL de confirmation ──
            await page.goto(confirm_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=10000)

            content = await page.content()
            logger.info(f"[Booker GGG] Page confirmation: {len(content)} chars")

            # ── Étape 3 : Confirmer la réservation ──────────
            # GGG affiche un bouton de confirmation après navigation vers confirm_url
            confirm_selectors = [
                "input[name='confirm'], input[value*='Confirm'], input[value*='Réserver']",
                "button:has-text('Confirmer'), button:has-text('Réserver')",
                "input[type='submit'][value*='onfirm']",
                ".btn-confirm, .btn-reserve, #confirm-btn",
                "input[name='sConfirm']",
            ]

            confirm_btn = None
            for sel in confirm_selectors:
                confirm_btn = await page.query_selector(sel)
                if confirm_btn:
                    break

            if confirm_btn:
                await confirm_btn.click()
                await page.wait_for_load_state("networkidle", timeout=10000)
                final_content = await page.content()

                # Vérifier si la réservation a réussi
                success_indicators = [
                    "réservation confirmée",
                    "booking confirmed",
                    "confirmation",
                    "réservé avec succès",
                    "successfully booked",
                    "votre réservation",
                ]
                for indicator in success_indicators:
                    if indicator.lower() in final_content.lower():
                        await browser.close()
                        logger.info(f"[Booker GGG] Réservation confirmée!")
                        return {
                            "succes": True,
                            "message": "Réservation confirmée! Vous recevrez une confirmation par email.",
                        }

                # Si pas d'indicateur clair, on assume succès si pas d'erreur
                error_indicators = ["erreur", "error", "impossible", "failed", "échec"]
                for indicator in error_indicators:
                    if indicator.lower() in final_content.lower():
                        await browser.close()
                        return {
                            "succes": False,
                            "message": "Erreur lors de la confirmation. Le départ est peut-être déjà pris.",
                        }

                await browser.close()
                return {
                    "succes": True,
                    "message": "Réservation soumise. Vérifiez votre email pour la confirmation.",
                }
            else:
                # Pas de bouton de confirmation trouvé
                # Peut-être que la page confirm_url réserve directement
                success_indicators = [
                    "réservation confirmée", "booking confirmed",
                    "confirmation", "réservé", "booked",
                ]
                for indicator in success_indicators:
                    if indicator.lower() in content.lower():
                        await browser.close()
                        return {
                            "succes": True,
                            "message": "Réservation confirmée! Vérifiez votre email.",
                        }

                await browser.close()
                logger.warning(f"[Booker GGG] Bouton de confirmation non trouvé")
                return {
                    "succes": False,
                    "message": "Impossible de finaliser la réservation. Essayez directement sur le site GGG Golf.",
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
