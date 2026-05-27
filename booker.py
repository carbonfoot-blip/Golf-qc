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
    Flow Chronogolf 100% Playwright — suit exactement le flow utilisateur:
    1. Login sur .ca
    2. Page club → cliquer date dans calendrier
    3. Popup trous → 18 trous → Continuer
    4. Popup joueurs → nb_joueurs → Continuer
    5. Cliquer le départ voulu
    6. Cocher checkbox → Confirmer
    """
    slug = terrain.get("chronogolf_slug", terrain["id"])
    url_base = f"https://www.chronogolf.ca/club/{slug}"
    heure_norm = f"{int(heure.split(':')[0]):02d}:{heure.split(':')[1]}" if heure and ":" in heure else ""

    logger.info(f"[Booker Chrono] Debut: {terrain['nom']} — {date} {heure_norm}")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
                viewport={"width": 1280, "height": 900},
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = await context.new_page()

            # ── 1. Aller sur la page du club D'ABORD, puis login ─────────────
            await page.goto(url_base, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            logger.info(f"[Booker Chrono] Page club initiale: {page.url}")

            # Cliquer Se connecter depuis la page du club
            login_trigger = await page.query_selector(
                "a:has-text('Se connecter'), a:has-text('Log In'), "
                "button:has-text('Se connecter'), [ng-click*='login'], [ng-click*='session']"
            )
            if login_trigger:
                await login_trigger.click()
                await page.wait_for_timeout(2000)
                logger.info(f"[Booker Chrono] Bouton login cliqué depuis page club")
            else:
                # Essayer de naviguer vers /fr pour le login puis revenir
                logger.warning(f"[Booker Chrono] Bouton login non trouvé sur page club — via /fr")
                await page.goto("https://www.chronogolf.ca/fr", timeout=TIMEOUT, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                login_trigger2 = await page.query_selector("a:has-text('Se connecter'), a:has-text('Log In')")
                if login_trigger2:
                    await login_trigger2.click()
                    await page.wait_for_timeout(2000)

            try:
                await page.wait_for_selector("input[type='email']", timeout=8000, state="visible")
            except Exception:
                pass

            email_f = await page.query_selector("input[type='email'], input[name='email']")
            pwd_f   = await page.query_selector("input[type='password'], input[name='password']")

            if not email_f or not pwd_f:
                await browser.close()
                return {"succes": False, "message": "Page login introuvable.", "url_fallback": url_base}

            await email_f.click()
            await email_f.type(username, delay=30)
            await pwd_f.click()
            await pwd_f.type(password, delay=30)
            await pwd_f.press("Enter")

            try:
                await page.wait_for_function(
                    "!document.querySelector('.session-lightbox.ng-scope.active')",
                    timeout=15000
                )
            except Exception:
                await page.wait_for_timeout(5000)

            if "/login" in page.url:
                await browser.close()
                return {"succes": False, "message": "Identifiants Chronogolf incorrects.", "url_fallback": url_base}

            logger.info(f"[Booker Chrono] Login OK: {page.url}")

            # Fermer le modal backdrop si encore présent
            await page.wait_for_timeout(1000)
            backdrop = await page.query_selector(".modal-backdrop, .session-lightbox.active")
            if backdrop:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(1000)
                logger.info(f"[Booker Chrono] Modal fermé via Escape")

            # Si on est sur /fr, naviguer vers le club
            if "/fr" in page.url and slug not in page.url:
                await page.goto(url_base, timeout=TIMEOUT, wait_until="domcontentloaded")
                await page.wait_for_timeout(5000)

            # ── 2. Vérifier qu'on est connecté sur la page du club ────────────
            nav_result = "direct"

            # Vérifier si connecté
            connected = await page.evaluate("""
                (() => {
                    var html = document.documentElement.innerHTML;
                    return html.includes('Déconnexion') || html.includes('logout') ||
                           html.includes('Mon compte') || html.includes('Felix') ||
                           document.querySelector('.user-menu, .user-name, [ng-if*="session.user"]') !== null;
                })()
            """)
            logger.info(f"[Booker Chrono] Club: {page.url} — connecté: {connected} — {len(await page.content())} chars")

            # ── 3. Interagir avec le widget de booking ────────────────────────
            date_parts = date.split('-')
            target_day = int(date_parts[2])

            # Ouvrir le widget de booking si nécessaire
            booking_widget = await page.query_selector(".booking-widget-container, .club-card-booking")
            if booking_widget:
                logger.info(f"[Booker Chrono] Widget booking trouvé")
            else:
                reserve_btn = await page.query_selector(
                    "a:has-text('Réservation'), button:has-text('Réserver'), "
                    ".club-profile-reservation, [class*='reservation']"
                )
                if reserve_btn:
                    await reserve_btn.click()
                    await page.wait_for_timeout(2000)

            # Le calendrier est uib-datepicker avec ng-change="confirmStep()"
            # Cliquer directement sur la cellule du bon jour
            click_result = await page.evaluate(f"""
                (async function() {{
                    // S'assurer que le panel date est ouvert
                    var btn = document.querySelector('[aria-controls="panel-date-body"]');
                    if (btn) btn.click();
                    await new Promise(r => setTimeout(r, 500));

                    // Trouver le datepicker
                    var dp = document.querySelector('div[uib-datepicker]');
                    if (!dp) return 'datepicker not found';

                    // Chercher la cellule du bon jour ({target_day})
                    var cells = dp.querySelectorAll('td[role="gridcell"] button, td.uib-day button');
                    var found = null;
                    var targetDay = '{target_day}';
                    var targetDayPad = targetDay.padStart(2, '0');
                    for (var cell of cells) {{
                        var txt = (cell.querySelector('span')?.textContent?.trim() || cell.textContent.trim());
                        if ((txt === targetDay || txt === targetDayPad) && !cell.disabled && !cell.closest('td.text-muted')) {{
                            found = cell;
                            break;
                        }}
                    }}
                    
                    if (found) {{
                        found.click();
                        await new Promise(r => setTimeout(r, 1000));
                        return 'clicked day ' + targetDay;
                    }}

                    // Fallback: chercher toutes les cellules visibles
                    var allCells = Array.from(cells).map(c => (c.querySelector('span')?.textContent || c.textContent).trim());
                    return 'cells found: ' + allCells.slice(0, 15).join(',');
                }})()
            """)
            logger.info(f"[Booker Chrono] Clic jour: {click_result}")
            await page.wait_for_timeout(2000)

            # Vérifier si les étapes suivantes sont maintenant actives
            steps_after = await page.evaluate("""
                (() => {
                    var els = document.querySelectorAll('[aria-busy]');
                    for (var el of els) {
                        var sc = angular.element(el).isolateScope() || angular.element(el).scope();
                        if (sc && sc.vm && sc.vm.steps) {
                            var s = sc.vm.steps;
                            return JSON.stringify({
                                date_set: s.date?.set,
                                course_disabled: s.course?.disabled,
                                players_disabled: s.players?.disabled,
                                teetime_disabled: s.teetime?.disabled
                            });
                        }
                    }
                    return 'vm not found';
                })()
            """)
            logger.info(f"[Booker Chrono] Steps après clic: {steps_after}")

            # Cliquer le bon jour dans le calendrier Angular
            day_result = await page.evaluate(f"""
                (() => {{
                    // Chercher dans widget-step-teetime ou booking-widget
                    var container = document.querySelector('.booking-widget-container, .club-profile-reservation');
                    if (!container) container = document;
                    var cells = container.querySelectorAll('td.day:not(.old):not(.new), td[class*="day"]:not([class*="old"]):not([class*="new"]), .datepicker td');
                    for (var c of cells) {{
                        if (c.textContent.trim() === '{target_day}' && !c.classList.contains('disabled')) {{
                            c.click();
                            return 'clicked day ' + {target_day};
                        }}
                    }}
                    // Essayer aussi via scope Angular
                    var scope = angular.element(document.querySelector('.booking-widget-container'))?.scope();
                    if (scope) {{
                        var keys = Object.keys(scope).filter(k => k.includes('date') || k.includes('Date'));
                        return 'scope keys: ' + keys.join(',');
                    }}
                    return 'day not found in calendar';
                }})()
            """)
            logger.info(f"[Booker Chrono] Clic date: {day_result}")
            await page.wait_for_timeout(2000)

            # Étape 2 : Course — cliquer l'INPUT 18 trous (radio)
            course_done = await page.evaluate("""
                (() => {
                    var body = document.querySelector('#panel-course-body');
                    if (!body) return 'no course body';
                    // Cliquer le label ou input pour 18 trous
                    var inputs = body.querySelectorAll('input');
                    for (var inp of inputs) {
                        if (inp.value === '18') {
                            inp.click();
                            return 'clicked input 18';
                        }
                    }
                    var labels = body.querySelectorAll('label');
                    for (var lbl of labels) {
                        if (lbl.textContent.includes('18')) {
                            lbl.click();
                            return 'clicked label 18';
                        }
                    }
                    return 'course input not found';
                })()
            """)
            logger.info(f"[Booker Chrono] Course: {course_done}")
            await page.wait_for_timeout(1000)

            # Continuer trous si disponible
            continuer_trous = await page.query_selector(
                "#panel-course-body button:has-text('Continue'), "
                "#panel-course-body button:has-text('Continuer')"
            )
            if continuer_trous:
                await continuer_trous.click()
                await page.wait_for_timeout(1000)
                logger.info(f"[Booker Chrono] Continuer trous")

            # Vérifier état après course
            steps_after_course = await page.evaluate("""
                (() => {
                    var els = document.querySelectorAll('[aria-busy]');
                    for (var el of els) {
                        var sc = angular.element(el).isolateScope() || angular.element(el).scope();
                        if (sc && sc.vm && sc.vm.steps) {
                            return JSON.stringify({
                                course_set: sc.vm.steps.course?.set,
                                players_disabled: sc.vm.steps.players?.disabled
                            });
                        }
                    }
                    return 'vm not found';
                })()
            """)
            logger.info(f"[Booker Chrono] Steps après course: {steps_after_course}")

            # Étape 3 : Players — ouvrir le step et cliquer le label du bon nombre
            # D'abord ouvrir le step players
            players_panel_btn = await page.query_selector("[aria-controls='panel-players-body']")
            if players_panel_btn:
                await players_panel_btn.click()
                await page.wait_for_timeout(1500)
                logger.info(f"[Booker Chrono] Panel players ouvert")

            # Cliquer le label radio pour le bon nombre de joueurs
            players_done = await page.evaluate(f"""
                (() => {{
                    var body = document.querySelector('#panel-players-body');
                    if (!body) return 'no players body';
                    // Chercher inputs radio avec valeur = nb_joueurs
                    var inputs = body.querySelectorAll('input');
                    for (var inp of inputs) {{
                        if (inp.value == '{nb_joueurs}') {{
                            inp.click();
                            return 'clicked input ' + inp.value;
                        }}
                    }}
                    // Chercher labels
                    var labels = body.querySelectorAll('label');
                    for (var lbl of labels) {{
                        var txt = lbl.textContent.trim();
                        if (txt === '{nb_joueurs}') {{
                            lbl.click();
                            return 'clicked label ' + txt;
                        }}
                    }}
                    // Logger ce qui est dispo
                    var allLabels = Array.from(labels).map(l => l.textContent.trim().substring(0,10));
                    var allInputs = Array.from(inputs).map(i => i.value);
                    return 'not found. labels:' + allLabels.join(',') + ' inputs:' + allInputs.join(',');
                }})()
            """)
            logger.info(f"[Booker Chrono] Players: {players_done}")
            await page.wait_for_timeout(2000)

            # Chercher Continuer joueurs avec plus de sélecteurs
            continuer2 = await page.query_selector(
                "#panel-players-body button:has-text('Continue'), "
                "#panel-players-body button:has-text('Continuer'), "
                "#panel-players-body button[type='submit'], "
                "button:has-text('Continue'), button:has-text('Continuer')"
            )
            if not continuer2:
                # Chercher via evaluate
                continuer2_el = await page.evaluate("""
                    (() => {
                        var btns = document.querySelectorAll('button');
                        for (var b of btns) {
                            var t = b.textContent.trim();
                            if ((t === 'Continue' || t === 'Continuer') && !b.disabled) return b.className;
                        }
                        return null;
                    })()
                """)
                logger.info(f"[Booker Chrono] Continuer class: {continuer2_el}")

            if continuer2:
                logger.info(f"[Booker Chrono] Continuer joueurs trouvé")
                await continuer2.click()
                await page.wait_for_timeout(5000)
                logger.info(f"[Booker Chrono] Continuer joueurs cliqué")
            else:
                # Essayer de cliquer le step teetime directement
                teetime_btn = await page.query_selector("[aria-controls='panel-teetime-body']")
                if teetime_btn:
                    await teetime_btn.click()
                    await page.wait_for_timeout(3000)
                    logger.info(f"[Booker Chrono] Teetime step ouvert directement")

            # Logger ce qu'on voit sur la page
            page_sample = await page.evaluate("document.body.innerText.substring(0, 500)")
            logger.info(f"[Booker Chrono] Page après joueurs: {page_sample[:300]}")

            # ── 6. Cliquer sur le départ voulu ───────────────────────────────
            logger.info(f"[Booker Chrono] Cherche départ {heure_norm}")

            # Attendre que les départs chargent
            await page.wait_for_timeout(2000)

            depart_clicked = await page.evaluate(f"""
                (function() {{
                    var heure = '{heure_norm}';
                    // Chercher tous les éléments texte qui correspondent à l'heure
                    var all = document.querySelectorAll('*');
                    for (var el of all) {{
                        if (el.children.length === 0 && el.textContent.trim() === heure) {{
                            // Trouver l'élément cliquable parent
                            var parent = el;
                            for (var i = 0; i < 5; i++) {{
                                parent = parent.parentElement;
                                if (!parent) break;
                                if (parent.tagName === 'BUTTON' || parent.tagName === 'A' ||
                                    parent.getAttribute('ng-click') || parent.getAttribute('onclick') ||
                                    window.getComputedStyle(parent).cursor === 'pointer') {{
                                    parent.click();
                                    return 'clicked ' + parent.tagName + ': ' + heure;
                                }}
                            }}
                            el.click();
                            return 'clicked element: ' + heure;
                        }}
                    }}
                    // Chercher aussi avec format H:MM
                    var heures_trouvees = [];
                    for (var el of all) {{
                        if (el.children.length === 0 && /^\d{{1,2}}:\d{{2}}$/.test(el.textContent.trim())) {{
                            heures_trouvees.push(el.textContent.trim());
                        }}
                    }}
                    return 'not found. Heures: ' + heures_trouvees.slice(0,8).join(',');
                }})()
            """)
            logger.info(f"[Booker Chrono] Départ: {depart_clicked}")

            if "not found" in str(depart_clicked):
                await browser.close()
                return {"succes": False, "message": f"Départ {heure} non trouvé.", "url_fallback": url_base}

            await page.wait_for_timeout(3000)

            # ── 7. Page de confirmation ───────────────────────────────────────
            logger.info(f"[Booker Chrono] Après clic départ: {page.url}")

            # Intercepter le POST reservation
            reservation_success = [False]

            async def on_response(resp):
                if "marketplace/reservations" in resp.url and resp.request.method == "POST":
                    try:
                        body = await resp.text()
                        status = resp.status
                        logger.info(f"[Booker Chrono] POST reservations: {status} — {body[:150]}")
                        if status in [200, 201] and '"id"' in body:
                            reservation_success[0] = True
                    except Exception:
                        pass

            page.on("response", on_response)

            # Cocher la checkbox
            await page.wait_for_timeout(2000)
            checkbox = await page.query_selector("input[type='checkbox']")
            if checkbox:
                await checkbox.click()
                await page.wait_for_timeout(500)
                logger.info(f"[Booker Chrono] Checkbox cochée")
            else:
                logger.warning(f"[Booker Chrono] Checkbox non trouvée")

            # Cliquer Confirmer
            confirm = await page.query_selector(
                "button:has-text('Confirmer la réservation'), "
                "button:has-text('Confirm'), "
                "button[type='submit']:not([disabled])"
            )
            if confirm:
                await confirm.click()
                logger.info(f"[Booker Chrono] Confirmer cliqué")
                await page.wait_for_timeout(5000)
            else:
                logger.warning(f"[Booker Chrono] Bouton Confirmer non trouvé")

            await browser.close()

            if reservation_success[0]:
                return {"succes": True, "message": "Réservation Chronogolf confirmée! Vérifiez votre courriel."}

            return {"succes": False, "message": "Réservation non complétée. Réessayez ou réservez manuellement.", "url_fallback": url_base}

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
