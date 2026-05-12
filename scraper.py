"""
scraper.py — GGG Golf charge les départs via AJAX après soumission du formulaire.
On intercepte les réponses réseau pour capturer les données JSON.
"""

import logging
import re
import json
from datetime import datetime, timedelta
from typing import Optional
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

logger = logging.getLogger(__name__)
TIMEOUT = 30_000


async def get_available_tee_times(terrain, date, heure_debut, heure_fin, nb_joueurs):
    systeme = terrain.get("systeme", "site_propre")
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
            )
            page = await context.new_page()
            if systeme == "gggolf":
                results = await _scrape_gggolf(page, terrain, date, heure_debut, heure_fin, nb_joueurs)
            elif systeme == "chronogolf":
                results = await _scrape_chronogolf(page, terrain, date, heure_debut, heure_fin, nb_joueurs)
            else:
                results = await _scrape_generic(page, terrain, date, heure_debut, heure_fin, nb_joueurs)
            await browser.close()
            return results
    except Exception as e:
        logger.error(f"Erreur scraping {terrain['nom']}: {e}")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)


async def _scrape_gggolf(page, terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    url = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"
    logger.info(f"[GGG] {terrain['nom']} — {date}")

    captured_responses = []
    captured_json = []

    async def handle_response(response):
        resp_url = response.url
        if any(k in resp_url for k in ["teetimes", "teetime", "req=", "task=", "ajax", "json"]):
            try:
                body = await response.body()
                text = body.decode("utf-8", errors="ignore")
                captured_responses.append({
                    "url": resp_url,
                    "status": response.status,
                    "body": text[:2000],
                })
                try:
                    data = json.loads(text)
                    captured_json.append(data)
                    logger.info(f"[GGG] JSON capturé: {str(data)[:300]}")
                except Exception:
                    pass
            except Exception:
                pass

    page.on("response", handle_response)

    try:
        await page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")

        # Lire le JSON de config GGG (calendarMin, calendarMax, hoursAM, hoursPM)
        content_initial = await page.content()
        options_match = re.search(r'var options\s*=\s*(\{[^;]+\})', content_initial)
        ggg_options = {}
        if options_match:
            try:
                ggg_options = json.loads(options_match.group(1))
                logger.info(
                    f"[GGG] calendarMin={ggg_options.get('calendarMin')}, "
                    f"calendarMax={ggg_options.get('calendarMax')}, "
                    f"hoursAM={ggg_options.get('hoursAM')}, "
                    f"hoursPM={ggg_options.get('hoursPM')}"
                )
            except Exception as e:
                logger.warning(f"[GGG] Parse options: {e}")

        # Vérifier la fenêtre de réservation
        cal_min = ggg_options.get("calendarMin")
        cal_max = ggg_options.get("calendarMax")
        if cal_min and cal_max:
            try:
                date_obj = datetime.strptime(date, "%Y-%m-%d").date()
                min_date = datetime.strptime(cal_min, "%Y-%m-%d").date()
                max_date = datetime.strptime(cal_max, "%Y-%m-%d").date()
                if not (min_date <= date_obj <= max_date):
                    logger.info(f"[GGG] Date {date} hors fenêtre [{cal_min} -> {cal_max}]")
                    return []
            except Exception:
                pass

        date_obj = datetime.strptime(date, "%Y-%m-%d")

        # Remplir la date
        try:
            date_input = await page.query_selector(
                "input.jquery_ui_datepicker, input[name='date'], input[id='date'], "
                "input[type='text'][name*='date']"
            )
            if date_input:
                await date_input.fill(date)
                await date_input.dispatch_event("change")
                logger.info(f"[GGG] Date: {date}")
        except Exception as e:
            logger.warning(f"[GGG] Date: {e}")

        # Remplir l'heure
        try:
            heure_h = str(int(heure_debut.split(":")[0]))
            hour_sel = await page.query_selector(
                "select[name='heure'], select[name='hour'], select[id='heure'], "
                "select[name*='hour'], select[name*='heure']"
            )
            if hour_sel:
                await hour_sel.select_option(value=heure_h)
                logger.info(f"[GGG] Heure: {heure_h}h")
        except Exception as e:
            logger.warning(f"[GGG] Heure: {e}")

        # Remplir joueurs
        try:
            players_sel = await page.query_selector(
                "select[name*='player'], select[name*='joueur'], select[name*='golfer'], "
                "select[id*='player'], select[id*='joueur']"
            )
            if players_sel:
                await players_sel.select_option(value=str(nb_joueurs))
                logger.info(f"[GGG] Joueurs: {nb_joueurs}")
        except Exception as e:
            logger.warning(f"[GGG] Joueurs: {e}")

        # Soumettre le formulaire
        try:
            submit = await page.query_selector(
                "input[name='sSearch'], input[type='submit'], button[type='submit'], "
                "button:has-text('Chercher'), input[value*='Chercher']"
            )
            if submit:
                await submit.click()
                logger.info("[GGG] Bouton Chercher clique")
                # Attendre que les resultats apparaissent
                try:
                    await page.wait_for_selector(
                        ".teetimes_results, .teetimes-results, #teetimes_results, "
                        ".teetimes_results-header-hour, table.teetimes, "
                        "[class*='teetimes_result']",
                        timeout=12000
                    )
                    logger.info("[GGG] Resultats apparus dans le DOM")
                except PwTimeout:
                    logger.warning("[GGG] Selecteur resultats non trouve — attente 5s")
                    await page.wait_for_timeout(5000)
            else:
                logger.warning("[GGG] Bouton submit non trouve")
        except Exception as e:
            logger.warning(f"[GGG] Submit: {e}")

        content_final = await page.content()
        logger.info(f"[GGG] HTML final: {len(content_final)} chars")

        # Logger la section des resultats
        idx = content_final.lower().find("teetimes_result")
        if idx > 0:
            logger.info(f"[GGG] Section resultats: {content_final[idx:idx+1000]}")
        else:
            logger.info(f"[GGG] Pas de section resultats — HTML[8000:11000]: {content_final[8000:11000]}")

        # Essayer JSON d'abord
        if captured_json:
            results = _parse_gggolf_json(captured_json, terrain, date, heure_debut, heure_fin, nb_joueurs)
            if results:
                return results

        # Sinon HTML
        results = _parse_gggolf_html_final(content_final, terrain, date, heure_debut, heure_fin, nb_joueurs)
        if results:
            return results

        # Fallback hoursAM/PM
        if ggg_options:
            return _parse_gggolf_from_options(ggg_options, terrain, date, heure_debut, heure_fin, nb_joueurs)

        return _mock_tee_times(terrain, date, heure_debut, heure_fin)

    except Exception as e:
        logger.error(f"[GGG] Erreur: {e}")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)


