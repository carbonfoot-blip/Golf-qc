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
    Flow Chronogolf 100% Playwright:
    1. Login sur .ca via popup Angular (Turnstile passe automatiquement)
    2. Naviguer vers la page booking du terrain
    3. Sélectionner la date
    4. Cliquer sur le départ voulu → génère teetime_freeze automatiquement
    5. Cocher "J'accepte" et confirmer → POST fait par Angular
    """
    slug = terrain.get("chronogolf_slug", terrain["id"])
    club_id = terrain.get("chronogolf_club_id")
    url_base = f"https://www.chronogolf.ca/club/{slug}"
    booking_url = f"https://www.chronogolf.ca/club/{slug}/booking/?source=chronogolf&medium=profile"

    logger.info(f"[Booker Chrono] Debut Playwright pur: {terrain['nom']} — {date} {heure}")

    heure_norm = f"{int(heure.split(':')[0]):02d}:{heure.split(':')[1]}" if heure and ":" in heure else ""

    try:
        import json as _json

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
                viewport={"width": 1280, "height": 800},
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = await context.new_page()

            # ── 1. Login sur .ca ──────────────────────────────────────────────
            await page.goto("https://www.chronogolf.ca/fr", timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Cliquer Se connecter
            login_btn = await page.query_selector("a[ng-click*='openLightbox'], a[ng-click*='login'], a:has-text('Se connecter')")
            if login_btn:
                await login_btn.click()
                await page.wait_for_timeout(2000)
                logger.info(f"[Booker Chrono] Popup login ouvert")

            email_field = await page.query_selector("input[name='email'], input[id='sessionEmail'], input[type='email']")
            pwd_field   = await page.query_selector("input[name='password'], input[id='sessionPassword'], input[type='password']")

            if not email_field or not pwd_field:
                await browser.close()
                return {"succes": False, "message": "Page login Chronogolf introuvable.", "url_fallback": url_base}

            await email_field.type(username, delay=30)
            await page.wait_for_timeout(300)
            await pwd_field.type(password, delay=30)
            await page.wait_for_timeout(500)

            # Soumettre
            submit = await page.query_selector("button[type='submit']")
            if submit:
                await submit.click()
            else:
                await pwd_field.press("Enter")

            # Attendre que le popup disparaisse
            try:
                await page.wait_for_function(
                    "!document.querySelector('.session-lightbox.ng-scope.active')",
                    timeout=15000
                )
            except Exception:
                await page.wait_for_timeout(5000)

            logger.info(f"[Booker Chrono] Apres login: {page.url}")

            # Vérifier si connecté
            content = await page.content()
            if "Se connecter" in content and "session-lightbox" in content and "active" in content:
                await browser.close()
                return {"succes": False, "message": "Login Chronogolf échoué.", "url_fallback": url_base}

            logger.info(f"[Booker Chrono] Login réussi!")

            # ── 2. Naviguer vers la page booking ─────────────────────────────
            await page.goto(booking_url, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
            logger.info(f"[Booker Chrono] Booking: {page.url}")

            # ── 3. Sélectionner la date ───────────────────────────────────────
            # Entrer la date dans le calendrier Angular
            date_set = await page.evaluate(f"""
                (function() {{
                    // Trouver le scope Angular de la page de booking
                    var el = document.querySelector('[ng-controller]') || document.querySelector('[data-ng-controller]');
                    if (!el) return 'no-controller';
                    var scope = angular.element(el).scope();
                    if (!scope) return 'no-scope';
                    scope.$apply(function() {{
                        if (scope.selectedDate !== undefined) scope.selectedDate = new Date('{date}T12:00:00');
                        if (scope.date !== undefined) scope.date = '{date}';
                        if (scope.search) scope.search.date = '{date}';
                    }});
                    return 'ok';
                }})()
            """)
            logger.info(f"[Booker Chrono] Date set: {date_set}")

            # Cliquer sur la date dans le calendrier si visible
            try:
                # Chercher le jour dans le calendrier
                day = str(int(date.split('-')[2]))
                date_btn = await page.query_selector(f"td.day:not(.old):not(.new):has-text('{day}'), .datepicker-days td:not(.old):not(.new):has-text('{day}')")
                if date_btn:
                    await date_btn.click()
                    await page.wait_for_timeout(2000)
                    logger.info(f"[Booker Chrono] Date cliquée: {day}")
            except Exception as e:
                logger.warning(f"[Booker Chrono] Clic date: {e}")

            await page.wait_for_timeout(2000)

            # ── 4. Intercepter les requêtes POST reservation ──────────────────
            reservation_result = {"done": False}

            async def handle_response(response):
                if "marketplace/reservations" in response.url and response.request.method == "POST":
                    status = response.status
                    try:
                        body = await response.text()
                        logger.info(f"[Booker Chrono] Intercept POST reservations: {status} — {body[:150]}")
                        reservation_result["status"] = status
                        reservation_result["body"] = body
                        reservation_result["done"] = True
                    except Exception:
                        pass

            page.on("response", handle_response)

            # ── 5. Cliquer sur le départ voulu ───────────────────────────────
            # Chercher les départs disponibles dans la page
            await page.wait_for_timeout(2000)

            # Chercher via l'API Angular dans le scope
            teeimes_info = await page.evaluate(f"""
                (function() {{
                    try {{
                        var scopes = document.querySelectorAll('[ng-repeat*="teetime"], [ng-repeat*="tee_time"]');
                        return 'found ' + scopes.length + ' ng-repeat';
                    }} catch(e) {{ return 'error: ' + e.message; }}
                }})()
            """)
            logger.info(f"[Booker Chrono] TeeTime elements: {teeimes_info}")

            # Chercher le départ par l'heure dans le DOM
            teetime_clicked = await page.evaluate(f"""
                (async function() {{
                    // Attendre que les départs chargent
                    await new Promise(r => setTimeout(r, 2000));

                    // Chercher tous les éléments avec l'heure cible
                    var heure = '{heure_norm}';
                    var elements = Array.from(document.querySelectorAll('*'));
                    var found = elements.filter(el =>
                        el.children.length === 0 &&
                        el.textContent.trim() === heure
                    );
                    if (found.length > 0) {{
                        // Cliquer sur le parent cliquable
                        var el = found[0];
                        while (el && !el.onclick && el.tagName !== 'BUTTON' && el.tagName !== 'A' && !el.getAttribute('ng-click')) {{
                            el = el.parentElement;
                        }}
                        if (el) {{ el.click(); return 'clicked: ' + el.tagName; }}
                        found[0].click();
                        return 'clicked fallback';
                    }}
                    return 'not found - heures: ' + elements.filter(e => e.textContent.match(/^\d{{2}}:\d{{2}}$/)).slice(0,5).map(e=>e.textContent.trim()).join(',');
                }})()
            """)
            logger.info(f"[Booker Chrono] Clic départ: {teetime_clicked}")

            await page.wait_for_timeout(3000)

            # Si le départ a été cliqué, chercher la checkbox et confirmer
            page_content = await page.content()
            logger.info(f"[Booker Chrono] Après clic: {page.url} — {len(page_content)} chars")

            # Cocher la checkbox "J'accepte"
            checkbox = await page.query_selector("input[type='checkbox'], input[ng-model*='accept'], input[ng-model*='terms']")
            if checkbox:
                await checkbox.click()
                await page.wait_for_timeout(500)
                logger.info(f"[Booker Chrono] Checkbox cochée")

            # Cliquer "Confirmer la réservation"
            confirm_btn = await page.query_selector(
                "button:has-text('Confirmer'), button:has-text('Confirm'), "
                "button[ng-click*='confirm'], button[ng-click*='book'], "
                "button[ng-disabled*='accept']"
            )
            if confirm_btn:
                await confirm_btn.click()
                logger.info(f"[Booker Chrono] Bouton confirmer cliqué")
                await page.wait_for_timeout(5000)
            else:
                logger.warning(f"[Booker Chrono] Bouton confirmer non trouvé")

            await browser.close()

            # Vérifier le résultat
            if reservation_result.get("done"):
                status = reservation_result.get("status", 0)
                body = reservation_result.get("body", "")
                if status in [200, 201] and '"id"' in body:
                    return {"succes": True, "message": "Réservation Chronogolf confirmée! Vérifiez votre courriel."}
                return {"succes": False, "message": f"Erreur Chronogolf ({status}).", "url_fallback": url_base}

            return {"succes": False, "message": "Réservation non complétée. Réessayez.", "url_fallback": url_base}

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
