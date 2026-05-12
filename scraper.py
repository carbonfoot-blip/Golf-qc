"""
scraper.py — GGG via POST httpx, Chronogolf via marketplace API httpx.
Deux versions Chronogolf:
  v1: /fr/marketplace/clubs/{club_id}/teetimes?date=...&course_id=...&affiliation_type_ids[]=...
  v2: /marketplace/v2/teetimes?start_date=...&course_ids={uuid} (chronogolf.com)
"""

import logging
import re
import json
import httpx
from datetime import datetime, timedelta
from typing import Optional
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)
TIMEOUT = 30_000
HTTP_TIMEOUT = 20


async def get_available_tee_times(terrain, date, heure_debut, heure_fin, nb_joueurs):
    systeme = terrain.get("systeme", "site_propre")
    try:
        if systeme == "gggolf":
            return await _scrape_gggolf_post(terrain, date, heure_debut, heure_fin, nb_joueurs)
        elif systeme == "chronogolf":
            return await _scrape_chronogolf(terrain, date, heure_debut, heure_fin, nb_joueurs)
        else:
            return await _scrape_generic_playwright(terrain, date, heure_debut, heure_fin, nb_joueurs)
    except Exception as e:
        logger.error(f"Erreur scraping {terrain['nom']}: {e}")
        return []


# ─────────────────────────────────────────────
# GGG Golf — POST direct httpx
# ─────────────────────────────────────────────

