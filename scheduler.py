"""
scheduler.py — Gestion du polling périodique avec APScheduler.

Chaque alerte active est vérifiée selon son propre intervalle.
Un job global tourne toutes les minutes pour dispatcher les checks.
"""

import json
import logging
import os
from datetime import datetime, date
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import get_active_alerts, mark_alert_notified, log_poll
from scraper import get_available_tee_times
from notifier import send_sms, format_alert_message

logger = logging.getLogger(__name__)

# Chemin vers la base de données des terrains
COURSES_PATH = Path(__file__).parent / "courses.json"

scheduler = AsyncIOScheduler(timezone="America/Montreal")


def load_courses() -> dict:
    """Charge le JSON des terrains et retourne un dict indexé par ID."""
    with open(COURSES_PATH, encoding="utf-8") as f:
        courses = json.load(f)
    return {c["id"]: c for c in courses}


async def check_all_alerts():
    """
    Job principal : vérifie toutes les alertes actives.
    Chaque alerte a son propre intervalle — on check si c'est le bon moment.
    """
    alerts = await get_active_alerts()
    if not alerts:
        return

    courses = load_courses()
    now = datetime.now()

    for alert in alerts:
        # Vérifier si la date de l'alerte est dans le passé
        alert_date = datetime.strptime(alert["date"], "%Y-%m-%d").date()
        if alert_date < date.today():
            logger.info(f"Alerte #{alert['id']} expirée (date passée) — désactivation")
            await mark_alert_notified(alert["id"])
            continue

        # Vérifier l'intervalle de l'alerte
        intervalle = alert.get("intervalle", 15)
        cree_le = datetime.fromisoformat(alert["cree_le"])
        minutes_depuis_creation = (now - cree_le).total_seconds() / 60

        # Vérifie toutes les N minutes depuis la création
        if not _should_check_now(alert["id"], intervalle, now):
            continue

        terrain = courses.get(alert["terrain_id"])
        if not terrain:
            logger.warning(f"Terrain '{alert['terrain_id']}' introuvable pour alerte #{alert['id']}")
            continue

        logger.info(
            f"🔍 Vérification alerte #{alert['id']} — "
            f"{terrain['nom']} le {alert['date']} "
            f"({alert['heure_debut']}–{alert['heure_fin']}, {alert['nb_joueurs']}j)"
        )

        await _check_single_alert(alert, terrain)


async def _check_single_alert(alert: dict, terrain: dict):
    """Vérifie un départ pour une alerte donnée et notifie si trouvé."""
    try:
        tee_times = await get_available_tee_times(
            terrain=terrain,
            date=alert["date"],
            heure_debut=alert["heure_debut"],
            heure_fin=alert["heure_fin"],
            nb_joueurs=alert["nb_joueurs"],
        )

        if tee_times:
            # Départ trouvé !
            premier = tee_times[0]
            is_mock = premier.get("_mock", False)
            label = " [MOCK]" if is_mock else ""

            logger.info(
                f"✅ Départ trouvé{label} pour alerte #{alert['id']}: "
                f"{premier['heure']} — {premier['places']} places"
            )

            message = format_alert_message(
                terrain_nom=terrain["nom"],
                date=_format_date_fr(alert["date"]),
                heure=premier["heure"],
                nb_joueurs=alert["nb_joueurs"],
                url=premier["url"],
            )

            success = send_sms(alert["telephone"], message)

            await log_poll(
                alert_id=alert["id"],
                statut="TROUVE" if not is_mock else "TROUVE_MOCK",
                details=f"Créneau: {premier['heure']}, Places: {premier['places']}, SMS: {success}",
            )

            if success:
                await mark_alert_notified(alert["id"])
        else:
            logger.info(f"⏳ Aucun départ disponible pour alerte #{alert['id']}")
            await log_poll(
                alert_id=alert["id"],
                statut="VIDE",
                details="Aucun créneau dans la plage horaire",
            )

    except Exception as e:
        logger.error(f"Erreur vérification alerte #{alert['id']}: {e}")
        await log_poll(
            alert_id=alert["id"],
            statut="ERREUR",
            details=str(e),
        )


# Tracking des dernières vérifications par alerte
_last_checks: dict[int, datetime] = {}


def _should_check_now(alert_id: int, intervalle: int, now: datetime) -> bool:
    """
    Retourne True si l'alerte doit être vérifiée maintenant.
    Basé sur l'intervalle en minutes depuis la dernière vérification.
    """
    last = _last_checks.get(alert_id)
    if last is None:
        _last_checks[alert_id] = now
        return True
    minutes_elapsed = (now - last).total_seconds() / 60
    if minutes_elapsed >= intervalle:
        _last_checks[alert_id] = now
        return True
    return False


def _format_date_fr(date_str: str) -> str:
    """Formate une date YYYY-MM-DD en format lisible français."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
        mois = [
            "janvier", "février", "mars", "avril", "mai", "juin",
            "juillet", "août", "septembre", "octobre", "novembre", "décembre",
        ]
        return f"{jours[d.weekday()]} {d.day} {mois[d.month - 1]}"
    except ValueError:
        return date_str


def start_scheduler():
    """Démarre le scheduler — job toutes les minutes."""
    scheduler.add_job(
        check_all_alerts,
        trigger=IntervalTrigger(minutes=1),
        id="check_alerts",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("✅ Scheduler démarré — polling toutes les minutes")


def stop_scheduler():
    """Arrête le scheduler proprement."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler arrêté")
