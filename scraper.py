"""
scraper.py — GGG Golf utilise un formulaire POST.
Il faut charger la page, remplir date/heure/joueurs, soumettre, puis parser.
"""

import logging
import re
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
    logger.info(f"[GGG] Chargement: {url}")

    try:
        await page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")
        date_obj = datetime.strptime(date, "%Y-%m-%d")

        # Remplir la date — GGG utilise un <select> pour Date avec options texte
        try:
            date_select = await page.query_selector("select[name='date'], select[id='date']")
            if date_select:
                # Format GGG: "Vendredi, 15 mai 2026" — on cherche par valeur ou texte partiel
                options = await date_select.query_selector_all("option")
                for opt in options:
                    val = await opt.get_attribute("value")
                    txt = await opt.inner_text()
                    if date in (val or "") or date_obj.strftime("%Y-%m-%d") in (val or ""):
                        await date_select.select_option(value=val)
                        logger.info(f"[GGG] Date sélectionnée: {val}")
                        break
                    # Chercher par jour/mois dans le texte
                    day_str = str(date_obj.day)
                    if day_str in txt and str(date_obj.year) in txt:
                        await date_select.select_option(value=val)
                        logger.info(f"[GGG] Date sélectionnée via texte: {txt}")
                        break
        except Exception as e:
            logger.warning(f"[GGG] Select date: {e}")

        # Heure de début
        try:
            heure_h = str(int(heure_debut.split(":")[0]))
            hour_sel = await page.query_selector("select[name='heure'], select[name='hour'], select[id='heure']")
            if hour_sel:
                await hour_sel.select_option(value=heure_h)
                logger.info(f"[GGG] Heure: {heure_h}")
        except Exception as e:
            logger.warning(f"[GGG] Heure: {e}")

        # Nombre de joueurs
        try:
            players_sel = await page.query_selector(
                "select[name*='player'], select[name*='joueur'], select[name*='golfer'], select[id*='player']"
            )
            if players_sel:
                await players_sel.select_option(value=str(nb_joueurs))
                logger.info(f"[GGG] Joueurs: {nb_joueurs}")
        except Exception as e:
            logger.warning(f"[GGG] Joueurs: {e}")

        # Soumettre
        try:
            submit = await page.query_selector(
                "input[type='submit'][name='sSearch'], input[type='submit'], button[type='submit']"
            )
            if submit:
                await submit.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                logger.info("[GGG] Formulaire soumis, résultats chargés")
        except Exception as e:
            logger.warning(f"[GGG] Submit: {e}")

        content = await page.content()
        logger.info(f"[GGG] HTML reçu: {len(content)} chars")

        # Logger extrait pour debug
        logger.info(f"[GGG] HTML extrait (milieu): {content[5000:8000]}")

        return _parse_gggolf_results(content, terrain, date, heure_debut, heure_fin, nb_joueurs)

    except Exception as e:
        logger.error(f"[GGG] Erreur: {e}")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)


def _parse_gggolf_results(html, terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    url_base = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"

    patterns = [
        r'<td[^>]*class="[^"]*t[- _]?time[^"]*"[^>]*>\s*(\d{1,2}:\d{2})\s*</td>',
        r'<td[^>]*class="[^"]*heure[^"]*"[^>]*>\s*(\d{1,2}:\d{2})\s*</td>',
        r'data-time="(\d{1,2}:\d{2})"',
        r'data-heure="(\d{1,2}:\d{2})"',
        r'<span[^>]*class="[^"]*time[^"]*"[^>]*>\s*(\d{1,2}:\d{2})\s*</span>',
        r'<td[^>]*>\s*(\d{1,2}h\d{2})\s*</td>',
        r'<td[^>]*>\s*(\d{1,2}:\d{2})\s*</td>',
    ]

    heures = set()
    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            raw = m.group(1).replace("h", ":")
            h = _normalize_time(raw)
            if h:
                heures.add(h)

    logger.info(f"[GGG] Heures parsées: {sorted(heures)}")

    results = []
    for heure in sorted(heures):
        if _in_range(heure, heure_debut, heure_fin):
            results.append({
                "heure": heure,
                "places": nb_joueurs,
                "prix": "Voir site",
                "url": url_base,
            })

    if not results:
        logger.warning(f"[GGG] Aucune heure dans {heure_debut}-{heure_fin} — mock")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)

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
                            "heure": h, "places": slot.get("available_spots", 4),
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
        if suffix == "PM" and h < 12: h += 12
        if suffix == "AM" and h == 12: h = 0
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
        slots.append({"heure": current.strftime("%H:%M"), "places": 4, "prix": "65$",
                      "url": terrain["url_reservation"], "_mock": True})
        current += timedelta(minutes=10)
    return slots