async def _scrape_gggolf_post(terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    url = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"
    logger.info(f"[GGG] POST: {terrain['nom']} — {date}")

    heure_h = str(int(heure_debut.split(":")[0]))
    heure_h_padded = heure_h.zfill(2)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": url,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
        "Origin": "https://secure.gggolf.ca",
    }

    payloads = [
        {"date": date, "hour": heure_h, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
        {"date": date, "hour": heure_h_padded, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
        {"date": date, "hour": heure_h, "minute": "00", "nb_players": str(nb_joueurs), "search": "Chercher les départs"},
        {"date": date, "hour": "0", "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
    ]

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            get_resp = await client.get(url, headers=headers)

            ggg_options = _extract_ggg_options(get_resp.text)
            if ggg_options:
                cal_min = ggg_options.get("calendarMin")
                cal_max = ggg_options.get("calendarMax")
                logger.info(f"[GGG] fenetre: {cal_min} -> {cal_max}")
                if cal_min and cal_max:
                    try:
                        target = datetime.strptime(date, "%Y-%m-%d").date()
                        if not (datetime.strptime(cal_min, "%Y-%m-%d").date() <= target
                                <= datetime.strptime(cal_max, "%Y-%m-%d").date()):
                            logger.info(f"[GGG] Hors fenetre")
                            return []
                    except Exception:
                        pass

            resp = None
            for payload in payloads:
                resp = await client.post(url, data=payload, headers=headers)
                logger.info(f"[GGG] POST {resp.status_code}: {len(resp.text)} chars (hour={payload.get('hour')})")
                if resp.status_code == 200 and len(resp.text) > 5000:
                    results = _parse_gggolf_html(resp.text, terrain, date, heure_debut, heure_fin, nb_joueurs)
                    if results:
                        logger.info(f"[GGG] {len(results)} depart(s)")
                        return results

            # Logger extrait HTML pour debug
            if resp and len(resp.text) > 5000:
                html = resp.text
                # Chercher autogrid ou teetimes dans le HTML
                for kw in ["autogrid", "teetimes_results", "data-colno", "agCol1"]:
                    idx = html.lower().find(kw.lower())
                    if idx > 0:
                        logger.info(f"[GGG] Debug '{kw}' trouve a {idx}: {html[max(0,idx-50):idx+200]}")
                        break
                else:
                    logger.info(f"[GGG] HTML[2000:3500]: {html[2000:3500]}")
            logger.info(f"[GGG] Aucun depart pour {terrain['nom']}")
            return []
    except Exception as e:
        logger.error(f"[GGG] Erreur: {e}")
        return []


def _extract_ggg_options(html):
    m = re.search(r'var options\s*=\s*(\{[^;]+\})', html)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return {}


def _parse_gggolf_html(html, terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    url_base = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"

    # ── Format 1 : Beloeil/standard — teetimes_results-hour avec data-confirm-url ──
    heures = []
    bloc_pattern = re.compile(
        r'data-confirm-url="([^"]+)"[^>]*>.*?teetimes_results-hour[^>]*>'
        r'.*?Heure:?</span>\s*(\d{1,2}:\d{2})',
        re.DOTALL | re.IGNORECASE
    )
    for m in bloc_pattern.finditer(html):
        h = _normalize_time(m.group(2).strip())
        if h and _in_range(h, heure_debut, heure_fin):
            heures.append({"heure": h, "places": nb_joueurs, "prix": "Voir site", "url": m.group(1)})
    if heures:
        logger.info(f"[GGG] Format 1 (teetimes_results-hour): {len(heures)} departs")
        return heures

    # ── Format 2 : Madeleine/autogrid — tableau avec data-colno="1" pour l'heure ──
    # Structure: <tr class="autogridEven/Odd"><td data-colno="0">...</td><td data-colno="1">10:08</td>...
    autogrid_pattern = re.compile(
        r'<tr[^>]*class="[^"]*autogrid[^"]*"[^>]*>.*?'
        r'<td[^>]*data-colno="1"[^>]*>\s*(\d{1,2}:\d{2})\s*</td>',
        re.DOTALL | re.IGNORECASE
    )
    results_autogrid = []
    for m in autogrid_pattern.finditer(html):
        h = _normalize_time(m.group(1).strip())
        if h and _in_range(h, heure_debut, heure_fin):
            results_autogrid.append({"heure": h, "places": nb_joueurs, "prix": "Voir site", "url": url_base})
    if results_autogrid:
        logger.info(f"[GGG] Format 2 (autogrid): {len(results_autogrid)} departs")
        return results_autogrid

    # ── Format 3 : fallback teetimes_results-hour sans data-confirm-url ──
    heure_pattern = re.compile(
        r'teetimes_results-hour[^>]*>.*?Heure:?</span>\s*(\d{1,2}:\d{2})',
        re.DOTALL | re.IGNORECASE
    )
    results = []
    for m in heure_pattern.finditer(html):
        h = _normalize_time(m.group(1).strip())
        if h and _in_range(h, heure_debut, heure_fin):
            results.append({"heure": h, "places": nb_joueurs, "prix": "Voir site", "url": url_base})
    if results:
        logger.info(f"[GGG] Format 3 (teetimes_results-hour fallback): {len(results)} departs")
    return results


# ─────────────────────────────────────────────
# Chronogolf — marketplace API httpx
# ─────────────────────────────────────────────

async def _scrape_chronogolf(terrain, date, heure_debut, heure_fin, nb_joueurs):
    api_version = terrain.get("chronogolf_api_version", "v1")

    if api_version == "v2":
        return await _scrape_chronogolf_v2(terrain, date, heure_debut, heure_fin, nb_joueurs)
    else:
        return await _scrape_chronogolf_v1(terrain, date, heure_debut, heure_fin, nb_joueurs)


async def _scrape_chronogolf_v1(terrain, date, heure_debut, heure_fin, nb_joueurs):
    """
    URL: /fr/marketplace/clubs/{club_id}/teetimes
    Params: date, course_id, affiliation_type_ids[], nb_holes
    """
    club_id = terrain.get("chronogolf_club_id")
    course_id = terrain.get("chronogolf_course_id")
    affiliation_id = terrain.get("chronogolf_affiliation_id")
    slug = terrain.get("chronogolf_slug", terrain["id"])

    if not club_id or not course_id:
        logger.warning(f"[Chrono v1] IDs manquants pour {terrain['nom']}")
        return []

    logger.info(f"[Chrono v1] {terrain['nom']} — club={club_id} course={course_id} — {date}")

    url = f"https://www.chronogolf.ca/fr/marketplace/clubs/{club_id}/teetimes"
    params = {
        "date": date,
        "course_id": str(course_id),
        "nb_holes": "18",
        "nb_players": str(nb_joueurs),
    }
    # Ajouter affiliation_type_ids si disponible
    if affiliation_id:
        params["affiliation_type_ids[]"] = str(affiliation_id)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-CA,fr;q=0.9",
        "Referer": f"https://www.chronogolf.ca/club/{slug}",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            # Cookie de session
            await client.get(f"https://www.chronogolf.ca/club/{slug}", headers=headers)

            resp = await client.get(url, params=params, headers=headers)
            logger.info(f"[Chrono v1] {resp.status_code}: {len(resp.text)} chars")

            if resp.status_code == 200:
                data = resp.json()
                logger.info(f"[Chrono v1] JSON recu: {str(data)[:300]}")
                return _parse_chronogolf_teetimes(data, terrain, heure_debut, heure_fin, nb_joueurs)

            logger.warning(f"[Chrono v1] Status {resp.status_code} pour {terrain['nom']}")
            return []

    except Exception as e:
        logger.error(f"[Chrono v1] Erreur {terrain['nom']}: {e}")
        return []


async def _scrape_chronogolf_v2(terrain, date, heure_debut, heure_fin, nb_joueurs):
    """
    URL: /marketplace/v2/teetimes (chronogolf.com)
    Params: start_date, course_ids (UUID), holes, page
    """
    course_id = terrain.get("chronogolf_course_id")
    slug = terrain.get("chronogolf_slug", terrain["id"])

    if not course_id:
        logger.warning(f"[Chrono v2] course_id manquant pour {terrain['nom']}")
        return []

    logger.info(f"[Chrono v2] {terrain['nom']} — course_id={course_id} — {date}")

    url = "https://www.chronogolf.com/marketplace/v2/teetimes"
    params = {
        "start_date": date,
        "course_ids": str(course_id),
        "holes": "9,18",
        "page": "1",
        "nb_players": str(nb_joueurs),
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-CA,fr;q=0.9",
        "Referer": f"https://www.chronogolf.com/club/{slug}",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            await client.get(f"https://www.chronogolf.com/club/{slug}", headers=headers)

            resp = await client.get(url, params=params, headers=headers)
            logger.info(f"[Chrono v2] {resp.status_code}: {len(resp.text)} chars")

            if resp.status_code == 200:
                data = resp.json()
                logger.info(f"[Chrono v2] JSON recu: {str(data)[:300]}")
                return _parse_chronogolf_teetimes(data, terrain, heure_debut, heure_fin, nb_joueurs)

            logger.warning(f"[Chrono v2] Status {resp.status_code} pour {terrain['nom']}")
            return []

    except Exception as e:
        logger.error(f"[Chrono v2] Erreur {terrain['nom']}: {e}")
        return []


def _parse_chronogolf_teetimes(data, terrain, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("chronogolf_slug", terrain["id"])
    url_base = f"https://www.chronogolf.ca/club/{slug}"
    results = []

    # Chronogolf peut retourner plusieurs structures
    slots = (
        data if isinstance(data, list) else
        data.get("tee_times") or data.get("data") or
        data.get("slots") or data.get("results") or []
    )

    for slot in slots:
        # Extraire l'heure — format ISO ou HH:MM
        start = (slot.get("start_time") or slot.get("time") or
                 slot.get("hour") or slot.get("tee_time") or "")
        if "T" in str(start):
            start = str(start).split("T")[1][:5]
        h = _normalize_time(str(start))
        if not h or not _in_range(h, heure_debut, heure_fin):
            continue

        # Places disponibles
        available = (slot.get("available_spots") or slot.get("spots") or
                    slot.get("nb_players_available") or slot.get("availability") or 4)
        if isinstance(available, int) and available < nb_joueurs:
            continue

        # Prix
        prix = slot.get("green_fee") or slot.get("price") or slot.get("rate") or "Voir site"
        if isinstance(prix, (int, float)):
            prix = f"{prix:.0f}$"

        results.append({
            "heure": h,
            "places": available if isinstance(available, int) else 4,
            "prix": str(prix),
            "url": url_base,
        })

    logger.info(f"[Chrono] {len(results)} depart(s) pour {terrain['nom']}")
    return results


# ─────────────────────────────────────────────
# Generique — Playwright
# ─────────────────────────────────────────────

async def _scrape_generic_playwright(terrain, date, heure_debut, heure_fin, nb_joueurs):
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await (await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                locale="fr-CA",
            )).new_page()
            await page.goto(terrain["url_scrape"], timeout=TIMEOUT, wait_until="domcontentloaded")
            content = await page.content()
            await browser.close()
            results = []
            for h_raw in set(re.findall(r'\b(\d{1,2}:\d{2})\b', content)):
                h = _normalize_time(h_raw)
                if h and _in_range(h, heure_debut, heure_fin):
                    results.append({"heure": h, "places": 4, "prix": "Voir site", "url": terrain["url_reservation"]})
            return sorted(results, key=lambda x: x["heure"])
    except Exception as e:
        logger.error(f"[Generic] {e}")
        return []


# ─────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────

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
