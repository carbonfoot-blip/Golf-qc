"""
scraper.py — Extraction des départs disponibles via Playwright.

GGG Golf  : secure.gggolf.ca/{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr
Chronogolf : chronogolf.ca/club/{slug} — interception API JSON
Site propre: extraction générique par regex
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

logger = logging.getLogger(__name__)
TIMEOUT = 25_000


async def get_available_tee_times(
    terrain: dict,
    date: str,
    heure_debut: str,
    heure_fin: str,
    nb_joueurs: int,
) -> list[dict]:
    """Point d'entrée principal. Retourne les créneaux dans la plage horaire."""
    systeme = terrain.get("systeme", "site_propre")
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
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


# ─────────────────────────────────────────────
# GGG Golf
# URL: /{slug}/index.php?option=com_ggpublic&req=teetimes&lang=fr&date=YYYY-MM-DD&players=N
# ─────────────────────────────────────────────

async def _scrape_gggolf(page, terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("ggg_slug", terrain["id"])
    # GGG Golf accepte date en paramètre GET
    url = (
        f"https://secure.gggolf.ca/{slug}/index.php"
        f"?option=com_ggpublic&req=teetimes&lang=fr"
        f"&date={date}&players={nb_joueurs}"
    )
    logger.info(f"[GGG] {url}")

    try:
        await page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")

        # Attendre le tableau des départs GGG
        try:
            await page.wait_for_selector(
                "table.teetimes, .teetimes-list, #teetimes, table[class*='teetime'], "
                "tr.teetime, td.teetime-time, .booking-time",
                timeout=10000
            )
        except PwTimeout:
            logger.warning(f"[GGG] Tableau non trouvé pour {terrain['nom']} — tentative HTML brut")

        # Parser le contenu HTML brut — GGG Golf affiche les heures dans des <td>
        content = await page.content()
        return _parse_gggolf_html(content, terrain, date, heure_debut, heure_fin, nb_joueurs)

    except Exception as e:
        logger.error(f"[GGG] Erreur navigation: {e}")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)


def _parse_gggolf_html(html: str, terrain, date, heure_debut, heure_fin, nb_joueurs) -> list[dict]:
    """
    Parse le HTML de GGG Golf.
    Les heures apparaissent typiquement dans des patterns comme :
      <td class="ttime">8:00</td>  ou  <span class="hour">08:00</span>
    On extrait toutes les heures et on filtre selon la plage.
    """
    tee_times = []

    # Patterns GGG Golf observés : heure dans des balises spécifiques
    # On cherche les heures au format H:MM ou HH:MM entourées de balises HTML
    patterns = [
        r'class="[^"]*time[^"]*"[^>]*>\s*(\d{1,2}:\d{2})\s*<',
        r'class="[^"]*hour[^"]*"[^>]*>\s*(\d{1,2}:\d{2})\s*<',
        r'<td[^>]*>\s*(\d{1,2}:\d{2})\s*</td>',
        r'data-time="(\d{1,2}:\d{2})"',
        r'"starttime"\s*:\s*"(\d{1,2}:\d{2})"',
    ]

    heures_trouvees = set()
    for pat in patterns:
        for match in re.finditer(pat, html, re.IGNORECASE):
            h = _normalize_time(match.group(1))
            if h:
                heures_trouvees.add(h)

    # Chercher aussi les places disponibles (GGG affiche souvent N spots)
    # Pattern: "4 joueurs" ou "spots: 4" près de l'heure
    for heure in sorted(heures_trouvees):
        if not _in_range(heure, heure_debut, heure_fin):
            continue

        # Construire l'URL de réservation directe avec la date et l'heure
        slug = terrain.get("ggg_slug", terrain["id"])
        url_reserv = (
            f"https://secure.gggolf.ca/{slug}/index.php"
            f"?option=com_ggpublic&req=teetimes&lang=fr"
            f"&date={date}&players={nb_joueurs}"
        )

        tee_times.append({
            "heure": heure,
            "places": 4,
            "prix": "Voir site",
            "url": url_reserv,
        })

    if not tee_times:
        logger.warning(f"[GGG] Aucune heure parsée dans le HTML de {terrain['nom']} — mode mock")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)

    return tee_times


# ─────────────────────────────────────────────
# Chronogolf
# ─────────────────────────────────────────────

