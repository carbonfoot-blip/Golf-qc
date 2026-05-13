"""
booker.py — GGG via httpx+Playwright, Chronogolf via Playwright+2captcha.
"""

import logging
import re
import os
import httpx
import asyncio
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

logger = logging.getLogger(__name__)
TIMEOUT = 30_000
HTTP_TIMEOUT = 20
TWOCAPTCHA_KEY = os.getenv("TWOCAPTCHA_API_KEY", "")


async def reserver_depart(
    terrain: dict, confirm_url: str, username: str, password: str,
    date: str = "", heure: str = "", nb_joueurs: int = 2
) -> dict:
    systeme = terrain.get("systeme", "site_propre")
    if systeme == "gggolf":
        return await _reserver_gggolf(terrain, username, password, date, heure, nb_joueurs)
    elif systeme == "chronogolf":
        return await _reserver_chronogolf(terrain, username, password, date, heure, nb_joueurs)
    return {"succes": False, "message": "Systeme non supporte."}


# ─────────────────────────────────────────────
# 2captcha — résoudre reCAPTCHA v2/v3
# ─────────────────────────────────────────────

async def _solve_recaptcha(site_key: str, page_url: str) -> str:
    return await _solve_recaptcha_v3(site_key, page_url, "login")


async def _solve_recaptcha_v3(site_key: str, page_url: str, action: str = "login") -> str:
    """Résout reCAPTCHA v3 via 2captcha."""
    if not TWOCAPTCHA_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Essayer v3 d'abord
            submit = await client.post(
                "https://2captcha.com/in.php",
                data={
                    "key": TWOCAPTCHA_KEY,
                    "method": "userrecaptcha",
                    "version": "v3",
                    "googlekey": site_key,
                    "pageurl": page_url,
                    "action": action,
                    "min_score": "0.3",
                    "json": 1,
                }
            )
            submit_data = submit.json()
            logger.info(f"[2captcha] Submit v3: {submit_data}")

            if submit_data.get("status") != 1:
                # Fallback v2
                submit = await client.post(
                    "https://2captcha.com/in.php",
                    data={
                        "key": TWOCAPTCHA_KEY,
                        "method": "userrecaptcha",
                        "googlekey": site_key,
                        "pageurl": page_url,
                        "json": 1,
                    }
                )
                submit_data = submit.json()
                logger.info(f"[2captcha] Submit v2 fallback: {submit_data}")
                if submit_data.get("status") != 1:
                    return ""

            captcha_id = submit_data["request"]

            for attempt in range(24):
                await asyncio.sleep(5)
                result = await client.get(
                    "https://2captcha.com/res.php",
                    params={"key": TWOCAPTCHA_KEY, "action": "get", "id": captcha_id, "json": 1}
                )
                result_data = result.json()
                if result_data.get("status") == 1:
                    logger.info(f"[2captcha] Token: {result_data['request'][:30]}...")
                    return result_data["request"]
                if result_data.get("request") not in ["CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"]:
                    logger.error(f"[2captcha] Erreur: {result_data}")
                    return ""

        return ""
    except Exception as e:
        logger.error(f"[2captcha] Exception: {e}")
        return ""


# ─────────────────────────────────────────────
# GGG Golf
# ─────────────────────────────────────────────

