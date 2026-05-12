"""
booker.py — Réservation GGG Golf via httpx (sessions cookies).
Flow:
  1. GET page login pour les cookies
  2. POST credentials → session authentifiée
  3. GET confirm_url avec la session → page de confirmation
  4. POST formulaire de confirmation (input name="nook")
"""

import logging
import re
import httpx

logger = logging.getLogger(__name__)
HTTP_TIMEOUT = 30


async def reserver_depart(terrain: dict, confirm_url: str, username: str, password: str) -> dict:
    systeme = terrain.get("systeme", "site_propre")
    if systeme == "gggolf":
        return await _reserver_gggolf(terrain, confirm_url, username, password)
    elif systeme == "chronogolf":
        return await _reserver_chronogolf(terrain, confirm_url, username, password)
    else:
        return {"succes": False, "message": "Système non supporté pour la réservation directe."}


async def _reserver_gggolf(terrain: dict, confirm_url: str, username: str, password: str) -> dict:
    slug = terrain.get("ggg_slug", terrain["id"])
    base_url = f"https://secure.gggolf.ca/{slug}"
    login_url = f"{base_url}/index.php?option=com_ggpublic&req=user&lang=fr"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-CA,fr;q=0.9",
        "Origin": f"https://secure.gggolf.ca",
        "Referer": login_url,
    }

    logger.info(f"[Booker GGG] Reservation: {terrain['nom']} — {confirm_url}")

    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers=headers,
        ) as client:

            # ── Étape 1 : GET page login (cookies initiaux) ──
            get_resp = await client.get(login_url)
            logger.info(f"[Booker GGG] GET login: {get_resp.status_code} — cookies: {dict(client.cookies)}")

            # ── Étape 2 : Extraire les champs hidden du formulaire de login (CSRF token) ──
            hidden_login = {}
            pat = re.compile(r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', re.IGNORECASE)
            for m in pat.finditer(get_resp.text):
                hidden_login[m.group(1)] = m.group(2)
            pat2 = re.compile(r'<input[^>]+name="([^"]+)"[^>]+type="hidden"[^>]+value="([^"]*)"', re.IGNORECASE)
            for m in pat2.finditer(get_resp.text):
                hidden_login[m.group(1)] = m.group(2)
            logger.info(f"[Booker GGG] Champs hidden login: {hidden_login}")

            login_payload = {
                **hidden_login,
                "email": username,
                "password": password,
                "option": "com_ggpublic",
                "req": "user",
                "lang": "fr",
                "task": "login",
            }

            login_resp = await client.post(
                login_url,
                data=login_payload,
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
            )
            logger.info(f"[Booker GGG] POST login: {login_resp.status_code} — URL finale: {login_resp.url}")
            logger.info(f"[Booker GGG] Cookies apres login: {dict(client.cookies)}")

            # Verifier si le login a reussi
            login_content = login_resp.text
            fail_indicators = [
                "identifiant incorrect", "mot de passe incorrect",
                "invalid", "login failed", "echec", "incorrect"
            ]
            for fi in fail_indicators:
                if fi in login_content.lower():
                    return {
                        "succes": False,
                        "message": "Identifiants incorrects. Vérifiez votre courriel et mot de passe GGG Golf.",
                    }

            # Verifier qu'on est connecte (GGG affiche "Se déconnecter" quand connecte)
            if "deconnecter" not in login_content.lower() and "se déconnecter" not in login_content.lower() and "mon compte" not in login_content.lower():
                logger.warning(f"[Booker GGG] Login possiblement echoue — pas d'indicateur de session")
                # On continue quand meme

            # ── Étape 3 : GET confirm_url avec la session ──
            logger.info(f"[Booker GGG] GET confirm: {confirm_url}")
            confirm_resp = await client.get(confirm_url)
            logger.info(f"[Booker GGG] GET confirm: {confirm_resp.status_code} — URL: {confirm_resp.url}")

            confirm_content = confirm_resp.text
            logger.info(f"[Booker GGG] Page confirm: {len(confirm_content)} chars")

            # Verifier qu'on est bien sur la page de confirmation
            if "req=confirm" not in str(confirm_resp.url) and "keys=" not in confirm_content.lower() and "j'accepte" not in confirm_content.lower():
                logger.warning(f"[Booker GGG] Pas sur la page confirm — URL: {confirm_resp.url}")
                logger.warning(f"[Booker GGG] HTML[0:500]: {confirm_content[:500]}")
                return {
                    "succes": False,
                    "message": "Ce départ n'est plus disponible ou votre session a expiré.",
                    "url_fallback": confirm_url,
                }

            # ── Étape 4 : Extraire et soumettre le formulaire de confirmation ──
            # GGG: <form id="confirmationForm" method="POST" action="...req=confirmli...">
            #       <input type="submit" name="nook" value="J'accepte les termes...">
            form_action_match = re.search(
                r'<form[^>]*id="[^"]*[Cc]onfirm[^"]*"[^>]*action="([^"]+)"',
                confirm_content, re.IGNORECASE
            )
            if not form_action_match:
                form_action_match = re.search(
                    r'<form[^>]*action="([^"]*req=confirm[^"]*)"',
                    confirm_content, re.IGNORECASE
                )

            form_action = form_action_match.group(1) if form_action_match else confirm_url
            # Decoder les entites HTML
            form_action = form_action.replace("&amp;", "&")
            logger.info(f"[Booker GGG] Form action: {form_action}")

            # Extraire les champs hidden du formulaire
            hidden_fields = {}
            for m in re.finditer(
                r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"',
                confirm_content, re.IGNORECASE
            ):
                hidden_fields[m.group(1)] = m.group(2)

            logger.info(f"[Booker GGG] Champs hidden: {hidden_fields}")

            # Payload de confirmation
            confirm_payload = {
                **hidden_fields,
                "nook": "J'accepte les termes et je confirme ma réservation",
                "option": "com_ggpublic",
                "req": "confirmli",
                "lang": "fr",
            }

            final_resp = await client.post(
                form_action,
                data=confirm_payload,
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded",
                         "Referer": confirm_url},
            )
            logger.info(f"[Booker GGG] POST confirm: {final_resp.status_code} — URL: {final_resp.url}")

            final_content = final_resp.text
            logger.info(f"[Booker GGG] Page finale: {len(final_content)} chars")

            # Verifier le succes
            success_indicators = [
                "numero de reservation", "numéro de réservation",
                "reservation confirmee", "réservation confirmée",
                "confirmation number", "booking confirmed",
                "merci", "thank you", "confirmli",
            ]
            for indicator in success_indicators:
                if indicator.lower() in final_content.lower():
                    logger.info(f"[Booker GGG] SUCCES — indicateur: '{indicator}'")
                    return {
                        "succes": True,
                        "message": "Réservation confirmée! Vous recevrez une confirmation par courriel.",
                    }

            # Verifier erreurs
            error_indicators = ["erreur", "impossible", "already", "deja reserve", "déjà réservé"]
            for indicator in error_indicators:
                if indicator.lower() in final_content.lower():
                    return {
                        "succes": False,
                        "message": "Erreur lors de la confirmation. Le départ est peut-être déjà pris.",
                        "url_fallback": confirm_url,
                    }

            # Logger pour debug si aucun indicateur
            logger.warning(f"[Booker GGG] Aucun indicateur clair. HTML[0:1000]: {final_content[:1000]}")
            return {
                "succes": True,
                "message": "Réservation soumise. Vérifiez votre courriel pour la confirmation.",
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
