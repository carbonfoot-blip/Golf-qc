"""
notifier.py — Envoi de notifications par email via SendGrid.
"""

import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def send_notification(to_email: str, message: str, subject: str = "⛳ Départ disponible — Golf Alert QC") -> bool:
    """
    Envoie une notification email.
    Retourne True si succès, False sinon.
    """
    api_key = os.getenv("SENDGRID_API_KEY", "")
    from_email = os.getenv("SENDGRID_FROM_EMAIL", "noreply@golfalert.ca")

    if not api_key or api_key == "stub":
        _send_stub(to_email, subject, message)
        return True

    return _send_sendgrid(to_email, subject, message, api_key, from_email)


def _send_stub(to_email: str, subject: str, message: str):
    separator = "=" * 60
    logger.info(separator)
    logger.info("📧 EMAIL (STUB — non envoyé)")
    logger.info(f"   À       : {to_email}")
    logger.info(f"   Sujet   : {subject}")
    logger.info(f"   Message : {message}")
    logger.info(f"   Heure   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(separator)


def _send_sendgrid(to_email: str, subject: str, message: str, api_key: str, from_email: str) -> bool:
    try:
        import urllib.request
        import json

        html_message = message.replace("\n", "<br>")
        html_body = f"""
        <div style="font-family: 'DM Sans', sans-serif; max-width: 500px; margin: 0 auto; padding: 24px;">
          <div style="background: #1a3a1f; border-radius: 12px; padding: 20px; text-align: center; margin-bottom: 20px;">
            <h1 style="color: white; margin: 0; font-size: 1.4rem;">⛳ Golf Alert QC</h1>
          </div>
          <div style="background: #eef7ef; border: 1.5px solid #7ab885; border-radius: 10px; padding: 20px; margin-bottom: 20px;">
            <p style="color: #1a3a1f; font-size: 1rem; margin: 0; line-height: 1.6;">{html_message}</p>
          </div>
          <p style="color: #5a7060; font-size: 0.75rem; text-align: center;">
            Golf Alert QC — Réservations de golf au Québec
          </p>
        </div>
        """

        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": from_email, "name": "Golf Alert QC"},
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": message},
                {"type": "text/html", "value": html_body},
            ],
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"SendGrid: {resp.status} — email envoyé à {to_email}")
            return resp.status in [200, 202]

    except Exception as e:
        logger.error(f"Erreur SendGrid: {e}")
        return False


def format_alert_message(terrain_nom: str, date: str, heure: str, nb_joueurs: int, url: str) -> str:
    return (
        f"Un départ est disponible!\n\n"
        f"🏌️ Terrain : {terrain_nom}\n"
        f"📅 Date    : {date}\n"
        f"🕐 Heure   : {heure}\n"
        f"👥 Joueurs : {nb_joueurs}\n\n"
        f"👉 Réserver : {url}"
    )