async def _reserver_gggolf(terrain, username, password, date, heure, nb_joueurs):
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
            await browser.close()

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": teetimes_url,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-CA,fr;q=0.9",
            "Origin": "https://secure.gggolf.ca",
        }
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, cookies=cookie_dict, headers=headers) as client:
            search_html = ""
            for h_val in [heure_h, heure_h_pad]:
                payload = {"date": date, "hour": h_val, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"}
                resp = await client.post(teetimes_url, data=payload)
                logger.info(f"[Booker GGG] httpx POST {resp.status_code}: {len(resp.text)} chars")
                if resp.status_code == 200 and len(resp.text) > 18000:
                    search_html = resp.text
                    break
            confirm_url_fresh = _trouver_confirm_url_ggg(search_html, heure, slug)
            if not confirm_url_fresh:
                return {"succes": False, "message": f"Le depart de {heure} n'est plus disponible.", "url_fallback": teetimes_url}
            logger.info(f"[Booker GGG] confirm_url: {confirm_url_fresh}")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36", locale="fr-CA")
            await context.add_cookies([{"name": k, "value": v, "domain": "secure.gggolf.ca", "path": "/"} for k, v in cookie_dict.items()])
            page = await context.new_page()
            await page.goto(confirm_url_fresh, timeout=TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            confirm_content = await page.content()
            if "accepte" not in confirm_content.lower():
                await browser.close()
                return {"succes": False, "message": "Le depart n'est plus disponible.", "url_fallback": teetimes_url}
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
                return {"succes": False, "message": "Bouton confirmation introuvable.", "url_fallback": teetimes_url}
            try:
                async with page.expect_navigation(timeout=20000):
                    await confirm_btn.click()
            except Exception:
                await page.wait_for_timeout(3000)
            final_content = await page.content()
            await browser.close()
            for indicator in ["numero de reservation", "numéro de réservation", "confirmée", "merci", "thank you"]:
                if indicator.lower() in final_content.lower():
                    return {"succes": True, "message": "Réservation GGG confirmée! Vérifiez votre courriel."}
            for indicator in ["erreur", "impossible", "déjà réservé", "already"]:
                if indicator.lower() in final_content.lower():
                    return {"succes": False, "message": "Erreur — le depart est peut-être déjà pris.", "url_fallback": teetimes_url}
            logger.warning(f"[Booker GGG] Aucun indicateur. HTML[0:400]: {final_content[:400]}")
            return {"succes": True, "message": "Réservation soumise. Vérifiez votre courriel."}

    except Exception as e:
        logger.error(f"[Booker GGG] Erreur: {e}")
        return {"succes": False, "message": "Erreur technique GGG.", "url_fallback": teetimes_url}


def _trouver_confirm_url_ggg(html, heure_cible, slug):
    if not html or not heure_cible:
        return ""
    heure_norm = f"{int(heure_cible.split(':')[0]):02d}:{heure_cible.split(':')[1]}"
    bloc = re.compile(r'data-confirm-url="([^"]+)"[^>]*>.*?teetimes_results-hour[^>]*>.*?Heure:?</span>\s*(\d{1,2}:\d{2})', re.DOTALL | re.IGNORECASE)
    for m in bloc.finditer(html):
        h = f"{int(m.group(2).split(':')[0]):02d}:{m.group(2).split(':')[1]}"
        if h == heure_norm:
            return m.group(1).replace("&amp;", "&")
    row_pat = re.compile(r'<tr[^>]*class="[^"]*autogrid(?:Even|Odd)[^"]*"[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
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
            return cm.group(1).replace("&amp;", "&")
    heures = re.findall(r'data-colno="1"[^>]*>\s*(\d{1,2}:\d{2})\s*<', html)
    logger.warning(f"[Booker GGG] Heure {heure_cible} non trouvee. Dispo: {heures[:8]}")
    return ""


# ─────────────────────────────────────────────
# Chronogolf — Playwright + 2captcha
# ─────────────────────────────────────────────

async def _reserver_chronogolf(terrain, username, password, date, heure, nb_joueurs):
    slug = terrain.get("chronogolf_slug", terrain["id"])
    club_id = terrain.get("chronogolf_club_id")
    course_id = terrain.get("chronogolf_course_id")
    affiliation_id = terrain.get("chronogolf_affiliation_id", 98)
    url_prefix = terrain.get("chronogolf_url_prefix", "fr/marketplace")
    url_base = f"https://www.chronogolf.ca/club/{slug}"
    login_url = "https://www.chronogolf.com/login"

    logger.info(f"[Booker Chrono] Debut: {terrain['nom']} — {date} {heure}")

    if not club_id or not course_id:
        return {"succes": False, "message": "Configuration manquante.", "url_fallback": url_base}

    heure_norm = f"{int(heure.split(':')[0]):02d}:{heure.split(':')[1]}" if heure and ":" in heure else ""

    try:
        import json as _json

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
                viewport={"width": 1280, "height": 800},
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = await context.new_page()

            # ── Login avec 2captcha ───────────────────────────
            await page.goto(login_url, timeout=TIMEOUT, wait_until="networkidle")
            await page.wait_for_timeout(2000)

            # Chercher reCAPTCHA v2 ET v3 (v3 est invisible, dans le JS)
            site_key = await page.evaluate("""
                document.querySelector('[data-sitekey]')?.dataset?.sitekey ||
                document.querySelector('.g-recaptcha')?.dataset?.sitekey ||
                (function() {
                    var scripts = Array.from(document.querySelectorAll('script'));
                    for (var s of scripts) {
                        var m = (s.src || '').match(/render=([^&]+)/);
                        if (m) return m[1];
                    }
                    return '';
                })() || ''
            """)
            logger.info(f"[Booker Chrono] reCAPTCHA site_key: '{site_key[:40] if site_key else 'non trouve'}'")

            # Si pas trouvé via DOM, chercher dans le source HTML
            if not site_key:
                page_html = await page.content()
                import re as _re2
                m = _re2.search(r"'([0-9A-Za-z_-]{40})'", page_html)
                if m:
                    site_key = m.group(1)
                    logger.info(f"[Booker Chrono] site_key depuis HTML: {site_key[:20]}")

            email_field = await page.query_selector("input[name='email'], input[id='sessionEmail']")
            pwd_field   = await page.query_selector("input[name='password'], input[id='sessionPassword']")

            if not email_field or not pwd_field:
                await browser.close()
                return {"succes": False, "message": "Page de connexion Chronogolf introuvable."}

            await email_field.click()
            await email_field.type(username, delay=30)
            await page.wait_for_timeout(300)
            await pwd_field.click()
            await pwd_field.type(password, delay=30)
            await page.wait_for_timeout(500)

            # Résoudre reCAPTCHA v3 (invisible) via 2captcha
            if TWOCAPTCHA_KEY:
                # Utiliser le site_key trouvé ou celui connu de Chronogolf
                captcha_site_key = site_key or "6LdSsN0UAAAAAMJmBPcmz9eRm4bpjYUBWNpgfBni"
                logger.info(f"[Booker Chrono] Résolution reCAPTCHA v3 via 2captcha (key={captcha_site_key[:20]})")
                captcha_token = await _solve_recaptcha_v3(captcha_site_key, login_url, "login")
                if captcha_token:
                    injected = await page.evaluate(f"""
                        (function() {{
                            // Injecter dans le champ standard
                            var el = document.getElementById('g-recaptcha-response');
                            if (el) {{ el.value = '{captcha_token}'; el.style.display = 'block'; }}

                            // Trouver et appeler le callback Angular reCAPTCHA
                            var called = false;
                            if (window.grecaptcha) {{
                                // Override getResponse pour retourner notre token
                                var orig = window.grecaptcha.getResponse;
                                window.grecaptcha.getResponse = function() {{ return '{captcha_token}'; }};
                                called = true;
                            }}

                            // Chercher le callback dans les widgets reCAPTCHA
                            if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {{
                                var clients = window.___grecaptcha_cfg.clients;
                                for (var key in clients) {{
                                    var client = clients[key];
                                    for (var k in client) {{
                                        if (client[k] && typeof client[k].callback === 'function') {{
                                            try {{ client[k].callback('{captcha_token}'); called = true; }} catch(e) {{}}
                                        }}
                                        if (client[k] && client[k].aa && typeof client[k].aa.callback === 'function') {{
                                            try {{ client[k].aa.callback('{captcha_token}'); called = true; }} catch(e) {{}}
                                        }}
                                    }}
                                }}
                            }}
                            return called;
                        }})()
                    """)
                    logger.info(f"[Booker Chrono] Token injecte, callback appele: {{injected}}")
                    await page.wait_for_timeout(1000)
                else:
                    logger.warning(f"[Booker Chrono] Pas de token — tentative sans captcha")
            else:
                logger.info(f"[Booker Chrono] Pas de clé 2captcha configurée")

            # Soumettre le formulaire
            login_btn = await page.query_selector("button:has-text('Log in'), button[type='submit']")
            if login_btn:
                await login_btn.click()
            else:
                await pwd_field.press("Enter")

            try:
                await page.wait_for_function("window.location.href.indexOf('/login') === -1", timeout=20000)
            except Exception:
                await page.wait_for_timeout(5000)

            logger.info(f"[Booker Chrono] Apres login: {page.url}")
            if "login" in page.url.lower():
                await browser.close()
                return {"succes": False, "message": "Login Chronogolf échoué (reCAPTCHA ou identifiants)."}

            # ── Page booking ──────────────────────────────────
            booking_url = f"https://www.chronogolf.ca/club/{slug}/booking/?source=chronogolf&medium=profile"
            await page.goto(booking_url, timeout=TIMEOUT, wait_until="networkidle")
            await page.wait_for_timeout(4000)
            logger.info(f"[Booker Chrono] Booking: {page.url}")

            csrf_token = await page.evaluate(
                "angular?.element(document)?.injector()?.get('$http')?.defaults?.headers?.common?.['X-CSRF-Token'] || ''"
            )
            logger.info(f"[Booker Chrono] CSRF: {'oui' if csrf_token else 'non'}")

            # ── GET teetimes via fetch JS ─────────────────────
            teetimes_url = f"https://www.chronogolf.ca/{url_prefix}/clubs/{club_id}/teetimes"
            params_qs = f"date={date}&course_id={course_id}&nb_holes=18&nb_players={nb_joueurs}&affiliation_type_ids[]={affiliation_id}"
            tt_result = await page.evaluate(f"""
                (async () => {{
                    const r = await fetch('{teetimes_url}?{params_qs}', {{
                        credentials: 'include',
                        headers: {{'Accept': 'application/json'}}
                    }});
                    const d = await r.json();
                    return JSON.stringify({{status: r.status, data: d}});
                }})()
            """)
            tt_data = _json.loads(tt_result) if tt_result else {}
            slots = tt_data.get("data", [])
            if isinstance(slots, dict):
                slots = slots.get("tee_times") or slots.get("teetimes") or []

            logger.info(f"[Booker Chrono] {len(slots)} departs, cherche {heure_norm}")
            teetime_id = None
            for slot in slots:
                start = slot.get("start_time") or slot.get("time") or ""
                if "T" in str(start):
                    start = str(start).split("T")[1][:5]
                h = f"{int(start.split(':')[0]):02d}:{start.split(':')[1]}" if ":" in str(start) else ""
                if h == heure_norm:
                    teetime_id = slot.get("id")
                    logger.info(f"[Booker Chrono] teetime_id: {teetime_id}")
                    break

            if not teetime_id:
                await browser.close()
                return {"succes": False, "message": f"Le depart de {heure} n'est plus disponible.", "url_fallback": url_base}

            # ── POST reservation via fetch JS ─────────────────
            rounds = [{"affiliation_type_id": affiliation_id, "guest": None, "state": "reserved"} for _ in range(nb_joueurs)]
            payload_str = _json.dumps({
                "reservation": {
                    "club_id": club_id, "teetime_id": teetime_id,
                    "state": "confirmed", "holes": 18,
                    "medium": "profile", "source": "chronogolf",
                    "booking_engine": 1, "made_online": True,
                    "rounds_attributes": rounds,
                }
            }).replace("'", "\\'")

            res_result = await page.evaluate(f"""
                (async () => {{
                    const r = await fetch('https://www.chronogolf.ca/marketplace/reservations', {{
                        method: 'POST',
                        credentials: 'include',
                        headers: {{
                            'Accept': 'application/json, text/plain, */*',
                            'Content-Type': 'application/json',
                            'X-CSRF-Token': '{csrf_token}',
                            'X-Requested-With': 'XMLHttpRequest'
                        }},
                        body: '{payload_str}'
                    }});
                    const text = await r.text();
                    return JSON.stringify({{status: r.status, body: text.substring(0, 500)}});
                }})()
            """)

            result = _json.loads(res_result) if res_result else {}
            status = result.get("status", 0)
            body = result.get("body", "")
            logger.info(f"[Booker Chrono] POST: {status} — {body[:200]}")
            await browser.close()

            if status in [200, 201]:
                # Vérifier que c'est du JSON avec un ID (pas du HTML)
                if body.strip().startswith("{") and '"id"' in body:
                    return {"succes": True, "message": "Réservation Chronogolf confirmée! Vérifiez votre courriel."}
                elif body.strip().startswith("<"):
                    return {"succes": False, "message": "Session insuffisante.", "url_fallback": url_base}
                return {"succes": True, "message": "Réservation Chronogolf confirmée! Vérifiez votre courriel."}

            return {"succes": False, "message": f"Erreur Chronogolf ({status}).", "url_fallback": url_base}

    except Exception as e:
        logger.error(f"[Booker Chrono] Erreur: {e}")
        return {"succes": False, "message": "Erreur technique Chronogolf.", "url_fallback": url_base}
