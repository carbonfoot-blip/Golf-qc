"""
scraper.py — GGG Golf utilise un POST server-side.
On envoie directement le POST avec httpx (plus rapide que Playwright pour ce cas).
Les resultats sont dans le HTML de la reponse POST directe.
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


# ─────────────────────────────────────────────
# GGG Golf — POST direct avec httpx
# ─────────────────────────────────────────────

async def _scrape_gggolf_post(terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    url = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"
    logger.info(f"[GGG] POST direct: {terrain['nom']} — {date}")

    date_obj = datetime.strptime(date, "%Y-%m-%d")
    heure_h = str(int(heure_debut.split(":")[0]))

    # Payload POST identique a ce que le navigateur envoie
    payload = {
        "date": date,
        "heure": heure_h,
        "nbPlayers": str(nb_joueurs),
        "sSearch": "Chercher les départs",
        "option": "com_ggpublic",
        "req": "teetimes",
        "lang": "fr",
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": url,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
        "Origin": "https://secure.gggolf.ca",
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            # D'abord un GET pour obtenir les cookies de session
            get_resp = await client.get(url, headers=headers)
            logger.info(f"[GGG] GET initial: {get_resp.status_code}")

            # Extraire les options GGG du HTML initial
            ggg_options = _extract_ggg_options(get_resp.text)
            if ggg_options:
                logger.info(
                    f"[GGG] calendarMin={ggg_options.get('calendarMin')}, "
                    f"calendarMax={ggg_options.get('calendarMax')}"
                )
                # Verifier la fenetre de reservation
                cal_min = ggg_options.get("calendarMin")
                cal_max = ggg_options.get("calendarMax")
                if cal_min and cal_max:
                    try:
                        target = datetime.strptime(date, "%Y-%m-%d").date()
                        min_d = datetime.strptime(cal_min, "%Y-%m-%d").date()
                        max_d = datetime.strptime(cal_max, "%Y-%m-%d").date()
                        if not (min_d <= target <= max_d):
                            logger.info(f"[GGG] Hors fenetre [{cal_min} -> {cal_max}]")
                            return []
                    except Exception:
                        pass

            # Maintenant le POST avec les donnees du formulaire
            # Essayer differents noms de champs (varient selon les terrains GGG)
            for payload_variant in [
                # Variant 1 — noms standards
                {
                    "date": date,
                    "heure": heure_h,
                    "nbPlayers": str(nb_joueurs),
                    "sSearch": "Chercher les departs",
                    "option": "com_ggpublic",
                    "req": "teetimes",
                    "lang": "fr",
                },
                # Variant 2 — noms alternatifs
                {
                    "date": date,
                    "heure": heure_h,
                    "nb_players": str(nb_joueurs),
                    "submit": "Chercher",
                    "option": "com_ggpublic",
                    "req": "teetimes",
                    "lang": "fr",
                },
                # Variant 3 — avec les minutes
                {
                    "date": date,
                    "heure": heure_h,
                    "minute": "00",
                    "nbPlayers": str(nb_joueurs),
                    "sSearch": "Chercher les departs",
                    "option": "com_ggpublic",
                    "req": "teetimes",
                    "lang": "fr",
                },
            ]:
                resp = await client.post(url, data=payload_variant, headers=headers)
                logger.info(f"[GGG] POST {resp.status_code}: {len(resp.text)} chars")

                if resp.status_code == 200 and len(resp.text) > 5000:
                    results = _parse_gggolf_html(resp.text, terrain, date, heure_debut, heure_fin, nb_joueurs)
                    if results:
                        logger.info(f"[GGG] {len(results)} depat(s) trouves")
                        return results

                    # Logger extrait pour debug
                    idx = resp.text.lower().find("reservez")
                    if idx < 0:
                        idx = resp.text.lower().find("result")
                    if idx > 0:
                        logger.info(f"[GGG] Extrait HTML: {resp.text[max(0,idx-200):idx+500]}")
                    else:
                        logger.info(f"[GGG] HTML[3000:5000]: {resp.text[3000:5000]}")

            # Fallback: utiliser les hoursAM/PM du config
            if ggg_options:
                return _parse_gggolf_from_options(ggg_options, terrain, date, heure_debut, heure_fin, nb_joueurs)

            return _mock_tee_times(terrain, date, heure_debut, heure_fin)

    except Exception as e:
        logger.error(f"[GGG] Erreur POST: {e}")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)


def _extract_ggg_options(html):
    """Extraire le JSON de config GGG depuis le HTML."""
    m = re.search(r'var options\s*=\s*(\{[^;]+\})', html)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return {}


def _parse_gggolf_html(html, terrain, date, heure_debut, heure_fin, nb_joueurs):
    """
    Parser le HTML des resultats GGG.
    Structure observee:
      <td>Heure:</td><td>15:02</td>
      ou
      <td class="...heure...">15:02</td>
    Avec bouton Reservez a cote.
    """
    slug = terrain.get("ggg_slug", terrain["id"])
    url_base = f"https://secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr"

    heures = set()

    # Patterns bases sur la structure observee dans le screenshot
    patterns = [
        # Heure dans une cellule de tableau apres "Heure:"
        r'Heure[^<]*</td>\s*<td[^>]*>\s*(\d{1,2}:\d{2})',
        r'Heure\s*:\s*</[^>]+>\s*<[^>]+>\s*(\d{1,2}:\d{2})',
        # Classes CSS teetimes
        r'class="[^"]*ttime[^"]*"[^>]*>\s*(\d{1,2}:\d{2})',
        r'class="[^"]*teetime[^"]*"[^>]*>\s*(\d{1,2}:\d{2})',
        r'class="[^"]*hour[^"]*"[^>]*>\s*(\d{1,2}:\d{2})',
        r'class="[^"]*heure[^"]*"[^>]*>\s*(\d{1,2}:\d{2})',
        # Attributs data
        r'data-time="(\d{1,2}:\d{2})"',
        r'data-heure="(\d{1,2}:\d{2})"',
        # Heures dans des <td> simples (filtrees par plage valide)
        r'<td[^>]*>\s*(\d{1,2}:\d{2})\s*</td>',
        r'<td[^>]*>\s*(\d{1,2}h\d{2})\s*</td>',
        # Dans des liens de reservation
        r'href="[^"]*heure[=_](\d{1,2}:\d{2})[^"]*"',
        r'href="[^"]*time[=_](\d{1,2}%3A\d{2})[^"]*"',
    ]

    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            raw = m.group(1).replace("%3A", ":").replace("h", ":")
            h = _normalize_time(raw)
            # Filtrer les heures de golf valides (6h a 20h)
            if h and "06:00" <= h <= "20:00":
                heures.add(h)

    logger.info(f"[GGG] Heures parsees: {sorted(heures)}")

    results = []
    for heure in sorted(heures):
        if _in_range(heure, heure_debut, heure_fin):
            results.append({
                "heure": heure,
                "places": nb_joueurs,
                "prix": "Voir site",
                "url": url_base,
            })

    return results


def _parse_gggolf_from_options(ggg_options, terrain, date, heure_debut, heure_fin, nb_joueurs):
    """Fallback: heures configurees dans GGG (approximatif)."""
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


# ─────────────────────────────────────────────
# Chronogolf
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# Generique
# ─────────────────────────────────────────────

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