async def _scrape_chronogolf(page, terrain, date, heure_debut, heure_fin, nb_joueurs):
    slug = terrain.get("chronogolf_slug", terrain["id"])
    logger.info(f"[Chrono] {terrain['nom']} — {date}")

    captured_data = []

    async def handle_response(response):
        url = response.url
        if any(k in url for k in ["tee_times", "availability", "slots", "teetime"]):
            try:
                data = await response.json()
                captured_data.append(data)
            except Exception:
                pass

    page.on("response", handle_response)

    try:
        url = f"https://www.chronogolf.ca/club/{slug}?date={date}&nb_players={nb_joueurs}&lang=fr"
        await page.goto(url, timeout=TIMEOUT, wait_until="networkidle")

        if captured_data:
            results = []
            for data in captured_data:
                results.extend(_parse_chronogolf_json(data, heure_debut, heure_fin, nb_joueurs, terrain, date))
            if results:
                return results

        # Fallback HTML
        content = await page.content()
        return _parse_chronogolf_html_fallback(content, terrain, date, heure_debut, heure_fin, nb_joueurs)

    except Exception as e:
        logger.error(f"[Chrono] Erreur: {e}")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)


def _parse_chronogolf_json(data, heure_debut, heure_fin, nb_joueurs, terrain, date) -> list[dict]:
    results = []
    slug = terrain.get("chronogolf_slug", terrain["id"])

    slots = (
        data.get("tee_times") or data.get("slots") or
        data.get("availability") or (data if isinstance(data, list) else [])
    )

    for slot in slots:
        start = slot.get("start_time") or slot.get("time") or slot.get("tee_time") or ""
        heure = _normalize_time(str(start))
        if not heure or not _in_range(heure, heure_debut, heure_fin):
            continue

        available = slot.get("available_spots", slot.get("spots", 4))
        if isinstance(available, int) and available < nb_joueurs:
            continue

        prix = slot.get("price") or slot.get("rate") or "Voir site"
        if isinstance(prix, (int, float)):
            prix = f"{prix:.0f}$"

        results.append({
            "heure": heure,
            "places": available if isinstance(available, int) else 4,
            "prix": str(prix),
            "url": f"https://www.chronogolf.ca/club/{slug}?date={date}&nb_players={nb_joueurs}",
        })

    return results


def _parse_chronogolf_html_fallback(html, terrain, date, heure_debut, heure_fin, nb_joueurs) -> list[dict]:
    slug = terrain.get("chronogolf_slug", terrain["id"])
    tee_times = []
    heures = set(re.findall(r'\b(\d{1,2}:\d{2})\b', html))
    for h_raw in heures:
        h = _normalize_time(h_raw)
        if h and _in_range(h, heure_debut, heure_fin):
            tee_times.append({
                "heure": h,
                "places": 4,
                "prix": "Voir site",
                "url": f"https://www.chronogolf.ca/club/{slug}?date={date}&nb_players={nb_joueurs}",
            })
    return sorted(tee_times, key=lambda x: x["heure"]) or _mock_tee_times(terrain, date, heure_debut, heure_fin)


# ─────────────────────────────────────────────
# Site propre (générique)
# ─────────────────────────────────────────────

async def _scrape_generic(page, terrain, date, heure_debut, heure_fin, nb_joueurs) -> list[dict]:
    url = terrain.get("url_scrape", terrain["url_reservation"])
    logger.info(f"[Générique] {url}")
    try:
        await page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")
        content = await page.content()
        heures = set(re.findall(r'\b(\d{1,2}:\d{2})\b', content))
        tee_times = []
        for h_raw in heures:
            h = _normalize_time(h_raw)
            if h and _in_range(h, heure_debut, heure_fin):
                tee_times.append({
                    "heure": h, "places": 4,
                    "prix": "Voir site", "url": terrain["url_reservation"],
                })
        return sorted(tee_times, key=lambda x: x["heure"]) or _mock_tee_times(terrain, date, heure_debut, heure_fin)
    except Exception as e:
        logger.error(f"[Générique] Erreur: {e}")
        return _mock_tee_times(terrain, date, heure_debut, heure_fin)


# ─────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────

def _normalize_time(text: str) -> Optional[str]:
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


def _in_range(heure: str, debut: str, fin: str) -> bool:
    try:
        h = datetime.strptime(heure, "%H:%M").time()
        d = datetime.strptime(debut, "%H:%M").time()
        f = datetime.strptime(fin, "%H:%M").time()
        return d <= h < f
    except ValueError:
        return False


def _mock_tee_times(terrain: dict, date: str, heure_debut: str, heure_fin: str) -> list[dict]:
    """Créneaux fictifs pour le développement local (MOCK_SCRAPER=true)."""
    import os
    if os.getenv("MOCK_SCRAPER", "true").lower() != "true":
        return []

    logger.info(f"[MOCK] Créneaux fictifs pour {terrain['nom']}")
    try:
        debut = datetime.strptime(heure_debut, "%H:%M")
        fin = datetime.strptime(heure_fin, "%H:%M")
    except ValueError:
        return []

    slots = []
    current = debut
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