def _parse_gggolf_json(json_list, terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    url_base = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"
    results = []
    for data in json_list:
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = (data.get("teetimes") or data.get("data") or
                    data.get("results") or data.get("times") or [])
        for item in items:
            if not isinstance(item, dict):
                continue
            time_val = (item.get("time") or item.get("heure") or
                       item.get("start_time") or item.get("teeTime") or "")
            h = _normalize_time(str(time_val))
            if h and _in_range(h, heure_debut, heure_fin):
                results.append({
                    "heure": h,
                    "places": item.get("available", item.get("spots", nb_joueurs)),
                    "prix": str(item.get("price", item.get("prix", "Voir site"))),
                    "url": url_base,
                })
    logger.info(f"[GGG] JSON parse: {len(results)} departs")
    return results


def _parse_gggolf_html_final(html, terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    url_base = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"
    patterns = [
        r'class="[^"]*ttime[^"]*"[^>]*>\s*(\d{1,2}:\d{2})',
        r'class="[^"]*teetime[^"]*"[^>]*>\s*(\d{1,2}:\d{2})',
        r'class="[^"]*start[^"]*"[^>]*>\s*(\d{1,2}:\d{2})',
        r'class="[^"]*heure[^"]*"[^>]*>\s*(\d{1,2}:\d{2})',
        r'data-time="(\d{1,2}:\d{2})"',
        r'data-heure="(\d{1,2}:\d{2})"',
        r'"time"\s*:\s*"(\d{1,2}:\d{2})"',
        r'<td[^>]*>\s*(\d{1,2}h\d{2})\s*</td>',
        r'<td[^>]*>\s*(\d{1,2}:\d{2})\s*</td>',
        r'<li[^>]*>\s*(\d{1,2}:\d{2})\s*</li>',
    ]
    heures = set()
    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            raw = m.group(1).replace("h", ":")
            h = _normalize_time(raw)
            if h and "06:00" <= h <= "20:00":
                heures.add(h)
    logger.info(f"[GGG] HTML final heures: {sorted(heures)}")
    results = []
    for heure in sorted(heures):
        if _in_range(heure, heure_debut, heure_fin):
            results.append({"heure": heure, "places": nb_joueurs, "prix": "Voir site", "url": url_base})
    return results


def _parse_gggolf_from_options(ggg_options, terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    url_base = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"
    all_hours = ggg_options.get("hoursAM", []) + ggg_options.get("hoursPM", [])
    results = []
    for h_str in all_hours:
        heure = f"{int(h_str):02d}:00"
        if _in_range(heure, heure_debut, heure_fin):
            results.append({"heure": heure, "places": 4, "prix": "Voir site", "url": url_base, "_approx": True})
    if results:
        logger.info(f"[GGG] Fallback hoursAM/PM: {[r['heure'] for r in results]}")
    return results


async def _scrape_chronogolf(page, terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("chronogolf_slug", terrain["id"])
    captured = []

    async def handle(response):
        if any(k in response.url for k in ["tee_times", "availability", "slots"]):
            try:
                captured.append(await response.json())
            except Exception:
                pass

    page.on("response", handle)
    try:
        await page.goto(
            f"https://www.chronogolf.ca/club/{slug}?date={date}&nb_players={nb_joueurs}&lang=fr",
            timeout=TIMEOUT, wait_until="networkidle"
        )
        if captured:
            results = []
            for data in captured:
                slots = data.get("tee_times") or data.get("slots") or (data if isinstance(data, list) else [])
                for slot in slots:
                    start = slot.get("start_time") or slot.get("time") or ""
                    h = _normalize_time(str(start))
                    if h and _in_range(h, heure_debut, heure_fin):
                        results.append({
                            "heure": h,
                            "places": slot.get("available_spots", 4),
                            "prix": "Voir site",
                            "url": f"https://www.chronogolf.ca/club/{slug}?date={date}&nb_players={nb_joueurs}",
                        })
            if results:
                return results
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)
    except Exception as e:
        logger.error(f"[Chrono] {e}")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)


async def _scrape_generic(page, terrain, date, heure_debut, heure_fin, nb_joueurs):
    try:
        await page.goto(terrain["url_scrape"], timeout=TIMEOUT, wait_until="domcontentloaded")
        content = await page.content()
        results = []
        for h_raw in set(re.findall(r'\b(\d{1,2}:\d{2})\b', content)):
            h = _normalize_time(h_raw)
            if h and _in_range(h, heure_debut, heure_fin):
                results.append({"heure": h, "places": 4, "prix": "Voir site", "url": terrain["url_reservation"]})
        return sorted(results, key=lambda x: x["heure"]) or _mock_tee_times(terrain, date, heure_debut, heure_fin)
    except Exception as e:
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)


def _normalize_time(text):
    text = text.strip()
    m = re.match(r'^(\d{1,2}):(\d{2})(\s*[AaPp][Mm])?$', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if mn > 59 or h > 23:
            return None
        suffix = (m.group(3) or "").strip().upper()
        if suffix == "PM" and h < 12:
            h += 12
        if suffix == "AM" and h == 12:
            h = 0
        return f"{h:02d}:{mn:02d}"
    return None


def _in_range(heure, debut, fin):
    try:
        h = datetime.strptime(heure, "%H:%M").time()
        d = datetime.strptime(debut, "%H:%M").time()
        f = datetime.strptime(fin, "%H:%M").time()
        return d <= h < f
    except ValueError:
        return False


def _mock_tee_times(terrain, date, heure_debut, heure_fin):
    import os
    if os.getenv("MOCK_SCRAPER", "true").lower() != "true":
        return []
    logger.info(f"[MOCK] {terrain['nom']}")
    try:
        debut = datetime.strptime(heure_debut, "%H:%M")
        fin = datetime.strptime(heure_fin, "%H:%M")
    except ValueError:
        return []
    slots, current = [], debut
    while current < fin:
        slots.append({
            "heure": current.strftime("%H:%M"),
            "places": 4,
            "prix": "65$",
            "url": terrain["url_reservation"],
            "_mock": True,
        })
        current += timedelta(minutes=10)
    return slots
