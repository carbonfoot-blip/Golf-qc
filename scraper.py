"""
scraper.py — GGG Golf POST direct avec httpx.
Pattern HTML confirme: class="teetimes_results-hour col-sm-1"
"""

import logging
import re
import json
import httpx
from datetime import datetime, timedelta
from typing import Optional
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

logger = logging.getLogger(__name__)
TIMEOUT = 30_000
HTTP_TIMEOUT = 20


async def get_available_tee_times(terrain, date, heure_debut, heure_fin, nb_joueurs):
    systeme = terrain.get("systeme", "site_propre")
    try:
        if systeme == "gggolf":
            return await _scrape_gggolf_post(terrain, date, heure_debut, heure_fin, nb_joueurs)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
            )
            page = await context.new_page()
            if systeme == "chronogolf":
                results = await _scrape_chronogolf(page, terrain, date, heure_debut, heure_fin, nb_joueurs)
            else:
                results = await _scrape_generic(page, terrain, date, heure_debut, heure_fin, nb_joueurs)
            await browser.close()
            return results
    except Exception as e:
        logger.error(f"Erreur scraping {terrain['nom']}: {e}")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)


async def _scrape_gggolf_post(terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    url = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"
    logger.info(f"[GGG] POST: {terrain['nom']} — {date}")

    heure_h = str(int(heure_debut.split(":")[0]))

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": url,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
        "Origin": "https://secure.gggolf.ca",
    }

    # Essayer plusieurs formats d'heure (GGG varie selon le terrain)
    heure_h_padded = heure_h.zfill(2)  # "07", "12" etc.

    payloads_a_essayer = [
        # Format observe sur Beloeil (fonctionne)
        {"date": date, "hour": heure_h, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
        # Format avec heure paddee
        {"date": date, "hour": heure_h_padded, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
        # Format avec nb_players au lieu de nbplayers
        {"date": date, "hour": heure_h, "minute": "00", "nb_players": str(nb_joueurs), "search": "Chercher les départs"},
        # Format avec heure de debut = 0 (toutes les heures)
        {"date": date, "hour": "0", "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
    ]

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            # GET initial pour les cookies de session
            get_resp = await client.get(url, headers=headers)
            logger.info(f"[GGG] GET: {get_resp.status_code}")

            # Verifier la fenetre de reservation
            ggg_options = _extract_ggg_options(get_resp.text)
            if ggg_options:
                cal_min = ggg_options.get("calendarMin")
                cal_max = ggg_options.get("calendarMax")
                logger.info(f"[GGG] fenetre: {cal_min} -> {cal_max}")
                if cal_min and cal_max:
                    try:
                        target = datetime.strptime(date, "%Y-%m-%d").date()
                        min_d = datetime.strptime(cal_min, "%Y-%m-%d").date()
                        max_d = datetime.strptime(cal_max, "%Y-%m-%d").date()
                        if not (min_d <= target <= max_d):
                            logger.info(f"[GGG] Hors fenetre")
                            return []
                    except Exception:
                        pass

            # Essayer les differents payloads
            for payload in payloads_a_essayer:
                resp = await client.post(url, data=payload, headers=headers)
                logger.info(f"[GGG] POST {resp.status_code}: {len(resp.text)} chars (hour={payload.get('hour')})")

                if resp.status_code == 200 and len(resp.text) > 5000:
                    results = _parse_gggolf_html(resp.text, terrain, date, heure_debut, heure_fin, nb_joueurs)
                    if results:
                        logger.info(f"[GGG] {len(results)} depart(s) avec payload hour={payload.get('hour')}")
                        return results

            # Aucun depart trouve — retourner liste vide (carte grisee dans l'UI)
            logger.info(f"[GGG] Aucun depart trouve pour {terrain['nom']} — carte grisee")
            return []

    except Exception as e:
        logger.error(f"[GGG] Erreur: {e}")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)


def _extract_ggg_options(html):
    m = re.search(r'var options\s*=\s*(\{[^;]+\})', html)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return {}


def _parse_gggolf_html(html, terrain, date, heure_debut, heure_fin, nb_joueurs):
    """
    Structure HTML confirmee par inspection:
    <div class="teetimes_results-hour col-sm-1">
        <span class="visible-xs">Heure:</span> 15:02
    </div>

    URL de reservation dans data-confirm-url:
    data-confirm-url="...&id=XXXXX&nbholes=18&nbplayers=2"
    """
    slug = terrain.get("ggg_slug", terrain["id"])
    url_base = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"

    heures = []

    # Pattern principal confirme — extraire heure ET url de confirmation par bloc
    # Chaque depart est dans un div avec data-confirm-url et contient teetimes_results-hour
    bloc_pattern = re.compile(
        r'data-confirm-url="([^"]+)"[^>]*>.*?teetimes_results-hour[^>]*>'
        r'.*?Heure:?</span>\s*(\d{1,2}:\d{2})',
        re.DOTALL | re.IGNORECASE
    )

    for m in bloc_pattern.finditer(html):
        confirm_url = m.group(1)
        heure_raw = m.group(2).strip()
        h = _normalize_time(heure_raw)
        if h and _in_range(h, heure_debut, heure_fin):
            heures.append({
                "heure": h,
                "places": nb_joueurs,
                "prix": "Voir site",
                "url": confirm_url,  # URL directe de reservation pour ce creneau
            })

    if heures:
        logger.info(f"[GGG] {len(heures)} depart(s) avec URLs directes")
        return heures

    # Fallback — juste extraire les heures sans URL specifique
    heure_pattern = re.compile(
        r'teetimes_results-hour[^>]*>.*?Heure:?</span>\s*(\d{1,2}:\d{2})',
        re.DOTALL | re.IGNORECASE
    )

    results = []
    for m in heure_pattern.finditer(html):
        h = _normalize_time(m.group(1).strip())
        if h and _in_range(h, heure_debut, heure_fin):
            results.append({
                "heure": h,
                "places": nb_joueurs,
                "prix": "Voir site",
                "url": url_base,
            })

    logger.info(f"[GGG] {len(results)} depart(s) trouves (fallback sans URL directe)")
    return results


def _parse_gggolf_from_options(ggg_options, terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    url_base = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"
    all_hours = ggg_options.get("hoursAM", []) + ggg_options.get("hoursPM", [])
    results = []
    for h_str in all_hours:
        heure = f"{int(h_str):02d}:00"
        if _in_range(heure, heure_debut, heure_fin):
            results.append({
                "heure": heure, "places": 4, "prix": "Voir site",
                "url": url_base, "_approx": True,
            })
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
