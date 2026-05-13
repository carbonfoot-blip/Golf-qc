"""
scheduler.py — Polling périodique avec logique 1 notification par jour/heure/terrain.
"""

import json
import logging
import os
from datetime import datetime, date
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import (
    get_active_alerts, mark_alert_notified, log_poll,
    notification_deja_envoyee, marquer_notification_envoyee
)
from scraper import get_available_tee_times
from notifier import send_notification, format_alert_message

logger = logging.getLogger(__name__)
COURSES_PATH = Path(__file__).parent / "courses.json"
scheduler = AsyncIOScheduler(timezone="America/Montreal")


def load_courses() -> dict:
    with open(COURSES_PATH, encoding="utf-8") as f:
        return {c["id"]: c for c in json.load(f)}


async def check_all_alerts():
    alerts = await get_active_alerts()
    if not alerts:
        return

    courses = load_courses()
    now = datetime.now()

    for alert in alerts:
        # Date expirée
        try:
            if datetime.strptime(alert["date"], "%Y-%m-%d").date() < date.today():
                await mark_alert_notified(alert["id"])
                continue
        except Exception:
            pass

        # Vérifier intervalle
        if not _should_check_now(alert["id"], alert.get("intervalle", 15), now):
            continue

        terrain = courses.get(alert["terrain_id"])
        if not terrain:
            continue

        logger.info(f"🔍 Alerte #{alert['id']} — {terrain['nom']} {alert['date']} {alert['heure_debut']}–{alert['heure_fin']}")
        await _check_single_alert(alert, terrain)


async def _check_single_alert(alert: dict, terrain: dict):
    try:
        tee_times = await get_available_tee_times(
            terrain=terrain,
            date=alert["date"],
            heure_debut=alert["heure_debut"],
            heure_fin=alert["heure_fin"],
            nb_joueurs=alert["nb_joueurs"],
        )

        if not tee_times:
            await log_poll(alert["id"], "VIDE", "Aucun créneau disponible")
            return

        # Filtrer les départs déjà notifiés aujourd'hui
        email = alert.get("email", "")
        nouveaux = []
        for tt in tee_times:
            if tt.get("_mock"):
                continue
            deja = await notification_deja_envoyee(
                alert["terrain_id"], alert["date"], tt["heure"], email
            )
            if not deja:
                nouveaux.append(tt)

        if not nouveaux:
            logger.info(f"Alerte #{alert['id']} — départs déjà notifiés")
            await log_poll(alert["id"], "DEJA_NOTIFIE", "Tous les départs déjà notifiés aujourd'hui")
            return

        # Envoyer une notification pour le premier départ disponible
        premier = nouveaux[0]
        date_fr = _format_date_fr(alert["date"])
        message = format_alert_message(
            terrain_nom=terrain["nom"],
            date=date_fr,
            heure=premier["heure"],
            nb_joueurs=alert["nb_joueurs"],
            url=premier["url"],
        )

        success = send_notification(email, message)

        if success:
            # Marquer TOUS les nouveaux départs comme notifiés
            for tt in nouveaux:
                await marquer_notification_envoyee(
                    alert["terrain_id"], alert["date"], tt["heure"], email
                )
            await mark_alert_notified(alert["id"])
            logger.info(f"✅ Notification envoyée à {email} pour alerte #{alert['id']}")

        await log_poll(
            alert["id"],
            "TROUVE" if success else "ERREUR_EMAIL",
            f"Heure: {premier['heure']}, Email: {success}",
        )

    except Exception as e:
        logger.error(f"Erreur alerte #{alert['id']}: {e}")
        await log_poll(alert["id"], "ERREUR", str(e))


_last_checks: dict[int, datetime] = {}


def _should_check_now(alert_id: int, intervalle: int, now: datetime) -> bool:
    last = _last_checks.get(alert_id)
    if last is None:
        _last_checks[alert_id] = now
        return True
    if (now - last).total_seconds() / 60 >= intervalle:
        _last_checks[alert_id] = now
        return True
    return False


def _format_date_fr(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
        mois  = ["janvier", "février", "mars", "avril", "mai", "juin",
                 "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
        return f"{jours[d.weekday()]} {d.day} {mois[d.month - 1]}"
    except ValueError:
        return date_str


def start_scheduler():
    scheduler.add_job(check_all_alerts, trigger=IntervalTrigger(minutes=1),
                      id="check_alerts", replace_existing=True, max_instances=1)
    scheduler.start()
    logger.info("✅ Scheduler démarré")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
