"""
scraper.py — GGG Golf via POST httpx, Chronogolf via API REST httpx.
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
        elif systeme == "chronogolf":
            return await _scrape_chronogolf_api(terrain, date, heure_debut, heure_fin, nb_joueurs)
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

    payloads_a_essayer = [
        {"date": date, "hour": heure_h, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
        {"date": date, "hour": heure_h_padded, "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
        {"date": date, "hour": heure_h, "minute": "00", "nb_players": str(nb_joueurs), "search": "Chercher les départs"},
        {"date": date, "hour": "0", "minute": "00", "nbplayers": str(nb_joueurs), "search": "Chercher les départs"},
    ]

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            get_resp = await client.get(url, headers=headers)
            logger.info(f"[GGG] GET: {get_resp.status_code}")

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

            for payload in payloads_a_essayer:
                resp = await client.post(url, data=payload, headers=headers)
                logger.info(f"[GGG] POST {resp.status_code}: {len(resp.text)} chars (hour={payload.get('hour')})")

                if resp.status_code == 200 and len(resp.text) > 5000:
                    results = _parse_gggolf_html(resp.text, terrain, date, heure_debut, heure_fin, nb_joueurs)
                    if results:
                        logger.info(f"[GGG] {len(results)} depart(s) trouves")
                        return results

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

    heures = []

    bloc_pattern = re.compile(
        r'data-confirm-url="([^"]+)"[^>]*>.*?teetimes_results-hour[^>]*>'
        r'.*?Heure:?</span>\s*(\d{1,2}:\d{2})',
        re.DOTALL | re.IGNORECASE
    )

    for m in bloc_pattern.finditer(html):
        confirm_url = m.group(1)
        h = _normalize_time(m.group(2).strip())
        if h and _in_range(h, heure_debut, heure_fin):
            heures.append({
                "heure": h,
                "places": nb_joueurs,
                "prix": "Voir site",
                "url": confirm_url,
            })

    if heures:
        return heures

    # Fallback sans URL directe
    heure_pattern = re.compile(
        r'teetimes_results-hour[^>]*>.*?Heure:?</span>\s*(\d{1,2}:\d{2})',
        re.DOTALL | re.IGNORECASE
    )
    results = []
    for m in heure_pattern.finditer(html):
        h = _normalize_time(m.group(1).strip())
        if h and _in_range(h, heure_debut, heure_fin):
            results.append({"heure": h, "places": nb_joueurs, "prix": "Voir site", "url": url_base})

    return results


# ─────────────────────────────────────────────
# Chronogolf — API REST directe avec httpx
# ─────────────────────────────────────────────

async def _scrape_chronogolf_api(terrain, date, heure_debut, heure_fin, nb_joueurs):
    course_id = terrain.get("chronogolf_course_id")
    slug = terrain.get("chronogolf_slug", terrain["id"])

    if not course_id:
        logger.warning(f"[Chrono] Pas de course_id pour {terrain['nom']} — carte grisee")
        return []

    logger.info(f"[Chrono] API: {terrain['nom']} — course_id={course_id} — {date}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-CA,fr;q=0.9",
        "Referer": f"https://www.chronogolf.ca/club/{slug}",
        "X-Requested-With": "XMLHttpRequest",
    }

    # Chronogolf supporte deux formats d'API selon la version du terrain
    urls_a_essayer = [
        # API v1 standard (course_id entier)
        (
            "https://www.chronogolf.ca/api/v1/tee_times",
            {"date": date, "course_id": str(course_id), "nb_players": str(nb_joueurs), "nb_holes": "18"},
        ),
        # API v2 ou UUID
        (
            f"https://www.chronogolf.ca/api/v1/tee_times",
            {"date": date, "course_id": str(course_id), "nb_players": str(nb_joueurs)},
        ),
        # Format alternatif avec club_id
        (
            "https://www.chronogolf.ca/api/v1/tee_times",
            {"date": date, "club_id": str(course_id), "nb_players": str(nb_joueurs), "nb_holes": "18"},
        ),
    ]

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            # GET initial pour les cookies de session
            await client.get(f"https://www.chronogolf.ca/club/{slug}", headers=headers)

            for api_url, params in urls_a_essayer:
                try:
                    resp = await client.get(api_url, params=params, headers=headers)
                    logger.info(f"[Chrono] {resp.status_code}: {len(resp.text)} chars — {resp.url}")

                    if resp.status_code == 200 and resp.text.strip():
                        data = resp.json()
                        results = _parse_chronogolf_response(data, terrain, date, heure_debut, heure_fin, nb_joueurs)
                        if results:
                            return results
                except Exception as e:
                    logger.warning(f"[Chrono] Variant echoue: {e}")
                    continue

        logger.info(f"[Chrono] Aucun depart pour {terrain['nom']}")
        return []

    except Exception as e:
        logger.error(f"[Chrono] Erreur: {e}")
        return []


def _parse_chronogolf_response(data, terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("chronogolf_slug", terrain["id"])
    url_base = f"https://www.chronogolf.ca/club/{slug}"
    results = []

    slots = data if isinstance(data, list) else (
        data.get("tee_times") or data.get("data") or data.get("slots") or []
    )

    for slot in slots:
        start = slot.get("start_time") or slot.get("time") or slot.get("hour") or ""
        # Format ISO: "2026-05-13T14:00:00" → "14:00"
        if "T" in str(start):
            start = str(start).split("T")[1][:5]
        h = _normalize_time(str(start))
        if not h or not _in_range(h, heure_debut, heure_fin):
            continue

        available = slot.get("available_spots", slot.get("spots", slot.get("nb_players_available", 4)))
        if isinstance(available, int) and available < nb_joueurs:
            continue

        prix = slot.get("green_fee", slot.get("price", slot.get("rate", "Voir site")))
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
# Generique (site_propre) — Playwright
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
