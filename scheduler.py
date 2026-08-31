"""
scheduler.py — Polling alertes avec réservation automatique GGG et notification Chronogolf.
"""

import json
import logging
import os
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import (
    get_active_alerts, mark_alert_notified, log_poll,
    notification_deja_envoyee, marquer_notification_envoyee,
)
from scraper import get_available_tee_times
from notifier import send_notification, format_alert_message
from booker import reserver_depart

logger = logging.getLogger(__name__)
TZ_MONTREAL = ZoneInfo("America/Montreal")
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
    now = datetime.now(TZ_MONTREAL)
    today_mtl = now.date()
    for alert in alerts:
        try:
            if datetime.strptime(alert["date"], "%Y-%m-%d").date() < today_mtl:
                await mark_alert_notified(alert["id"])
                continue
        except Exception:
            pass
        if not _should_check_now(alert["id"], alert.get("intervalle", 15), now):
            continue
        terrain = courses.get(alert["terrain_id"])
        if not terrain:
            continue
        logger.info(f"[Scheduler] Alerte #{alert['id']} — {terrain['nom']} {alert['date']} {alert['heure_debut']}–{alert['heure_fin']}")
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
            await log_poll(alert["id"], "VIDE", "Aucun créneau")
            return

        email = alert.get("email", "")
        systeme = terrain.get("systeme", "site_propre")

        # Filtrer départs déjà notifiés
        nouveaux = []
        for tt in tee_times:
            if tt.get("_mock"):
                continue
            deja = await notification_deja_envoyee(alert["terrain_id"], alert["date"], tt["heure"], email)
            if not deja:
                nouveaux.append(tt)

        if not nouveaux:
            await log_poll(alert["id"], "DEJA_NOTIFIE", "Tous déjà notifiés")
            return

        premier = nouveaux[0]
        date_fr = _format_date_fr(alert["date"])

        # ── GGG Golf : réserver automatiquement ──────────────
        if systeme == "gggolf":
            ggg_user = alert.get("ggg_username", "")
            ggg_pwd  = alert.get("ggg_password", "")

            if ggg_user and ggg_pwd:
                logger.info(f"[Scheduler] Réservation auto GGG: {terrain['nom']} {premier['heure']}")
                result = await reserver_depart(
                    terrain=terrain,
                    confirm_url=premier["url"],
                    username=ggg_user,
                    password=ggg_pwd,
                    date=alert["date"],
                    heure=premier["heure"],
                    nb_joueurs=alert["nb_joueurs"],
                )
                if result.get("succes"):
                    message = (
                        f"✅ Votre départ a été réservé automatiquement!\n\n"
                        f"🏌️ Terrain : {terrain['nom']}\n"
                        f"📅 Date    : {date_fr}\n"
                        f"🕐 Heure   : {premier['heure']}\n"
                        f"👥 Joueurs : {alert['nb_joueurs']}\n\n"
                        f"Vérifiez votre courriel GGG Golf pour la confirmation."
                    )
                    subject = f"✅ Départ réservé — {terrain['nom']} {premier['heure']}"
                    await _envoyer_et_marquer(alert, terrain, nouveaux, email, message, subject, date_fr)
                    return
                else:
                    # Réservation échouée — notifier quand même
                    logger.warning(f"[Scheduler] Réservation auto échouée: {result.get('message')}")
                    message = (
                        f"⚠️ Un départ est disponible mais la réservation auto a échoué.\n\n"
                        f"🏌️ Terrain : {terrain['nom']}\n"
                        f"📅 Date    : {date_fr}\n"
                        f"🕐 Heure   : {premier['heure']}\n"
                        f"👥 Joueurs : {alert['nb_joueurs']}\n\n"
                        f"👉 Réservez rapidement : {premier['url']}"
                    )
            else:
                # Pas de credentials GGG — notifier sans réserver
                message = format_alert_message(terrain['nom'], date_fr, premier['heure'], alert['nb_joueurs'], premier['url'])

        # ── Chronogolf : envoyer lien direct ─────────────────
        elif systeme == "chronogolf":
            slug = terrain.get("chronogolf_slug", terrain["id"])
            url_reservation = f"https://www.chronogolf.ca/club/{slug}"
            message = (
                f"⛳ Un départ est disponible sur Chronogolf!\n\n"
                f"🏌️ Terrain : {terrain['nom']}\n"
                f"📅 Date    : {date_fr}\n"
                f"🕐 Heure   : {premier['heure']}\n"
                f"👥 Joueurs : {alert['nb_joueurs']}\n\n"
                f"👉 Réservez maintenant (dépêchez-vous!) :\n{url_reservation}"
            )

        else:
            message = format_alert_message(terrain['nom'], date_fr, premier['heure'], alert['nb_joueurs'], premier['url'])

        subject = f"⛳ Départ disponible — {terrain['nom']} {premier['heure']}"
        await _envoyer_et_marquer(alert, terrain, nouveaux, email, message, subject, date_fr)

    except Exception as e:
        logger.error(f"[Scheduler] Erreur alerte #{alert['id']}: {e}")
        await log_poll(alert["id"], "ERREUR", str(e))


async def _envoyer_et_marquer(alert, terrain, nouveaux, email, message, subject, date_fr):
    success = send_notification(email, message, subject)
    if success:
        for tt in nouveaux:
            await marquer_notification_envoyee(alert["terrain_id"], alert["date"], tt["heure"], email)
        await mark_alert_notified(alert["id"])
        logger.info(f"[Scheduler] ✅ Notification envoyée à {email}")
    await log_poll(
        alert["id"],
        "TROUVE" if success else "ERREUR_EMAIL",
        f"Heure: {nouveaux[0]['heure']}, Email: {success}",
    )


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
    scheduler.add_job(
        check_all_alerts,
        trigger=IntervalTrigger(minutes=1),
        id="check_alerts",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("✅ Scheduler démarré")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
